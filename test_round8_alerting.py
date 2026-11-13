"""Shared-cause alerting: several tasks failing at once become one incident e-mail that names the
likely cause, and more failures with that same cause wait for a digest instead of mailing hourly.

The log lines below are modelled on a database storage outage in which many tasks failed for
more than a day. The replay test runs a failure/recovery timeline of that shape through the alert
logic: the old logic sent 13 e-mails, none naming the cause. No network, no SMTP: send_email is faked.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import taskdash_publish as publish

NOW = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)

REAL_TAILS = {
    "Geodatabase Upkeep": "TypeError: Could not open SDE workspace.",
    "Address Point Sync": "ERROR - Feature class GISDB.GISOWNER.ADDRESS_POINTS not found in workspace",
    "Street Centerline Sync": "ERROR - Feature class GISDB.GISOWNER.STREET_CENTERLINES not found in workspace",
    "Address Null Fill": "Script exited with code 1.",
    "Stormwater Dataset Upload": "Acme_StormPipe                FAILED - Source not found      (0 features)",
    "Permit Locator Rebuild": "get_token attempt 2/3 failed: Expecting value: line 3 column 1 (char 4) - retrying in 4s",
    "Parcel Copy Job": r"ERROR - FAILED: Input layer does not exist or is not supported: C:\Jobs\Admin@Data@Default.gdbconn\GISDB.GISOWNER.ParcelPublicing\GISDB.GISOWNER.Parcels",
    "Streets Open Data Push": r"FAILED: Source feature class not found: C:\Jobs\Admin@Data@Default.gdbconn\GISOwner.STREET_CENTERLINES",
    "Version Nightly Refresh": "Script exited with code 1.",
    "Tax Rate Refresh": r"ERROR 000732: Input Rows: Dataset C:\Jobs\Admin@Data@Default.gdbconn\GISDB.GISOWNER.tax_rate_groups does not exist or is not supported",
    "City Limits Refresh": "ERROR - An error occurred: Failed to execute. Parameters are not valid.",
    "Subdivision Refresh": "ERROR 000187: Only supports Geodatabase tables and feature classes",
    "Appraiser Future Polygons": r"ERROR 000732: Input Rows: Dataset C:\Jobs\Admin@Data@Default.gdbconn\GISDB.GISOWNER.FutureApprPoly does not exist or is not supported",
    "Parcel Diff": "ArcPy Error: Failed to execute. Parameters are not valid.",
    "Parcel Refresh": "ERROR - SDE publish failed: Failed to execute. Parameters are not valid.",
    "Utility Account Parcels": "Script exited with code 1.",
    "Roads Feed Export": "INFO - Processing records...",
    "Replica Keep-Alive Job": "- 120777 Growth_Area: dataset GISDB.GISOWNER.GROWTHAREABOUNDARY not found through the DEFAULT connection",
}


def _task(tmp_path, name, failed=True, tail=None):
    log = tmp_path / (name.replace(" ", "_") + ".log")
    log.write_text((tail if tail is not None else REAL_TAILS.get(name, "")) + "\n", encoding="utf-8")
    return {"task_name": name, "last_run_failed": int(failed), "is_overdue": 0,
            "last_result_text": "Script exited with code 1." if failed else "The operation completed successfully.",
            "attention_reason": "", "next_run_in_hours": 5.0,
            "latest_log_path": str(log), "latest_log_name": log.name}


# ---------------------------------------------------------------- cause classification

@pytest.mark.parametrize("name,cause", [
    ("Geodatabase Upkeep", "database"), ("Address Point Sync", "database"),
    ("Stormwater Dataset Upload", "database"), ("Parcel Copy Job", "database"),
    ("Streets Open Data Push", "database"), ("Tax Rate Refresh", "database"),
    ("City Limits Refresh", "database"), ("Subdivision Refresh", "database"),
    ("Replica Keep-Alive Job", "database"), ("Permit Locator Rebuild", "arcgis"),
    ("Roads Feed Export", ""), ("Address Null Fill", ""),
])
def test_real_outage_log_lines_classify_to_their_cause(tmp_path, name, cause):
    assert publish._failure_cause(_task(tmp_path, name)) == cause


def test_licence_and_network_lines_classify(tmp_path):
    lic = _task(tmp_path, "L", tail="RuntimeError: The Product License has not been initialized.")
    net = _task(tmp_path, "N", tail="OSError: [WinError 53] The network path was not found")
    assert publish._failure_cause(lic) == "licence"
    assert publish._failure_cause(net) == "network"


# ---------------------------------------------------------------- incident detection

def test_shared_cause_needs_min_tasks_and_ignores_overdue(tmp_path):
    inv = [_task(tmp_path, "Geodatabase Upkeep"), _task(tmp_path, "Parcel Copy Job")]
    current = ["FAILED: Geodatabase Upkeep", "FAILED: Parcel Copy Job", "OVERDUE: X"]
    assert publish.shared_cause(inv, current, 3) is None
    inv.append(_task(tmp_path, "Tax Rate Refresh"))
    current.append("FAILED: Tax Rate Refresh")
    incident = publish.shared_cause(inv, current, 3)
    assert incident["count"] == 3 and incident["cause"] == "database"
    assert publish.shared_cause(inv, current, 0) is None  # 0 turns the feature off


def test_shared_cause_is_unclear_without_a_strict_majority(tmp_path):
    inv = [_task(tmp_path, "Geodatabase Upkeep"), _task(tmp_path, "Permit Locator Rebuild"),
           _task(tmp_path, "Roads Feed Export"), _task(tmp_path, "Address Null Fill")]
    incident = publish.shared_cause(inv, [f"FAILED: {t['task_name']}" for t in inv], 3)
    assert incident["cause"] == ""
    assert incident["counts"] == {"database": 1, "arcgis": 1, "": 2}


def test_since_is_the_earliest_first_seen_not_the_latest_run(tmp_path):
    inv = [_task(tmp_path, n) for n in ("Geodatabase Upkeep", "Parcel Copy Job", "Tax Rate Refresh")]
    keys = [f"FAILED: {t['task_name']}" for t in inv]
    first_seen = {keys[0]: "2026-10-03T02:00:00+00:00", keys[1]: "2026-10-03T06:00:00+00:00",
                  keys[2]: "2026-10-03T08:00:00+00:00"}
    incident = publish.shared_cause(inv, keys, 3, first_seen)
    assert incident["since"] == datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)


def test_first_seen_keeps_the_old_time_and_stamps_new_problems_now():
    previous = {"problem_first_seen_utc": {"FAILED: A": "2026-10-03T02:00:00+00:00",
                                           "FAILED: Gone": "2026-10-01T00:00:00+00:00"}}
    seen = publish.problem_first_seen(previous, ["FAILED: A", "FAILED: B"], NOW)
    assert seen == {"FAILED: A": "2026-10-03T02:00:00+00:00", "FAILED: B": NOW.isoformat()}


# ---------------------------------------------------------------- digest batching

def _compose(tmp_path, names, previous, now=NOW, extra=None, digest=6):
    inv = [_task(tmp_path, n) for n in names] + (extra or [])
    config = {"alerting": {"incident_digest_hours": digest}, "arcgis": {"dashboard_id": "D"}}
    return publish.compose_alert({"host_name": "H", "inventory": inv}, config, previous, now)


THREE = ["Geodatabase Upkeep", "Parcel Copy Job", "Tax Rate Refresh"]


def _state(names, incident_at):
    keys = sorted(f"FAILED: {n}" for n in names)
    state = {"signature": keys, "incident_alert_utc": incident_at.isoformat() if incident_at else None,
             "problem_last_alert_utc": {k: NOW.isoformat() for k in keys}}
    if incident_at:
        state["incident_cause"] = "database"  # what the last incident e-mail announced
    return state


def test_first_incident_email_is_sent_at_once_with_the_cause_in_the_subject(tmp_path):
    alert = _compose(tmp_path, THREE, _state(THREE[:2], None))
    assert alert["send"] and alert["reason"] == "changed"
    assert alert["subject"].startswith("[TaskDash] 3 tasks failing on H since ")
    assert alert["subject"].endswith("likely shared cause: the database or data source (SDE/SQL)")
    assert "Likely shared cause: 3 tasks have failed since" in alert["body"]
    assert ("Most of their logs point at the database or data source (SDE/SQL). "
            "Check the database server first") in alert["body"]
    assert "Log evidence: 3 tasks point at the database or data source (SDE/SQL)." in alert["body"]


def test_evidence_line_ranks_causes_and_names_unclear_ones(tmp_path):
    names = ["Geodatabase Upkeep", "Parcel Copy Job", "Permit Locator Rebuild", "Roads Feed Export"]
    alert = _compose(tmp_path, names, _state(names[:1], None))
    assert ("Log evidence: 2 tasks point at the database or data source (SDE/SQL), "
            "1 at ArcGIS Server or the portal, 1 with no clear cause.") in alert["body"]


def test_more_of_the_same_waits_for_the_digest(tmp_path):
    alert = _compose(tmp_path, THREE + ["Subdivision Refresh"],
                     _state(THREE, NOW - timedelta(hours=1)))
    assert alert["send"] is False and alert["reason"] == "digest"
    assert alert["digest_due"] == NOW + timedelta(hours=5)


def test_digest_is_sent_once_it_is_due(tmp_path):
    alert = _compose(tmp_path, THREE + ["Subdivision Refresh"],
                     _state(THREE, NOW - timedelta(hours=6)))
    assert alert["send"] and alert["reason"] == "changed"


def test_a_failure_with_a_different_cause_is_sent_at_once(tmp_path):
    alert = _compose(tmp_path, THREE + ["Permit Locator Rebuild"], _state(THREE, NOW - timedelta(hours=1)))
    assert alert["send"] and alert["reason"] == "changed"


def test_a_failure_with_no_recognisable_cause_waits_with_the_incident(tmp_path):
    alert = _compose(tmp_path, THREE + ["Roads Feed Export"], _state(THREE, NOW - timedelta(hours=1)))
    assert alert["send"] is False and alert["reason"] == "digest"


def test_an_overdue_task_during_an_incident_is_sent_at_once(tmp_path):
    overdue = {"task_name": "X", "last_run_failed": 0, "is_overdue": 1, "next_run_in_hours": -2.0,
               "last_result_text": "", "latest_log_path": ""}
    alert = _compose(tmp_path, THREE, _state(THREE, NOW - timedelta(hours=1)), extra=[overdue])
    assert alert["send"] and alert["reason"] == "changed"


def test_a_recovery_during_an_incident_is_sent_at_once(tmp_path):
    names = THREE + ["Subdivision Refresh"]
    alert = _compose(tmp_path, THREE, _state(names, NOW - timedelta(hours=1)))
    assert alert["send"] and alert["reason"] == "changed"
    assert "Cleared:" in alert["body"]


def test_digest_off_when_hours_not_positive(tmp_path):
    alert = _compose(tmp_path, THREE + ["Subdivision Refresh"],
                     _state(THREE, NOW - timedelta(hours=1)), digest=0)
    assert alert["send"]


def test_no_incident_below_threshold_keeps_the_old_subject(tmp_path):
    alert = _compose(tmp_path, THREE[:2], _state(THREE[:1], None))
    assert alert["subject"] == "[TaskDash] problem list changed on H (2 open)"
    assert "Likely shared cause" not in alert["body"]


def test_maybe_send_alert_records_incident_and_first_seen_then_holds_the_next_addition(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("taskdash_common.load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda mail, to, subject, body: sent.append(subject) or True)
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {"dashboard_id": "D"}}
    logs = tmp_path / "logs"
    logs.mkdir()
    # An earlier alert exists: with no state at all the first e-mail lists old failures, not an incident.
    (logs / "alert_state.json").write_text(json.dumps(_state(THREE[:2], None)), encoding="utf-8")
    collected = {"host_name": "H", "inventory": [_task(tmp_path, n) for n in THREE]}
    assert publish.maybe_send_alert(collected, config, logs) is True
    state = json.loads((logs / "alert_state.json").read_text(encoding="utf-8"))
    assert state["incident_alert_utc"] and set(state["problem_first_seen_utc"]) == set(state["signature"])

    collected["inventory"].append(_task(tmp_path, "Subdivision Refresh"))
    assert publish.maybe_send_alert(collected, config, logs) is False
    # Only the held list is recorded; the e-mailed signature and incident time stay as they were.
    held = json.loads((logs / "alert_state.json").read_text(encoding="utf-8"))
    assert held["digest_pending"] == ["FAILED: Subdivision Refresh"]
    assert "FAILED: Subdivision Refresh" in held["problem_first_seen_utc"]
    for key in ("signature", "incident_alert_utc", "incident_cause", "problem_last_alert_utc"):
        assert held[key] == state[key]
    assert len(sent) == 1


def test_preview_explains_a_held_digest(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "alert_state.json").write_text(json.dumps(_state(THREE, NOW - timedelta(hours=1))), encoding="utf-8")
    inv = [_task(tmp_path, n) for n in THREE + ["Subdivision Refresh"]]
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {"dashboard_id": "D"}}
    text = publish.preview_alert({"host_name": "H", "inventory": inv}, config, logs, NOW).read_text(encoding="utf-8")
    assert "would NOT send yet: new problem(s) during an open incident wait for the digest" in text


# ---------------------------------------------------------------- replay of the real outage

# Local (EDT) hour -> tasks that started failing / recovered then, from prod TaskChanges 77-112.
_FAIL = {(2, 22): ["Geodatabase Upkeep"],
         (3, 0): ["Address Point Sync", "Street Centerline Sync"],
         (3, 1): ["Address Null Fill", "Stormwater Dataset Upload"],
         (3, 2): ["Permit Locator Rebuild", "Parcel Copy Job"],
         (3, 3): ["Streets Open Data Push"], (3, 4): ["Version Nightly Refresh"],
         (3, 5): ["Tax Rate Refresh", "City Limits Refresh", "Subdivision Refresh"],
         (3, 7): ["Appraiser Future Polygons", "Parcel Diff", "Parcel Refresh", "Utility Account Parcels"],
         (3, 20): ["Roads Feed Export"], (4, 1): ["Replica Keep-Alive Job"]}
_RECOVER = {(4, 7): ["Utility Account Parcels"],
            (4, 17): [n for n in REAL_TAILS if n not in ("Geodatabase Upkeep", "Utility Account Parcels")],
            (4, 19): ["Geodatabase Upkeep"]}
EDT = timezone(timedelta(hours=-4))


def _replay(tmp_path, monkeypatch, alerting):
    sent = []
    monkeypatch.setattr("taskdash_common.load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda mail, to, subject, body: sent.append((stamp, subject)) or True)
    # No heartbeat_hour: should_alert reads the machine's local hour, so a heartbeat would make the
    # expected lists depend on the time zone the tests run in.
    config = {"alerting": {"enabled": True, "to": ["a@b.c"], "realert_hours": 24, **alerting},
              "arcgis": {"dashboard_id": "D"}}
    logs = tmp_path / ("logs_" + str(len(alerting)))
    logs.mkdir()
    failing = set()
    hour = datetime(2026, 10, 2, 20, tzinfo=EDT)
    while hour <= datetime(2026, 10, 4, 20, tzinfo=EDT):
        stamp = hour.strftime("%m-%d %H:%M")
        failing |= set(_FAIL.get((hour.day, hour.hour), []))
        failing -= set(_RECOVER.get((hour.day, hour.hour), []))
        inv = [_task(tmp_path, n, failed=n in failing) for n in REAL_TAILS]
        monkeypatch.setattr(publish, "utc_now", lambda h=hour: h.astimezone(timezone.utc))
        publish.maybe_send_alert({"host_name": "TASKHOST-1", "inventory": inv}, config, logs)
        hour += timedelta(hours=1)
    return sent


def test_replay_of_the_real_outage_sends_fewer_emails_that_name_the_cause(tmp_path, monkeypatch):
    old = _replay(tmp_path, monkeypatch, {"shared_cause_min_tasks": 0})
    new = _replay(tmp_path, monkeypatch, {})
    # The old logic reproduces what prod sent in that window (CollectorRuns alert_sent=1 rows).
    assert [s for s, _ in old] == ["10-02 22:00", "10-03 00:00", "10-03 01:00", "10-03 02:00",
                                   "10-03 03:00", "10-03 04:00", "10-03 05:00", "10-03 07:00",
                                   "10-03 20:00", "10-04 01:00", "10-04 07:00", "10-04 17:00",
                                   "10-04 19:00"]
    assert not any("shared cause" in s for _, s in old)
    # 22:00 first failure; 00:00 incident; 02:00 a different recognised cause (token error) is
    # sent at once; 08:00 is the 6-h digest of 03:00-07:00; 20:00 is a new failure after a quiet
    # 12 h (no digest pending, so it goes at once); 02:00 the digest of the 01:00 keep-alive
    # failure; then the recoveries. 9 e-mails instead of 13.
    assert [s for s, _ in new] == ["10-02 22:00", "10-03 00:00", "10-03 02:00", "10-03 08:00",
                                   "10-03 20:00", "10-04 02:00", "10-04 07:00", "10-04 17:00",
                                   "10-04 19:00"]
    incident_mails = [s for _, s in new if "tasks failing on TASKHOST-1" in s]
    assert incident_mails and all("the database or data source (SDE/SQL)" in s for s in incident_mails)
    print("\nold:", *old, sep="\n  ")
    print("new:", *new, sep="\n  ")


# ---------------------------------------------------------------- code-review fixes

LICENCE = "RuntimeError: The Product License has not been initialized."


def test_new_failures_of_another_kind_cannot_flip_the_majority_and_hide(tmp_path):
    """4 licence failures arriving during a 3-task database incident outnumber it (4 vs 3), but the
    last e-mail announced 'database', so they are a different problem and go out at once."""
    lic = [_task(tmp_path, f"L{i}", tail=LICENCE) for i in range(4)]
    alert = _compose(tmp_path, THREE, _state(THREE, NOW - timedelta(hours=1)), extra=lic)
    assert alert["send"] and alert["reason"] == "changed"
    assert alert["incident"]["cause"] == "licence"


def test_a_held_failure_that_recovers_is_still_reported(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("taskdash_common.load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda mail, to, subject, body: sent.append(body) or True)
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {"dashboard_id": "D"}}
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "alert_state.json").write_text(json.dumps(_state(THREE, datetime.now(timezone.utc))), encoding="utf-8")
    inv = [_task(tmp_path, n) for n in THREE]
    held = _task(tmp_path, "Subdivision Refresh")
    assert publish.maybe_send_alert({"host_name": "H", "inventory": inv + [held]}, config, logs) is False
    state = json.loads((logs / "alert_state.json").read_text(encoding="utf-8"))
    assert state["digest_pending"] == ["FAILED: Subdivision Refresh"]
    assert state["signature"] == sorted(f"FAILED: {n}" for n in THREE)  # nothing else moved

    healthy = _task(tmp_path, "Subdivision Refresh", failed=False)
    assert publish.maybe_send_alert({"host_name": "H", "inventory": inv + [healthy]}, config, logs) is True
    assert "Failed and recovered before this e-mail:\n  FAILED: Subdivision Refresh" in sent[-1]
    assert json.loads((logs / "alert_state.json").read_text(encoding="utf-8"))["digest_pending"] == []


def test_info_lines_with_an_sde_path_do_not_make_a_network_error_look_like_the_database(tmp_path):
    info = "INFO - Connected via ODBC Driver 18 for SQL Server"
    tail = info + "\nERROR - OSError: [WinError 53] The network path was not found"
    assert publish._failure_cause(_task(tmp_path, "N", tail=tail)) == "network"
    assert publish._failure_cause(_task(tmp_path, "E", tail=info.replace("INFO", "ERROR"))) == "database"
    assert publish._failure_cause(_task(tmp_path, "I", tail="INFO - Signed in to portal")) == ""


def test_reference_profile_classifies_an_sde_connection_path_as_database(tmp_path):
    tail = "ERROR - bad source: C:/Jobs/Admin@Data@Default." + "sd" + "e/Layer"
    assert publish._failure_cause(_task(tmp_path, "S", tail=tail)) == "database"


def test_a_healthy_namesake_is_not_counted_in_the_incident(tmp_path):
    inv = [_task(tmp_path, n) for n in THREE] + [_task(tmp_path, "Parcel Copy Job", failed=False)]
    incident = publish.shared_cause(inv, [f"FAILED: {n}" for n in THREE], 3)
    assert incident["count"] == 3


def test_subject_shows_the_date_when_the_incident_began_on_an_earlier_day(tmp_path):
    previous = _state(THREE[:2], None)
    previous["problem_first_seen_utc"] = {"FAILED: Geodatabase Upkeep": "2026-10-01T02:00:00+00:00"}
    alert = _compose(tmp_path, THREE, previous)
    local = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc).astimezone()
    assert f" since {local:%b %d %H:%M} - " in alert["subject"]


def test_batching_sentence_only_when_the_digest_is_on(tmp_path):
    on = _compose(tmp_path, THREE, _state(THREE[:2], None))
    off = _compose(tmp_path, THREE, _state(THREE[:2], None), digest=0)
    assert "batched into the next e-mail" in on["body"]
    assert "batched into the next e-mail" not in off["body"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_config_rejects_non_finite_numbers(tmp_path, bad):
    import taskdash_common as common
    path = tmp_path / "TaskDash.config.json"
    path.write_text(json.dumps({"alerting": {"incident_digest_hours": bad}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="incident_digest_hours"):
        common.load_config(path)


# ---------------------------------------------------------------- second review fixes

def test_old_state_without_incident_cause_never_holds(tmp_path):
    previous = _state(THREE, NOW - timedelta(hours=1))
    del previous["incident_cause"]  # a state file written before this feature
    alert = _compose(tmp_path, THREE + ["Subdivision Refresh"], previous)
    assert alert["send"] and alert["reason"] == "changed"


def test_an_unclear_incident_holds_more_database_failures(tmp_path):
    unclear = ["Roads Feed Export", "Address Null Fill", "Geodatabase Upkeep"]
    previous = {**_state(unclear, NOW - timedelta(hours=1)), "incident_cause": "",
                "incident_causes": ["", "database"]}
    alert = _compose(tmp_path, unclear + ["Parcel Copy Job"], previous)
    assert alert["send"] is False and alert["reason"] == "digest"


def test_odbc_network_name_line_is_the_database_not_the_network(tmp_path):
    line = ("ERROR - pyodbc.OperationalError: [Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: "
            "The specified network name is no longer available.")
    assert publish._failure_cause(_task(tmp_path, "S", tail=line)) == "database"


def test_a_held_task_moving_into_maintenance_is_not_called_recovered(tmp_path):
    previous = {**_state(THREE, NOW - timedelta(hours=1)), "digest_pending": ["FAILED: Subdivision Refresh"]}
    inv = [_task(tmp_path, n) for n in THREE + ["Subdivision Refresh"]]
    window = {"task": "^Subdivisions", "start": (NOW - timedelta(hours=1)).isoformat(),
              "end": (NOW + timedelta(hours=1)).isoformat()}
    config = {"alerting": {"maintenance_windows": [window]}, "arcgis": {"dashboard_id": "D"}}
    alert = publish.compose_alert({"host_name": "H", "inventory": inv}, config, previous, NOW)
    assert "Failed and recovered" not in alert["body"]
    assert alert["send"] is False


def test_a_hold_keeps_the_first_seen_time_of_the_held_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("taskdash_common.load_mail_settings", lambda cfg: {"smtp server": "x", "from address": "a@b.c"})
    monkeypatch.setattr("taskdash_common.send_email", lambda *a, **k: True)
    config = {"alerting": {"enabled": True, "to": ["a@b.c"]}, "arcgis": {"dashboard_id": "D"}}
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "alert_state.json").write_text(json.dumps(_state(THREE, datetime.now(timezone.utc))), encoding="utf-8")
    inv = [_task(tmp_path, n) for n in THREE + ["Subdivision Refresh"]]
    publish.maybe_send_alert({"host_name": "H", "inventory": inv}, config, logs)
    first = json.loads((logs / "alert_state.json").read_text(encoding="utf-8"))["problem_first_seen_utc"]
    publish.maybe_send_alert({"host_name": "H", "inventory": inv}, config, logs)
    again = json.loads((logs / "alert_state.json").read_text(encoding="utf-8"))["problem_first_seen_utc"]
    assert again["FAILED: Subdivision Refresh"] == first["FAILED: Subdivision Refresh"]


def test_causes_are_not_read_when_the_feature_is_off(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(publish, "_failure_cause", lambda t: calls.append(t) or "")
    _compose(tmp_path, THREE, _state(THREE[:2], None))
    assert calls  # on by default
    calls.clear()
    inv = [_task(tmp_path, n) for n in THREE]
    publish.compose_alert({"host_name": "H", "inventory": inv},
                          {"alerting": {"shared_cause_min_tasks": 0}, "arcgis": {"dashboard_id": "D"}}, {}, NOW)
    assert calls == []

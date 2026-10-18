"""Phase 2: secrets never reach a published field; the definition hash stays on raw values."""
from __future__ import annotations

import datetime as dt
import json

import taskdash_collect as collect
import taskdash_metrics as metrics
import taskdash_publish as publish

NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone.utc)
STAMP = NOW.isoformat()
SECRET = "Hunter2Secret"
SHOWN = {"hide_command_lines": False, "hide_sample_lines": False, "hide_accounts": False, "hide_paths": False}
SECRET_ARGUMENTS = [
    f"-Password {SECRET}",
    f"--password={SECRET}",
    f"pwd={SECRET}",
    f"-Password '{SECRET} with space'",
    f"Server=db1;Database=d;User Id=u;Password={SECRET};Encrypt=yes",
]


def _raw_task(arguments="", **overrides):
    raw = {
        "task_name": "Nightly Sync", "task_path": "\\", "task_full_name": "\\Nightly Sync",
        "state": "Ready", "enabled": True, "run_as_user": "svc_sync", "logon_type": "Password",
        "run_level": "Limited", "trigger_types": "Daily", "trigger_details": [],
        "primary_executable": r"C:\Tools\python.exe",
        "primary_arguments": rf"C:\Tools\sync.py {arguments}".strip(),
        "last_run_time": (NOW - dt.timedelta(hours=6)).isoformat(),
        "next_run_time": (NOW + dt.timedelta(hours=18)).isoformat(),
        "last_task_result": 0, "number_of_missed_runs": 0,
    }
    raw["actions"] = f"{raw['primary_executable']} {raw['primary_arguments']}"
    raw.update(overrides)
    return raw


def _inventory_row(raw):
    row = collect.build_inventory_rows(
        [raw], host_name="H1", descriptions={}, audit_dt=NOW, exists=lambda p: True)[0]
    row["definition_hash"] = publish.compute_definition_hash(raw)
    return row


def _every_published_value(rows_by_table):
    return json.dumps(rows_by_table, default=str)


def _metric_row(tmp_path, line):
    logs = tmp_path / "Logs"
    logs.mkdir(exist_ok=True)
    (logs / "sync_20261016.log").write_text(line + "\n", encoding="utf-8")
    (tmp_path / "sync.py").write_text("", encoding="utf-8")
    rows, _ = metrics.build_metric_rows(
        [{"task_name": "Nightly Sync", "task_full_name": "\\Nightly Sync",
          "command_target_path": str(tmp_path / "sync.py")}], host_name="H1")
    return rows


def test_password_in_arguments_log_error_and_summary_reaches_no_published_row(tmp_path):
    log_line = f"2026-10-16 02:00:01,000 - INFO - Total records in feature class: 120 pwd={SECRET}"
    metric_rows = _metric_row(tmp_path, log_line)
    assert metric_rows, "the synthetic log line must yield a metric row"
    assert SECRET not in json.dumps(metric_rows), "the builder scrubs a sample line before its cut"

    for arguments in SECRET_ARGUMENTS:
        raw = _raw_task(arguments)
        old_row = _inventory_row(_raw_task(f"-Password {SECRET}old"))
        new_row = _inventory_row(raw)
        assert SECRET in new_row["actions"], "precondition: the builder keeps the raw command line"
        old_row["definition_hash"] = publish.DEFINITION_HASH_PREFIX + "0" * 62
        changes = publish.detect_task_changes({new_row["task_full_name"]: old_row}, [new_row], "H1", STAMP)
        run_row = publish._collector_run_row(
            {"host_name": "H1"}, run_started=NOW, run_ended=NOW, exit_status="failed",
            error_text=f"login failed {arguments}", watermark_from=0.0, watermark_to=0.0,
            publish_counts={}, alert_sent=False)
        for privacy in (None, {"hide_command_lines": True}):
            published = {
                publish.TABLE_INVENTORY: publish.prepare_rows(
                    [old_row, new_row], publish.TABLE_INVENTORY, STAMP, privacy),
                publish.TABLE_TASK_CHANGES: publish.prepare_rows(
                    changes, publish.TABLE_TASK_CHANGES, STAMP, privacy),
                publish.TABLE_METRICS: publish.prepare_rows(metric_rows, publish.TABLE_METRICS, STAMP, privacy),
                publish.TABLE_COLLECTOR_RUNS: publish.prepare_rows(
                    [run_row], publish.TABLE_COLLECTOR_RUNS, STAMP, privacy),
            }
            assert changes, "the changed hash must log a definition_changed row"
            assert SECRET not in _every_published_value(published), arguments


def test_scrub_covers_the_new_forms_and_keeps_the_old_ones():
    for text in SECRET_ARGUMENTS:
        assert SECRET not in publish._scrub_secrets(f"run.py {text} --apply")
    assert "--apply" in publish._scrub_secrets(f"run.py -Password {SECRET} --apply")
    assert publish._scrub_secrets("ftp://u:p4ss@h/x") == "ftp://<redacted>@h/x"
    assert "abc123" not in publish._scrub_secrets("Authorization: Bearer abc123")
    assert "abc" not in publish._scrub_secrets('{"password": "abc"}')


def test_rows_without_a_secret_are_unchanged(tmp_path):
    raw = _raw_task("--apply --days 3 -ExecutionPolicy Bypass -Passthru")
    inventory = [_inventory_row(raw)]
    metric_rows = _metric_row(tmp_path, "2026-10-16 02:00:01,000 - INFO - Total records in feature class: 120")
    changes = [{"host_name": "H1", "task_full_name": "\\T", "task_name": "T", "change_kind": "definition_changed",
                "changed_utc": STAMP, "old_summary": "Enabled: no", "new_summary": "Enabled: yes"}]
    run_row = publish._collector_run_row(
        {"host_name": "H1"}, run_started=NOW, run_ended=NOW, exit_status="success", error_text="",
        watermark_from=0.0, watermark_to=0.0, publish_counts={}, alert_sent=False)
    for table, rows in ((publish.TABLE_INVENTORY, inventory), (publish.TABLE_METRICS, metric_rows),
                        (publish.TABLE_TASK_CHANGES, changes), (publish.TABLE_COLLECTOR_RUNS, [run_row])):
        prepared = publish.prepare_rows(rows, table, STAMP, SHOWN)
        for before, after in zip(rows, prepared):
            assert all(after[k] == before[k] for k in after if k not in publish.DATE_FIELDS[table]
                       and k != "collected_at_utc"), table


def test_definition_hash_is_on_raw_values_and_a_secret_only_edit_is_still_a_change():
    plain, other = _raw_task(f"-Password {SECRET}"), _raw_task(f"-Password {SECRET}x")
    assert publish.compute_definition_hash(plain) == publish.compute_definition_hash(dict(plain))
    assert publish.compute_definition_hash(plain) != publish.compute_definition_hash(other)
    scrubbed = dict(plain, actions=publish._scrub_secrets(plain["actions"]))
    assert publish.compute_definition_hash(scrubbed) != publish.compute_definition_hash(plain)

    stored = publish.prepare_rows([_inventory_row(plain)], publish.TABLE_INVENTORY, STAMP)[0]
    fresh = _inventory_row(plain)
    assert publish.detect_task_changes({fresh["task_full_name"]: stored}, [fresh], "H1", STAMP) == []
    assert publish.detect_task_changes(
        {fresh["task_full_name"]: stored}, [publish.prepare_rows([fresh], publish.TABLE_INVENTORY, STAMP)[0]],
        "H1", STAMP) == []


def test_privacy_switches_blank_fields_and_default_to_hidden(tmp_path):
    inventory = [_inventory_row(_raw_task("--apply"))]
    metric_rows = _metric_row(tmp_path, "2026-10-16 02:00:01,000 - INFO - Total records in feature class: 120")
    runs = [{"run_key": "k", "user_context": "svc_sync"}]
    for missing in ({}, None):
        default = publish.prepare_rows(inventory, publish.TABLE_INVENTORY, STAMP, missing)[0]
        assert default["actions"] == "" and default["command_target_arguments"] == ""
        assert default["run_as_user"] == "" and default["command_target_path"] == ""
        assert publish.prepare_rows(metric_rows, publish.TABLE_METRICS, STAMP, missing)[0]["sample_line"] == ""
        assert publish.prepare_rows(runs, publish.TABLE_RUNS, STAMP, missing)[0]["user_context"] == ""

    shown = publish.prepare_rows(inventory, publish.TABLE_INVENTORY, STAMP, SHOWN)[0]
    assert shown["actions"] and shown["command_target_arguments"] and shown["run_as_user"] == "svc_sync"
    assert publish.prepare_rows(metric_rows, publish.TABLE_METRICS, STAMP, SHOWN)[0]["sample_line"]
    assert publish.prepare_rows(runs, publish.TABLE_RUNS, STAMP, SHOWN)[0]["user_context"] == "svc_sync"

    hidden = publish.prepare_rows(inventory, publish.TABLE_INVENTORY, STAMP,
                                  dict(SHOWN, hide_command_lines=True))[0]
    assert hidden["actions"] == "" and hidden["command_target_arguments"] == ""
    assert hidden["run_as_user"] == "svc_sync"
    assert publish.prepare_rows(metric_rows, publish.TABLE_METRICS, STAMP, dict(SHOWN, hide_sample_lines=True))[0][
        "sample_line"] == ""
    accounts = dict(SHOWN, hide_accounts=True)
    assert publish.prepare_rows(inventory, publish.TABLE_INVENTORY, STAMP, accounts)[0]["run_as_user"] == ""
    assert publish.prepare_rows(runs, publish.TABLE_RUNS, STAMP, accounts)[0]["user_context"] == ""


def test_hidden_command_lines_show_hidden_on_both_sides_of_a_definition_change():
    old, new = _inventory_row(_raw_task("--days 1")), _inventory_row(_raw_task("--days 2"))
    assert old["definition_hash"] != new["definition_hash"]
    changes = publish.detect_task_changes({old["task_full_name"]: old}, [new], "H1", STAMP,
                                          hide_commands=True)
    (change,) = [c for c in changes if c["change_kind"] == "definition_changed"]
    assert "Actions: (hidden)" in change["old_summary"] and "Actions: (hidden)" in change["new_summary"]
    assert "--days" not in change["old_summary"] + change["new_summary"]


def test_privacy_keys_validate_as_booleans():
    import taskdash_common as common
    assert common._validate_config({"privacy": {"hide_accounts": True}}) == []
    assert common._validate_config({"privacy": {"hide_accounts": "yes"}})


def test_quoted_secret_with_trailing_text_is_fully_redacted():
    assert publish._scrub_secrets("pwd='abc'def") == "pwd=<redacted>"
    assert publish._scrub_secrets("-Password 'it''s'") == "-Password <redacted>"
    escaped = publish._scrub_secrets('{"password": "p\\"w0rd"}')
    assert "w0rd" not in escaped and "<redacted>" in escaped
    assert "b" not in publish._scrub_secrets('--token "a"b').replace("<redacted>", "").replace("--token", "")


def test_scrub_never_pushes_a_value_past_its_field_length():
    length = next(f["length"] for f in publish.TABLE_SPECS[publish.TABLE_INVENTORY][0]() if f["name"] == "actions")
    row = {"actions": "a" * (length - 7) + " -pwd x"}
    assert len(row["actions"]) == length
    (prepared,) = publish.prepare_rows([row], publish.TABLE_INVENTORY, STAMP, SHOWN)
    assert len(prepared["actions"]) == length


def test_hidden_accounts_show_hidden_on_both_sides_of_a_definition_change():
    old, new = _inventory_row(_raw_task()), _inventory_row(_raw_task(run_as_user="svc_other"))
    changes = publish.detect_task_changes({old["task_full_name"]: old}, [new], "H1", STAMP,
                                          hide_accounts=True)
    (change,) = [c for c in changes if c["change_kind"] == "definition_changed"]
    assert "(hidden)" in change["old_summary"] and "svc_" not in change["old_summary"] + change["new_summary"]

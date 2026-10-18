"""Round-6 audit fixes in taskdash_collect.py / taskdash_publish.py:

1. A Skipped-AlreadyRunning row whose instance_id matches a real run that started within 60 s no
   longer inflates skipped_last_7_days / skipped_already_running_24h (benign duplicate launch).
2. A trigger start with a fixed UTC offset ("Synchronize across time zones") gets a config-warning
   note, and describe_trigger marks it so the definition hash moves only for the affected tasks
   (DEFINITION_HASH_PREFIX v5 -> v6).
3. A cleared config-warning note says whether the underlying setting changed or was only
   acknowledged / no longer raised.
4. The 0xC000013A result text drops console jargon a scheduled task never has.

No network, no ArcGIS Online, no SMTP: fakes throughout. Matches test_round4_collect_publish.py
style.
"""

from __future__ import annotations

import datetime as dt

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402
import taskdash_publish as publish  # noqa: E402


NOW = dt.datetime(2026, 9, 29, 12, 27, 1, tzinfo=dt.timezone.utc)


# ==================================================================== 1. benign duplicate launch

def _run(task="\\Task Dash", start=None, status=None, duration=10.0, instance_id="", is_error=0):
    row = {"task_full_name": task, "duration_seconds": duration, "is_error": is_error,
          "start_time_utc": start, "instance_id": instance_id}
    if status:
        row["run_status"] = status
    return row


def test_duplicate_skip_of_a_run_that_started_is_not_counted():
    """task_detail-taskdash-benign-skip-counted / health_table-taskdash-skipped-count-
    undercounted: the 09-29 prod-snapshot pair (TaskRuns OBJECTID 4641 Success / 4642
    Skipped-AlreadyRunning, same instance_id {4ebeac23...}, 0.27 s apart) is a refused duplicate
    launch of a run that did start -- no run was lost, so it must not count as a skip."""
    rows = [
        _run(start="2026-09-29T11:00:00.53Z", status="Success", instance_id="4ebeac23"),
        _run(start="2026-09-29T11:00:00.80Z", status="Skipped-AlreadyRunning", duration=None,
             instance_id="4ebeac23"),
    ]
    summary = publish.summarize_runs(rows, NOW)["\\Task Dash"]
    assert summary["skipped_last_7_days"] == 0
    assert summary["skipped_already_running_24h"] == 0
    assert summary["runs_last_7_days"] == 1


def test_a_skip_with_no_matching_started_instance_still_counts():
    """A Skipped-AlreadyRunning row is only excused when ITS OWN instance_id started shortly
    before it. A different instance (a genuine lost run) still counts as skipped."""
    rows = [
        _run(start="2026-09-29T07:00:00Z", status="Success", instance_id="aaa"),
        _run(start="2026-09-29T11:00:00Z", status="Skipped-AlreadyRunning", duration=None,
             instance_id="bbb"),
    ]
    summary = publish.summarize_runs(rows, NOW)["\\Task Dash"]
    assert summary["skipped_last_7_days"] == 1
    assert summary["skipped_already_running_24h"] == 1


def test_a_skip_more_than_60s_after_its_own_instance_started_still_counts():
    """Same instance_id, but the gap is beyond RUN_FAILURE_TOLERANCE_SECONDS (60 s): not a
    same-second duplicate launch, so it is a real skip."""
    assert publish.RUN_FAILURE_TOLERANCE_SECONDS == 60
    rows = [
        _run(start="2026-09-29T07:00:00Z", status="Success", instance_id="4ebeac23"),
        _run(start="2026-09-29T08:00:00Z", status="Skipped-AlreadyRunning", duration=None,
             instance_id="4ebeac23"),
    ]
    summary = publish.summarize_runs(rows, NOW)["\\Task Dash"]
    assert summary["skipped_last_7_days"] == 1


# ==================================================================== 2. fixed-UTC-offset trigger

# The Replica Lag Monitor trigger from scratchpad/truth6/gt_tasks.json.
_OFFSET_TRIGGER = {
    "type": "Daily", "start": "2026-09-18T07:05:00-04:00", "end": "", "enabled": True,
    "days_interval": "1", "days_of_week": "", "weeks_interval": "", "repetition_interval": "",
    "repetition_duration": "", "random_delay": "", "delay": "", "months": "",
    "days_of_month": "", "weeks_of_month": "", "month_days_of_week": "",
}


def _set_eastern_zone(monkeypatch):
    """Pin the server's local zone to US Eastern (std UTC-5, DST UTC-4) so the offset-direction
    tests do not depend on the machine that runs them (wording-utc-offset-note-vague)."""
    monkeypatch.setattr(collect.time, "timezone", 18000)   # EST, UTC-5
    monkeypatch.setattr(collect.time, "altzone", 14400)    # EDT, UTC-4
    monkeypatch.setattr(collect.time, "daylight", 1)


def test_offset_trigger_gets_a_dst_config_warning(monkeypatch):
    """operations-dst-synchronized-triggers / wording-utc-offset-note-vague: a start boundary
    registered with 'Synchronize across time zones' keeps a literal UTC offset. -04:00 is this
    (Eastern) server's DST offset, so the trigger runs 1 h early once the clock falls back to
    standard time."""
    _set_eastern_zone(monkeypatch)
    raw = {"multiple_instances_policy": "IgnoreNew", "execution_time_limit": "PT30M",
          "start_when_available": True, "trigger_details": [_OFFSET_TRIGGER]}
    warnings = collect.build_config_warnings(raw, "Daily", exists=lambda p: True)
    assert ("Start time is fixed to UTC-04:00, so in standard time it runs 1 hour early. "
           "To fix: clear \"Synchronize across time zones\" on the trigger") in warnings


def test_offset_trigger_matching_local_standard_time_runs_late(monkeypatch):
    """The other direction: an offset that matches the server's local standard time runs 1 h
    late once the clock springs forward into daylight-saving time."""
    _set_eastern_zone(monkeypatch)
    standard_offset_trigger = dict(_OFFSET_TRIGGER, start="2026-09-18T07:05:00-05:00")
    warnings = collect.check_dst_offset_trigger([standard_offset_trigger])
    assert ("Start time is fixed to UTC-05:00, so in daylight-saving time "
           "it runs 1 hour late. To fix:") in warnings[0]


def test_offset_trigger_matching_neither_local_offset_just_says_it_moves(monkeypatch):
    """A "Z" (UTC) boundary, or any offset that matches neither the server's standard nor DST
    offset, cannot be labelled early/late from the server's own zone."""
    _set_eastern_zone(monkeypatch)
    z_trigger = dict(_OFFSET_TRIGGER, start="2026-09-18T11:05:00Z")
    warnings = collect.check_dst_offset_trigger([z_trigger])
    assert ("Start time is fixed to UTC, so its local start time moves by 1 hour at each "
           "daylight-saving change. To fix:") in warnings[0]


def test_a_plain_local_start_gets_no_dst_warning():
    plain_trigger = dict(_OFFSET_TRIGGER, start="2026-08-11T07:00:00")
    assert collect.check_dst_offset_trigger([plain_trigger]) == []
    raw = {"multiple_instances_policy": "IgnoreNew", "execution_time_limit": "PT30M",
          "start_when_available": True, "trigger_details": [plain_trigger]}
    assert "UTC offset" not in collect.build_config_warnings(raw, "Daily", exists=lambda p: True)


def test_describe_trigger_keeps_local_time_and_marks_the_offset():
    """describe_trigger/_plain_time must not silently drop the offset."""
    text = collect.describe_trigger(_OFFSET_TRIGGER)
    assert "7:05 AM, fixed to UTC-04:00" in text  # local wall-clock time, not the UTC time
    plain_trigger = dict(_OFFSET_TRIGGER, start="2026-08-11T07:00:00")
    assert "fixed to UTC" not in collect.describe_trigger(plain_trigger)
    z_trigger = dict(_OFFSET_TRIGGER, start="2026-09-18T11:05:00Z")
    assert "fixed to UTC (since" in collect.describe_trigger(z_trigger)


def _definition(trigger):
    return {"actions": "run.cmd", "trigger_details": [trigger], "run_as_user": "svc_gisjobs",
           "logon_type": "Password", "run_level": "Highest", "enabled": True,
           "multiple_instances_policy": "IgnoreNew", "execution_time_limit": "PT30M",
           "start_when_available": True}


def test_definition_hash_moves_only_for_the_offset_trigger():
    """The offset marker changes the hashed trigger text. Whatever the current prefix is, its
    stored predecessor is a silent baseline, so the deploy pass itself logs no
    definition_changed row (see the DEFINITION_HASH_PREFIX comment)."""
    plain_trigger = dict(_OFFSET_TRIGGER, start="2026-08-11T07:00:00")
    assert (publish.compute_definition_hash(_definition(_OFFSET_TRIGGER))
           != publish.compute_definition_hash(_definition(plain_trigger)))


# ==================================================================== 3. acknowledged vs fixed

def test_cleared_note_says_no_setting_changed_when_the_hash_is_unchanged():
    """operations-acknowledged-note-reads-as-fixed / collector_tab-acknowledged-note-shown-as-
    removed / wording-ack-reads-as-setting-fixed: Geodatabase Upkeep's StartWhenAvailable note
    disappeared because workflow_descriptions.json acknowledges it, not because catch-up was
    turned on -- gt_tasks still has start_when_available=False for that task, so its
    definition_hash (which covers describe_task_settings) is unchanged."""
    hash_value = publish.DEFINITION_HASH_PREFIX + "a" * 62
    base = {"task_full_name": "\\T", "task_name": "T", "definition_hash": hash_value,
           "last_run_failed": 0, "is_overdue": 0,
           "config_warnings": "StartWhenAvailable is false on a Daily/Weekly task"}
    new = dict(base, config_warnings="", definition_hash=hash_value)
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-28T21:00:22+00:00")
    [cleared] = [c for c in changes if c["change_kind"] == "config_warning_cleared"]
    assert cleared["new_summary"] == (
        "The task settings did not change: the problem went away (for example a missing file "
        "was restored), the note was acknowledged, or TaskDash no longer raises it.")


def test_cleared_note_says_the_setting_was_changed_when_the_hash_moved():
    """The 09-29 08:06 fixes (Address & ERP Script, Animal Services catch-up turned on) must still
    read as a real fix: their definition_hash moved."""
    hash_before = publish.DEFINITION_HASH_PREFIX + "a" * 62
    hash_after = publish.DEFINITION_HASH_PREFIX + "b" * 62
    base = {"task_full_name": "\\T", "task_name": "T", "definition_hash": hash_before,
           "last_run_failed": 0, "is_overdue": 0,
           "config_warnings": "Catch-up is off: if the server is down at start time"}
    new = dict(base, config_warnings="", definition_hash=hash_after)
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-29T12:27:01+00:00")
    [cleared] = [c for c in changes if c["change_kind"] == "config_warning_cleared"]
    assert cleared["new_summary"] == "The task definition was changed in the same hour."


# ==================================================================== 4. plain stopped-run text

def test_stopped_run_result_text_is_plain_not_console_jargon():
    """wording-terminated-result-jargon: the task is scheduled and has no console, so 'Ctrl+C /
    console close' sent the operator the wrong way."""
    text = collect.HRESULT_TEXT[0xC000013A]
    assert text == "The run was stopped before it finished (it was ended, or its console closed)."
    assert collect.describe_hresult(3221225786) == text
    assert "Ctrl+C" not in text


def test_refresh_result_text_rewrites_a_stored_terminated_row():
    """--refresh-result-text (publish.run_result_text, called by refresh_result_text) must pick
    up the new wording for rows stored with the old text or the generic unmapped text."""
    old_text = "Unmapped task result code (3221225786, 0xC000013A)."
    new_text = publish.run_result_text(3221225786, old_text, "Error")
    assert new_text == collect.HRESULT_TEXT[0xC000013A]


# ==================================================================== review fixes

def test_live_summary_reads_instance_id_so_the_duplicate_skip_is_excused():
    """The live path summarizes STORED rows: without instance_id in out_fields the 09-29 pair
    still counted as a skip in production."""
    class Result:
        def __init__(self, features):
            self.features = features

    class Feature:
        def __init__(self, attributes):
            self.attributes = attributes

    class Table:
        properties = {"maxRecordCount": 2000}

        def query(self, where, out_fields, **kwargs):
            assert "instance_id" in out_fields.split(",")
            if kwargs.get("result_offset"):
                return Result([])
            return Result([Feature({"task_full_name": "\\Task Dash", "run_status": "Success",
                                    "start_time_utc": 1790679600530, "instance_id": "4ebeac23",
                                    "duration_seconds": 10.0, "is_error": 0}),
                           Feature({"task_full_name": "\\Task Dash",
                                    "run_status": "Skipped-AlreadyRunning",
                                    "start_time_utc": 1790679600800, "instance_id": "4ebeac23",
                                    "duration_seconds": None, "is_error": 0})])

    summary = publish.summarize_runs_from_agol(Table(), "H", NOW)["\\Task Dash"]
    assert summary["skipped_last_7_days"] == 0


def test_a_stored_older_hash_logs_no_note_rows_at_all():
    """operations-rule-deploy-note-dated-as-task-change: a stored hash from an older scheme is a
    baseline the same way it is for definition_changed -- both the added and the cleared loop are
    skipped for it, so a new TaskDash rule (or a reworded note) is never dated as a change on a
    task whose real definition did not move. The old "The note no longer applies." wording is
    gone; there is no row at all here to carry it."""
    base = {"task_full_name": "\\T", "task_name": "T", "definition_hash": "v5" + "a" * 62,
            "last_run_failed": 0, "is_overdue": 0, "config_warnings": "Some note"}
    new = dict(base, config_warnings="A new note", definition_hash=publish.DEFINITION_HASH_PREFIX + "a" * 62)
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-29T12:27:01+00:00")
    assert [c for c in changes if c["change_kind"].startswith("config_warning_")] == []


def test_a_utc_z_start_boundary_is_also_a_fixed_offset():
    assert collect._FIXED_UTC_OFFSET_RE.search("2026-09-18T11:05:00Z")
    assert not collect._FIXED_UTC_OFFSET_RE.search("2026-09-18T07:05:00")


def test_a_host_without_daylight_saving_gets_no_offset_note(monkeypatch):
    """With no DST a fixed offset never drifts, so a note would be false (a UTC server)."""
    monkeypatch.setattr(collect.time, "daylight", 0)
    monkeypatch.setattr(collect.time, "timezone", 0)
    monkeypatch.setattr(collect.time, "altzone", 0)
    assert collect.check_dst_offset_trigger([_OFFSET_TRIGGER]) == []

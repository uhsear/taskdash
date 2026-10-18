"""Round-7 audit fixes in taskdash_collect.py / taskdash_publish.py:

1. A stored row whose definition_hash is from an older DEFINITION_HASH_PREFIX scheme is a
   baseline for its config-warning notes too, not just for definition_changed: both the added
   and the cleared loop in detect_task_changes are skipped for it, so a new TaskDash rule (or a
   reworded note) is never dated as a task change on the deploy pass
   (operations-rule-deploy-note-dated-as-task-change).
2. summarize_runs keeps last_completed_result_code (the newest FINISHED run's result code), and
   apply_own_run_view now also sets last_result_code from it while Task Dash's own row is judged
   mid-run (operations-own-task-result-code-stale).
3. parse_description_entry returns acknowledge_reason, and build_inventory_rows appends it to
   settings_detail as " | Accepted: <reason>" so an owner-accepted setting shows why
   (overview-catchup-off-acknowledgement-not-shown). It is not part of the definition hash.

No network, no ArcGIS Online, no SMTP: fakes throughout. Matches test_round6_collect_publish.py
style.
"""

from __future__ import annotations

from datetime import timedelta

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402
import taskdash_publish as publish  # noqa: E402
from test_taskdash import NOW, _task, iso  # noqa: E402


# ============================================================== 1. deploy-note-not-a-task-change

def test_a_stored_baseline_hash_logs_no_note_rows_at_all():
    """A v6 row (the predecessor of the current prefix) never had its notes compared under the
    current scheme. Without the guard on both loops, a note present only in the new row reads as
    "Setting note added" today, though the task itself has not changed since the v6 pass -- this
    is exactly what happened to Replica Lag Monitor on the round-6 deploy (TaskChanges
    OBJECTID 63)."""
    base = {"task_full_name": "\\T", "task_name": "T", "definition_hash": "v6" + "a" * 62,
            "last_run_failed": 0, "is_overdue": 0, "config_warnings": ""}
    new = dict(base, config_warnings="Start time is fixed to UTC-04:00, so in standard time "
                                      "it runs 1 hour early",
               definition_hash=publish.DEFINITION_HASH_PREFIX + "a" * 62)
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-29T13:34:50+00:00")
    assert changes == []


def test_a_stored_baseline_hash_also_hides_a_note_that_cleared():
    """Same guard, the other loop: a note present only in the OLD row must not read as
    "cleared" either when the stored hash is a baseline."""
    base = {"task_full_name": "\\T", "task_name": "T", "definition_hash": "v6" + "a" * 62,
            "last_run_failed": 0, "is_overdue": 0, "config_warnings": "An old-scheme note"}
    new = dict(base, config_warnings="", definition_hash=publish.DEFINITION_HASH_PREFIX + "a" * 62)
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-29T13:34:50+00:00")
    assert changes == []


def test_two_current_scheme_rows_still_detect_a_real_added_and_cleared_note():
    """The baseline guard only fires for an OLDER hash scheme; two rows both stored under the
    current prefix still detect real note changes, one added and one cleared."""
    hash_value = publish.DEFINITION_HASH_PREFIX + "a" * 62
    base = {"task_full_name": "\\T", "task_name": "T", "definition_hash": hash_value,
            "last_run_failed": 0, "is_overdue": 0, "config_warnings": "An old note"}
    new = dict(base, config_warnings="A brand new note")
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-29T13:34:50+00:00")
    kinds = sorted(c["change_kind"] for c in changes)
    assert kinds == ["config_warning_added", "config_warning_cleared"]


# ==================================================================== 2. own-run result code

def _run(hours_ago, status="Success", duration=30.0, is_error=0, result_code=0, task="\\T"):
    row = {"task_full_name": task, "run_status": status, "duration_seconds": duration,
           "is_error": is_error, "result_code": result_code,
           "result_text": "The operation completed successfully.",
           "start_time_utc": iso(NOW - timedelta(hours=hours_ago))}
    return row


def test_summarize_runs_keeps_the_newest_finished_runs_result_code():
    rows = [_run(0.01, status="Running", duration=None, result_code=None),
            _run(1, result_code=0)]
    stats = publish.summarize_runs(rows)["\\T"]
    assert stats["last_completed_result_code"] == 0


def test_summarize_runs_result_code_is_none_with_no_finished_run():
    rows = [_run(0.01, status="Running", duration=None, result_code=None)]
    stats = publish.summarize_runs(rows)["\\T"]
    assert stats["last_completed_result_code"] is None


def _own_task(**overrides):
    fields = {"task_name": "Task Dash", "task_full_name": "\\Task Dash", "state": "Running",
              "health_status": "Running", "needs_attention": 0, "last_run_failed": 0,
              "is_overdue": 0, "last_result_code": 267009,
              "command_target_path": str(common.runtime_dir() / "run_taskdash.cmd"),
              "last_result_text": "Task is currently running.", "attention_reason": "",
              "last_run_time": None}
    fields.update(overrides)
    return fields


def test_apply_own_run_view_replaces_the_running_result_code_with_the_previous_runs():
    """The snapshot defect: last_result_code stayed 267009 ("currently running") next to
    last_result_text/last_run_time from the previous finished run."""
    task = _own_task()
    stats = publish.summarize_runs([_run(0.01, status="Running", duration=None, result_code=None),
                                    _run(1, result_code=0)])["\\T"]
    publish.apply_own_run_view(task, stats)
    assert task["last_result_code"] == 0


def test_apply_own_run_view_leaves_the_result_code_when_no_finished_run_is_known():
    task = _own_task()
    publish.apply_own_run_view(task, {"last_completed_result_text": ""})
    assert task["last_result_code"] == 267009


# ==================================================================== 3. acknowledged settings

def test_parse_description_entry_returns_acknowledge_reason():
    entry = collect.parse_description_entry({
        "description": "d", "acknowledge_config_warnings": ["StartWhenAvailable"],
        "acknowledge_reason": "Catch-up is off on purpose.",
    })
    assert entry["acknowledge_reason"] == "Catch-up is off on purpose."


def test_parse_description_entry_plain_string_has_an_empty_reason():
    assert collect.parse_description_entry("Just a description.")["acknowledge_reason"] == ""


def test_build_inventory_rows_appends_the_accepted_reason_to_settings_detail():
    descriptions = {"geodatabase upkeep": {
        "description": "", "acknowledge_config_warnings": ["StartWhenAvailable"],
        "acknowledge_reason": "Catch-up is off on purpose. A catch-up run could start heavy "
                               "database work in business hours.",
    }}
    row = collect.build_inventory_rows(
        [_task(task_name="Geodatabase Upkeep", start_when_available=False)],
        host_name="H", descriptions=descriptions, audit_dt=NOW,
    )[0]
    assert row["settings_detail"].endswith(
        "Catch-up: off | Accepted: Catch-up is off on purpose. A catch-up run could start heavy "
        "database work in business hours.")


def test_build_inventory_rows_settings_detail_unchanged_with_no_acknowledge_reason():
    row = collect.build_inventory_rows(
        [_task(start_when_available=False)], host_name="H", descriptions={}, audit_dt=NOW,
    )[0]
    assert row["settings_detail"].endswith("Catch-up: off")
    assert "Accepted" not in row["settings_detail"]


def test_settings_detail_is_not_part_of_the_definition_hash():
    """compute_definition_hash calls collect.describe_task_settings(raw) directly, not the
    settings_detail row field, so appending " | Accepted: ..." to settings_detail must not move
    the hash -- no DEFINITION_HASH_PREFIX bump is needed for this fix. Two tasks whose raw
    Task Scheduler data is identical hash the same, whatever workflow_descriptions.json says
    about them."""
    import inspect
    source = inspect.getsource(publish.compute_definition_hash)
    assert "describe_task_settings" in source
    assert "settings_detail" not in source

    raw = _task(start_when_available=False)
    with_reason = collect.build_inventory_rows(
        [raw], host_name="H", audit_dt=NOW,
        descriptions={"parcel refresh": {
            "description": "", "acknowledge_config_warnings": ["StartWhenAvailable"],
            "acknowledge_reason": "On purpose.",
        }},
    )[0]
    without_reason = collect.build_inventory_rows(
        [raw], host_name="H", descriptions={}, audit_dt=NOW,
    )[0]
    assert with_reason["settings_detail"] != without_reason["settings_detail"]
    # definition_hash itself is computed later (publish.collect_everything), straight from the
    # raw Task Scheduler data -- it never sees either inventory row's settings_detail.
    assert publish.compute_definition_hash(raw) == publish.compute_definition_hash(raw)

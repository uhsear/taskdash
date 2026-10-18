"""Phase 0 history: TaskChanges keeps the reason, not the live "Running now" lead or the command line."""
from __future__ import annotations

import taskdash_dashboard as dashboard
import taskdash_publish as publish

LEAD = "Running now. Status is from the previous run: "
REASON = "Script exited with code 1."
NOW = "2026-10-16T00:00:00+00:00"
RAW = {"actions": "python.exe run.py (wd=C:\\Work)", "trigger_details": [], "run_as_user": "u",
       "logon_type": "Password", "run_level": "Limited", "enabled": True}


def _row(**fields):
    row = {"task_full_name": "\\T", "task_name": "T", "definition_hash": publish.compute_definition_hash(RAW),
           "last_run_failed": 0, "is_overdue": 0, "config_warnings": "", "attention_reason": "",
           "actions": RAW["actions"], "run_as_user": "u", "logon_type": "Password",
           "run_level": "Limited", "enabled": 1, "schedule_detail": "Daily at 02:00",
           "command_target_name": "run.py"}
    row.update(fields)
    return row


def test_became_failing_and_recovered_drop_the_live_lead():
    ok = _row()
    bad = _row(last_run_failed=1, attention_reason=LEAD + REASON)
    failing = publish.detect_task_changes({"\\T": ok}, [bad], "H", NOW)
    assert [(c["change_kind"], c["new_summary"]) for c in failing] == [("became_failing", REASON)]
    recovered = publish.detect_task_changes({"\\T": bad}, [ok], "H", NOW)
    assert [(c["change_kind"], c["old_summary"]) for c in recovered] == [("recovered", REASON)]


def test_task_added_keeps_schedule_script_run_as_enabled_but_not_command_or_folder():
    added = publish.detect_task_changes({"\\Other": _row(task_full_name="\\Other")}, [_row()], "H", NOW)
    summary = next(c["new_summary"] for c in added if c["change_kind"] == "task_added")
    assert summary == ("Schedule: Daily at 02:00 | Script: run.py | Runs as: u (Password, Limited)"
                       " | Enabled: yes")
    assert "python.exe" not in summary and "Working folder" not in summary and "C:\\Work" not in summary


def test_task_added_wording_never_touches_hash_or_comparison():
    before = publish.compute_definition_hash(RAW)
    publish._definition_summary(_row(), brief=True)
    assert publish.compute_definition_hash(dict(RAW)) == before
    # The full summary (used by definition_changed and task_removed) still shows the command line.
    assert "python.exe run.py" in publish._definition_summary(_row())
    # Same stored state before and after, only reason text differs: no row at all.
    old = _row(last_run_failed=1, attention_reason=LEAD + REASON)
    new = _row(last_run_failed=1, attention_reason=REASON)
    assert publish.detect_task_changes({"\\T": old}, [new], "H", NOW) == []
    assert publish.detect_task_changes({"\\T": new}, [old], "H", NOW) == []


def test_display_cleanup_strips_the_lead_from_rows_already_stored():
    assert dashboard.OLD_SUMMARY_FIXES[-1] ==(LEAD, "")
    expression = dashboard._tidy_old_summary("old_summary")
    assert expression.endswith(f'"{LEAD}", "")') and expression.startswith("Replace(")

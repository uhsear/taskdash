"""Final-audit findings 3, 6, 10 and 11: the Task health Why cell. Arcade cannot run here, so
the checks read the generated expression (same pattern as test_phase0_dashboard.py)."""

from __future__ import annotations

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402


def test_log_errors_count_only_for_a_log_written_since_the_last_run():
    expr = dash.task_table_expression()
    assert "if (!IsEmpty(logT) && !IsEmpty(runT) && logT < runT - 60000) { logErrLines = 0; }" in expr
    # The guard runs before the log note is chosen.
    assert expr.index("logErrLines = 0;") < expr.index('plural(logErrLines, "log error")')


def test_disabled_task_reads_like_the_phone_list():
    expr = dash.task_table_expression()
    assert 'whyText = "Disabled in Task Scheduler.";' in expr
    assert "Disabled in Task Scheduler." in dash.mobile_attention_expression()
    # A real reason or the disabled note wins over a stale log note.
    assert expr.index("Disabled in Task Scheduler.") < expr.index('plural(logErrLines, "log error")')


def test_a_run_in_progress_says_running_now():
    expr = dash.task_table_expression()
    assert 'if (whyText == "" && health == "Running") { whyText = "Running now"; }' in expr
    # Task Dash's own row always carries the "Running now." reason, so it must not drive the cell.
    assert 'Find("Running now.", why)' not in expr
    assert expr.index('whyText = "Running now"') < expr.index('plural(logErrLines, "log error")')

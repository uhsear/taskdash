"""Round-7 (prod audit) fixes, dashboard items. One test per non-trivial change.

Follows the pattern of test_round4_dashboard.py / test_round6_dashboard.py: build the
definition in the test, since Arcade cannot run here, and check the generated Arcade source
for the logic changes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402


def _templates():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    return {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
            "serial_config": by_name["Schedule Mix"]["chartConfig"]}


@pytest.fixture
def build(monkeypatch):
    def make(**flags):
        for name, value in flags.items():
            monkeypatch.setitem(dash.FEATURE_FLAGS, name, value)
        return dash.build("TESTITEM", _templates())
    return make


def _by_name(definition):
    return {w["name"]: w for w in dash.all_items(definition)}


# ------------------------------------------------------------------ item 1: cleared-note summaryText

def test_cleared_note_summary_keeps_was_marker_and_appends_the_reason():
    expression = dash.changes_list_expression()
    assert ('ck == "config_warning_cleared" && oldS != "", "Was: " + oldS'
            ' + IIf(newS == "", "", ". " + newS)') in expression
    assert 'whenText: fmtDate($datapoint["changed_utc"])' in expression


# ------------------------------------------------------------------ item 1 (wording-mixed-date-formats)

def test_metric_change_last_seen_uses_the_short_date_formatter():
    assert 'last_seen: {displayText: fmtDateShort($datapoint["last_seen"])' in dash.metric_change_expression()


# ------------------------------------------------------------------ item 4: Task Health note fallback

def test_task_health_drops_the_setting_notes_column_and_shows_a_note_count_in_why(build):
    widget = _by_name(build())["Task Health"]
    titles = {c["fieldName"]: c["title"] for c in widget["columnSettings"]}
    assert "config_warnings" not in titles
    expression = widget["expression"]
    assert 'if (whyText == "" && noteCount > 0) {' in expression
    assert f'whyText = {dash._s(dash.sym_prefix("ConfigWarning"))} + plural(noteCount, "setting note")' in expression


# ------------------------------------------------------------------ item 9: fmtMetric duration units

def test_fmt_metric_drops_the_seconds_suffix():
    assert 'return Text(Round(v), "#,##0"); }' in dash.FMT_HELPERS
    assert 'return Text(Round(v), "#,##0") + " s"' not in dash.FMT_HELPERS


# ------------------------------------------------------------------ item 10: own-row lastText names the writer

def test_own_task_running_names_the_run_that_wrote_the_page():
    expression = dash.detail_list_expression()
    assert '" | this page was written by the run at " + fmtDate($datapoint["collected_at_utc"])' in expression

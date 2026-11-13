"""Dashboard polish (audit step s08): hidden working folder, metric wording, alert help, axes."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import taskdash_dashboard as dash  # noqa: E402


def test_hidden_working_folder_reads_dashes_not_not_set():
    hidden = dash.detail_list_expression(hidden=["command_target_working_directory"])
    assert 'var workDir = "--";' in hidden
    shown = dash.detail_list_expression()
    assert 'var workDir = esc(DefaultValue($datapoint["command_target_working_directory"], ""));' in shown
    assert 'wdText: IIf(workDir == "", "not set", workDir)' in shown


def test_metric_rows_sent_help_says_changed_log_files_only():
    text = dash.HELP["Collector Runs"][0]
    assert "Task rows sent counts every row sent" in text
    assert "Metric rows sent counts the rows from log files that changed since the last run." in text


def _templates():
    import json
    template = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in template["desktopView"]["widgets"]}
    return {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
            "serial_config": by_name["Schedule Mix"]["chartConfig"]}


def _widgets():
    return {w["name"]: w for w in dash.all_items(dash.build("TESTITEM", _templates(), config={}))}


def test_same_named_tasks_are_told_apart_by_folder_and_host():
    widgets = _widgets()
    detail = widgets["Task Detail"]
    assert "<strong>Folder:</strong> {expression/f_task_path}" in detail["text"]
    assert 'f_task_path: Replace(' in detail["expression"]
    change = widgets["Metric Change"]
    assert [c["title"] for c in change["columnSettings"]][:3] == ["Task", "Host", "Metric"]
    assert 'host_name: {displayText: DefaultValue($datapoint["host_name"], "")' in change["expression"]
    # The Task cell shows the folder path, so two tasks of one name in two folders read apart.
    assert 'DefaultValue($datapoint["task_full_name"]' in change["expression"]
    assert "task_full_name: h[\"task_full_name\"]" in dash.metric_change_source_script("https://x", "ITEM", 1)
    summary = widgets["Metric Summary"]
    assert [c["fieldName"] for c in summary["columnSettings"]][:2] == ["task_full_name", "host_name"]
    assert summary["datasets"][0]["groupByFields"] == ["task_full_name", "host_name", "display_name"]


def test_metric_range_shows_two_decimals():
    columns = {c["fieldName"]: c for c in _widgets()["Metric Summary"]["columnSettings"]}
    for field in ("MIN_metric_value", "AVG_metric_value", "MAX_metric_value"):
        assert columns[field][dash.COLUMN_FORMAT_KEY]["maximumFractionDigits"] == 2


def _email_help(alerting):
    terms = dash.help_terms({"alerting": alerting})
    return dash.localize_help(dash.HELP["Collector Runs"][0], terms)


def test_email_sent_help_says_alerting_is_off_unless_enabled_with_an_address():
    for alerting in ({}, {"enabled": True}, {"enabled": False, "to": ["a@example.com"]}, {"enabled": True, "to": [" "]}):
        text = _email_help(alerting)
        assert dash.NO_ALERTING_TEXT in text and "after 24 hours" not in text, alerting


def test_email_sent_help_follows_realert_hours():
    on = {"enabled": True, "to": ["a@example.com"]}
    assert "still open after 24 hours" in _email_help(on)
    assert "still open after 6 hours" in _email_help({**on, "realert_hours": 6})
    assert "still open after 1 hour." in _email_help({**on, "realert_hours": 1})
    assert "still open" not in _email_help({**on, "realert_hours": None})


def test_config_notes_help_tells_a_viewer_to_ask_the_owner_and_names_the_file():
    todo = dash.HELP["Setting Notes"][2]
    assert "ask the dashboard owner to accept it" in todo
    assert "acknowledge_config_warnings" in todo and todo.endswith("in the task descriptions file.")


def test_trend_axes_use_12_hour_times_like_the_rest_of_the_dashboard():
    per_unit = _widgets()["Duration Trend"]["chartConfig"]["axes"][0]["valueFormat"]["formatPerDateTimeUnit"]
    for unit in ("hour", "minute", "second"):
        assert per_unit[unit]["hour12"] is True


def test_use_guide_states_the_refresh_intervals_the_dashboard_sets():
    use = " ".join((Path(dash.__file__).parent / "docs" / "USE.md").read_text(encoding="utf-8").split())
    assert dash.HEARTBEAT_REFRESH_MINUTES == 5 and dash.METRIC_CHANGE_REFRESH_MINUTES == 60
    assert "the heartbeat tiles update every 5 minutes and **Metric change** updates every hour" in use


def test_use_guide_explains_the_mute_notes_in_the_why_column():
    use = " ".join((Path(dash.__file__).parent / "docs" / "USE.md").read_text(encoding="utf-8").split())
    assert "`Muted (alerts off)` or `In maintenance until <end>`" in use

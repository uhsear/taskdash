"""Round-4 audit fixes, dashboard items. Every definition is built in the test; Arcade cannot
run here, so Arcade checks are on the generated source (see test_round1_dashboard.py for the
same pattern).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402

SHOWN = {"privacy": {"hide_command_lines": False, "hide_sample_lines": False,
                     "hide_accounts": False, "hide_paths": False}}


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
        # Every privacy switch off: these tests read the fields a missing switch now hides.
        return dash.build("TESTITEM", _templates(), config=SHOWN)
    return make


def _by_name(definition):
    return {w["name"]: w for w in dash.all_items(definition)}


# ------------------------------------------------------------------ health_table-r3regress-next-run-past-not-fixed

def test_next_run_falls_back_to_schedule_when_it_is_already_past(build):
    expression = _by_name(build())["Task Health"]["expression"]
    assert 'var nm = minsFromNow($datapoint["next_run_time"]);' in expression
    assert ('IIf(kind == "" && !IsEmpty(nm) && nm < 0,'
           ' DefaultValue($datapoint["schedule_bucket"], "--"), fmtDateShort($datapoint["next_run_time"]))'
           in expression)


# ------------------------------------------------------------------ health_table-r3regress-null-log-count-sorts-last / -disabled-not-sorted-first

def test_task_health_order_drops_the_null_sorting_key_and_sorts_disabled_with_problems(build):
    order = _by_name(build())["Task Health"]["datasets"][0]["orderByFields"]
    assert order == ["last_run_failed DESC", "is_overdue DESC", "needs_attention DESC",
                     "enabled ASC", "latest_log_error_lines DESC", "failures_last_7_days DESC",
                     "task_name ASC"]


# ------------------------------------------------------------------ task_detail-r3regress-runs-as-interactive-wording / -service-account-grammar

def test_logon_type_words_are_plain_and_do_not_repeat_the_raw_token():
    assert dash.LOGON_TYPE_WORDS["Interactive"] == "only while this account is signed in"
    assert "user is logged on" not in dash.LOGON_TYPE_WORDS["Interactive"]
    assert dash.LOGON_TYPE_WORDS["ServiceAccount"] == "whether or not a user is logged on (built-in service account)"


# ------------------------------------------------------------------ task_detail-r3regress-own-task-running-previous-duration

def test_own_task_running_shows_the_previous_run_separately(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert 'Find("Running now.", DefaultValue($datapoint["attention_reason"], "")) == 0' in expression
    # The "a new run is in progress" suffix never went away once that new run finished, since
    # Task Dash's own row is always written by a run still in progress at write time
    # (task_detail-taskdash-always-in-progress round-6 fix): dropped, not replaced.
    assert "a new run is in progress" not in expression
    assert '" | took " + fmtDur($datapoint["last_duration_seconds"])' in expression


# ------------------------------------------------------------------ task_detail-taskdash-no-log-found-false / -log-name-shown-twice / -csc-debug-log-wording

def test_task_dash_own_log_is_named_and_the_log_name_is_not_repeated(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert 'Right(Lower(cmdPath), 16) == "run_taskdash.cmd"' in expression
    assert "No taskdash_*.log found in its log folder" in expression
    # The counts/unreadable lines no longer prefix logName: logPathText (the next line) names
    # the file already. Round-6: the debug branch dropped logName too (repeat-task_detail-log-
    # name-shown-twice), now that metrics.py can count the last 7 days of error lines in a
    # cumulative debug file instead of always nulling the count.
    assert 'logName + logAge' not in expression
    assert 'logName + " | cumulative error file"' not in expression
    assert '"written " + fmtSpan(logMins) + " ago | ")' in expression
    assert "error-only file kept across runs; " in expression


# ------------------------------------------------------------------ collector_tab-recovered-row-shows-old-failure

def test_a_recovered_change_row_marks_the_old_reason_as_past():
    expression = dash.changes_list_expression()
    assert '"Was: " + oldS' in expression


# ------------------------------------------------------------------ wording-metric-change-dash-for-zero

def test_metric_change_shows_zero_percent_when_latest_and_usual_are_both_zero():
    script = dash.metric_change_source_script("https://example.maps.arcgis.com", "TESTITEM", 2)
    assert "usual == 0 && latest == 0) { change = 0; }" in script


# ------------------------------------------------------------------ metrics_tab-r3regress-06-caption

def test_metric_change_caption_says_where_the_no_usual_rows_sort(build):
    caption = _by_name(build())["Metric Change"]["topCaption"]
    assert "rows with no usual value (--) are at the bottom" in caption
    assert "Select a row to see its trend" in caption


# ------------------------------------------------------------------ metrics_tab-04

def test_metric_change_help_names_the_usually_logged_share():
    todo = dash.HELP["Metric Change"][2]
    assert f"{int(dash.METRIC_CHANGE_USUALLY_LOGGED * 100)}% of its runs" in todo
    assert "logs in most runs" not in todo


# ------------------------------------------------------------------ metrics_tab-05

def test_metric_observations_log_line_is_cut_at_300(build):
    expression = _by_name(build())["Metric Observations"]["expression"]
    assert 'shortText(DefaultValue($datapoint["sample_line"], ""), 300)' in expression


# ------------------------------------------------------------------ failures_trend-r3regress-units-differ-from-detail / wording-r3regress-duration-trend-units

def test_duration_trend_is_plotted_in_seconds(build):
    trend = _by_name(build())["Duration Trend"]
    config = json.dumps(trend["chartConfig"])
    assert "duration_seconds" in config and '"Seconds (60 = 1 min)"' in config
    assert "in seconds" in dash.HELP["Duration Trend"][0]
    assert "in minutes" not in dash.HELP["Duration Trend"][0]


# ------------------------------------------------------------------ indicators-lowest-rate-flags-single-failure / -not-traceable / wording-success-help-fails-often

def test_success_rate_help_explains_the_two_failure_floor():
    shows, why, todo = dash.HELP["7-Day Success Rate"]
    assert "2 or more failures" in shows
    assert "Most failures in 7 days" in why and "100%" in why
    assert "Most failures in 7 days" in todo and "Failures (7 days)" in todo


# ------------------------------------------------------------------ indicators-overdue-help-describes-missed-run / indicators-other-warnings-jargon

def test_overdue_and_other_warnings_help_are_distinct_and_plain():
    overdue = dash.HELP["Overdue"][0]
    assert "15 min" in overdue and "grace time" in overdue
    other = dash.HELP["Other Warnings"][0]
    assert "missed runs" not in other and "a warning result" not in other
    assert "Task Scheduler reports as missed" in other


# ------------------------------------------------------------------ indicators-heartbeat-only-tile-claim-false / collector_tab-heartbeat-help-drops-reload

def test_both_heartbeat_tiles_name_the_two_live_widgets_and_keep_the_reload_sentence():
    for name in ("Collector Heartbeat", "Heartbeat"):
        why = dash.HELP[name][1]
        assert "Only this tile updates" not in why
        assert "Metric change" in why and "Reload the page" in why


# ------------------------------------------------------------------ indicators-health-help-stuck-under-running / interactions-r3regress-stuck-under-running

def test_status_help_files_stuck_and_blocked_under_warning():
    shows = dash.HELP["Health"][0]
    assert "Stuck task is under Running" not in shows
    assert "Overdue, Stuck or Blocked task is under Warning" in shows


# ------------------------------------------------------------------ collector_tab-ack-setting-name-hides-other-notes

def test_config_notes_help_says_to_use_a_word_unique_to_one_note():
    todo = dash.HELP["Config Notes"][2]
    assert "add the setting name" not in todo
    assert "appears only in that note" in todo


# ------------------------------------------------------------------ collector_tab-columns-not-defined / wording-collector-help-log-name-eaten

def test_collector_runs_help_defines_events_and_runs_and_names_no_angle_brackets():
    shows, why, todo = dash.HELP["Collector Runs"]
    assert "Events read counts" in shows and "Runs saved counts" in shows
    assert "<" not in todo
    assert "Task Dash log" in todo


def test_help_html_escapes_html_special_characters(monkeypatch):
    monkeypatch.setitem(dash.HELP, "Host", ("A <script> tag & an ampersand.", "why", "todo"))
    rendered = dash.help_html("Host")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered and "&amp;" in rendered


def test_no_help_string_holds_a_literal_angle_bracket():
    for shows, why, todo in dash.HELP.values():
        for text in (shows, why, todo):
            if text:
                assert "<" not in text and ">" not in text


# ------------------------------------------------------------------ wording-r3regress-taskdash-two-spellings

def test_tasks_tracked_and_collector_runs_help_name_the_task_dash_task():
    assert "the collector (the Task Dash task) monitors" in dash.HELP["Tasks Tracked"][0]
    assert "TaskDash monitors" not in dash.HELP["Tasks Tracked"][0]


# ------------------------------------------------------------------ wording-metric-selector-help-stale-trend

def test_metric_selector_help_no_longer_claims_a_pick_alone_draws_the_trend():
    why = dash.HELP["Metric"][1]
    assert "needs one metric to draw one line" not in why
    assert "shows one metric across all tasks" in why


# ------------------------------------------------------------------ wording-script-time-vs-took

def test_metric_trend_help_explains_script_time_is_shorter_than_the_run_time():
    shows = dash.HELP["Metric Trend"][0]
    # round7 metrics_tab-script-time-help-reason-still-wrong: "took" is Task detail's run time.
    assert 'shorter than "took" in Task detail' in shows


# ------------------------------------------------------------------ failures_trend-r2regress-monthly-empty-state / wording-duration-trend-empty-timed

def test_duration_trend_empty_state_matches_task_detail_wording():
    assert dash.EMPTY_STATES["Duration Trend"] == (
        "No successful runs with a recorded duration in the saved history (up to 90 days).")


# ------------------------------------------------------------------ interactions-schedule-status-skip-failed-runs

def test_recent_failures_empty_state_names_host_and_task_not_filters():
    assert dash.EMPTY_STATES["Recent Failures"] == (
        "No failed runs for the selected host or task in the saved history (up to 90 days). "
        "Schedule and Status do not filter this list.")


# ------------------------------------------------------------------ interactions-r3regress-status-schedule-no-task-cascade

def test_health_and_schedule_selectors_narrow_the_task_selector(build):
    definition = build()
    selectors = {s["name"]: s for s in definition["desktopView"]["sidebar"]["selectors"]}
    task_id = selectors["Task"]["id"]
    for name, field in (("Health", "health_status"), ("Schedule", "schedule_bucket")):
        targets = [t for e in selectors[name]["events"] for a in e["actions"] for t in a["targets"]]
        task_targets = [t for t in targets if t["targetId"].split("#")[0] == task_id]
        assert task_targets, name
        assert task_targets[0]["fieldMap"] == [{"sourceName": field, "targetName": field}]


def test_task_help_names_the_one_exception(build):
    todo = dash.HELP["Task"][2]
    assert "except Collector runs" in todo


# ------------------------------------------------------------------ interactions-mobile-attention-title-vs-disabled / wording-mobile-attention-list-scope

def test_mobile_attention_list_title_and_empty_state_name_disabled_tasks(build):
    attention = _by_name(build())["Attention Tasks" + dash.MOBILE_SUFFIX]
    assert attention["title"] == "Tasks that need attention or are disabled"
    assert dash.EMPTY_STATES["Attention Tasks"] == "No task needs attention or is disabled."


# ------------------------------------------------------------------ critic-05

def test_mobile_tile_pairs_group_needs_attention_with_its_three_parts():
    pairs = dict(dash.MOBILE_TILE_PAIRS)
    assert pairs["Needs Attention"] == "Failed (last run)"
    assert pairs["Overdue"] == "Other Warnings"
    assert pairs["Disabled"] == "Tasks Tracked"


# ------------------------------------------------------------------ critic-02

def test_task_detail_shows_the_settings_detail_field(build):
    task_detail = _by_name(build())["Task Detail"]
    assert 'settingsText: IIf(settingsDetail == "", "--", settingsDetail)' in task_detail["expression"]
    assert "{expression/settingsText}" in task_detail["text"]
    dash.validate_definition(build())


# ------------------------------------------------------------------ critic-03 (refuted: the constant is live)

def test_metric_trend_picker_prompt_constant_is_still_used_by_the_no_cross_filter_fallback(build):
    # The round-4 finding said this constant was dead; it is the noFilterState fallback text for
    # Metric Trend when cross_table_row_filter is off, so it was kept rather than deleted.
    fallback = _by_name(build(cross_table_row_filter=False))["Metric Trend"]
    assert fallback["noFilterState"]["text"] == f'<p style="text-align:center">{dash.METRIC_TREND_PICKER_PROMPT}</p>'



def test_round5_stuck_and_disabled_and_phone_lists(build):
    definition = build()
    widgets = _by_name(definition)
    detail = widgets["Task Detail"]["expression"]
    # A STUCK task is Running in Windows but health Warning: its card must say in progress.
    assert '|| DefaultValue($datapoint["state"], "") == "Running", "in progress",' in detail
    assert 'IIf(health == "Disabled", "Off (task is disabled)",' in detail
    assert 'IIf(health == "Disabled", "Off",' in widgets["Task Health"]["expression"]
    phone = {w["name"]: w for w in definition["mobileView"]["widgets"]}
    assert phone["Attention Tasks" + dash.MOBILE_SUFFIX]["showFilter"] is False
    assert phone["Recent Failures" + dash.MOBILE_SUFFIX]["showFilter"] is False

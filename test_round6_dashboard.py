"""Round-6 (final prod audit) fixes, dashboard items. One test per non-trivial change.

Follows the pattern of test_round4_dashboard.py: build the definition in the test, since Arcade
cannot run here, and check the generated Arcade source for the logic changes.
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


# ------------------------------------------------------------------ metrics_tab-far-below-flags-an-improvement / wording-far-below-flags-good-news

def test_metric_change_never_flags_far_below_for_a_problem_count_key():
    script = dash.metric_change_source_script("https://example.maps.arcgis.com", "TESTITEM", 2)
    assert "var lowerIsBetter" in script
    assert "&& !lowerIsBetter" in script
    for substring in dash.METRIC_CHANGE_LOWER_IS_BETTER:
        assert f'Find("{substring}", mk)' in script


# ------------------------------------------------------------------ wording-metric-change-help-approximate

def test_metric_change_help_flag_rule_matches_the_arcade_history_cap():
    todo = dash.HELP["Metric Change"][2]
    assert f"lower than each of the last {dash.METRIC_CHANGE_HISTORY} earlier values" in todo
    assert "lower than every earlier value" not in todo
    assert "never flagged for a drop" in todo


# ------------------------------------------------------------------ task_detail-next-run-shown-in-past

def test_task_detail_next_run_in_the_past_shows_schedule_not_a_negative_ago(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert ('IIf(kind == "" && !IsEmpty(nextMins) && nextMins < 0,'
            ' DefaultValue($datapoint["schedule_bucket"], "--") + " (next run time not refreshed yet)"') in expression


# ------------------------------------------------------------------ task_detail-taskdash-always-in-progress

def test_taskdash_own_run_last_text_drops_the_in_progress_suffix(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert "a new run is in progress" not in expression
    assert '" | took " + fmtDur($datapoint["last_duration_seconds"])' in expression
    assert ', took "' not in expression


# ------------------------------------------------------------------ repeat-task_detail-log-name-shown-twice

def test_debug_log_branch_shows_seven_day_error_count_without_repeating_the_name(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert "error-only file kept across runs; " in expression
    assert '+ plural($datapoint["latest_log_error_lines"], "error line") + " in the last 7 days"' in expression
    assert 'logName + " | cumulative error file"' not in expression


# ------------------------------------------------------------------ collector_tab-removed-note-uses-warning-badge

def test_config_warning_cleared_is_neutral_not_a_warning_badge():
    assert dash.CHANGE_KINDS["config_warning_cleared"] == ("Setting note removed", "")


# ------------------------------------------------------------------ operations-change-time-is-detection-time / operations-acknowledged-note-reads-as-fixed

def test_recent_changes_help_explains_detection_time_and_acknowledged_notes():
    shows = dash.HELP["Recent Changes"][0]
    assert "up to 1 hour after it happened" in shows
    assert "was acknowledged" in shows


# ------------------------------------------------------------------ collector_tab-alert-sent-undefined / wording-collector-caption-two-days

def test_collector_runs_column_is_named_email_sent_with_the_newest_48_caption(build):
    widget = _by_name(build())["Collector Runs"]
    labels = {c["fieldName"]: c["title"] for c in widget["columnSettings"]}
    assert labels["alert_sent"] == "E-mail sent"
    assert "Newest 48. Read Error" in widget["topCaption"]
    assert "Two days of hourly runs" not in widget["topCaption"]
    shows = dash.HELP["Collector Runs"][0]
    # round7 wording-email-sent-help / collector_interactions-email-sent-help-incomplete: the
    # daily send is not conditional on health, and realert/changed also send.
    assert "E-mail sent is Yes" in shows and "7 AM daily check" in shows


# ------------------------------------------------------------------ indicators-other-warnings-help-says-runs

def test_other_warnings_help_counts_tasks_not_runs():
    shows = dash.HELP["Other Warnings"][0]
    assert "counts runs" not in shows
    assert "It also counts tasks that Task Scheduler reports as missed" in shows
    # Every sentence obeys the 20-word cap that
    # test_help_is_short_plain_and_points_at_real_controls enforces across all of HELP.
    for sentence in shows.split(". "):
        assert len(sentence.split()) <= 20, sentence


# ------------------------------------------------------------------ indicators-mobile-why-names-desktop-widgets

def test_mobile_tile_help_does_not_name_desktop_only_widgets(build):
    mobile_widgets = build()["mobileView"]["widgets"]
    checked = 0
    for widget in mobile_widgets:
        if widget.get("type") == "indicatorWidget":
            desc = widget.get("description", "")
            assert "Most failures" not in desc
            assert "Metric change" not in desc
            checked += 1
    assert checked > 0  # the loop actually examined tiles


# ------------------------------------------------------------------ metrics_tab-script-time-two-names / repeat-wording-script-time-vs-took

def test_metric_trend_help_uses_the_shown_metric_name_and_the_right_reason():
    shows = dash.HELP["Metric Trend"][0]
    assert "Script-reported time (seconds)" in shows
    assert "Script time is" not in shows
    # round7 metrics_tab-script-time-help-reason-still-wrong: start-up is not the only reason;
    # a script often times only one step of its own work.
    assert "Python start-up" in shows and "one step of their work" in shows


# ------------------------------------------------------------------ critic-all-values-title-ignores-metric-filter

def test_metric_observations_help_notes_the_metric_filter_narrows_it_too():
    shows = dash.HELP["Metric Observations"][0]
    assert "Metric filter narrows it further" in shows


# ------------------------------------------------------------------ failures_trend-estimated-start-is-end-time

def test_failed_runs_estimated_start_says_ended_about(build):
    expression = _by_name(build())["Recent Failures"]["expression"]
    assert ('IIf(estStart, "ended about " + fmtDate($datapoint["end_time_utc"]),'
            ' fmtDate($datapoint["start_time_utc"]))') in expression
    assert 'IIf(estStart, "about ", "")' not in expression


# ------------------------------------------------------------------ interactions-mobile-failed-runs-one-card

def test_mobile_failed_runs_and_attention_lists_are_balanced():
    assert dash.MOBILE_ATTENTION_HEIGHT == 0.24
    assert dash.MOBILE_FAILURES_HEIGHT == 0.24
    tiles_total = len(dash.MOBILE_TILE_PAIRS) * dash.MOBILE_TILE_HEIGHT
    assert abs(tiles_total + dash.MOBILE_ATTENTION_HEIGHT + dash.MOBILE_FAILURES_HEIGHT - 1.0) < 1e-9

"""Round-3 audit fixes, dashboard items. Every definition is built in the test; Arcade cannot
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


# ------------------------------------------------------------------ decision 1: lowest task success

def test_success_rate_tile_reads_the_lowest_per_task_rate_truncated_not_rounded(build):
    widgets = _by_name(build())
    tile = widgets["7-Day Success Rate"]
    assert tile["title"] == "Lowest 7-day success"  # owner decision, phase 0: the rule is under the value
    assert tile["datasets"][0]["dataSource"]["type"] == "arcadeDataSource"  # silent computers are left out
    fallback = _by_name(build(heartbeat_arcade_refresh=False))["7-Day Success Rate"]
    dataset = fallback["datasets"][0]
    assert dataset["statisticDefinitions"][0]["onStatisticField"] == "lowest_success_rate_7d"
    assert "MIN_lowest_success_rate_7d" in fallback["expression"]  # MIN: the tile errs toward the alarm
    # Floor to 1 decimal, not Round: a rounded 98.0% used to read "OK" while the unrounded 97.97
    # was "Low" (indicators-success-rate-rounding-contradiction).
    assert "pct = Floor(pct * 10) / 10;" in tile["expression"]
    assert dash.SUCCESS_OK_PCT == 90 and dash.SUCCESS_LOW_PCT == 70


# ------------------------------------------------------------------ decision 2: consistent names

def test_no_help_text_points_at_the_old_widget_names():
    blob = " ".join(x for v in dash.HELP.values() for x in v if x)
    assert "Recent failures" not in blob
    assert "Metric observations" not in blob


# ------------------------------------------------------------------ decision 3: duration trend

def test_duration_trend_prompt_title_and_axis_are_render_accurate(build):
    trend = _by_name(build())["Duration Trend"]
    assert dash.PROMPTS["Duration Trend"] == "Select a row in Task health to see its run durations."
    assert trend["title"] == f"Duration trend - newest {dash.DURATION_TREND_CAP} runs"
    assert trend["minPeriod"] == "mm"
    axis = trend["chartConfig"]["axes"][0]["valueFormat"]
    assert axis["intlOptions"]["hour"] == "numeric" and axis["intlOptions"]["minute"] == "2-digit"


# ------------------------------------------------------------------ decision 4: task health why/order

def test_task_health_why_note_is_short_and_sorted_first(build):
    widgets = _by_name(build())
    expression = widgets["Task Health"]["expression"]
    assert 'whyText = plural(logErrLines, "log error")' in expression
    order = widgets["Task Health"]["datasets"][0]["orderByFields"]
    # enabled ASC sorts a disabled task with the problems; log-error rows follow them.
    assert order == ["last_run_failed DESC", "is_overdue DESC", "needs_attention DESC",
                     "enabled ASC", "latest_log_error_lines DESC", "failures_last_7_days DESC",
                     "task_name ASC"]
    titles = {c["fieldName"]: c["title"] for c in widgets["Task Health"]["columnSettings"]}
    assert "host_name" in titles  # kept: multi-host installs (skip host-column removal)
    assert "log error" in dash.HELP["Task Health"][0]


# ------------------------------------------------------------------ decision 5: task detail

def test_task_detail_working_folder_running_state_logon_and_arguments(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert 'wdText: IIf(workDir == "", "not set", workDir)' in expression
    assert 'IIf(health == "Running"' in expression and '"in progress"' in expression
    assert 'userText: IIf(runAs == "", "--", runAs + ", runs " + IIf(ltWord == "", lt, ltWord + " [" + lt + "]"))' in expression
    assert 'pathText: IIf(cmdPath == "", "--", cmdPath + IIf(args == "", "", " " + args))' in expression
    assert 'var args = esc(DefaultValue($datapoint["command_target_arguments"], ""));' in expression


# ------------------------------------------------------------------ decision 6: failed runs / mobile

def test_failed_run_null_duration_and_mobile_empty_state(build):
    definition = build()
    widgets = _by_name(definition)
    assert ('durationText: IIf(IsEmpty($datapoint["duration_seconds"]), "duration not recorded",'
           in widgets["Recent Failures"]["expression"])
    mobile_failures = widgets["Recent Failures" + dash.MOBILE_SUFFIX]
    assert mobile_failures["noDataState"]["text"] == (
        '<p style="text-align:center">No failed runs in the saved history (up to 90 days).</p>')
    assert "filter" not in mobile_failures["noDataState"]["text"].lower()
    assert "task health" not in mobile_failures["noDataState"]["text"].lower()
    assert "for the selected task or filters" in dash.EMPTY_STATES["Top Failing Tasks"]


# ------------------------------------------------------------------ decision 7: metrics tab

def test_metric_change_three_tier_rank_and_observations_sorted_by_display_name(build):
    definition = build()
    widgets = _by_name(definition)
    source = next(a["script"] for a in definition["arcadeDataSourceItems"]
                 if a["name"].startswith("TaskDash metric change"))
    assert 'flag_rank: IIf(flag != "", 0, IIf(IsEmpty(usual), 2, 1))' in source
    order = widgets["Metric Observations"]["datasets"][0]["orderByFields"]
    assert order == ["task_name ASC", "display_name ASC", "observed_at_utc DESC"]
    columns = {c["fieldName"] for c in widgets["Metric Observations"]["columnSettings"]}
    assert "sample_line" in columns
    assert '"sample_line"' in widgets["Metric Observations"]["expression"]


def test_metrics_tab_heights_favour_the_always_full_tables():
    layout = dash.desktop_layout(_by_name(dash.build("TESTITEM", _templates())))
    metrics_tab = next(t for t in layout["rootElement"]["elements"] if t["tabName"] == "Workflow Metrics")
    rows = metrics_tab["elements"][0]["elements"]
    heights = [round(r["height"], 2) for r in rows]
    # Metric change (row 0) and Metric range (row 1) now outweigh the always-idle-until-a-task-pick
    # bottom panel (row 3) (metrics_tab-02).
    assert heights[0] > 0.20 and heights[1] > 0.15
    assert heights[3] < heights[0]


def test_fmt_metric_shows_duration_in_seconds_only():
    # No " s" suffix: the display name already says "(seconds)" (round7
    # wording-script-time-units-differ-on-one-tab).
    assert 'return Text(Round(v), "#,##0"); }' in dash.FMT_HELPERS
    assert "Duration is in seconds" not in dash.HELP["Metric Trend"][0]


# ------------------------------------------------------------------ decision 8: collector & changes

def test_collector_runs_error_length_and_row_labels(build):
    widgets = _by_name(build())
    assert 'shortText(DefaultValue($datapoint["error_text"], ""), 300)' in widgets["Collector Runs"]["expression"]
    titles = {c["fieldName"]: c["title"] for c in widgets["Collector Runs"]["columnSettings"]}
    assert titles["inventory_written"] == "Task rows sent"
    assert titles["metrics_written"] == "Metric rows sent"


# ------------------------------------------------------------------ decision 9: filter coverage

def test_status_and_schedule_selectors_also_target_config_notes(build):
    definition = build()
    widgets = _by_name(definition)
    selectors = {s["name"]: s for s in definition["desktopView"]["sidebar"]["selectors"]}
    for name in ("Health", "Schedule"):
        targeted = {t["targetId"].split("#")[0] for e in selectors[name]["events"]
                   for a in e["actions"] for t in a["targets"]}
        assert widgets["Setting Notes"]["id"] in targeted, name


# ------------------------------------------------------------------ decision 10: heartbeat / tiles

def test_heartbeat_tiles_share_one_todo_and_tasks_tracked_points_at_recent_changes():
    assert dash.HELP["Collector Heartbeat"][2] == dash.HELP["Heartbeat"][2]
    assert "Recent changes" in dash.HELP["Tasks Tracked"][2]
    assert "Collector runs" not in dash.HELP["Tasks Tracked"][2]
    # The partition sentence survives on a phone: "What to do" is stripped there, "Shows" is not.
    assert "add up to this number" in dash.HELP["Needs Attention"][0]

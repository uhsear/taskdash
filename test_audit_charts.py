"""Final-audit chart fixes: Duration trend axes, empty-state texts, Metric Observations prompt."""

from __future__ import annotations

import json
from pathlib import Path

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402


def _widgets():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return {w["name"]: w for w in dash.all_items(dash.build("TESTITEM", templates))}


def test_duration_axis_matches_the_harvested_minimum_shape():
    # Every harvested axis with "minimum" has buffer False; the live render ignored it beside buffer True.
    axis = _widgets()["Duration Trend"]["chartConfig"]["axes"][1]
    assert axis["minimum"] == 0 and axis["buffer"] is False
    assert axis["integerOnlyValues"] is False and axis["isLogarithmic"] is False


def test_metric_trend_axis_keeps_its_buffer_and_no_minimum():
    axis = _widgets()["Metric Trend"]["chartConfig"]["axes"][1]
    assert axis["buffer"] is True and "minimum" not in axis


def test_date_axis_names_the_day_for_a_month_step():
    per_unit = _widgets()["Duration Trend"]["chartConfig"]["axes"][0]["valueFormat"]["formatPerDateTimeUnit"]
    assert per_unit["month"] == {"month": "short", "day": "numeric"}
    assert "hour12" in per_unit["minute"]


def test_duration_axis_title_gives_the_minute_equivalent():
    title = _widgets()["Duration Trend"]["chartConfig"]["axes"][1]["title"]["content"]["text"]
    assert title == "Seconds (60 = 1 min)"


def test_task_detail_and_failed_runs_empty_states():
    widgets = _widgets()
    assert "filters may hide" in widgets["Task Detail"]["noDataState"]["text"]
    assert "Schedule and Status do not filter" in widgets["Recent Failures"]["noDataState"]["text"]


def test_metric_observations_prompt_says_a_metric_change_click_selects_no_task():
    prompt = _widgets()["Metric Observations"]["noFilterState"]["text"]
    assert "A click in Metric change does not select a task." in prompt

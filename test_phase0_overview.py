"""Phase 0 Overview polish: Failed runs scope and tint, Most failures weight, Task health order,
Duration trend axis, and the collapsible Filters sidebar. Arcade cannot run here, so the tint
check reads the generated source (same pattern as test_phase0_dashboard.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402


def _build():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return dash.build("TESTITEM", templates)


def _widget(definition, name):
    return {w["name"]: w for w in dash.all_items(definition)}[name]


def test_failed_runs_title_says_schedule_and_status_are_ignored():
    definition = _build()
    assert "Schedule and Status do not filter it" in _widget(definition, "Recent Failures")["topCaption"]
    # The phone has no selectors, so its list carries no such note.
    phone = {w["name"]: w for w in definition["mobileView"]["widgets"]}
    assert "do not filter" not in phone["Recent Failures" + dash.MOBILE_SUFFIX].get("topCaption", "")


def test_failed_runs_are_not_wired_to_schedule_or_status():
    definition = _build()
    failures_id = _widget(definition, "Recent Failures")["id"] + "#main"
    for name in ("Health", "Schedule"):
        targets = _widget(definition, name)["events"][0]["actions"][0]["targets"]
        assert failures_id not in {t["targetId"] for t in targets}


def test_only_failures_from_the_last_7_days_are_tinted():
    expression = _widget(_build(), "Recent Failures")["expression"]
    assert "var recent = IsEmpty(ageMins) || Abs(ageMins) <= 10080;" in expression
    assert "backgroundColor: IIf(recent, bg, \"\")" in expression
    # The word and glyph stay on every card, tinted or not.
    assert "statusText: statusText" in expression


def test_most_failures_bars_are_lighter_than_their_outline():
    series = _widget(_build(), "Top Failing Tasks")["chartConfig"]["series"][0]["fillSymbol"]
    assert series["color"][3] < 255 and series["outline"]["color"][3] == 255


def test_task_health_orders_recent_failures_after_the_problem_keys():
    order = _widget(_build(), "Task Health")["datasets"][0]["orderByFields"]
    assert order[-2:] == ["failures_last_7_days DESC", "task_name ASC"]
    assert order.index("latest_log_error_lines DESC") < order.index("failures_last_7_days DESC")


def test_duration_trend_value_axis_starts_at_zero():
    axes = _widget(_build(), "Duration Trend")["chartConfig"]["axes"]
    assert axes[1]["minimum"] == 0


def test_filters_sidebar_can_slide_and_dashboard_validates():
    definition = _build()
    assert definition["desktopView"]["sidebar"]["allowSliding"] is True
    assert definition["desktopView"]["settings"]["allowReset"] is True
    dash.validate_definition(definition)  # raises SystemExit on a bad shape


def test_metric_trend_axis_is_not_forced_to_start_at_zero():
    # A metric can be negative (taskdash_metrics accepts -?\d), so only Duration trend pins 0.
    axes = _widget(_build(), "Metric Trend")["chartConfig"]["axes"]
    assert all("minimum" not in axis for axis in axes)

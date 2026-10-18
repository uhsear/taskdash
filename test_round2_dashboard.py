"""Round-2 audit fixes, dashboard items. Every definition is built in the test; Arcade cannot
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


# ------------------------------------------------------------------ health_table-column-widths-ignored / wording-02

def test_task_table_dates_use_the_short_table_formatter_not_the_full_one(build):
    expression = _by_name(build())["Task Health"]["expression"]
    assert 'last_run_time: {displayText: fmtDateShort($datapoint["last_run_time"])' in expression
    # A healthy task's next_run_time cell falls back to the schedule bucket once that time has
    # passed (health_table-r3regress-next-run-past-not-fixed), so fmtDateShort is now its second
    # branch, not the whole cell.
    assert 'fmtDateShort($datapoint["next_run_time"]))), textAlign: "left"},\n' in expression
    assert 'function fmtDateShort(v)' in dash.FMT_HELPERS
    # The detail card keeps the year: only the equal-width table cells lose it.
    detail = _by_name(build())["Task Detail"]["expression"]
    assert 'fmtDate($datapoint["last_run_time"])' in detail


def test_column_no_longer_declares_a_width_that_is_never_applied(build):
    import inspect
    assert "width" not in inspect.signature(dash.column).parameters
    definition = build()
    for widget in dash.all_items(definition):
        for c in widget.get("columnSettings", []):
            assert "width" not in c, (widget["name"], c["fieldName"])


def test_task_health_header_is_short_and_keeps_the_host_column(build):
    titles = {c["fieldName"]: c["title"] for c in _by_name(build())["Task Health"]["columnSettings"]}
    assert titles["failures_last_7_days"] == "Failures (7 days)"
    assert "host_name" in titles  # multi-host installs after public release


# ------------------------------------------------------------------ health_table-log-critical-shows-ok

def test_a_task_with_no_attention_reason_but_log_errors_gets_an_amber_note(build):
    expression = _by_name(build())["Task Health"]["expression"]
    # Count first, no "Latest log:" prefix: the narrow Why column cut off the word "error"
    # (wording-05/health_table-why-log-note-truncated).
    assert 'whyText = plural(logErrLines, "log error")' in expression
    assert 'if (whyText == "" && logErrLines > 0) {' in expression
    assert f'whyColor = {dash._s(dash.TIERS["amber"][0])}' in expression


# ------------------------------------------------------------------ failures_trend-02 / failures_trend-03 (Duration Trend)

def test_duration_trend_filters_successful_runs_and_caps_at_the_newest(build):
    trend = _by_name(build())["Duration Trend"]
    dataset = trend["datasets"][0]
    rules = list(dash._walk_rules(dataset.get("filter")))
    fields = {r["field"]["name"]: r["constraint"] for r in rules}
    assert fields["is_success"] == {"type": "value", "value": 1}
    assert dataset["maxFeatures"] == dash.DURATION_TREND_CAP
    assert dataset["orderByFields"] == ["start_time_utc DESC"]
    assert str(dash.DURATION_TREND_CAP) in trend["title"]  # no silent caps (check_caps_and_help)
    dash.validate_definition(build())


# ------------------------------------------------------------------ interactions-mobile-disabled-invisible / -no-attention-list

def test_mobile_has_a_disabled_tile_and_an_attention_list_before_failures(build):
    definition = build()
    mobile = definition["mobileView"]
    names = [w["name"] for w in mobile["widgets"]]
    assert "Disabled" + dash.MOBILE_SUFFIX in names
    assert names.index("Attention Tasks" + dash.MOBILE_SUFFIX) < names.index("Recent Failures" + dash.MOBILE_SUFFIX)
    attention = _by_name(definition)["Attention Tasks" + dash.MOBILE_SUFFIX]
    rules = list(dash._walk_rules(attention["datasets"][0].get("filter")))
    fields = {r["field"]["name"] for r in rules}
    assert fields == {"needs_attention", "enabled"}
    # The outer filter is _dataset()'s own AND wrapper; the OR is the one nested group inside it.
    inner = attention["datasets"][0]["filter"]["rules"][0]
    assert inner["type"] == "filterGroup" and inner["condition"] == "OR"
    assert attention["noDataState"]["text"] == (
        '<p style="text-align:center">No task needs attention or is disabled.</p>')
    dash.validate_definition(definition)


# ------------------------------------------------------------------ task_detail-08

def test_task_detail_has_no_search_box(build):
    assert _by_name(build())["Task Detail"]["showFilter"] is False


# ------------------------------------------------------------------ metrics_tab-06

def test_metric_selector_also_targets_metric_range_and_latest_values(build):
    definition = build()
    widgets = _by_name(definition)
    selector = definition["desktopView"]["sidebar"]["selectors"][4]
    assert selector["name"] == "Metric"
    targeted = {t["targetId"].split("#")[0] for e in selector["events"] for a in e["actions"] for t in a["targets"]}
    assert widgets["Metric Summary"]["id"] in targeted
    assert widgets["Latest Metric Observations"]["id"] in targeted


# ------------------------------------------------------------------ indicators-06 heartbeat age

def test_heartbeat_age_switches_units_instead_of_staying_in_minutes(build):
    expression = _by_name(build())["Collector Heartbeat"]["expression"]
    # Minutes show only in the OK band (< HEARTBEAT_LATE_MINUTES): at the old 120 min bound, the
    # same displayed "3.0 h ago" could be either Late or Down (indicators-heartbeat-units-mismatch).
    assert (f"minutesSince < {dash.HEARTBEAT_LATE_MINUTES}" in expression
           and 'Text(minutesSince, "#,##0") + " min ago"' in expression)
    assert 'Text(Ceil(minutesSince / 6) / 10, "#,##0.0") + " h ago"' in expression  # 181 min reads 3.1 h, never 3.0
    assert 'Text(minutesSince / 1440, "#,##0.0") + " days ago"' in expression


# ------------------------------------------------------------------ failures_trend-06 / recent failures age

def test_recent_failures_show_an_about_prefix_for_estimated_starts_and_an_age(build):
    expression = _by_name(build())["Recent Failures"]["expression"]
    # A legacy row's "start" was really the reconstructed END-event time, up to 41 min after the
    # true start; "about <that time>" read as an approximate start, not the end
    # (failures_trend-estimated-start-is-end-time round-6 fix).
    assert ('IIf(estStart, "ended about " + fmtDate($datapoint["end_time_utc"]),'
            ' fmtDate($datapoint["start_time_utc"]))') in expression
    assert 'ageText: IIf(IsEmpty(ageMins), "", fmtSpan(Abs(ageMins)) + " ago")' in expression
    assert "{expression/ageText}" in _by_name(build())["Recent Failures"]["text"]


# ------------------------------------------------------------------ decision 5: skipped_last_7_days

def test_task_detail_week_line_adds_skipped_runs_only_when_present(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert 'DefaultValue($datapoint["skipped_last_7_days"], 0) > 0' in expression
    assert '" skipped"' in expression

"""Audit round 1, dashboard items (D1-D6, X1, X4, X8-X13). Every definition is built in the test.

Arcade cannot run here, so the Arcade checks are on the generated source: balanced brackets and
quotes, and the exact fields and thresholds each expression reads.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402
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


def _targets(widget):
    return [t for e in widget.get("events", []) for a in e["actions"] for t in a["targets"]]


def _balanced(source):
    """True when (), [] and {} balance outside string literals and every string closes."""
    stripped, quote, i = [], None, 0
    while i < len(source):
        ch = source[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        else:
            stripped.append(ch)
        i += 1
    if quote:
        return False
    stack, pairs = [], {")": "(", "]": "[", "}": "{"}
    for ch in stripped:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs and (not stack or stack.pop() != pairs[ch]):
            return False
    return not stack


def test_every_generated_arcade_source_is_balanced(build):
    definition = build()
    sources = [w.get("expression") or "" for w in dash.all_items(definition)]
    sources += [a["script"] for a in definition["arcadeDataSourceItems"]]
    for source in sources:
        assert _balanced(source), source[:200]


# ------------------------------------------------------------------ D1 metric change

def test_metric_change_reads_an_arcade_source_that_declares_every_column(build):
    definition = build()
    table = _by_name(definition)["Metric Change"]
    source_id = table["datasets"][0]["dataSource"]["itemId"]
    source = next(a for a in definition["arcadeDataSourceItems"] if a["itemId"] == source_id)
    assert source["refreshInterval"] == dash.METRIC_CHANGE_REFRESH_MINUTES
    assert dash.arcade_source_fields(definition, table) == [n for n, _, _ in dash.METRIC_CHANGE_FIELDS]
    assert set(table["dataSettings"]["valueFields"]) <= set(dash.arcade_source_fields(definition, table))
    script = source["script"]
    # Median() failed the whole data source in the Dashboards profile (bisected 2026-09-28);
    # the median is computed by Sort instead.
    assert "Sort(p, byNumber)" in script and f"Count(prev) < {dash.METRIC_CHANGE_HISTORY}" in script
    assert f"change <= {dash.METRIC_CHANGE_DROP_PCT}" in script and f"gapHours > {dash.METRIC_CHANGE_MISSING_HOURS}" in script
    assert '"flag_rank ASC, task_name ASC, display_name ASC"' in script
    dash.validate_definition(definition)


def test_metric_change_help_names_its_columns_and_flags(build):
    table = _by_name(build())["Metric Change"]
    help_text = " ".join(x for x in dash.HELP["Metric Change"] if x).lower()
    for title in ("latest", "usual", "change"):
        assert title in [c["title"].lower() for c in table["columnSettings"]] and title in help_text
    for flag in dash.METRIC_CHANGE_FLAGS:
        assert f'"{flag.lower()}"' in help_text and flag in table["expression"] + json.dumps(build())


def test_metric_summary_is_a_range_table_and_its_help_matches(build):
    summary = _by_name(build())["Metric Summary"]
    titles = [c["title"] for c in summary["columnSettings"]]
    assert titles == ["Task", "Metric", "Lowest", "Average", "Highest", "Times logged"]
    help_text = " ".join(x for x in dash.HELP["Metric Summary"] if x)
    for word in ("lowest", "average", "highest"):
        assert word in help_text
    for stale in ("Avg", "Max", "Peak", "sane"):
        assert stale not in help_text and stale not in summary["topCaption"]


def test_arcade_flag_off_keeps_the_layer_summary_and_drops_metric_change(build):
    off = build(heartbeat_arcade_refresh=False)
    widgets = _by_name(off)
    assert "Metric Change" not in widgets and "arcadeDataSource" not in json.dumps(off)
    assert widgets["Metric Summary"]["datasets"][0]["dataSource"]["layerId"] == dash.LAYER_METRICS
    dash.validate_definition(off)


def test_validator_refuses_a_column_the_arcade_source_does_not_return(build):
    definition = build()
    table = _by_name(definition)["Metric Change"]
    table["dataSettings"]["valueFields"].append("no_such_field")
    table["columnSettings"].append({"title": "X", "fieldName": "no_such_field"})
    with pytest.raises(SystemExit, match="does not return"):
        dash.validate_definition(definition)


def test_validator_refuses_a_field_the_arcade_source_does_not_return(build):
    definition = build()
    widgets = _by_name(definition)
    widgets["Task"]["events"][0]["actions"][0]["targets"].append(
        dash.target(widgets["Metric Change"], join="task_full_name"))
    with pytest.raises(SystemExit, match="Arcade data source"):
        dash.validate_definition(definition)


def test_validator_refuses_a_two_field_map(build):
    definition = build()
    t = _by_name(definition)["Metric Change"]["events"][0]["actions"][0]["targets"][0]
    t["fieldMap"].append({"sourceName": "task_name", "targetName": "task_name"})
    with pytest.raises(SystemExit, match="only the first"):
        dash.validate_definition(definition)


# ------------------------------------------------------------------ D2-D5 durations and nulls

def test_durations_use_the_minute_and_second_formatter(build):
    widgets = _by_name(build())
    assert 'fmtDur($datapoint["duration_seconds"])' in widgets["Recent Failures"]["expression"]
    assert "duration_minutes" not in widgets["Recent Failures"]["expression"]
    assert 'fmtDur($datapoint["duration_seconds"])' in widgets["Collector Runs"]["expression"]
    for name in ("Latest Metric Observations", "Metric Observations", "Metric Change"):
        assert "fmtMetric(" in widgets[name]["expression"], name
    # fmtMetric shows duration keys in seconds, matching the "Script time (seconds)" name: fmtDur
    # produced "15 min 30 s" beside a "(seconds)" label (wording-03/interactions-r2regress).
    # No " s" suffix: the display name already says "(seconds)" (wording-script-time-units-
    # differ-on-one-tab).
    assert 'return Text(Round(v), "#,##0"); }' in dash.FMT_HELPERS
    assert 'if (t < 60) { return Text(t) + " s"; }' in dash.FMT_HELPERS


def test_duration_trend_axis_names_its_unit_and_keeps_decimals(build):
    trend = _by_name(build())["Duration Trend"]
    y_axis = trend["chartConfig"]["axes"][1]
    # Seconds, not minutes: every other widget (Task detail, Failed runs, Collector runs) shows
    # a duration in seconds, and 2-decimal minutes flattened sub-minute runs to 0.00-0.05
    # (failures_trend-r3regress-units-differ-from-detail / wording-r3regress-duration-trend-units).
    assert y_axis["title"]["visible"] and y_axis["title"]["content"]["text"] == "Seconds (60 = 1 min)"
    assert y_axis["valueFormat"]["intlOptions"]["maximumFractionDigits"] == 2


def test_task_detail_computes_offsets_at_view_time_and_shows_no_fake_log_values(build):
    expression = _by_name(build())["Task Detail"]["expression"]
    assert "next_run_in_hours" not in expression and "latest_log_age_hours" not in expression
    assert 'minsFromNow($datapoint["next_run_time"])' in expression and "Number(Now())" in expression
    # Task Dash reads no logs of its own by design; every other task with no log name gets
    # the plain "No log found." (task_detail-taskdash-no-log-found-false).
    assert 'IIf(logName == "", IIf(Right(Lower(cmdPath), 16) == "run_taskdash.cmd"' in expression
    assert '"No log found."' in expression
    assert 'plural($datapoint["runs_last_7_days"], "run")' in expression
    assert '"n/a"' not in dash.FMT_HELPERS and '"--"' in dash.FMT_HELPERS


# ------------------------------------------------------------------ X1 one word per state

def test_status_words_are_sentence_case_and_the_status_selector_uses_them(build):
    for word, _, _ in dash.PALETTE.values():
        assert word == "OK" or word[0].isupper() and not word[1:].replace(" ", "").isupper(), word
    for word, _ in dash.CHANGE_KINDS.values():
        assert not word.isupper(), word
    health = _by_name(build())["Health"]
    assert health["label"] == "Status (Overview and Setting notes)"
    overrides = {o["key"]: o["label"] for o in health["category"]["labelOverrides"]}
    assert overrides == {v: dash.badge(v) for v in dash.HEALTH_VALUES}
    emitted = set(re.findall(r'return "(\w+)"', inspect.getsource(collect.get_health_status)))
    assert emitted == set(dash.HEALTH_VALUES)
    for value in dash.HEALTH_VALUES:
        assert dash.PALETTE[value][0] in dash.HELP["Health"][0]


def test_attention_kinds_show_their_sentence_case_word(build):
    expression = _by_name(build())["Task Health"]["expression"]
    for kind, word in dash.ATTENTION_WORDS.items():
        assert f'"{kind}", "{word}"' in expression
    assert set(dash.ATTENTION_WORDS) == set(dash.ATTENTION_TIER)


# ------------------------------------------------------------------ X4 thresholds in the help

def test_tile_thresholds_in_the_help_match_the_arcade(build):
    widgets = _by_name(build())
    rate = widgets["7-Day Success Rate"]["expression"]
    assert f"pct >= {dash.SUCCESS_OK_PCT}" in rate and f"pct >= {dash.SUCCESS_LOW_PCT}" in rate
    rate_help = dash.HELP["7-Day Success Rate"][0]
    assert f"{dash.SUCCESS_OK_PCT}%" in rate_help and f"{dash.SUCCESS_LOW_PCT}%" in rate_help
    assert "Low or Poor" in dash.HELP["7-Day Success Rate"][2]
    beat = widgets["Collector Heartbeat"]["expression"]
    assert f"minutesSince < {dash.HEARTBEAT_LATE_MINUTES}" in beat and f"minutesSince <= {dash.HEARTBEAT_DOWN_MINUTES}" in beat
    # In hours, matching the tile's own display past 120 min (indicators-heartbeat-units-mismatch).
    late_h, down_h = dash.HEARTBEAT_LATE_MINUTES / 60, dash.HEARTBEAT_DOWN_MINUTES / 60
    for name in ("Collector Heartbeat", "Heartbeat"):
        shows = dash.HELP[name][0]
        assert f"{late_h:g} h" in shows and f"{down_h:g} h" in shows
        assert "Task Dash" in shows
    # Both tiles read the same data, so their "What to do" cannot drift apart
    # (indicators-heartbeat-two-todo-texts).
    assert dash.HELP["Collector Heartbeat"][2] == dash.HELP["Heartbeat"][2]
    assert "Collector & Changes" in dash.HELP["Collector Heartbeat"][2]


# ------------------------------------------------------------------ X8-X13 text

def test_why_column_cuts_at_a_word_with_an_ellipsis(build):
    expression = _by_name(build())["Task Health"]["expression"]
    assert "shortText(reasons[0], 90)" in expression and "Left(DefaultValue" not in expression
    assert '+ "…"' in dash.FMT_HELPERS


def test_one_term_for_setting_notes_and_no_repeated_row_label(build):
    definition = build()
    # Shown text only: the widget name "Config Notes" is a stable key that other tests use.
    shown = ("title", "text", "description", "expression", "topCaption", "label")
    blob = json.dumps([[w.get(k) for k in shown] + [w.get("noDataState"), w.get("columnSettings")]
                       for w in dash.all_items(definition)], ensure_ascii=False)
    assert "Config Note" not in blob and "config warning" not in blob.lower()
    widgets = _by_name(definition)
    assert "noteLabel" not in widgets["Config Notes"]["expression"]
    assert "Setting notes" in widgets["Config Notes"]["title"]
    titles = {c["fieldName"]: c["title"] for c in widgets["Task Health"]["columnSettings"]}
    # Setting notes lost its own Task Health column (round7 overview-task-names-cut-setting-
    # notes-column): it surfaces in the Why cell instead, so Task name has room to fit.
    assert "config_warnings" not in titles and titles["failures_last_7_days"] == "Failures (7 days)"


def test_help_is_short_plain_and_points_at_real_controls():
    texts = [x for v in dash.HELP.values() for x in v if x] + list(dash.PROMPTS.values())
    texts += list(dash.EMPTY_STATES.values())
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            assert len(sentence.split()) <= 20, sentence
        assert not re.search(r"tiles? \d", text), text
    above = [t for t in texts if "above" in t]
    assert not above, above
    assert "sort" not in dash.HELP["Failed (last run)"][2].lower()


def test_headers_and_titles_have_no_short_forms(build):
    definition = build()
    headers = [c["title"] for w in dash.all_items(definition) for c in w.get("columnSettings", [])]
    for short in ("Fails 7d", "Obs", "Events", "Runs", "Inventory", "Metrics", "Alert", "Duration (s)"):
        assert short not in headers, short
    titles = [w.get("title", "") for w in dash.all_items(definition)]
    assert not [t for t in titles if t.lower().count("top") > 1]


def test_empty_states_hold_under_a_filter_and_use_no_internal_terms():
    joined = " ".join(dash.EMPTY_STATES.values())
    for stale in ("across all tasks", "real recorded", "One row per log file", "has no logged observations"):
        assert stale not in joined
    # One retention phrase everywhere (wording-11): "saved history (up to 90 days)".
    assert "saved history (up to 90 days)" in dash.EMPTY_STATES["Recent Failures"]
    assert "saved history (up to 90 days)" in dash.EMPTY_STATES["Duration Trend"]

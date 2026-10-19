"""Phase 0 dashboard polish: contrast, chart labels, date axis, heartbeat thresholds, help terms,
and the Recent changes clean-up of old text. Definitions are built in the test; Arcade cannot run
here, so Arcade checks are on the generated source (same pattern as test_round4_dashboard.py).
"""

from __future__ import annotations

import json
import re
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


def _build(config=None):
    return dash.build("TESTITEM", _templates(), config=config)


def _by_name(definition):
    return {w["name"]: w for w in dash.all_items(definition)}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------ 1: secondary grey contrast

def test_secondary_text_colour_clears_4_5_on_white_and_every_tier_tint():
    text = json.dumps(_build())
    assert "#7a7a7a" not in text.lower()
    greys = {c for c in re.findall(r"color:(#[0-9A-Fa-f]{6})", text)}
    assert "#595959" in greys
    backgrounds = ["#FFFFFF"] + [tint for _, tint in dash.TIERS.values()]
    for colour in greys:
        for background in backgrounds:
            assert _contrast(colour, background) >= 4.5, (colour, background)


# ------------------------------------------------------------------ 2: Most failures labels

def test_most_failures_axis_labels_are_cut_at_two_lines():
    top = _by_name(_build())["Top Failing Tasks"]
    assert top["chartConfig"]["axes"][0]["valueFormat"]["characterLimit"] == 24


# ------------------------------------------------------------------ 4: duration trend date axis

def test_duration_trend_axis_names_month_and_day():
    options = _by_name(_build())["Duration Trend"]["chartConfig"]["axes"][0]["valueFormat"]["intlOptions"]
    assert options == {"month": "short", "day": "numeric", "hour": "numeric", "minute": "2-digit"}
    assert "dateStyle" not in options  # Intl rejects dateStyle mixed with field options


# ------------------------------------------------------------------ 5: heartbeat thresholds

def _heartbeat_expressions(definition):
    return [w["expression"] for w in dash.all_items(definition) if w["name"] in ("Collector Heartbeat", "Heartbeat")]


def test_hourly_collector_keeps_90_and_180_exactly():
    assert dash.heartbeat_thresholds(60) == (90, 180)
    assert dash.heartbeat_thresholds() == (90, 180)
    assert dash.heartbeat_expression("f") == dash.heartbeat_expression("f", 90, 180)
    default, hourly = _build(), _build({"scheduler": {"collector_interval_minutes": 60}})
    for definition in (default, hourly):
        for expression in _heartbeat_expressions(definition):
            assert "minutesSince < 90," in expression and "minutesSince <= 180," in expression
            assert "1.5 h" not in expression


def test_fifteen_minute_collector_gets_22_5_and_45_minutes():
    assert dash.heartbeat_thresholds(15) == (22.5, 45)
    definition = _build({"scheduler": {"collector_interval_minutes": 15}})
    expressions = _heartbeat_expressions(definition)
    assert len(expressions) == 2
    for expression in expressions:
        assert "minutesSince < 22.5," in expression and "minutesSince <= 45," in expression
        assert "minutesSince < 90" not in expression
    help_text = _by_name(definition)["Collector Heartbeat"]["description"]
    assert "OK is less than 22.5 min. Late is 22.5 to 45 min. Down is more than 45 min." in help_text
    assert "1.5 h" not in help_text


def test_fifteen_minute_collector_age_text_keeps_late_and_down_apart():
    # Hours round to 6 min, so 45 (Late) and 46 (Down) would both read "0.8 h ago".
    for expression in _heartbeat_expressions(_build({"scheduler": {"collector_interval_minutes": 15}})):
        assert 'var ageText = When(minutesSince < 120, Text(minutesSince, "#,##0") + " min ago"' in expression
    hourly = _heartbeat_expressions(_build())[0]
    assert 'var ageText = When(minutesSince < 90, Text(minutesSince, "#,##0") + " min ago"' in hourly


def test_bad_collector_interval_fails_with_the_fix_in_the_message():
    with pytest.raises(SystemExit, match="collector_interval_minutes"):
        _build({"scheduler": {"collector_interval_minutes": 0}})


# ------------------------------------------------------------------ 6: help text literals

def _help_blob(definition):
    return json.dumps([w.get("description", "") for w in dash.all_items(definition)], sort_keys=True)


def _normalised(definition):
    # Widget ids are random per build; only the text has to match.
    return re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "ID", json.dumps(definition, sort_keys=True))


def test_reference_defaults_leave_every_help_text_byte_identical():
    reference = {"scheduler": {"overdue_grace_minutes": 15, "collector_interval_minutes": 60,
                               "collector_task_name": "Task Dash"},
              "alerting": {"heartbeat_hour": 7}}
    # A config with no heartbeat_hour has no daily e-mail (phase 2.5), so the plain build differs
    # from the reference only in that sentence; the reference config keeps the old text. The
    # reference install names its task "Task Dash" in its config since v1.1 (H1-9), because the
    # code default became the installer's "TaskDash Collector".
    plain = _build({"alerting": {"heartbeat_hour": 7}, "scheduler": {"collector_task_name": "Task Dash"}})
    assert "TaskDash Collector task" in _help_blob(_build({"alerting": {"heartbeat_hour": 7}}))
    configured = _build(reference)
    assert _help_blob(plain) == _help_blob(configured)
    assert _normalised(plain) == _normalised(configured)
    for literal in ("7 AM daily check", "The grace time is 15 min", "Task Dash task", "Task Dash log"):
        assert literal in _help_blob(plain)
    # And the reference build equals the old one: the help text still reads straight from HELP.
    assert dash.help_html("Collector Runs") == dash.help_html("Collector Runs", terms=dash.help_terms(reference))


def test_other_sites_values_replace_the_literals():
    config = {"scheduler": {"overdue_grace_minutes": 10, "collector_task_name": "Fleet Watch"},
              "alerting": {"heartbeat_hour": 13}}
    blob = _help_blob(_build(config))
    assert "for the 1 PM daily check" in blob and "7 AM" not in blob
    assert "The grace time is 10 min" in blob and "15 min unless" not in blob
    assert "Fleet Watch task" in blob and "Fleet Watch log" in blob
    assert "Task Dash task" not in blob and "Task Dash log" not in blob


@pytest.mark.parametrize("hour, text", [(0, "12 AM"), (7, "7 AM"), (12, "12 PM"), (23, "11 PM")])
def test_daily_check_hour_reads_as_a_12_hour_clock(hour, text):
    assert dash.help_terms({"alerting": {"heartbeat_hour": hour}})["daily"] == text


# ------------------------------------------------------------------ 8: Recent changes old text

def test_recent_changes_cleans_old_text_at_display_time():
    expression = _by_name(_build())["Recent Changes"]["expression"]
    for field in ("old_summary", "new_summary"):
        assert f'DefaultValue($datapoint["{field}"], "")' in expression
    for old, new in dash.OLD_SUMMARY_FIXES:
        assert f'"{old}", "{new}")' in expression
    assert 'Replace(' in expression and '".; ", "; "' in expression
    # The separator that the table and phone lists split on must stay.
    assert dash.OLD_SUMMARY_FIXES[0][1] == "; "


def test_duration_ratio_card_text_matches_the_changes_wording():
    expression = dash.detail_list_expression()
    assert 'Floor(Round(ratio * 100, 6)) < 1, "Duration: under 1% of the usual"' in expression
    assert '"% of the usual"' in expression


def test_definition_still_validates():
    dash.validate_definition(_build())
    dash.validate_definition(_build({"scheduler": {"collector_interval_minutes": 15}}))

"""Phase 0 item 13: one title rule. A noun title; scope and count go in a 12 px subtitle line.

Help text names widgets by their shown titles, so these tests build the real definition and check
that every widget the help names is a title that is on screen. Arcade and Dashboards cannot run here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402

# The only title with a bracket: the owner chose its wording.
OWNER_TITLE = "Lowest 7-day success"
# Widget names that HELP, PROMPTS and MOBILE_WHY use today.
REFERENCED = ("Task health", "Task detail", "Failed runs", "Duration trend", "Most failures in 7 days",
              "Metric change", "Latest metric values", "Recent changes",
              "Collector runs", "Setting notes", "Collector heartbeat")
# Titles from before the rule. None may survive in help text or on screen.
OLD_TITLES = ("Failed runs (newest", "Most failures in 7 days (top 10)", "Duration trend, seconds",
              "Metric trend (selected row", "Latest metric values (newest", "Recent changes (newest",
              "Collector runs (newest", "(not failures)", "Recent failures", "Metric observations")


def _build():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return dash.build("TESTITEM", templates)


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _shown_title(widget) -> str:
    """A table shows the bold line of its topCaption; every other widget shows `title`."""
    if widget["type"] == "tableWidget":
        match = re.search(r"<strong>(.*?)</strong>", widget.get("topCaption", ""))
        return _plain(match.group(1)) if match else ""
    return _plain(widget.get("title", ""))


def _titles(definition):
    return {w["name"]: _shown_title(w) for w in dash.all_items(definition)}


def _help_blob():
    parts = [x for entry in dash.HELP.values() for x in entry if x]
    return " ".join(parts + list(dash.PROMPTS.values()) + list(dash.MOBILE_WHY.values()))


def test_every_named_widget_is_a_shown_title():
    shown = [t.lower() for t in _titles(_build()).values()]
    blob = _help_blob().lower()
    for ref in REFERENCED:
        assert any(ref.lower() in t for t in shown), f"help names '{ref}', no title shows it"
        assert ref.lower() in blob, f"'{ref}' is no longer named in help; trim REFERENCED"


def test_help_never_names_an_old_title():
    blob = _help_blob()
    for old in OLD_TITLES:
        assert old not in blob, old


def test_titles_are_nouns_with_no_count_unit_or_instruction_in_brackets():
    definition = _build()
    for name, title in _titles(definition).items():
        if title in (OWNER_TITLE, "Failed (last run)", "Tasks tracked (all)"):  # tile titles outside this item
            continue
        assert "(" not in title and ")" not in title, f"{name}: {title!r}"
        assert "," not in title, f"{name}: {title!r}"
    # Shown title and caption text only: Task detail's inline "(not failures)" label is not a title.
    blob = json.dumps([[w.get("title"), w.get("topCaption")] for w in dash.all_items(definition)])
    for old in OLD_TITLES:
        assert old not in blob, old


def test_cap_numbers_sit_in_title_on_lists_and_charts_and_the_cap_check_passes():
    """Only a table's topCaption is proven to draw; a list or chart shows `title` (review finding)."""
    definition = _build()
    dash.validate_definition(definition)
    widgets = {w["name"]: w for w in dash.all_items(definition)}
    expected = {"Recent Failures": "Failed runs - newest 25",
                "Top Failing Tasks": "Most failures in 7 days - top 10",
                "Duration Trend": f"Duration trend - newest {dash.DURATION_TREND_CAP} runs",
                "Latest Metric Observations": "Latest metric values - newest 60",
                "Recent Changes": "Recent changes - newest 50"}
    for name, title in expected.items():
        assert widgets[name]["title"] == title, name
        # No cap may depend on a subtitle key that no list or chart has been seen to draw.
        assert not any(ch.isdigit() for ch in _plain(widgets[name].get("topCaption", ""))), name
    assert "Newest 48" in _plain(widgets["Collector Runs"]["topCaption"])  # a table: topCaption draws
    assert widgets["Metric Trend"]["title"] == "Metric trend"
    assert "not failures" in widgets["Config Notes"]["topCaption"]
    assert "Setting notes" in widgets["Config Notes"]["title"]


def test_tile_title_the_owner_chose_is_unchanged():
    assert _titles(_build())["7-Day Success Rate"] == OWNER_TITLE


def test_phone_failed_runs_has_the_same_title_and_its_own_count():
    phone = {w["name"]: w for w in _build()["mobileView"]["widgets"]}
    widget = phone["Recent Failures" + dash.MOBILE_SUFFIX]
    assert widget["title"] == "Failed runs - newest 10"


def test_every_sentence_in_a_subtitle_is_20_words_or_fewer():
    for widget in dash.all_items(_build()):
        if widget["type"] == "tableWidget":
            continue
        for sentence in re.split(r"[.!?]\s*", _plain(widget.get("topCaption", ""))):
            assert len(sentence.split()) <= 20, sentence

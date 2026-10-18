"""Phase 0 tile polish: titles, layout, tile emphasis, heartbeat "just now", Collector tab widths."""

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
    definition = dash.build("TESTITEM", templates)
    dash.validate_definition(definition)
    return definition


def _widgets(definition):
    return {w["name"]: w for w in definition["desktopView"]["widgets"]}


def _layout_item(definition, name):
    wid = _widgets(definition)[name]["id"]

    def walk(node):
        if node.get("type") == "itemLayoutElement" and node["id"] == wid:
            return node
        for child in node.get("elements", []):
            found = walk(child)
            if found:
                return found
        return None
    return walk(definition["desktopView"]["layout"]["rootElement"])


def test_titles():
    w = _widgets(_build())
    assert w["7-Day Success Rate"]["title"] == "Lowest 7-day success"
    assert 'bottomText: state + " · 2+ failures only"' in w["7-Day Success Rate"]["expression"]
    assert w["Tasks Tracked"]["title"] == "Tasks tracked (all)"


def test_tile_columns_have_equal_counts_and_aligned_heights():
    assert len(dash.TILE_ROW_1) == len(dash.TILE_ROW_2) == 4
    definition = _build()
    heights = [[_layout_item(definition, n)["height"] for n in col]
               for col in (dash.TILE_ROW_1, dash.TILE_ROW_2)]
    assert heights[0] == heights[1] == [0.4, 0.2, 0.2, 0.2]


def test_needs_attention_is_the_only_large_count_and_tasks_tracked_is_small():
    w = _widgets(_build())
    assert 'middleTextMaxSize: "large"' in w["Needs Attention"]["expression"]
    for name in ("Failed (last run)", "Overdue", "Other Warnings", "Disabled", "Tasks Tracked"):
        assert 'middleTextMaxSize: "medium"' in w[name]["expression"], name


def test_mobile_view_keeps_every_tile_and_mapping():
    definition = _build()
    desktop = _widgets(definition)
    mobile_names = {w["name"] for w in definition["mobileView"]["widgets"]}
    for name in dash.MOBILE_TILES:
        assert desktop[name]["id"] in definition["elementMappings"], name
        assert name + dash.MOBILE_SUFFIX in mobile_names, name


def test_heartbeat_says_just_now_under_one_minute():
    expr = dash.heartbeat_expression("MIN_collected_at_utc")
    block = 'if (minutesSince < 1) {\n  ageText = "just now";\n}'
    assert block in expr
    assert expr.index("var ageText") < expr.index(block) < expr.index("return {\n  textColor")


def test_collector_tab_heartbeat_is_narrow_and_caption_says_what_table_is_for():
    definition = _build()
    assert _layout_item(definition, "Heartbeat")["width"] == 0.15
    assert _layout_item(definition, "Collector Runs")["width"] == 0.85
    caption = _widgets(definition)["Collector Runs"]["topCaption"]
    assert "Newest 48. Read Error for a failed run." in caption
    assert "heartbeat is OK" not in caption

"""The version number and the About TaskDash panel on the Collector & Changes tab.

Dashboards cannot run here, so these tests check the built definition only: the text, the
layout weights and the validator. How the panel draws is not tested.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402


def _build(config=None):
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return dash.build("TESTITEM", templates, config=config)


def _about(definition):
    return next(w for w in definition["desktopView"]["widgets"] if w["name"] == dash.ABOUT_NAME)


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _collector_tab(definition):
    tabs = definition["desktopView"]["layout"]["rootElement"]["elements"]
    return next(t for t in tabs if t["tabName"] == "Collector & Changes")


def _ids(element):
    if element["type"] == "itemLayoutElement":
        yield element["id"]
    for child in element.get("elements", []):
        yield from _ids(child)


def test_version_constant_has_the_major_minor_patch_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", taskdash_common.TASKDASH_VERSION)
    assert taskdash_common.TASKDASH_VERSION != "1.0.0"  # 1.0.0 is the public release


def test_about_panel_is_on_the_collector_tab_with_version_and_interval():
    definition = _build()
    about = _about(definition)
    text = _plain(about["text"])
    assert about["type"] == "richTextWidget"
    assert about["id"] in set(_ids(_collector_tab(definition)))
    assert f"TaskDash {taskdash_common.TASKDASH_VERSION}" in text
    assert "collector runs every 60 min" in text
    assert about["name"] == "About TaskDash" and "About TaskDash" in text


def test_a_15_minute_interval_shows_in_the_panel():
    config = {"scheduler": {"collector_interval_minutes": 15}}
    assert "every 15 min" in _plain(_about(_build(config))["text"])


def test_about_text_names_no_host_path_address_or_item_id():
    text = _about(_build())["text"]
    for banned in ("TASKHOST", "C:\\", "@", "TESTITEM"):
        assert banned not in text
    assert not re.search(r"[0-9a-fA-F]{32}", text)


def test_about_text_uses_palette_colours_and_short_sentences():
    text = _about(_build())["text"]
    assert set(re.findall(r"color:(#[0-9a-fA-F]{6})", text)) == {"#4c4c4c"}
    for sentence in re.split(r"(?<=[.:]) ", _plain(text)):
        assert len(sentence.split()) <= 20, sentence


def test_definition_with_the_panel_passes_validation():
    dash.validate_definition(_build())


def test_mobile_view_has_no_about_panel():
    assert all(w["name"] != dash.ABOUT_NAME for w in _build()["mobileView"]["widgets"])


def test_collector_tab_weights_still_sum_to_one():
    def check(element):
        children = element.get("elements", [])
        if element["type"] == "stackLayoutElement" and children:
            axis = "width" if element["orientation"] == "col" else "height"
            assert abs(sum(c[axis] for c in children) - 1) < 0.01, element["orientation"]
        for child in children:
            check(child)

    check(_collector_tab(_build())["elements"][0])

"""Phase 2 deliverable 10a: dashboard_templates.json holds three generic widget skeletons.

Equivalence with the previous template file, a copy of a real dashboard, was proved once by an
equal build digest. That digest is not kept: the tests below check structure instead.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402

BASE = Path(dash.__file__).parent
TEMPLATE_PATH = BASE / dash.TEMPLATE_FILE
MAX_TEMPLATE_BYTES = 20 * 1024


def _skeletons_and_build():
    data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in data["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return by_name, dash.build("TESTITEM", templates)["desktopView"]["widgets"]


def _deny_terms():
    spec = importlib.util.spec_from_file_location("deny_scan", BASE / "_tools" / "deny_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GENERIC_DENY


def test_every_skeleton_key_reaches_the_widgets_build_makes_from_it():
    skeletons, built = _skeletons_and_build()
    for name in ("Task Inventory", "Recent Runs"):
        copies = [w for w in built if w["type"] == skeletons[name]["type"]]
        assert copies, name
        for widget in copies:
            assert set(skeletons[name]) <= set(widget), (name, widget["name"])
    chart_keys = set(skeletons["Schedule Mix"]["chartConfig"])
    configs = [set(w["chartConfig"]) for w in built if "chartConfig" in w]
    assert any(chart_keys <= keys for keys in configs)
    assert chart_keys <= set().union(*configs)


def test_template_file_has_exactly_the_three_widgets_the_builder_reads():
    data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert [w["name"] for w in data["desktopView"]["widgets"]] == ["Task Inventory", "Recent Runs", "Schedule Mix"]


def test_template_file_is_small_and_names_no_deny_listed_term():
    raw = TEMPLATE_PATH.read_bytes()
    assert len(raw) < MAX_TEMPLATE_BYTES
    text = raw.decode("utf-8").lower()
    assert [term for term in _deny_terms() if term.lower() in text] == []

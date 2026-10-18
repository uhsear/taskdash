"""Audit findings 19, 23, 29: date rule, time zone note, mobile tile size, Disabled glyph."""
import json
from pathlib import Path

import taskdash_dashboard as dash


def _build():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return dash.build("TESTITEM", templates)


def test_disabled_glyph_is_not_the_hollow_square():
    assert dash.PALETTE["Disabled"][1] == "‖"


def test_table_cells_use_the_short_date_and_the_time_zone_is_stated():
    by = {w["name"]: w for w in _build()["desktopView"]["widgets"]}
    assert "observed_at_utc: {displayText: fmtDateShort(" in by["Metric Observations"]["expression"]
    assert "Run times show in your device's time zone. Schedules use the server's clock." in by["Task Health"]["topCaption"]
    assert "Run times show in your device's time zone. Schedules use the server's clock." in by[dash.ABOUT_NAME]["text"]


def test_mobile_tiles_have_no_medium_value_size_but_desktop_keeps_it():
    d = _build()
    mobile = [w for w in d["mobileView"]["widgets"] if w["name"].endswith(dash.MOBILE_SUFFIX)
              and "expression" in w]
    assert mobile
    # Tasks tracked stays medium on the phone, as on desktop: it must not be the biggest figure.
    tracked = "Tasks Tracked" + dash.MOBILE_SUFFIX
    assert all('middleTextMaxSize: "medium"' not in w["expression"] for w in mobile if w["name"] != tracked)
    assert any(w["name"] == tracked and 'middleTextMaxSize: "medium"' in w["expression"] for w in mobile)
    assert any('middleTextMaxSize: "medium"' in w.get("expression", "")
               for w in d["desktopView"]["widgets"])

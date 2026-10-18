"""Phase 0 metrics polish: Metric change arithmetic, tab-scoped selector labels, the Workflow
Metrics layout and the date rule. Arcade cannot run here, so Arcade checks read the generated source.
"""

from __future__ import annotations

import json
from pathlib import Path

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402


def _templates():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    return {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
            "serial_config": by_name["Schedule Mix"]["chartConfig"]}


def _build():
    return dash.build("TESTITEM", _templates())


def _by_name(definition):
    return {w["name"]: w for w in dash.all_items(definition)}


def test_metric_change_rounds_usual_and_computes_change_from_shown_numbers():
    expr = dash.metric_change_expression()
    # usual rounds with latest for whole values and durations; the change reads the rounded pair
    assert 'key == "duration_seconds" || lat == Round(lat)' in expr
    assert "lat = Round(lat); usu = Round(usu);" in expr
    assert "ch = IIf(usu != 0, 100 * (lat - usu) / Abs(usu), IIf(lat == 0, 0, null));" in expr
    assert "fmtMetric(key, usu)" in expr and "fmtMetric(key, lat)" in expr
    # a change that rounds to zero has no sign
    assert 'IIf(chRound == 0, "0%"' in expr
    assert 'IIf(ch > 0, "+"' not in expr


def test_selector_labels_name_their_tab_scope():
    labels = {name: w["label"] for name, w in _by_name(_build()).items()
              if w.get("type") == "categorySelectorWidget"}
    assert labels["Metric"] == "Metric (Workflow Metrics)"
    assert labels["Health"] == "Status (Overview and Setting notes)"
    assert labels["Schedule"] == "Schedule (Overview and Setting notes)"
    assert labels["Host"] == "Host" and labels["Task"] == "Task"


def test_metric_selector_only_targets_metric_widgets():
    # the scope in the label is true: every Metric target lives on the Workflow Metrics tab
    widgets = _by_name(_build())
    ids = {w["id"]: n for n, w in widgets.items()}
    hit = {ids[t["targetId"].split("#")[0]] for t in widgets["Metric"]["events"][0]["actions"][0]["targets"]}
    assert hit <= {"Metric Change", "Metric Trend", "Metric Observations", "Metric Summary",
                   "Latest Metric Observations"}


def test_workflow_metrics_gives_the_data_tables_most_of_the_height():
    definition = _build()
    ids = {w["id"]: n for n, w in _by_name(definition).items()}
    tab = next(t for t in definition["desktopView"]["layout"]["rootElement"]["elements"]
               if t["tabName"] == "Workflow Metrics")
    heights = {ids[r["elements"][0]["id"]]: r["height"] for r in tab["elements"][0]["elements"]}
    assert heights["Metric Change"] >= 0.33 and heights["Metric Summary"] >= 0.30
    assert heights["Metric Observations"] <= 0.16
    # The row holds a titled, searchable list and a chart that must show a slope: 0.15 left both
    # about 140 px tall at 1080p (a 0.15 row showed 3 table rows).
    assert heights["Metric Trend"] >= 0.22  # the row is keyed by its first widget


def test_idle_all_values_band_carries_a_title():
    prompt = _by_name(_build())["Metric Observations"]["noFilterState"]["text"]
    assert "All values of the selected task. Open the Filters panel at the left edge and pick a task" in prompt
    assert "<strong>" not in prompt and "<br>" not in prompt  # Dashboards dropped them: "taskPick"


def test_lists_use_the_long_date_and_a_disabled_next_run_says_off():
    changes = dash.changes_list_expression()
    assert 'whenText: fmtDate($datapoint["changed_utc"])' in changes
    assert 'IIf(health == "Disabled", "Off", IIf(' in _by_name(_build())["Task Health"]["expression"]

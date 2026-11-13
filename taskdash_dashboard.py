"""Build and publish the TaskDash dashboard.

Driver: taskdash_dashboard.py (run by hand after a schema or layout change)
Source: dashboard_templates.json (proven Dashboards 5.0.0 widget structures) + the hosted table service
Target: the TaskDash dashboard item (prod: arcgis.dashboard_id) or TaskDash_TEST (test service)

Widgets are cloned from proven structures and retargeted at the six-table schema. Nothing
carries a hardcoded host filter -- an earlier dashboard full of them froze after a server move.
Every Dashboards JSON shape that has not been render-tested sits behind FEATURE_FLAGS, so a
shape that renders wrong is switched off in one place after the render test.

    python taskdash_dashboard.py --target test            # write JSON, validate, show the plan
    python taskdash_dashboard.py --target test --apply    # create/update the TaskDash_TEST item
    python taskdash_dashboard.py --target prod --apply    # update the item in arcgis.dashboard_id
"""

from __future__ import annotations

import argparse
import copy
import functools
import html
import json
import re
import socket
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import taskdash_metrics
from taskdash_common import (
    TABLE_COLLECTOR_RUNS,
    TABLE_INVENTORY,
    TABLE_METRIC_CHANGE,
    TABLE_METRICS,
    TABLE_RUNS,
    TABLE_TASK_CHANGES,
    TASKDASH_VERSION,
    CONFIG_ARG_HELP,
    DEFAULT_CONFIG_FILE,
    command_text,
    config_default,
    config_range_problem,
    connect_agol,
    data_dir,
    emit,
    get_tables,
    load_config,
    normalize_text,
    runtime_dir,
    see_troubleshooting,
)

OUTPUT_JSON = "dashboard_taskdash.json"
PROD_DASHBOARD_TITLE = "TaskDash"
TEST_DASHBOARD_TITLE = "TaskDash_TEST"
DEFAULT_PORTAL_URL = config_default("arcgis.url")
OFFLINE_ITEM_ID = "0" * 32

LAYER_INVENTORY = 0
LAYER_RUNS = 1
LAYER_METRICS = 2
# Only a fallback for offline builds and tests. main() resolves the real ids by table name:
# CollectorRuns and TaskChanges get theirs from add_to_definition, so 3 and 4 are not guaranteed.
DEFAULT_LAYER_IDS = {
    TABLE_INVENTORY: LAYER_INVENTORY, TABLE_RUNS: LAYER_RUNS, TABLE_METRICS: LAYER_METRICS,
    TABLE_COLLECTOR_RUNS: 3, TABLE_TASK_CHANGES: 4,
}

# One switch per Dashboards JSON shape that phase4_shapes.md marks UNPROVEN in 5.0.0. Turning a
# flag off removes that shape completely and falls back to the proven behaviour, so a bad
# render is fixed here without touching the builders.
FEATURE_FLAGS: Dict[str, bool] = {
    "sidebar_selectors": True,        # F1: desktopView.sidebar with 5 category selectors
    "cross_table_row_filter": True,   # F2: table/chart row -> another table, fieldMap task_name
    "detail_requires_selection": True,  # F3: Task Detail stays empty until a row is picked
    "no_filter_prompt_text": True,    # F3: noFilterState.text prompt on selection targets
    "no_data_text": True,             # F3: noDataState.text empty-state message
    "widget_description": True,       # F4: per-widget help in description + showDescription
    # F5: Arcade data sources -- the heartbeat (5-min refresh) and the Metric Change table. One
    # flag for both: the table reuses the heartbeat's data-source shape, and a table on an Arcade
    # source is not render-tested yet. Off = layer-statistic heartbeat and no Metric Change.
    "heartbeat_arcade_refresh": True,
    "trend_charts": True,             # F7: date-axis line charts (duration, metric) scoped to one task
    "column_number_format": True,     # 4.10: per-column number format key on grouped tables
    "status_symbols": True,           # F9: plain-text glyph beside every status word
    "mobile_element_mappings": True,  # F6: elementMappings desktop id -> mobile id
    "config_notes_filter": True,      # Setting notes list: config_warnings is_not_empty
}

HEARTBEAT_REFRESH_MINUTES = 5
# UNPROVEN key name: no harvested 5.x table carries a column number format. Found by one UI
# round-trip on TEST ("0 decimal places" on one column), then corrected here if it differs.
COLUMN_FORMAT_KEY = "valueFormat"

# --------------------------------------------------------------------------- palette (4.4)

# Tier -> (text colour, tint). Every fg clears WCAG 4.5:1 on its tint and on white.
TIERS = {
    "green": ("#1A7A39", "#E6F4EA"), "blue": ("#0B5FA8", "#E3F0FB"),
    "amber": ("#8A5300", "#FFF1D6"), "red": ("#A32A2A", "#FBE7E6"),
    "grey": ("#595959", "#ECECEC"), "gold": ("#6B5900", "#F5EFDD"),
    "slate": ("#3E4C63", "#E8ECF1"),
}
# Status -> (word, symbol, tier). The word always renders; colour is only the second cue. The
# glyphs have no emoji presentation, so phones do not swap in colour pictographs. One word per
# state in sentence case, everywhere: the status column, the Status selector, the lists, the tiles.
PALETTE = {
    "Success": ("OK", "✓", "green"),
    "Running": ("Running", "▶", "blue"),
    "Warning": ("Warning", "▲", "amber"),
    "Error": ("Failed", "✕", "red"),
    # ‖ and not the hollow square U+25A1: the square drew as a missing-character box in the
    # Dashboards font. ‖ already renders for Stopped and Skipped; the word still tells them apart.
    "Disabled": ("Disabled", "‖", "grey"),
    "NotYetRun": ("Not yet run", "○", "gold"),
    "StartFailed": ("Did not start", "⊘", "red"),
    "NotLoggedOn": ("Account not signed in", "⊘", "red"),
    "KilledTimeLimit": ("Stopped at time limit", "■", "red"),
    "Terminated": ("Terminated", "■", "red"),
    "Missed": ("Missed", "○", "amber"),
    "Skipped-AlreadyRunning": ("Skipped (already running)", "‖", "slate"),
    "Stopped": ("Stopped", "‖", "amber"),
    "ConfigWarning": ("Setting note", "△", "slate"),
}
# health_status values (taskdash_collect.get_health_status). The Status selector shows each as
# its palette badge, so the selector and the status column use the same words.
HEALTH_VALUES = ("Success", "Running", "Warning", "Error", "Disabled", "NotYetRun")
NEUTRAL_FG = "#4c4c4c"
# attention_kind (enrich_inventory) -> palette status. An unknown kind falls back to Warning,
# so a new kind can never render as healthy.
ATTENTION_TIER = {"FAILED": "Error", "STUCK": "Warning", "BLOCKED": "Warning",
                  "OVERDUE": "Warning", "WARNING": "Warning"}
ATTENTION_WORDS = {"FAILED": "Failed", "STUCK": "Stuck", "BLOCKED": "Blocked",
                   "OVERDUE": "Overdue", "WARNING": "Warning"}
CHANGE_KINDS = {  # TaskChanges.change_kind -> (word, palette status or "" for neutral)
    "became_failing": ("Started failing", "Error"), "became_overdue": ("Became overdue", "Warning"),
    "recovered": ("Recovered", "Success"), "config_warning_added": ("Setting note added", "ConfigWarning"),
    # Neutral, not the ConfigWarning triangle/tint: a cleared note is a risk going away, not a new
    # one (collector_tab-removed-note-uses-warning-badge). It can also mean the note was
    # acknowledged rather than the setting fixed -- HELP["Recent Changes"] says so.
    "config_warning_cleared": ("Setting note removed", ""), "task_added": ("Task added", ""),
    "task_removed": ("Task removed", ""), "definition_changed": ("Task changed", ""),
}


def fg(status: str) -> str:
    return TIERS[PALETTE[status][2]][0]


def bg(status: str) -> str:
    return TIERS[PALETTE[status][2]][1]


def badge(status: str, word: Optional[str] = None) -> str:
    word = word or PALETTE[status][0]
    return f"{PALETTE[status][1]} {word}" if FEATURE_FLAGS["status_symbols"] else word


def sym_prefix(status: str) -> str:
    """The status glyph and a space, for text that is built in Arcade; "" when symbols are off."""
    return f"{PALETTE[status][1]} " if FEATURE_FLAGS["status_symbols"] else ""


def new_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- widget builders

ESRI_TO_FILTER_TYPE = {
    "esriFieldTypeOID": "integer",
    "esriFieldTypeInteger": "integer",
    "esriFieldTypeSmallInteger": "integer",
    "esriFieldTypeDouble": "double",
    "esriFieldTypeSingle": "double",
    "esriFieldTypeString": "string",
    "esriFieldTypeDate": "date",
}


def field_filter_type(table: str, field_name: str) -> str:
    """Look the filter type up from the table schema.

    Dashboards accepts only string/integer/double/date here. A wrong value (e.g. "number")
    is not rejected at save time -- the widget just renders "Cannot access data" forever.
    """
    import taskdash_publish as publish

    for field in publish.TABLE_SPECS[table][0]():
        if field["name"] == field_name:
            return ESRI_TO_FILTER_TYPE[field["type"]]
    raise SystemExit(
        f"Dashboard references {field_name!r}, which is not a field on {table}. "
        f"Add it to the matching *_fields() list in taskdash_publish.py, or correct the widget."
    )


def rule(field: str, value: Any, table: str = TABLE_INVENTORY, *, operator: str = "equal") -> Dict[str, Any]:
    """A structured filter rule, typed from the schema.

    Operators and constraint types are the ones seen in 4.3x/5.x org exports. Deliberately
    never used to pin a host name: that is exactly what froze the old dashboard.
    """
    if operator in ("is_in", "is_not_in"):
        constraint: Dict[str, Any] = {"type": "values", "values": list(value)}
    elif operator in ("is_null", "is_not_null", "is_empty", "is_not_empty"):
        constraint = {"type": "nil"}
    else:
        constraint = {"type": "value", "value": value}
    return {
        "type": "filterRule",
        "field": {"name": field, "type": field_filter_type(table, field)},
        "operator": operator,
        "constraint": constraint,
    }


def _dataset(item_id: str, layer_id: int, *, group_by: Optional[List[str]] = None,
             order_by: Optional[List[str]] = None, stats: Optional[List[Dict[str, Any]]] = None,
             where: Optional[List[Dict[str, Any]]] = None,
             max_features: Optional[int] = None) -> Dict[str, Any]:
    dataset: Dict[str, Any] = {
        "type": "serviceDataset",
        "name": "main",
        "dataSource": {"type": "layerDataSource", "itemId": item_id, "layerId": layer_id},
        "groupByFields": group_by or [],
        "orderByFields": order_by or [],
        "outFields": ["*"],
        "returnDistinctValues": False,
        "allowSourceDownload": False,
        "allowSummaryDownload": False,
    }
    if stats:
        dataset["statisticDefinitions"] = stats
    if max_features is not None:
        dataset["maxFeatures"] = max_features
    if where:
        dataset["filter"] = {"type": "filterGroup", "condition": "AND", "rules": list(where)}
    return dataset


def stat_def(field: str, stat_type: str) -> Dict[str, Any]:
    return {"onStatisticField": field, "statisticType": stat_type,
            "outStatisticFieldName": f"{stat_type.upper()}_{field}"}


def indicator(item_id: str, name: str, layer_id: int, *, where=None,
              stat_field: str = "OBJECTID", stat_type: str = "count",
              caption: str = "", color: str = "#003f5c",
              expression: Optional[str] = None,
              extra_stats: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    stats = [stat_def(stat_field, stat_type)]
    stats.extend(stat_def(s["field"], s["type"]) for s in extra_stats or [])
    arcade = expression is not None
    widget = {
        "id": new_id(),
        "name": name,
        "title": name,
        "showLastUpdate": False,
        "noDataState": {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True},
        "noFilterState": {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True},
        "datasets": [_dataset(item_id, layer_id, where=where, stats=stats)],
        "arcadeEnabled": arcade,
        "type": "indicatorWidget",
        "defaultSettings": {
            "textColor": color,
            # No fallback to `name`: the widget already renders `title`, so echoing it here
            # printed every header label twice and ate a third of each tile's height.
            "topSection": {"fontSize": 32, "textInfo": ({"text": caption} if caption else {})},
            "middleSection": {"fontSize": 110, "textInfo": {
                "text": "{calculated/value}"}},
            "bottomSection": {"fontSize": 40, "textInfo": {}},
        },
        "comparison": "none",
        "valueFormat": {"name": "value", "prefix": False, "style": "decimal", "useGrouping": True,
                        "minimumFractionDigits": 0, "maximumFractionDigits": 0, "valueSuffix": ""},
        "percentageFormat": {"name": "percentage", "prefix": False, "style": "percent",
                             "useGrouping": True, "maximumFractionDigits": 1},
        "ratioFormat": {"name": "ratio", "prefix": False, "style": "decimal",
                        "useGrouping": True, "maximumFractionDigits": 2},
        "valueType": "statistic",
        "noValueState": {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True},
    }
    if arcade:
        widget["expression"] = expression
    return widget


def _chart_config(template: Dict[str, Any], category_field: str, value_field: str) -> Dict[str, Any]:
    """Clone the proven chartConfig and rebind its series to this chart's fields.

    The template hardcodes x="schedule_bucket", y="COUNT_OBJECTID". Copying it verbatim makes
    any chart with a different group-by or statistic render "Cannot access data".
    """
    config = copy.deepcopy(template)
    for series in config.get("series", []):
        series["x"] = category_field
        series["y"] = value_field
    return config


def serial_chart(item_id: str, name: str, layer_id: int, category_field: str,
                 chart_config: Dict[str, Any], *, stat_field: str = "OBJECTID",
                 stat_type: str = "count", where=None, max_features: int = 15,
                 title: Optional[str] = None) -> Dict[str, Any]:
    out_name = f"{stat_type.upper()}_{stat_field}"
    return {
        "id": new_id(),
        "name": name,
        "title": title or name,
        "showLastUpdate": False,
        "noDataState": {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True},
        "noFilterState": {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True},
        "datasets": [_dataset(item_id, layer_id, group_by=[category_field],
                              order_by=[f"{out_name} desc"], where=where, max_features=max_features,
                              stats=[{"onStatisticField": stat_field, "statisticType": stat_type,
                                      "outStatisticFieldName": out_name}])],
        "actionMode": "monoSelection",
        "categoryType": "groupByValues",
        "type": "serialChartWidget",
        "valueFormat": {"name": "value", "prefix": False, "style": "decimal",
                        "useGrouping": True, "maximumFractionDigits": 0},
        "labelFormat": {"name": "label", "prefix": False, "style": "decimal", "useGrouping": True,
                        "minimumFractionDigits": 0, "maximumFractionDigits": 0},
        "category": {"labelOverrides": [], "nullLabel": "(none)", "blankLabel": "(blank)"},
        "parseDates": False,
        "minPeriod": "DD",
        "categoryAxisLabelsBehavior": "wrap",
        "chartConfig": _chart_config(chart_config, category_field, out_name),
    }


def _trend_chart_config(x_field: str, y_field: str, y_title: str) -> Dict[str, Any]:
    """A time-series lineSeries chartConfig, built directly rather than cloned.

    The only chartConfig template on hand (Schedule Mix) is a rotated barSeries with its category
    axis labels hidden -- fine for a handful of bars, wrong for a date axis with many points. No
    lineSeries template exists to borrow instead, so this is assembled to match the same
    axis/series/legend shape Dashboards expects, just with a date-formatted category axis and an
    unrotated layout.
    """
    axis_text = {"type": "esriTS", "angle": 0, "font": {"family": "inherit", "style": "normal", "weight": "normal"}}
    return {
        "version": "25.0.0", "type": "chart", "orderOptions": {}, "colorMatch": False,
        "chartRenderer": {"type": "uniqueValue", "uniqueValueInfos": []},
        "axes": [
            {
                "type": "chartAxis", "visible": True,
                "title": {"type": "chartText", "visible": False,
                         "content": {"type": "esriTS", "angle": 0,
                                     "font": {"family": "inherit", "style": "normal", "weight": "bold"}, "text": ""}},
                # Time fields: a date-only label could not tell apart a 15-min task's 200 points
                # (critic-05); minPeriod "mm" needs the time to be visible on the axis and in the
                # tooltip, or the extra precision is invisible. Month, day, hour and minute are
                # named one by one: the newest 200 runs of a daily task span 6 months and the
                # axis showed only "Sep", and dateStyle cannot be mixed with field options.
                # formatPerDateTimeUnit is the only date-axis format key the Dashboards editor itself
                # saves (4 harvested date axes, all with intlOptions {}). The live render ignored
                # intlOptions and drew one bare "Sep" for an Aug-Oct range (audit 8/20): the axis
                # labels its grid steps per unit, so a month step must carry the day too.
                "valueFormat": {"type": "date", "intlOptions": {
                    "month": "short", "day": "numeric", "hour": "numeric", "minute": "2-digit"},
                    "formatPerDateTimeUnit": {
                        "year": {"month": "short", "year": "numeric"},
                        "month": {"month": "short", "day": "numeric"},
                        "day": {"month": "short", "day": "numeric"},
                        "hour": {"hour": "2-digit", "minute": "2-digit", "hour12": True},
                        "minute": {"hour": "2-digit", "minute": "2-digit", "hour12": True},
                        "second": {"hour": "2-digit", "minute": "2-digit", "second": "2-digit", "hour12": True}}},
                "lineSymbol": {"type": "esriSLS", "style": "esriSLSSolid", "width": 1},
                "labels": {"type": "chartText", "visible": True, "content": axis_text},
                "grid": {"type": "esriSLS", "style": "esriSLSSolid", "color": [140, 140, 140, 80], "width": 1},
                "guides": [], "scrollbar": {"visible": False, "width": 15, "gripSize": 22},
            },
            {
                "type": "chartAxis", "visible": True,
                # The unit is on the axis, and 2 decimals: with 0, a task whose runs all take
                # under a minute drew an axis that read only 0 and 1 (audit D3).
                "title": {"type": "chartText", "visible": True,
                         "content": {"type": "esriTS", "angle": 0,
                                     "font": {"family": "inherit", "style": "normal", "weight": "bold"},
                                     "text": y_title}},
                "valueFormat": {"type": "number", "intlOptions": {
                    "style": "decimal", "notation": "standard",
                    "minimumFractionDigits": 0, "maximumFractionDigits": 2}},
                "lineSymbol": {"type": "esriSLS", "style": "esriSLSSolid", "width": 1},
                "labels": {"type": "chartText", "visible": True, "content": axis_text},
                "grid": {"type": "esriSLS", "style": "esriSLSSolid", "color": [140, 140, 140, 80], "width": 1},
                "guides": [], "buffer": True,
            },
        ],
        "series": [{
            "type": "lineSeries", "id": "main", "name": "main", "x": x_field, "y": y_field,
            "dataLabels": {"type": "chartText", "visible": False, "content": {
                "type": "esriTS", "angle": 0, "font": {"family": "inherit", "size": 11, "style": "normal", "weight": "normal"}}},
            "dataTooltipVisible": True, "dataTooltipReverseColor": True,
            "lineSymbol": {"type": "esriSLS", "style": "esriSLSSolid", "color": [27, 127, 92, 255], "width": 2},
            "markerSymbol": {"type": "esriSMS", "style": "esriSMSCircle", "color": [27, 127, 92, 255], "size": 5,
                             "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": [27, 127, 92, 255], "width": 1}},
        }],
        "legend": {"type": "chartLegend", "visible": False,
                  "body": {"type": "esriTS", "angle": 0,
                           "font": {"family": "inherit", "size": 11, "style": "normal", "weight": "normal"}},
                  "position": "bottom"},
        "horizontalAxisLabelsBehavior": "rotate", "rotated": False,
        "cursorCrosshair": {"type": "cursorCrosshair"},
    }


def trend_chart(item_id: str, name: str, layer_id: int, x_field: str, y_field: str, *,
                y_title: str, where=None, max_features: Optional[int] = None,
                order_desc: bool = False, title: Optional[str] = None) -> Dict[str, Any]:
    """A genuine time-series chart: one point per observation.

    serial_chart() is always categoryType "groupByValues" -- it groups and counts rows, which
    discards metric_value/observed_at_utc entirely. This binds categoryType "features" straight
    to feature fields so the actual metric values reach the chart, ordered chronologically.
    Only ever built as a requiresSelection target: unscoped, one 3,400 s task flattens the rest.

    order_desc + max_features caps a high-frequency task's trend at its newest rows instead of
    silently keeping its oldest ones under an uncapped ASC query hitting a service page limit
    (failures_trend-03). Dashboards re-sorts a date-axis line series by date, so DESC rows draw
    in time order (rendered on TEST 2026-09-28: one task's newest 200 runs).
    """
    direction = "DESC" if order_desc else "ASC"
    return {
        "id": new_id(),
        "name": name,
        "title": title or name,
        "showLastUpdate": False,
        "noDataState": {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True},
        "noFilterState": {"verticalAlignment": "middle", "showTopCaption": True, "showBottomCaption": True},
        "datasets": [_dataset(item_id, layer_id, order_by=[f"{x_field} {direction}"], where=where,
                              max_features=max_features)],
        "actionMode": "monoSelection",
        "categoryType": "features",
        "type": "serialChartWidget",
        "valueFormat": {"name": "value", "prefix": False, "style": "decimal",
                        "useGrouping": True, "maximumFractionDigits": 2},
        "labelFormat": {"name": "label", "prefix": False, "style": "decimal", "useGrouping": True,
                        "minimumFractionDigits": 0, "maximumFractionDigits": 0},
        "category": {"labelOverrides": [], "nullLabel": "(none)", "blankLabel": "(blank)"},
        "parseDates": True,
        # A date-only axis (DD) lumped every run of a 15-min or hourly task onto one day, losing
        # its run times (critic-05); minutes keep the newest 200 runs of a 15-min task readable.
        "minPeriod": "mm",
        "categoryAxisLabelsBehavior": "wrap",
        "chartConfig": _trend_chart_config(x_field, y_field, y_title),
    }


def table_widget(item_id: str, name: str, layer_id: int, columns: List[Dict[str, Any]],
                 template: Dict[str, Any], *, order_by: List[str],
                 expression: Optional[str] = None,
                 top_caption: str = "", where=None, max_features: Optional[int] = None,
                 group_by: Optional[List[str]] = None,
                 stats: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """expression=None renders the columns natively.

    Worth having: a grouped table with statistic aliases errored every row under a cells
    expression across five separate attempts (date formatting, missing displayText, number
    format, orphan statistic all ruled out), and no proven template in this org pairs Arcade
    with a grouped dataset. Native rendering is correct and boring; the tint is not worth
    an unbounded search.
    """
    widget = copy.deepcopy(template)
    widget["id"] = new_id()
    widget["name"] = name
    widget["topCaption"] = top_caption
    widget["datasets"] = [_dataset(item_id, layer_id, group_by=group_by, order_by=order_by,
                                   where=where, max_features=max_features, stats=stats)]
    widget["arcadeEnabled"] = expression is not None
    if expression is not None:
        widget["expression"] = expression
    else:
        widget.pop("expression", None)
    # Columns are defined by dataSettings.valueFields + columnSettings. There is no "table" key;
    # setting one is silently ignored and the widget keeps the cloned template's columns.
    widget["dataSettings"] = {"type": "features", "valueFields": [c["name"] for c in columns]}
    widget["columnSettings"] = []
    for c in columns:
        setting = {"title": c["label"], "fieldName": c["name"], "textAlign": c.get("align", "left"),
                   "isBold": False, "isItalic": False, "isUnderlined": False}
        if c.get("decimals") is not None and FEATURE_FLAGS["column_number_format"]:
            setting[COLUMN_FORMAT_KEY] = {"name": "value", "prefix": False, "style": "decimal",
                                          "useGrouping": True, "minimumFractionDigits": 0,
                                          "maximumFractionDigits": c["decimals"]}
        widget["columnSettings"].append(setting)
    widget.pop("table", None)
    widget.pop("events", None)
    return widget


def list_widget(item_id: str, name: str, layer_id: int, template: Dict[str, Any], *,
                order_by: List[str], expression: str, text: str, where=None,
                max_features: Optional[int] = None, title: Optional[str] = None) -> Dict[str, Any]:
    widget = copy.deepcopy(template)
    widget["id"] = new_id()
    widget["name"] = name
    widget["title"] = title or name
    widget["datasets"] = [_dataset(item_id, layer_id, order_by=order_by,
                                   where=where, max_features=max_features)]
    widget["arcadeEnabled"] = True
    widget["expression"], widget["text"] = route_fields_through_arcade(expression, text)
    widget.pop("events", None)
    return widget


def subtitle_html(text: str) -> str:
    """The one subtitle line under a title: scope and count, 12 px, as the table captions draw it.

    Only a table's topCaption is proven to draw. A list or chart puts its cap number in `title`
    ("Noun - newest 25") and uses this only for a note it can afford to lose (HELP repeats it).
    """
    return f'<p><span style="font-size:12px;">{text}</span></p>'


BARE_FIELD_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
ARCADE_ATTRIBUTES = "  attributes: {\n"


def route_fields_through_arcade(expression: str, text: str) -> Tuple[str, str]:
    """Rewrite bare {field} placeholders as {expression/f_field}.

    With Arcade enabled on a list, Dashboards renders only {expression/...} values and drops a
    bare {task_name} without a word -- found in the prod render on 2026-09-25: no list showed
    which task a row was about, and the old dashboard's Recent Failures had the same blank.
    """
    fields = sorted(set(BARE_FIELD_RE.findall(text)))
    if not fields:
        return expression, text
    if expression.count(ARCADE_ATTRIBUTES) != 1:
        raise SystemExit("A list template names a field, but its Arcade has no single "
                         "'attributes: {' block to carry it -- add the field to the expression.")
    # The text is HTML. Replace chain as in FMT_HELPERS' esc(): "&" first so entities are not doubled.
    lines = "".join(
        f'    f_{f}: Replace(Replace(Replace(DefaultValue($datapoint["{f}"], ""), "&", "&amp;"), '
        f'"<", "&lt;"), ">", "&gt;"),\n' for f in fields)
    expression = expression.replace(ARCADE_ATTRIBUTES, ARCADE_ATTRIBUTES + lines)
    return expression, BARE_FIELD_RE.sub(lambda m: "{expression/f_" + m.group(1) + "}", text)


def category_selector(item_id: str, name: str, layer_id: int, field: str, *, label: str = "",
                      label_overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A dropdown category selector, per the S-TRAF (5.1.0) and S-EECA shapes.

    allowNone, not requireDefault: the targets start unfiltered ("All"), so opening the
    dashboard never hides a task behind a default pick. No maxFeatures: a capped group-by
    silently drops categories. labelOverrides uses the {key, label} shape of every harvested
    override (categorySelector v27, serial charts to 4.33); it is not render-tested on a 5.0.0
    selector, and a key it does not match falls back to the raw value.
    """
    overrides = [{"key": k, "label": v} for k, v in (label_overrides or {}).items()]
    return {
        "id": new_id(),
        "dashboardItemType": "selector",
        "type": "categorySelectorWidget",
        "name": name,
        "label": label or name,
        "noDataState": {},
        "noFilterState": {},
        "category": {"type": "groupByValues", "labelOverrides": overrides},
        # noneLabel is not in any harvested 5.x selector shape (interactions-no-category-selected);
        # kept because an unrecognized key is silently dropped elsewhere (e.g. column() width was),
        # so it is a free try, not a proven fix for the sidebar's "No category selected" text.
        "selection": {"type": "single", "operator": "equal", "allowNone": True,
                      "noneLabelPlacement": "first", "noneLabel": "All"},
        "showFilter": True,
        "displayType": "list",
        "presentationMode": "dropdown",
        "placeholderText": "All",
        "datasets": [_dataset(item_id, layer_id, group_by=[field], order_by=[f"{field} ASC"],
                              stats=[{"onStatisticField": field, "statisticType": "count",
                                      "outStatisticFieldName": f"COUNT_{field}"}])],
        "events": [],
    }


def column(field: str, label: str, *, align: str = "left",
           decimals: Optional[int] = None) -> Dict[str, Any]:
    # No width: table_widget's columnSettings loop never copies one, and no harvested 5.x table
    # export carries a per-column width key (health_table-column-widths-ignored). Declaring one
    # here claimed a control the build never applied.
    return {"name": field, "label": label, "type": "column",
            "align": align, "visible": True, "decimals": decimals}


# --------------------------------------------------------------------------- arcade

def _s(value: str) -> str:
    """An Arcade string literal. Raw UTF-8, not \\u escapes, so the status glyphs reach Arcade intact."""
    return json.dumps(value, ensure_ascii=False)


def _decode(var: str, mapping: Dict[str, str], default: str) -> str:
    if not mapping:
        return default
    pairs = ", ".join(f"{_s(k)}, {_s(v)}" for k, v in mapping.items())
    return f"Decode({var}, {pairs}, {default})"


def palette_arcade(tier_var: str = "tier") -> str:
    """Arcade that sets word/sym/fg/bg from the palette status held in `tier_var`.

    Generated from PALETTE so the three hand-kept copies of the hex values cannot drift again.
    """
    symbols = {k: p[1] for k, p in PALETTE.items()} if FEATURE_FLAGS["status_symbols"] else {}
    return (
        f'var word = {_decode(tier_var, {k: p[0] for k, p in PALETTE.items()}, tier_var)};\n'
        f'var sym = {_decode(tier_var, symbols, _s(""))};\n'
        f'var fg = {_decode(tier_var, {k: fg(k) for k in PALETTE}, _s(NEUTRAL_FG))};\n'
        f'var bg = {_decode(tier_var, {k: bg(k) for k in PALETTE}, _s(""))};\n'
    )


STATUS_TEXT = 'var statusText = IIf(sym == "", label, sym + " " + label);\n'

# "--" is the one mark for a missing value: a default 0 or "0.0 h old" read as real data (D4).
FMT_HELPERS = (
    'function fmtDate(v) { return IIf(IsEmpty(v), "--", Text(Date(Number(v)), "MMM D, YYYY h:mm A")); }\n'
    # Table cells are too narrow for the year: this drops it so the minute and AM/PM still fit
    # in an equal-width column (defect: column widths are not applied, see column()).
    'function fmtDateShort(v) { return IIf(IsEmpty(v), "--", Text(Date(Number(v)), "M/D h:mm A")); }\n'
    # Arcade has no escape function. List text is HTML, and task paths, descriptions, command
    # lines and log text can hold < > &. "&" goes first so the entities are not escaped twice.
    # Render-tested on TaskDash_TEST 2026-10-22: a task named with "&" shows a plain "&" in the
    # Task detail card, so Dashboards parses Arcade output as HTML and this is not a double escape.
    'function esc(t) { return Replace(Replace(Replace(t, "&", "&amp;"), "<", "&lt;"), ">", "&gt;"); }\n'
    'function fmtN(v) { return IIf(IsEmpty(v), "--", Text(v, "#,##0")); }\n'
    'function plural(n, word) { var c = DefaultValue(n, 0); return Text(c, "#,##0") + " " + word + IIf(c == 1, "", "s"); }\n'
    # Seconds under a minute, then "M min S s", then "H h M min": "0.0 min" hid 1-3 s failures (D5).
    'function fmtDur(s) {\n'
    '  if (IsEmpty(s)) { return "--"; }\n'
    '  if (s < 1) { return "< 1 s"; }\n'
    '  var t = Round(s, 0);\n'
    '  if (t < 60) { return Text(t) + " s"; }\n'
    '  if (t < 3600) { var sec = t % 60; return Text(Floor(t / 60)) + " min" + IIf(sec == 0, "", " " + Text(sec) + " s"); }\n'
    '  var m = Floor((t % 3600) / 60);\n'
    '  return Text(Floor(t / 3600)) + " h" + IIf(m == 0, "", " " + Text(m) + " min");\n'
    '}\n'
    'function fmtSpan(mins) { var m = Abs(mins); if (m < 60) { return Text(m, "#,##0") + " min"; }'
    ' if (m < 2880) { return Text(m / 60, "#,##0.0") + " h"; } return Text(m / 1440, "#,##0.0") + " days"; }\n'
    # Epoch-millisecond arithmetic, the heartbeat's timezone-safe idiom. Computed when the view
    # loads, so an offset never freezes at the collector's last run (D4).
    'function minsFromNow(v) { if (IsEmpty(v)) { return null; } return (Number(v) - Number(Now())) / 60000; }\n'
    # "Script time (seconds)" must show seconds everywhere: fmtDur's "15 min 30 s" contradicted
    # its own label in Metric change and Latest metric values (wording-03/interactions-r2regress).
    # No " s" suffix: the display name already says "(seconds)", and Metric range showed the
    # same value with no unit right below it (wording-script-time-units-differ-on-one-tab).
    'function fmtMetric(key, v) { if (IsEmpty(v)) { return "--"; } if (key == "duration_seconds") {'
    ' return Text(Round(v), "#,##0"); } return Text(v, "#,##0.##"); }\n'
    # Cut at a word boundary and mark the cut: Left(t, 80) stopped mid-word with no sign (X8).
    'function shortText(t, n) {\n'
    '  if (Count(t) <= n) { return t; }\n'
    '  var words = Split(Left(t, n), " ");\n'
    '  if (Count(words) < 2) { return Left(t, n) + "…"; }\n'
    '  return Concatenate(Slice(words, 0, Count(words) - 1), " ") + "…";\n'
    '}\n'
)


def task_status_arcade() -> str:
    """kind/tier/statusText for a TaskInventory row: attention_kind first, else health_status."""
    unknown_kind = 'Proper(Lower(Replace(kind, "_", " ")), "firstword")'  # a new kind still reads as a word
    return (
        'var kind = DefaultValue($datapoint["attention_kind"], "");\n'
        'var health = DefaultValue($datapoint["health_status"], "Unknown");\n'
        f'var tier = IIf(kind == "", health, {_decode("kind", ATTENTION_TIER, _s("Warning"))});\n'
        + palette_arcade()
        + f'var label = IIf(kind == "", word, {_decode("kind", ATTENTION_WORDS, unknown_kind)});\n'
        + STATUS_TEXT
        + 'var cw = DefaultValue($datapoint["config_warnings"], "");\n'
        + 'var noteCount = IIf(cw == "", 0, Count(Split(cw, "; ")));\n'
    )


def task_table_expression() -> str:
    return FMT_HELPERS + task_status_arcade() + (
        # A stale next_run_time (a snapshot, not live) read as a valid future start next to
        # "OK" long after it passed (health_table-r3regress-next-run-past-not-fixed): a healthy
        # task whose next run already came and went shows its schedule instead of a false time.
        'var nm = minsFromNow($datapoint["next_run_time"]);\n'
        # The first reason, cut at a word, plus a count of the others: the detail card has them all.
        'var why = DefaultValue($datapoint["attention_reason"], "");\n'
        'var whyText = "";\n'
        'if (kind != "" && why != "") {\n'
        '  var reasons = Split(why, "; ");\n'
        '  whyText = shortText(reasons[0], 90) + IIf(Count(reasons) > 1, " (+" + Text(Count(reasons) - 1) + " more)", "");\n'
        '}\n'
        # A task can pass on its Windows result code but its own newest log says otherwise (a
        # monitor that logs CRITICAL/ERROR but exits 0). Say so in the note colour; never touch
        # health_status, attention_kind or the tiles.
        'var logErrLines = DefaultValue($datapoint["latest_log_error_lines"], 0);\n'
        # Only a log written since the last run started speaks for that run: a disabled task
        # showed "4 log errors" from a log a day older than its last three runs (final audit 3).
        'var logT = $datapoint["latest_log_time_utc"]; var runT = $datapoint["last_run_time"];\n'
        'if (!IsEmpty(logT) && !IsEmpty(runT) && logT < runT - 60000) { logErrLines = 0; }\n'
        f'var whyColor = {_s(NEUTRAL_FG)};\n'
        # Same words as the phone list, so desktop and phone agree on a disabled task (final
        # audit 11); a run in progress says so instead of an empty cell (final audit 6/10). Not
        # Task Dash's own row: its "Running now." reason is on every page it writes, so the cell
        # read "Running now" between collections too (same trap as task_detail-taskdash-always-in-progress).
        'if (whyText == "" && health == "Disabled") { whyText = "Disabled in Task Scheduler."; }\n'
        'if (whyText == "" && health == "Running") { whyText = "Running now"; }\n'
        # A healthy task has no attention_kind, so the reasons above are blank for it. Its mute note
        # (publish.apply_mute_notes writes it as the last reason) still shows: a muted task sends no
        # alert, and a quiet row would otherwise look like a task nobody watches.
        'if (whyText == "" && why != "") {\n'
        '  var muteParts = Split(why, "; ");\n'
        '  var muteNote = muteParts[Count(muteParts) - 1];\n'
        '  if (Find("Muted (alerts off)", muteNote) == 0 || Find("In maintenance until", muteNote) == 0) {'
        ' whyText = muteNote; }\n'
        '}\n'
        # Count first, no "Latest log:" prefix: the ~98px Why column cut "Latest log: 12..." before
        # the word "error" (wording-05/health_table-why-log-note-truncated).
        'if (whyText == "" && logErrLines > 0) {\n'
        '  whyText = plural(logErrLines, "log error");\n'
        f'  whyColor = {_s(TIERS["amber"][0])};\n'
        '}\n'
        # A setting note otherwise never showed in the table once names were no longer cut off:
        # the dedicated column was dropped to widen Task name (overview-task-names-cut-setting-
        # notes-column), so an OK task with a note now surfaces it here instead.
        'if (whyText == "" && noteCount > 0) {\n'
        f'  whyText = {_s(sym_prefix("ConfigWarning"))} + plural(noteCount, "setting note");\n'
        f'  whyColor = {_s(fg("ConfigWarning"))};\n'
        '}\n'
        'return {\n'
        '  backgroundColor: IIf(kind == "", "", bg),\n'
        '  cells: {\n'
        '    attention_kind: {displayText: statusText, textColor: fg, backgroundColor: bg, textAlign: "center"},\n'
        '    task_name: {displayText: DefaultValue($datapoint["task_name"], ""), textAlign: "left"},\n'
        '    attention_reason: {displayText: whyText, textColor: whyColor, textAlign: "left"},\n'
        '    last_run_time: {displayText: fmtDateShort($datapoint["last_run_time"]), textAlign: "left"},\n'
        '    next_run_time: {displayText: IIf(health == "Disabled", "Off", IIf(kind == "" && !IsEmpty(nm) && nm < 0,'
    ' DefaultValue($datapoint["schedule_bucket"], "--"), fmtDateShort($datapoint["next_run_time"]))),'
    ' textAlign: "left"},\n'
        '    failures_last_7_days: {displayText: Text(DefaultValue($datapoint["failures_last_7_days"], 0)),'
        ' textAlign: "right"},\n'
        '    host_name: {displayText: DefaultValue($datapoint["host_name"], ""), textAlign: "left"}\n'
        '  }\n'
        '};\n'
    )


DETAIL_VARS = (
    'var nextMins = minsFromNow($datapoint["next_run_time"]);\n'
    'var rel = "";\n'
    'if (!IsEmpty(nextMins)) { rel = IIf(nextMins >= 0, " (in " + fmtSpan(nextMins) + ")", " (" + fmtSpan(nextMins) + " ago)"); }\n'
    'var logName = DefaultValue($datapoint["latest_log_name"], "");\n'
    'var logMins = minsFromNow($datapoint["latest_log_time_utc"]);\n'
    'var ratio = $datapoint["duration_ratio"];\n'
    'var detail = esc(DefaultValue($datapoint["schedule_detail"], ""));\n'
    'var runAs = esc(DefaultValue($datapoint["run_as_user"], ""));\n'
    'var lt = DefaultValue($datapoint["logon_type"], "--");\n'
    # The collector splits the command line, so the script's arguments ("--apply") arrive ready.
    'var cmdPath = esc(DefaultValue($datapoint["command_target_path"], ""));\n'
    'var args = esc(DefaultValue($datapoint["command_target_arguments"], ""));\n'
    # Read once here so detail_list_expression can blank them when privacy.hide_paths hides them.
    'var workDir = esc(DefaultValue($datapoint["command_target_working_directory"], ""));\n'
    'var logPath = esc(DefaultValue($datapoint["latest_log_path"], ""));\n'
    # settings_detail is collected and stored but shown nowhere (critic-02): an acknowledged
    # risky setting (Setting notes filters it out once acknowledged) was otherwise invisible.
    'var settingsDetail = esc(DefaultValue($datapoint["settings_detail"], ""));\n'
)

# LogonType enum -> plain words. The raw token stays visible in brackets for anyone checking it
# against Task Scheduler (task_detail-09: "svc_gisjobs (Password)" read like a credential).
LOGON_TYPE_WORDS = {
    "Interactive": "only while this account is signed in",
    "Password": "whether or not a user is logged on (stored password)",
    "S4U": "whether or not a user is logged on (no stored password)",
    "ServiceAccount": "whether or not a user is logged on (built-in service account)",
}


def detail_list_expression(hidden: Sequence[str] = ()) -> str:
    # A field the view hides is absent there, and indexing an absent field is an Arcade error.
    # The collector already writes "" to it, so reading "" changes nothing on screen.
    detail_vars = DETAIL_VARS
    for field in hidden:
        detail_vars = detail_vars.replace(f'DefaultValue($datapoint["{field}"], "")', '""')
    # A hidden working folder reads "" too, so "not set" would be false: show "--" like Script does.
    if "command_target_working_directory" in hidden:
        detail_vars = detail_vars.replace('var workDir = esc("");', 'var workDir = "--";')
    return FMT_HELPERS + task_status_arcade() + detail_vars + (
        # "" when lt is not a LOGON_TYPE_WORDS key, so userText can tell a real word from the raw
        # token echoed back at it (task_detail-runs-as-wording: unmapped types read "Group [Group]").
        f'var ltWord = {_decode("lt", LOGON_TYPE_WORDS, _s(""))};\n'
        'return {\n'
        '  textColor: "#4c4c4c", separatorColor: "#D6D6D6",\n'
        '  backgroundColor: IIf(kind == "", "", bg),\n'
        '  selectionColor: "#00304d", selectionTextColor: "#ffffff",\n'
        '  attributes: {\n'
        '    statusText: statusText,\n'
        '    whyText: IIf(kind == "", "", "Why: " + esc(DefaultValue($datapoint["attention_reason"], ""))),\n'
        '    descText: esc(DefaultValue($datapoint["description"], "No description yet. Ask the TaskDash owner to add one.")),\n'
        # The bucket word repeated the detail's own opening word on every task ("Daily | Daily
        # every day at..."); show the detail alone once it exists (task_detail-06).
        '    scheduleText: IIf(detail == "", DefaultValue($datapoint["schedule_bucket"], "--"), detail),\n'
        # A running task's own last_duration_seconds is still the PREVIOUS finished run's: pairing
        # it with the CURRENT start time read as that run's duration (task_detail-running-shows-previous-duration).
        # Task Dash judges itself by its previous run while it collects: apply_own_run_view moves
        # last_run_time to that run and leads attention_reason with "Running now.", so the line
        # shows one run and adds that a new one is in progress
        # (task_detail-r3regress-own-task-running-previous-duration).
        # Task Dash judges itself by the PREVIOUS finished run while its own new run collects
        # (see the comment above); that previous run is real and over, so the line shows only
        # it -- no "a new run is in progress" suffix, which never went away once the new run
        # finished (task_detail-taskdash-always-in-progress). "took" now uses the same " | "
        # separator as the other branch below, not the stray ", took" that read as "successfully.,".
        '    lastText: IIf(IsEmpty($datapoint["last_run_time"]), esc(DefaultValue($datapoint["last_result_text"], "--")),'
        ' IIf(Find("Running now.", DefaultValue($datapoint["attention_reason"], "")) == 0,'
        ' fmtDate($datapoint["last_run_time"]) + " | " + esc(DefaultValue($datapoint["last_result_text"], "--"))'
        ' + IIf(IsEmpty($datapoint["last_duration_seconds"]), "", " | took " + fmtDur($datapoint["last_duration_seconds"]))'
        # Task Dash's own row: the line above is the PREVIOUS finished run (see the comment
        # above), so say which run actually wrote this page (round7 item 10).
        ' + " | this page was written by the run at " + fmtDate($datapoint["collected_at_utc"]),'
        ' fmtDate($datapoint["last_run_time"]) + " | " + IIf(health == "Running"'
        ' || DefaultValue($datapoint["state"], "") == "Running", "in progress",'
        ' esc(DefaultValue($datapoint["last_result_text"], "--"))'
        ' + " | " + IIf(IsEmpty($datapoint["last_duration_seconds"]), "duration not recorded",'
        ' "took " + fmtDur($datapoint["last_duration_seconds"]))))),\n'
        # The table's fallback (task_table_expression next_run_time cell) is repeated here: a
        # time-of-day task viewed after its start and before the next hourly collection showed a
        # next run "1 min ago" though it is only unrefreshed, not overdue
        # (task_detail-next-run-shown-in-past).
        '    nextText: IIf(health == "Disabled", "Off (task is disabled)",'
        ' IIf(kind == "" && !IsEmpty(nextMins) && nextMins < 0,'
        ' DefaultValue($datapoint["schedule_bucket"], "--") + " (next run time not refreshed yet)",'
        ' fmtDate($datapoint["next_run_time"]) + rel)),\n'
        '    weekText: plural($datapoint["runs_last_7_days"], "run") + ", " + plural($datapoint["failures_last_7_days"], "failure")'
        ' + IIf(DefaultValue($datapoint["skipped_last_7_days"], 0) > 0,'
        ' ", " + Text(DefaultValue($datapoint["skipped_last_7_days"], 0), "#,##0") + " skipped", ""),\n'
        # An anomaly is not a problem on its own -- show it only when whyText (a real attention
        # reason) is not already shown, so the card never states the same thing twice (wording-16).
        '    anomalyText: IIf(kind == "" && DefaultValue($datapoint["is_duration_anomaly"], 0) == 1 && !IsEmpty(ratio),'
        ' When(ratio >= 1, "Duration: " + Text(ratio, "#,##0.0") + " times the usual",'
        ' Floor(Round(ratio * 100, 6)) < 1, "Duration: under 1% of the usual",'
        ' "Duration: " + Text(Floor(Round(ratio * 100, 6)), "#,##0") + "% of the usual"), ""),\n'
        # An unmapped logon type used to echo the raw token twice ("runs Group [Group]"): show
        # the token once when there is no plain-English word for it (task_detail-runs-as-wording).
        '    userText: IIf(runAs == "", "--", runAs + ", runs " + IIf(ltWord == "", lt, ltWord + " [" + lt + "]")),\n'
        '    pathText: IIf(cmdPath == "", "--", cmdPath + IIf(args == "", "", " " + args)),\n'
        '    wdText: IIf(workDir == "", "not set", workDir),\n'
        # No log is a real state: say so, and invent no age or counts. Task Dash does not read
        # its own logs (task_detail-taskdash-no-log-found-false: it read "No log found." the same
        # as a task whose log is simply missing, though Task Dash writes one every run).
        # Empty counts mean unknown (unreadable, or a cumulative debug file that metrics.py nulls
        # on purpose -- task_detail-05): say which, instead of the same "not known" for both.
        # The log file name is not repeated in the counts/unreadable lines: logPathText, the next
        # line on the card, already names it in the full path (task_detail-log-name-shown-twice).
        # The cumulative debug branch used to lead with logName too (repeat-task_detail-log-name-
        # shown-twice: the round-4 fix landed only in the non-debug counts branch). It now checks
        # "debug" first: with a real count (metrics.py now counts the last 7 days of error lines
        # in a cumulative file instead of nulling them) it says so with no file name; with no
        # count (unreadable) it falls back to the same wording as any other unreadable log.
        '    logText: IIf(logName == "", IIf(Right(Lower(cmdPath), 16) == "run_taskdash.cmd",'
        ' "No taskdash_*.log found in its log folder", "No log found."),'
        ' IIf(Find("debug", Lower(logName)) > -1,'
        ' IIf(!IsEmpty($datapoint["latest_log_error_lines"]),'
        ' "error-only file kept across runs; "'
        ' + plural($datapoint["latest_log_error_lines"], "error line") + " in the last 7 days",'
        ' IIf(IsEmpty(logMins), "", "written " + fmtSpan(logMins) + " ago | ")'
        ' + "Error count not known (log could not be read)"),'
        ' IIf(!IsEmpty($datapoint["latest_log_error_lines"]),'
        ' IIf(IsEmpty(logMins), "", "written " + fmtSpan(logMins) + " ago | ")'
        ' + plural($datapoint["latest_log_error_lines"], "error line") + ", "'
        ' + plural($datapoint["latest_log_warning_lines"], "warning line"),'
        ' IIf(IsEmpty(logMins), "", "written " + fmtSpan(logMins) + " ago | ")'
        ' + "Error count not known (log could not be read)"))),\n'
        '    logPathText: logPath,\n'
        # The original build (93f811b) found that markup built in Arcade does not render as HTML,
        # so "; " becomes a bullet run. Whether Dashboards also decodes "&amp;" from Arcade is unproven:
        # see the esc() comment in FMT_HELPERS.
        f'    cfgLabel: IIf(cw == "", "", {_s(badge("ConfigWarning", "Setting notes") + " (not failures):")}),\n'
        '    cfgText: IIf(cw == "", "", "• " + esc(Replace(cw, "; ", " • "))),\n'
        # Published but shown nowhere until now (critic-02): the settings that a Setting note
        # warns about were invisible even after the note was read and acknowledged.
        '    settingsText: IIf(settingsDetail == "", "--", settingsDetail)\n'
        '  }\n'
        '};\n'
    )


def failure_list_expression() -> str:
    # Tier from run_status, not is_error: StartFailed, NotLoggedOn, KilledTimeLimit and
    # Terminated are failures too and each needs its own word. Unknown statuses fall to Error
    # because this list only holds is_error = 1 rows.
    known = {k: k for k in PALETTE}
    return FMT_HELPERS + (
        'var rs = DefaultValue($datapoint["run_status"], "Error");\n'
        f'var tier = {_decode("rs", known, _s("Error"))};\n'
        + palette_arcade()
        + 'var label = word;\n' + STATUS_TEXT + (
            # A legacy row's start is really the reconstructed END-event time, not the start: on
            # a long run it is minutes after the real start (round-1 R5 said only "minutes", but
            # verification found it is exactly end_time_utc, up to 41 min later than the true
            # start). Label it as the end time, not as an approximate start
            # (failures_trend-estimated-start-is-end-time).
            'var estStart = DefaultValue($datapoint["start_time_estimated"], 0) == 1;\n'
            'var ageMins = minsFromNow($datapoint["start_time_utc"]);\n'
            # Only a failure from the last 7 days is tinted: on a healthy day four pink cards of
            # old failures outweighed the all-OK tiles. Older cards keep the glyph and the word.
            # An unknown age stays tinted, so a failure never looks calmer than it is.
            'var recent = IsEmpty(ageMins) || Abs(ageMins) <= 10080;\n'
            'return {\n'
            '  textColor: "#4c4c4c", backgroundColor: IIf(recent, bg, ""), separatorColor: "#D6D6D6",\n'
            '  selectionColor: "#00304d", selectionTextColor: "#ffffff",\n'
            '  attributes: {\n'
            '    statusText: statusText,\n'
            '    startText: IIf(estStart, "ended about " + fmtDate($datapoint["end_time_utc"]),'
            ' fmtDate($datapoint["start_time_utc"])),\n'
            '    ageText: IIf(IsEmpty(ageMins), "", fmtSpan(Abs(ageMins)) + " ago"),\n'
            '    resultText: esc(DefaultValue($datapoint["result_text"], "")),\n'
            # A null duration (4 prod rows, start_time_estimated=1) rendered a bare "--", the mark
            # for missing data everywhere else, while Task detail already said "duration not
            # recorded" for the same value (failures_trend-null-duration-bare-dash).
            '    durationText: IIf(IsEmpty($datapoint["duration_seconds"]), "duration not recorded",'
            ' fmtDur($datapoint["duration_seconds"]))\n'
            '  }\n'
            '};\n')
    )


def collector_runs_expression() -> str:
    return FMT_HELPERS + (
        'var ex = Lower(DefaultValue($datapoint["exit_status"], ""));\n'
        'var tier = Decode(ex, "success", "Success", "failed", "Error", "");\n'
        + palette_arcade()
        + 'var label = IIf(tier == "", ex, word);\n' + STATUS_TEXT + (
            'return {\n'
            '  backgroundColor: IIf(tier == "Error", bg, ""),\n'
            '  cells: {\n'
            '    exit_status: {displayText: statusText, textColor: fg, backgroundColor: bg, textAlign: "center"},\n'
            '    started_utc: {displayText: fmtDateShort($datapoint["started_utc"]), textAlign: "left"},\n'
            '    duration_seconds: {displayText: fmtDur($datapoint["duration_seconds"]), textAlign: "right"},\n'
            '    events_read: {displayText: fmtN($datapoint["events_read"]), textAlign: "right"},\n'
            '    runs_written: {displayText: fmtN($datapoint["runs_written"]), textAlign: "right"},\n'
            '    inventory_written: {displayText: fmtN($datapoint["inventory_written"]), textAlign: "right"},\n'
            '    metrics_written: {displayText: fmtN($datapoint["metrics_written"]), textAlign: "right"},\n'
            '    alert_sent: {displayText: IIf(DefaultValue($datapoint["alert_sent"], 0) == 1, "Yes", "No"), textAlign: "center"},\n'
            '    host_name: {displayText: DefaultValue($datapoint["host_name"], ""), textAlign: "left"},\n'
            # 300, not 120: the stored value holds up to 2,000 characters, and the help now says
            # where to read the rest (collector_tab-10).
            '    error_text: {displayText: shortText(DefaultValue($datapoint["error_text"], ""), 300), textAlign: "left"}\n'
            '  }\n'
            '};\n')
    )


# TaskChanges rows keep the attention_reason text of the day they were written (retention 365
# days, newest 50 shown), so wording that publish no longer writes stays on screen for weeks.
# Older rows hold ".; " where two sentences were joined, and "about 0.0 times the usual" for a
# run under 5% of its usual time ("0.0" only means below 0.05, so "under 5%" is the honest form).
# Only a low duration anomaly (ratio under 0.33) wrote 0.0-0.3.
OLD_SUMMARY_FIXES = ((".; ", "; "), ("about 0.0 times the usual", "under 5% of the usual"),
                     ("about 0.1 times the usual", "about 10% of the usual"),
                     ("about 0.2 times the usual", "about 20% of the usual"),
                     ("about 0.3 times the usual", "about 30% of the usual"),
                     ("Running now. Status is from the previous run: ", ""))


def _tidy_old_summary(field: str) -> str:
    expression = f'DefaultValue($datapoint["{field}"], "")'
    for old, new in OLD_SUMMARY_FIXES:
        expression = f"Replace({expression}, {_s(old)}, {_s(new)})"
    return expression


def changes_list_expression() -> str:
    tiers = {k: status for k, (_, status) in CHANGE_KINDS.items() if status}
    words = {k: word for k, (word, _) in CHANGE_KINDS.items()}
    unknown_word = 'Proper(Lower(Replace(ck, "_", " ")), "firstword")'  # a new change_kind still shows as a word
    return FMT_HELPERS + (
        'var ck = DefaultValue($datapoint["change_kind"], "");\n'
        f'var tier = {_decode("ck", tiers, _s(""))};\n'
        + palette_arcade()
        + f'var label = {_decode("ck", words, unknown_word)};\n' + STATUS_TEXT + (
            f'var oldS = esc({_tidy_old_summary("old_summary")});\n'
            f'var newS = esc({_tidy_old_summary("new_summary")});\n'
            'return {\n'
            '  textColor: "#4c4c4c", backgroundColor: bg, separatorColor: "#D6D6D6",\n'
            '  selectionColor: "#00304d", selectionTextColor: "#ffffff",\n'
            '  attributes: {\n'
            '    kindText: statusText,\n'
            '    whenText: fmtDate($datapoint["changed_utc"]),\n'
            # A recovered row (newS == "") showed the old failure reason with no cue that it was
            # over: "Was: " marks it as past (collector_tab-recovered-row-shows-old-failure).
            # A cleared note (ck == config_warning_cleared) now carries a reason in new_summary;
            # without this branch it fell into the arrow form and looked like a live "To fix"
            # instruction turned into a sentence (round7 collector_interactions-cleared-note-arrow-format).
            '    summaryText: When(oldS == "" && newS == "", "", ck == "config_warning_cleared" && oldS != "",'
            ' "Was: " + oldS + IIf(newS == "", "", ". " + newS), oldS == "", newS, newS == "", "Was: " + oldS,'
            ' oldS + " -> " + newS)\n'
            '  }\n'
            '};\n')
    )


def config_notes_expression() -> str:
    # No per-row label: every row of this list is a setting note, and the title says so (X9).
    return FMT_HELPERS + (
        'var cw = DefaultValue($datapoint["config_warnings"], "");\n'
        'return {\n'
        f'  textColor: {_s(fg("ConfigWarning"))}, separatorColor: "#D6D6D6",\n'
        '  selectionColor: "#00304d", selectionTextColor: "#ffffff",\n'
        '  attributes: {\n'
        '    noteText: IIf(cw == "", "No setting note.", "• " + esc(Replace(cw, "; ", " • ")))\n'
        '  }\n'
        '};\n'
    )


def mobile_attention_expression() -> str:
    """statusText + a short reason, for the phone's list of tasks that need a look or are off.

    A phone tile gives only a count (interactions-mobile-no-attention-list); a disabled task
    has no attention_reason (get_health_status returns "Disabled" before the reason is built),
    so it needs its own reason text here.
    """
    return FMT_HELPERS + task_status_arcade() + (
        'var why = DefaultValue($datapoint["attention_reason"], "");\n'
        'var reasonText = "";\n'
        'if (kind != "" && why != "") {\n'
        '  var reasons = Split(why, "; ");\n'
        '  reasonText = esc(shortText(reasons[0], 90));\n'
        '}\n'
        'if (reasonText == "" && DefaultValue($datapoint["enabled"], 1) == 0) {\n'
        '  reasonText = "Disabled in Task Scheduler.";\n'
        '}\n'
        'return {\n'
        '  textColor: "#4c4c4c", backgroundColor: IIf(kind == "", "", bg), separatorColor: "#D6D6D6",\n'
        '  selectionColor: "#00304d", selectionTextColor: "#ffffff",\n'
        '  attributes: {\n'
        '    statusText: statusText,\n'
        '    reasonText: reasonText\n'
        '  }\n'
        '};\n'
    )


METRIC_LIST_EXPRESSION = FMT_HELPERS + (
    'return {\n'
    '  textColor: "#4c4c4c", separatorColor: "#D6D6D6",\n'
    '  selectionColor: "#00304d", selectionTextColor: "#ffffff",\n'
    '  attributes: {\n'
    '    valueText: fmtMetric($datapoint["metric_key"], $datapoint["metric_value"]),\n'
    '    whenText: fmtDate($datapoint["observed_at_utc"]),\n'
    '    metricText: esc(DefaultValue($datapoint["display_name"], "")),\n'
    '    logText: esc(DefaultValue($datapoint["log_file_name"], ""))\n'
    '  }\n'
    '};\n'
)

# The collector runs hourly: past 90 min it missed one full run, past 180 min two. The help
# text quotes these, so they live in one place.
HEARTBEAT_LATE_MINUTES = 90
HEARTBEAT_DOWN_MINUTES = 180
DEFAULT_COLLECTOR_INTERVAL_MINUTES = config_default("scheduler.collector_interval_minutes")
# scheduler.heartbeat_ignore_after_days default: a computer with no collector run for this long is silent.
DEFAULT_HEARTBEAT_IGNORE_DAYS = config_default("scheduler.heartbeat_ignore_after_days")


def heartbeat_thresholds(interval_minutes: float = DEFAULT_COLLECTOR_INTERVAL_MINUTES) -> Tuple[float, float]:
    """(Late, Down) in minutes: 1.5 and 3 collector intervals, so 90 and 180 for the hourly collector."""
    return 1.5 * interval_minutes, 3 * interval_minutes


# The oldest computer's name sits above the age, so a Late or Down tile says which computer to
# examine. A silent computer (no Collector runs row inside the ignore window) still counts in the
# age; the bottom line says how many there are, so a computer that cannot publish stays visible
# until it is retired.
_HEARTBEAT_DETAIL_ARCADE = (
    'var host = Text(DefaultValue($datapoint["oldest_host"], ""));\n'
    'var silent = Number(DefaultValue($datapoint["silent_count"], 0));\n'
    'var silentText = When(silent == 0, "", silent == 1, ", 1 computer silent",'
    ' ", " + Text(silent) + " computers silent");\n')


def heartbeat_expression(field: str, late: float = HEARTBEAT_LATE_MINUTES,
                         down: float = HEARTBEAT_DOWN_MINUTES, detail: bool = False) -> str:
    """The tile's Arcade. detail adds the oldest computer's name and the silent-computer count,
    which only the Arcade data source returns; the layer-statistic fallback has neither field."""
    minutes_until = f"{late if down >= 120 else 120:g}"
    late, down = f"{late:g}", f"{down:g}"  # "90", not "90.0": the hourly text stays byte-identical
    return (
        f'var raw = DefaultValue($datapoint[{_s(field)}], null);\n'
        'if (IsEmpty(raw)) {\n'
        # An indicatorWidget's Arcade returns a FLAT object (middleText/middleTextMaxSize/textColor).
        # `attributes: {...}` is the listWidget contract -- Dashboards ignores it here and the tile
        # renders blank. Verified against the three proven arcade indicators in dashboard_templates.
        f'  return {{textColor: {_s(fg("Error"))}, middleText: "no data", middleTextMaxSize: "small",'
        f' bottomText: {_s(badge("Error", "Down"))}}};\n'
        '}\n'
        # Subtract in raw epoch-millisecond space. Number(Now()) and the stored field are both
        # absolute UTC millis, so no timezone conversion is involved and the thresholds hold
        # across DST. This is the same idiom the proven 'Next Audit In' template uses.
        'var minutesSince = Round((Number(Now()) - Number(raw)) / 60000, 0);\n'
        f'var color = When(minutesSince < {late}, {_s(fg("Success"))}, minutesSince <= {down}, {_s(fg("Warning"))},'
        f' {_s(fg("Error"))});\n'
        f'var state = When(minutesSince < {late}, {_s(badge("Success", "OK"))}, minutesSince <= {down},'
        f' {_s(badge("Warning", "Late"))}, {_s(badge("Error", "Down"))});\n'
        # A Down collector reads in whatever unit keeps the number small: minutes past 120 read
        # as "4,320 min ago" with no context, so an operator had to convert it by hand (indicators-06).
        # Minutes show only in the OK band, and hours round UP to a tenth, so one displayed age never
        # carries two states: 180 min is "3.0 h" (Late), 181 min "3.1 h" (Down). That holds only when
        # Down is a multiple of 6 min, so a short interval (Down under 120) reads in minutes up to
        # 120, the same unit rule as heartbeat_states_text.
        f'var ageText = When(minutesSince < {minutes_until}, Text(minutesSince, "#,##0") + " min ago",'
        ' minutesSince < 2880, Text(Ceil(minutesSince / 6) / 10, "#,##0.0") + " h ago",'
        ' Text(minutesSince / 1440, "#,##0.0") + " days ago");\n'
        # "0 min ago" was the commonest healthy reading and looked like a placeholder.
        'if (minutesSince < 1) {\n'
        '  ageText = "just now";\n'
        '}\n'
        + (_HEARTBEAT_DETAIL_ARCADE if detail else '')
        + 'return {\n'
        '  textColor: color,\n'
        '  middleText: ageText,\n'
        '  middleTextMaxSize: "large",\n'
        + ('  topText: host,\n  topTextMaxSize: "small",\n  bottomText: state + silentText,\n'
           if detail else '  bottomText: state,\n')
        + '  bottomTextMaxSize: "medium"\n'
        '};\n'
    )


# MIN, not MAX: each run stamps every inventory row of its own host, so MIN is the stalest
# host. MAX hid a dead host behind a live one.
HEARTBEAT_EXPRESSION = heartbeat_expression("MIN_collected_at_utc")
HEARTBEAT_SOURCE_FIELD = "min_collected_at"
HEARTBEAT_HOST_FIELD = "oldest_host"
HEARTBEAT_SILENT_FIELD = "silent_count"
SUCCESS_SOURCE_FIELD = "lowest_success_rate_7d"


def _live_computers_script(portal_url: str, item_id: str,
                           runs_layer_id: int, ignore_after_days: float) -> str:
    """Arcade lines that fill `live`: the computers with a CollectorRuns row in the last
    ignore_after_days days. The server filters by the cutoff, so the script reads only the recent
    rows, not the whole table. The cutoff is whole minutes (at least 1) because DateAdd takes an
    integer."""
    cutoff_minutes = max(1, round(ignore_after_days * 1440))
    return (
        f'var runs = FeatureSetByPortalItem(Portal({_s(portal_url)}), {_s(item_id)}, {runs_layer_id},'
        ' ["host_name", "started_utc"], false);\n'
        f'var cutoff = DateAdd(Now(), -{cutoff_minutes}, "minutes");\n'
        'var live = {};\n'
        'for (var r in Filter(runs, "started_utc > @cutoff")) {\n'
        '  live[Lower(Text(r["host_name"]))] = true;\n'
        '}\n')


def _single_feature_script(fields: str, attributes: str) -> str:
    return ('return FeatureSet(Text({\n'
            f'  fields: [{fields}],\n'
            '  geometryType: "",\n'
            f'  features: [{{attributes: {{{attributes}}}}}]\n'
            '}));\n')


def heartbeat_source_script(portal_url: str, item_id: str, layer_id: int,
                            runs_layer_id: int = DEFAULT_LAYER_IDS[TABLE_COLLECTOR_RUNS],
                            ignore_after_days: float = DEFAULT_HEARTBEAT_IGNORE_DAYS) -> str:
    """Arcade data source returning one feature: the oldest collected_at_utc in epoch millis, the
    computer it belongs to, and how many computers are silent.

    Exists for refreshInterval: Now() in the tile's formatting Arcade is re-evaluated only when
    its data refreshes, and a plain layer dataset never refreshes on its own.

    Every computer in the inventory counts until `taskdash publish --retire-host` removes it, so a
    computer that cannot publish keeps the tile at Late or Down. A computer with no CollectorRuns
    row in the last ignore_after_days days is silent: a retired or renamed computer, or one whose
    runs cannot reach the service (a failed run keeps its row in a pending file on that computer).
    The tile shows how many are silent. It reads both tables on every refresh, so a retired
    computer drops out at the next load with no dashboard rebuild.
    """
    return (
        f'var fs = FeatureSetByPortalItem(Portal({_s(portal_url)}), {_s(item_id)}, {layer_id},'
        ' ["host_name", "collected_at_utc"], false);\n'
        + _live_computers_script(portal_url, item_id, runs_layer_id, ignore_after_days)
        + 'var ms = null;\n'
        'var oldest = null;\n'
        'var silent = {};\n'
        'for (var f in Filter(fs, "collected_at_utc IS NOT NULL")) {\n'
        '  var key = Lower(Text(f["host_name"]));\n'
        '  var t = Number(f["collected_at_utc"]);\n'
        '  if (!HasKey(live, key)) { silent[key] = true; }\n'
        '  if (IsEmpty(ms) || t < ms) { ms = t; oldest = Text(f["host_name"]); }\n'
        '}\n'
        + _single_feature_script(
            f'{{name: {_s(HEARTBEAT_SOURCE_FIELD)}, alias: "Oldest computer data", type: "esriFieldTypeDouble"}},'
            f' {{name: {_s(HEARTBEAT_HOST_FIELD)}, alias: "Oldest computer", type: "esriFieldTypeString"}},'
            f' {{name: {_s(HEARTBEAT_SILENT_FIELD)}, alias: "Silent computers", type: "esriFieldTypeInteger"}}',
            f'{HEARTBEAT_SOURCE_FIELD}: ms, {HEARTBEAT_HOST_FIELD}: oldest, '
            f'{HEARTBEAT_SILENT_FIELD}: Count(Keys(silent))')
    )


def success_rate_source_script(portal_url: str, item_id: str, layer_id: int,
                               runs_layer_id: int = DEFAULT_LAYER_IDS[TABLE_COLLECTOR_RUNS],
                               ignore_after_days: float = DEFAULT_HEARTBEAT_IGNORE_DAYS) -> str:
    """Arcade data source returning one feature: the lowest lowest_success_rate_7d stamp among the
    computers that are not silent (see heartbeat_source_script). A silent computer's stamp is a
    week old or more, so it describes no run in the 7-day window; the heartbeat reports it."""
    return (
        f'var fs = FeatureSetByPortalItem(Portal({_s(portal_url)}), {_s(item_id)}, {layer_id},'
        f' ["host_name", {_s(SUCCESS_SOURCE_FIELD)}], false);\n'
        + _live_computers_script(portal_url, item_id, runs_layer_id, ignore_after_days)
        + 'var low = null;\n'
        f'for (var f in Filter(fs, "{SUCCESS_SOURCE_FIELD} IS NOT NULL")) {{\n'
        f'  var v = Number(f[{_s(SUCCESS_SOURCE_FIELD)}]);\n'
        '  if (HasKey(live, Lower(Text(f["host_name"]))) && (IsEmpty(low) || v < low)) { low = v; }\n'
        '}\n'
        + _single_feature_script(
            f'{{name: {_s(SUCCESS_SOURCE_FIELD)}, alias: "Lowest task success (7d)", type: "esriFieldTypeDouble"}}',
            f'{SUCCESS_SOURCE_FIELD}: low')
    )


# Metric Change: the latest value of each series against the median of its earlier values. A
# grouped table cannot do this -- it has no "latest row" statistic, and Arcade on a grouped
# dataset errored every row -- so an Arcade data source computes it. Avg vs Peak over ~30 runs
# could not show a collapse: one night at 0 moved the average by ~3 % (audit D1).
# The collector's compute_metric_change uses the same two numbers for the MetricChange table.
METRIC_CHANGE_HISTORY = taskdash_metrics.METRIC_CHANGE_HISTORY        # earlier values in the median
METRIC_CHANGE_MIN_HISTORY = taskdash_metrics.METRIC_CHANGE_MIN_HISTORY  # fewer give no usual value
METRIC_CHANGE_DROP_PCT = -50      # "Far below usual" at this change or lower
# Below this usual value a drop is noise: Parcel Diff's change count goes 1 -> 0 on a quiet
# day. ponytail: one floor for every metric; a per-metric floor if a small count ever matters.
METRIC_CHANGE_MIN_USUAL = 10
# A series last seen this long before its task's newest value was missing from the latest log.
# Wider than the longest run (about 1 h), narrower than a daily gap.
METRIC_CHANGE_MISSING_HOURS = 12
# "Not in last run" needs the series in at least this share of its task's most-logged series.
METRIC_CHANGE_USUALLY_LOGGED = 0.8
# A rise of this much or more in a lower-is-better count is "Far over usual": unmatched parcels
# went 34 -> 79 in a week with no flag, because only a drop was ever flagged.
METRIC_CHANGE_RISE_PCT = 50
METRIC_CHANGE_FLAGS = ("Not in last run", "Far below usual", "Far over usual")
# A key that names a problem count (a fail/error/gap/duplicate tally) is lower-is-better: a drop
# never gets "Far below usual", the same amber flag as a real data loss. The Replica Watermark
# Monitor's 08:10 fix cut its over-threshold count 38 -> 12 by excluding 26 dead registrations --
# nothing was lost, but the rule flagged it as the only warning on the tab
# (metrics_tab-far-below-flags-an-improvement / wording-far-below-flags-good-news). Checked by
# substring on metric_key, not a fixed key list, so a new problem-count metric is covered too.
# The substrings are rules data (metric_change_lower_is_better); this is the same list object,
# so it holds whatever profiles load_config applied.
METRIC_CHANGE_LOWER_IS_BETTER = taskdash_metrics.METRIC_CHANGE_LOWER_IS_BETTER
METRIC_CHANGE_FIELDS = (
    ("task_name", "esriFieldTypeString", "Task"), ("task_full_name", "esriFieldTypeString", "Task and folder"),
    ("display_name", "esriFieldTypeString", "Metric"),
    ("metric_key", "esriFieldTypeString", "Metric key"), ("latest_value", "esriFieldTypeDouble", "Latest"),
    ("usual_value", "esriFieldTypeDouble", "Usual"), ("change_pct", "esriFieldTypeDouble", "Change (%)"),
    ("last_seen", "esriFieldTypeDate", "Last seen"), ("flag", "esriFieldTypeString", "Flag"),
    ("flag_rank", "esriFieldTypeInteger", "Flag rank"), ("host_name", "esriFieldTypeString", "Host"),
    # The row click that draws Metric Trend filters TaskMetrics.series_key with this value.
    ("series_key", "esriFieldTypeString", "Series"),
)


def metric_change_source_script(portal_url: str, item_id: str, layer_id: int) -> str:
    """Arcade data source: one feature per host + task + metric, flagged rows first.

    Reads every TaskMetrics row. It is the source until the service has the MetricChange table;
    metric_change_from_table_script gives the same rows from the small table."""
    # Median() on an array fails the whole data source in the Dashboards profile (bisected
    # 2026-09-28: "Data source does not exist or is inaccessible"), so the median is by Sort.
    median = (
        f'  if (Count(p) >= {METRIC_CHANGE_MIN_HISTORY}) {{\n'
        '    var sp = Sort(p, byNumber); var n = Count(sp); lowest = sp[0]; highest = sp[n - 1];\n'
        '    usual = IIf(n % 2 == 1, sp[Floor(n / 2)], (sp[n / 2 - 1] + sp[n / 2]) / 2);\n'
        '  }\n'
    )
    return (
        f'var fs = FeatureSetByPortalItem(Portal({_s(portal_url)}), {_s(item_id)}, {layer_id},'
        ' ["host_name", "task_name", "task_full_name", "metric_key", "display_name", "metric_value",'
        ' "observed_at_utc"], false);\n'
        'var rows = OrderBy(Filter(fs, "metric_value IS NOT NULL AND observed_at_utc IS NOT NULL"),'
        ' "host_name ASC, task_full_name ASC, metric_key ASC, observed_at_utc DESC");\n'
        # One pass: the first row of each series is its latest; the next rows are its history.
        # A finished series is pushed as a new dictionary, so no dictionary is changed in place.
        'var series = []; var sk = ""; var head = null; var prev = []; var prevAt = []; var allAt = [];\n'
        'for (var f in rows) {\n'
        '  var k = f["host_name"] + "|" + f["task_full_name"] + "|" + f["metric_key"];\n'
        '  if (k != sk) {\n'
        '    if (!IsEmpty(head)) { Push(series, {head: head, prev: prev, at: prevAt, times: allAt}); }\n'
        '    sk = k; prev = []; prevAt = []; allAt = [Number(f["observed_at_utc"])];\n'
        '    head = {host_name: f["host_name"], task_full_name: f["task_full_name"], task_name: f["task_name"],'
        ' display_name: f["display_name"], metric_key: f["metric_key"], metric_value: f["metric_value"],'
        ' seen: Number(f["observed_at_utc"]), series_key: k};\n'
        '  } else {\n'
        '    Push(allAt, Number(f["observed_at_utc"]));\n'
        f'    if (Count(prev) < {METRIC_CHANGE_HISTORY}) {{ Push(prev, f["metric_value"]);'
        ' Push(prevAt, Number(f["observed_at_utc"])); }\n'
        '  }\n'
        '}\n'
        'if (!IsEmpty(head)) { Push(series, {head: head, prev: prev, at: prevAt, times: allAt}); }\n'
        # The shared window, as taskdash_metrics.compute_metric_change defines it: it ends at the series'
        # own newest value and starts at the newest start among the task's series that hold a full
        # history before then. A series is counted by the earlier values inside that window, so a metric
        # logged only on busy runs does not look as often logged as one logged on every run (TD-037),
        # and a metric that stopped keeps its count. inWin is a separate dictionary: no series is changed in place.
        'var inWin = {};\n'
        'for (var x in series) {\n'
        '  var xh = series[x]["head"]; var xt = xh["host_name"] + "|" + xh["task_full_name"];\n'
        '  var xa = series[x]["at"]; var xs = xh["seen"]; var ws = null;\n'
        '  for (var r in series) {\n'
        '    var rh = series[r]["head"]; var ra = series[r]["times"]; var older = 0; var cutoff = null;\n'
        '    for (var q in ra) {\n'
        f'      if (ra[q] < xs && older < {METRIC_CHANGE_HISTORY}) {{ older = older + 1; cutoff = ra[q]; }}\n'
        '    }\n'
        f'    if (older == {METRIC_CHANGE_HISTORY} && rh["host_name"] + "|" + rh["task_full_name"] == xt'
        ' && (IsEmpty(ws) || ws < cutoff)) { ws = cutoff; }\n'
        '  }\n'
        '  var cnt = 0;\n'
        '  for (var y in xa) {\n'
        '    if (IsEmpty(ws) || xa[y] >= ws) { cnt = cnt + 1; }\n'
        '  }\n'
        '  inWin[xh["series_key"]] = cnt;\n'
        '}\n'
    ) + _metric_change_rules_script('inWin[series[j]["head"]["series_key"]]',
                                    'inWin[series[i]["head"]["series_key"]]',
                                    'var p = series[i]["prev"]; ', median)


def metric_change_from_table_script(portal_url: str, item_id: str, layer_id: int) -> str:
    """Arcade data source: the same rows as metric_change_source_script, read from the MetricChange
    table. The collector stored each series' usual, lowest and highest value, so the browser reads
    one row per series instead of every TaskMetrics row."""
    names = ", ".join(_s(n) for n in ("host_name", "task_name", "task_full_name", "metric_key", "display_name",
                                     "series_key", "latest_value", "latest_at_utc", "usual_value",
                                     "lowest_value", "highest_value", "history_count"))
    return (
        f'var fs = FeatureSetByPortalItem(Portal({_s(portal_url)}), {_s(item_id)}, {layer_id}, [{names}], false);\n'
        'var series = [];\n'
        'for (var f in fs) {\n'
        '  Push(series, {head: {host_name: f["host_name"], task_full_name: f["task_full_name"],'
        ' task_name: f["task_name"], display_name: f["display_name"], metric_key: f["metric_key"],'
        ' metric_value: f["latest_value"], seen: Number(f["latest_at_utc"]), series_key: f["series_key"]},'
        ' n: f["history_count"], usual: f["usual_value"], lowest: f["lowest_value"],'
        ' highest: f["highest_value"]});\n'
        '}\n'
    ) + _metric_change_rules_script(
        'series[j]["n"]', 'series[i]["n"]', '',
        '  usual = series[i]["usual"]; lowest = series[i]["lowest"]; highest = series[i]["highest"];\n')


def _metric_change_rules_script(history_j: str, history_i: str, prep: str, stats: str) -> str:
    """The Arcade shared by both Metric Change sources, from the per-task newest times to the
    flagged rows. `series` holds one dictionary per metric, with its newest value in "head". The
    arguments are the parts that differ: how to count the earlier values (in the task loop j and
    the metric loop i), an extra declaration for the metric loop, and the statements that set
    usual, lowest and highest."""
    fields = ", ".join(f'{{name: {_s(n)}, alias: {_s(a)}, type: {_s(t)}}}' for n, t, a in METRIC_CHANGE_FIELDS)
    not_in_run, far_below, far_above = (_s(f) for f in METRIC_CHANGE_FLAGS)
    lower_is_better_check = " || ".join(f'Find({_s(s)}, mk) > -1' for s in METRIC_CHANGE_LOWER_IS_BETTER)
    return (
        # Per task: its newest value time, and how many values its most-logged series has.
        # Distinct names per loop: a second `var` of one name in one scope is not safe in Arcade.
        'var newest = {}; var most = {};\n'
        'for (var j in series) {\n'
        '  var hj = series[j]["head"]; var tk = hj["host_name"] + "|" + hj["task_full_name"];\n'
        f'  var t = hj["seen"]; var nj = {history_j} + 1;\n'
        '  if (!HasKey(newest, tk)) { newest[tk] = t; most[tk] = nj; }\n'
        '  else { if (newest[tk] < t) { newest[tk] = t; } if (most[tk] < nj) { most[tk] = nj; } }\n'
        '}\n'
        'function byNumber(a, b) { return a - b; }\n'
        'var feats = [];\n'
        'for (var i in series) {\n'
        f'  var h = series[i]["head"]; {prep}var tki = h["host_name"] + "|" + h["task_full_name"];\n'
        '  var latest = h["metric_value"]; var seen = h["seen"]; var mk = Lower(h["metric_key"]);\n'
        f'  var lowerIsBetter = {lower_is_better_check};\n'
        '  var usual = null; var lowest = null; var highest = null; var change = null; var flag = "";\n'
        f'{stats}'
        # A usual of 0 divides by zero, so change stayed null and rendered "--", the missing-data
        # mark, even where latest also 0 is really no change (wording-metric-change-dash-for-zero).
        '  if (!IsEmpty(usual) && usual != 0) { change = Round(100 * (latest - usual) / Abs(usual), 1); }\n'
        '  else if (!IsEmpty(usual) && usual == 0 && latest == 0) { change = 0; }\n'
        '  var gapHours = (newest[tki] - seen) / 3600000;\n'
        # Missing only counts for a series logged in most runs: a line printed only when there is
        # work (a sync that found no edits, a failure count) is absent on a quiet night by design.
        # history_i counts the series' earlier values inside the task's shared window, so a series
        # logged on every second run has half the count of the task's most-logged series.
        f'  if (gapHours > {METRIC_CHANGE_MISSING_HOURS} && {history_i} + 1 >= {METRIC_CHANGE_USUALLY_LOGGED} * most[tki]) {{'
        f' flag = {not_in_run}; }}\n'
        # A drop counts only below every earlier value: a count that is often this low (AP updates
        # on a quiet night) is normal, a first-ever low is news. Never for a lower-is-better key:
        # a smaller problem count is good news, not a loss (metrics_tab-far-below-flags-an-improvement).
        f'  else if (!IsEmpty(change) && change <= {METRIC_CHANGE_DROP_PCT} && usual >= {METRIC_CHANGE_MIN_USUAL}'
        f' && latest < lowest && !lowerIsBetter) {{ flag = {far_below}; }}\n'
        # The mirror rule for a problem count: a rise is the bad news. Same floor and same
        # "beyond every earlier value" test, so one noisy night in a jumpy series does not flag.
        f'  else if (!IsEmpty(change) && change >= {METRIC_CHANGE_RISE_PCT} && usual >= {METRIC_CHANGE_MIN_USUAL}'
        f' && latest > highest && lowerIsBetter) {{ flag = {far_above}; }}\n'
        # 0 flagged, 1 has a usual value, 2 does not: a monthly task can never reach
        # METRIC_CHANGE_MIN_HISTORY earlier values, so its "--" rows sorted first under the old
        # two-tier rank and filled the top of the table (metrics_tab-06).
        '  Push(feats, {attributes: {task_name: h["task_name"], task_full_name: h["task_full_name"],'
        ' display_name: h["display_name"],'
        ' metric_key: h["metric_key"], latest_value: latest, usual_value: usual, change_pct: change,'
        ' last_seen: seen, flag: flag,'
        ' flag_rank: IIf(flag != "", 0, IIf(IsEmpty(usual), 2, 1)), host_name: h["host_name"],'
        ' series_key: h["series_key"]}});\n'
        '}\n'
        # Sorted here, not by the dataset: no harvested Arcade dataset carries orderByFields.
        f'return OrderBy(FeatureSet(Text({{fields: [{fields}], geometryType: "", features: feats}})),'
        ' "flag_rank ASC, task_name ASC, display_name ASC");\n'
    )


def metric_change_expression() -> str:
    return FMT_HELPERS + (
        'var key = $datapoint["metric_key"];\n'
        'var ch = $datapoint["change_pct"];\n'
        'var flag = DefaultValue($datapoint["flag"], "");\n'
        # The median of an even count gave "356,919.5" (half a record) and a change computed on
        # unrounded values sat beside "Latest 5, Usual 5" as -7%. A whole latest value (a count,
        # or a duration that fmtMetric rounds) rounds the usual value too, and the shown change
        # is worked out from the two shown numbers, so the row always agrees with itself.
        'var lat = $datapoint["latest_value"]; var usu = $datapoint["usual_value"];\n'
        'if (!IsEmpty(lat) && !IsEmpty(usu) && (key == "duration_seconds" || lat == Round(lat))) {\n'
        '  lat = Round(lat); usu = Round(usu);\n'
        '  ch = IIf(usu != 0, 100 * (lat - usu) / Abs(usu), IIf(lat == 0, 0, null));\n'
        '}\n'
        # A change that rounds to 0 carries no sign: "+0%" read as a rise.
        'var chShown = "--";\n'
        'if (!IsEmpty(ch)) {\n'
        '  var chRound = Round(ch, 0);\n'
        '  chShown = IIf(chRound == 0, "0%", IIf(chRound > 0, "+", "") + Text(chRound, "#,##0") + "%");\n'
        '}\n'
        'return {cells: {\n'
        # The folder path tells apart two tasks of one name on one computer.
        '  task_name: {displayText: DefaultValue($datapoint["task_full_name"], DefaultValue($datapoint["task_name"], "")),'
        ' textAlign: "left"},\n'
        '  display_name: {displayText: DefaultValue($datapoint["display_name"], ""), textAlign: "left"},\n'
        '  latest_value: {displayText: fmtMetric(key, lat), textAlign: "right"},\n'
        '  usual_value: {displayText: fmtMetric(key, usu), textAlign: "right"},\n'
        '  change_pct: {displayText: chShown, textAlign: "right"},\n'
        '  last_seen: {displayText: fmtDateShort($datapoint["last_seen"]), textAlign: "left"},\n'
        '  host_name: {displayText: DefaultValue($datapoint["host_name"], ""), textAlign: "left"},\n'
        f'  flag: {{displayText: IIf(flag == "", "", {_s(sym_prefix("Warning"))} + flag),'
        f' textColor: {_s(fg("Warning"))}, backgroundColor: IIf(flag == "", "", {_s(bg("Warning"))}), textAlign: "left"}}\n'
        '}};\n'
    )


# The tile is the lowest per-task 7-day rate (taskdash_publish.stamp_success_rate), not a fleet
# average, so one chronically-failing task can no longer hide inside a 98%+ mean.
SUCCESS_OK_PCT = 90   # the tile and its help text both read these
SUCCESS_LOW_PCT = 70

# The uncapped, oldest-first query grew past a typical maxRecordCount on a 15-minute task
# (failures_trend-03). The cap and the title both read this.
DURATION_TREND_CAP = 200


def success_rate_expression(pct_arcade: str) -> str:
    ok, low = SUCCESS_OK_PCT, SUCCESS_LOW_PCT
    return pct_arcade + (
        'if (IsEmpty(pct)) {\n'
        f'  return {{textColor: {_s(fg("Disabled"))}, middleText: "--", middleTextMaxSize: "large"}};\n'
        '}\n'
        # Truncate, not round: a rounded 98.0% read "OK" while the unrounded 97.97 was "Low", so
        # the state and the displayed number must agree (indicators-success-rate-rounding-contradiction).
        'pct = Floor(pct * 10) / 10;\n'
        f'var color = When(pct >= {ok}, {_s(fg("Success"))}, pct >= {low}, {_s(fg("Warning"))}, {_s(fg("Error"))});\n'
        f'var state = When(pct >= {ok}, {_s(badge("Success", "OK"))}, pct >= {low}, {_s(badge("Warning", "Low"))},'
        f' {_s(badge("Error", "Poor"))});\n'
        'return {\n'
        '  textColor: color,\n'
        '  middleText: Text(pct, "#,##0.0") + "%",\n'
        '  middleTextMaxSize: "large",\n'
        # The rule sits under the value: the title "Lowest 7-day success (2+ failures)" was cut
        # off on desktop and phone (phase 0 TEST render), and a bare 100.0% next to the Most
        # failures chart read as a contradiction (one failure does not count here).
        '  bottomText: state + " · 2+ failures only",\n'
        '  bottomTextMaxSize: "medium"\n'
        '};\n'
    )


def count_tile_expression(out_field: str, status: str, word: str, size: str = "large") -> str:
    """Count tile: the word carries the state, so a zero never looks like an unlabelled number."""
    return (
        f'var n = DefaultValue($datapoint[{_s(out_field)}], 0);\n'
        'return {\n'
        f'  textColor: IIf(n > 0, {_s(fg(status))}, {_s(fg("Success"))}),\n'
        '  middleText: Text(n, "#,##0"),\n'
        f'  middleTextMaxSize: {_s(size)},\n'
        f'  bottomText: IIf(n > 0, {_s(badge(status, word))}, {_s(badge("Success", "None"))}),\n'
        '  bottomTextMaxSize: "medium"\n'
        '};\n'
    )


# --------------------------------------------------------------------------- help text (4.5)


def heartbeat_states_text(late: float, down: float) -> str:
    # In hours, matching the tile's own display past 120 min: minutes-only thresholds made the reader
    # convert "2.5 h ago" by hand to compare it with the rule (indicators-heartbeat-units-mismatch).
    # A short collector interval (Down under 2 h) reads better in minutes than as "0.375 h".
    divisor, unit = (60, "h") if down >= 120 else (1, "min")
    return (f"OK is less than {late / divisor:g} {unit}. Late is {late / divisor:g} to {down / divisor:g} {unit}. "
            f"Down is more than {down / divisor:g} {unit}.")


_HEARTBEAT_STATES = heartbeat_states_text(HEARTBEAT_LATE_MINUTES, HEARTBEAT_DOWN_MINUTES)
# Shared by both heartbeat tiles (Overview and Collector & Changes tabs) so their "What to do"
# cannot drift apart again (indicators-heartbeat-two-todo-texts): both read the same data.
_HEARTBEAT_TODO = ("If the tile shows Late or Down, find the newest Collector runs row of each "
                   "computer (Collector & Changes tab). Read its Error column. "
                   "If a computer has no new row, examine the Task Dash task and its log on that computer. "
                   f"If a computer is gone for good, retire it with {command_text('collect', '--retire-host')}.")
# Both heartbeat tiles show this, so the two "Shows" texts agree.
_HEARTBEAT_COMPUTERS = ("With more than one computer, the tile shows the computer that saved data longest ago, "
                        "and names it. A silent computer still counts until you retire it, "
                        "and the tile says how many are silent.")
# Both heartbeat tiles AND Metric change refresh live (indicators-heartbeat-only-tile-claim-false /
# collector_tab-heartbeat-help-drops-reload): shared so the two "Why it matters" texts cannot say
# different things about which widgets update on their own.
# The Collector Runs help sentence that alerting settings change. Alerting off replaces all of it;
# realert_hours replaces the 24; an unset realert_hours (no repeats) drops the repeat clause.
EMAIL_SENT_TEXT = ("E-mail sent is Yes when the run sent an e-mail. That happens when a problem started, "
                   "changed or cleared, or when a problem is still open after 24 hours. It is also Yes "
                   "for the 7 AM daily check, sent every day.")
NO_ALERTING_TEXT = "Alerting is off, so TaskDash sends no e-mail and this column is always No."
_HEARTBEAT_LIVE = ("Only the heartbeat tiles and Metric change update while the page is open. "
                   "Reload the page to update the rest.")
# name -> (Shows, Why it matters, What to do or None). Plain English (ASD-STE100): 20 words or
# fewer per sentence, one term per concept. Help names widgets by their shown titles and the
# selectors by "the Filters panel" -- the layout has no "above" and no tile numbers (X10).
HELP: Dict[str, Tuple[str, str, Optional[str]]] = {
    "Host": ("The computers that run the scheduled tasks.",
             "One computer can stop while the other computers work. This filter shows the tasks of one computer.",
             'Pick a computer to filter the tables, lists and charts. The tiles always show all computers.'),
    "Task": ("The names of the tasks on the picked computer.",
             "Most questions are about one task. This filter shows only the history and metrics of that task.",
             # Health and Schedule now narrow this list too (interactions-r3regress-status-schedule-no-task-cascade),
             # and Collector runs has no task field, so it is named as the one exception.
             "Pick a task to filter the tables, lists and charts, except Collector runs. "
             "Task detail and Duration trend also need a Task health row. "
             "The tiles always show all tasks."),
    "Health": ("The status of each task: OK, Failed, Warning, Running, Not yet run or Disabled. "
               "The Status column can show a more exact word. "
               # apply_stuck_or_blocked_warning sets health_status Warning for Stuck too
               # (indicators-health-help-stuck-under-running): the old text put Stuck under Running.
               "An Overdue, Stuck or Blocked task is under Warning.",
               "Use it to show only the failed tasks, or only the warnings.",
               "Pick a status to filter the Task health table, Task detail, Setting notes and the "
               "Most failures chart. The tiles always show all tasks."),
    "Schedule": ("The schedule groups, for example Hourly, Daily or Weekly.",
                 "It groups the tasks by how often they run. You can examine one group at a time.",
                 "Pick a group to filter the Task health table, Task detail, Setting notes and the "
                 "Most failures chart. The tiles always show all tasks."),
    "Metric": ('The names of the metrics that the tasks write to their logs, for example "Copied record count".',
               # A Metric pick alone narrows Metric change (and Metric range/Latest metric values):
               # the trend needs a Metric change row click, not just a Metric pick
               # (wording-metric-selector-help-stale-trend).
               "One task can write many metrics. This filter shows one metric across all tasks.",
               "Pick a metric to filter all widgets on the Workflow Metrics tab. "
               "Then select a row in Metric change to see its trend."),
    "Collector Heartbeat": (
        f"The time since the collector (the Task Dash task) last saved data. {_HEARTBEAT_STATES} "
        + _HEARTBEAT_COMPUTERS,
        # Metric change also refreshes on its own (indicators-heartbeat-only-tile-claim-false):
        # the old text said "Only this tile updates", though Metric change reads a live Arcade
        # source too. Both heartbeat tiles keep the reload sentence (collector_tab-heartbeat-help-drops-reload).
        "If this time is long, all other data on this dashboard is old. Examine this tile first. "
        f"{_HEARTBEAT_LIVE}",
        _HEARTBEAT_TODO),
    # The partition sentence is in "Shows", not "What to do": the phone view drops "What to do"
    # (indicators-mobile-partition-sentence-dropped), and a phone reader still needs to know that
    # Failed, Overdue and Other warnings add up to this count.
    "Needs Attention": ("The number of tasks that have a problem now. "
                        "Failed, Overdue and Other warnings add up to this number. "
                        "Each task counts in one tile only.",
                        "This number tells you how many tasks you must examine today.",
                        "Read the Why column of the Task health table."),
    "Failed (last run)": ("The number of enabled tasks whose last run failed.",
                          "A failed run is the most urgent problem. Repair these tasks first.",
                          "Failed tasks are at the top of the Task health table. Read their Why column."),
    # DEFAULT_OVERDUE_GRACE_MINUTES (taskdash_collect.py) is 15; hardcoded, not imported, because
    # this module does not import taskdash_collect (indicators-overdue-help-describes-missed-run /
    # wording-overdue-grace-vague: the old text named no grace time at all).
    "Overdue": ("The number of tasks that did not start within a grace time after their planned start. "
                "The grace time is 15 min, unless the dashboard owner set another time for all tasks or for one.",
                "An overdue task did not start on time, also when its last run was OK.",
                "Read the Why column of the Task health table for each overdue task."),
    "Other Warnings": (
        # "missed runs" also names what Overdue counts, and "a warning result" is a Task
        # Scheduler code the dashboard explained nowhere (indicators-overdue-help-describes-missed-run /
        # indicators-other-warnings-jargon). This tile is COUNT(OBJECTID) over one row per task,
        # so a task with 3 missed runs still adds only 1: "It also counts RUNS that Task
        # Scheduler reports as missed" read as if the tile counted runs, though every sentence
        # here counts tasks (indicators-other-warnings-help-says-runs). Each sentence stays at or
        # under 20 words (test_help_is_short_plain_and_points_at_real_controls), so the fix is
        # the one wrong word, not a rewrite into fewer, longer sentences.
        "The number of tasks with a different problem, for example a stuck or blocked run. "
        "It also counts tasks that Task Scheduler reports as missed, for example while the "
        "computer was off. It also counts tasks with no next run time, or a Windows warning. "
        "An example warning is a run stopped by a user or a task with no valid trigger.",
        "These tasks need a check, but no run failed.",
        "Read the Why column of the Task health table. It gives the problem."),
    "7-Day Success Rate": ("The rate of the task with the lowest share of successful runs in the "
                           "last 7 days. "
                           # Only tasks with 2+ failures in 7 days set the rate: one failure is
                           # already the Failed (last run) tile's job (indicators-lowest-rate-flags-single-failure).
                           "Only tasks with 2 or more failures in 7 days count. "
                           f"OK is {SUCCESS_OK_PCT}% or more. Low is {SUCCESS_LOW_PCT}% to {SUCCESS_OK_PCT}%. "
                           f"Poor is less than {SUCCESS_LOW_PCT}%. "
                           "The tile leaves out a silent computer, which the Collector heartbeat reports.",
                           "One failure does not count here, but the Most failures in 7 days chart still shows it. "
                           "With no such task the tile shows 100%.",
                           # Names the chart AND the table column, so the task that sets the rate
                           # can be found even if it falls off the chart's top 10
                           # (indicators-lowest-rate-not-traceable).
                           "If the tile shows Low or Poor, find the task in the Most failures in 7 days chart. "
                           "Or check the Task health table's Failures (7 days) column."),
    # "TaskDash" names the product; "the Task Dash task" names the Windows task that does the
    # monitoring (wording-r3regress-taskdash-two-spellings): this tile is about that task's count.
    "Tasks Tracked": ("The number of scheduled tasks that the collector (the Task Dash task) monitors.",
                      "A sudden decrease shows that a task was deleted or excluded from monitoring. "
                      "If a computer stops sending data, its tasks stay in this count. "
                      "The Collector heartbeat shows that problem.",
                      "If the number decreases and you did not remove a task, find the Task "
                      "removed rows in Recent changes. It is on the Collector & Changes tab."),
    "Disabled": ("The number of tasks that are off in Windows Task Scheduler.",
                 "A disabled task does not run, so it never shows as failed.",
                 "If a disabled task must run, enable it in Task Scheduler."),
    "Task Health": ("Every task, one row each: status and the reason for a problem. "
                    "It also shows the last and next run and failures in 7 days. "
                    "A task with no other problem shows its setting note count. "
                    "A setting note is a risk, not a failure. "
                    'A "log error" count shows the error lines in the newest log. '
                    "For a log file kept across runs, it counts the error lines of the last 7 days. "
                    "It does not change the status or the tiles. "
                    "The status comes from Task Scheduler. The failure counts come from the run history, "
                    "which starts when TaskDash first reads the event log. "
                    "On the first day a failed task can show 0 failures.",
                    "This is the full work list. Tasks with a problem are at the top.",
                    "Select a row to show that task in Task detail, Failed runs and Duration trend."),
    "Task Detail": ("All data for the selected task: schedule, last and next run, log, and script path.",
                    "It gives the account, the script and the log that you need to repair a task.",
                    "Status comes from the Windows result, not from the log. "
                    "If the log shows error lines, open the log."),
    # The count is in the subtitle: the desktop list holds 25, the phone list 10. The Why text is
    # shared with the phone view, which has no Task health table, so it names no widget
    # (interactions-mobile-text-points-at-desktop-controls).
    "Recent Failures": ("The newest failed runs of all tasks in the saved history (up to 90 days). "
                        "The history starts when TaskDash first reads the event log. "
                        "On the first day this list can be empty, even for a task that Windows reports as failed.",
                        "This list shows if a task fails once or every night.",
                        "Pick a task in the Filters panel to show only its failures. "
                        "The Schedule and Status filters do not change this list. "
                        "A pink card is a failure from the last 7 days. A white card is older."),
    "Top Failing Tasks": ("The 10 tasks with the most failed runs in the last 7 days.",
                          "A task that fails again and again can look OK in Task health. This chart shows it.",
                          "Select a bar to filter the Task health table and Failed runs to that task."),
    "Duration Trend": (f"The duration of each successful run of the selected task, in seconds (newest {DURATION_TREND_CAP}).",
                       "A task that becomes slower each week can later miss its time window, also when it succeeds.",
                       None),
    "Metric Change": ("For each task and metric: the latest value, the usual value, and the change in percent. "
                      f"The usual value is the median of up to {METRIC_CHANGE_HISTORY} earlier values. "
                      f"A metric needs {METRIC_CHANGE_MIN_HISTORY} earlier values to have a usual value.",
                      "A task can end with no error but process much less data than usual. This table shows that decrease.",
                      # The wording must match the Arcade exactly: a visible drop with no flag
                      # read as a missed alarm (metrics_tab-01) before "and lower than every
                      # earlier value" was added here. "Logs in most runs" did not match the
                      # METRIC_CHANGE_USUALLY_LOGGED share the Arcade actually checks
                      # (metrics_tab-04): the help now reads the same constant. "every earlier
                      # value" was still not exact: the Arcade compares only the last
                      # METRIC_CHANGE_HISTORY values, so a series with more than that many
                      # readings could be flagged though an older value was lower
                      # (wording-metric-change-help-approximate).
                      f'"{METRIC_CHANGE_FLAGS[1]}": {-METRIC_CHANGE_DROP_PCT}% or more below a usual value '
                      f'of {METRIC_CHANGE_MIN_USUAL} or more. The value must also be lower than each '
                      f'of the last {METRIC_CHANGE_HISTORY} earlier values. '
                      'A problem count is never flagged for a drop, because a lower count is good news. '
                      'Problem counts are failures, unmatched or blank items, gaps or chains over a '
                      'limit, and stale registrations. '
                      f'"{METRIC_CHANGE_FLAGS[2]}": a problem count {METRIC_CHANGE_RISE_PCT}% or more over a usual value '
                      f'of {METRIC_CHANGE_MIN_USUAL} or more. The count must also be higher than each '
                      f'of the last {METRIC_CHANGE_HISTORY} earlier values. '
                      f'"{METRIC_CHANGE_FLAGS[0]}": a metric that the task logs in at least '
                      f'{int(METRIC_CHANGE_USUALLY_LOGGED * 100)}% of the task\'s recent runs. '
                      "It is missing from its newest log. Flagged rows are at the top. Select a row to see its trend."),
    "Metric Summary": ("For each task and metric: the lowest, average and highest value, and the number of times logged.",
                       "A lowest value of 0 for a record count can show a run that loaded nothing. "
                       "For change and error counts, 0 is normal.",
                       "For the newest values, read Latest metric values."),
    "Metric Trend": ("The values of one metric over time, for one task. "
                     # "Script time" named the metric one way here and "Script-reported time
                     # (seconds)" everywhere else (metrics_tab-script-time-two-names). The reason
                     # it can run shorter was also wrong: it is not the run's start-up alone, it
                     # is that a script often times only one step of its own work, while "took"
                     # spans the whole process including Python start-up and imports
                     # (repeat-wording-script-time-vs-took).
                     "\"Script-reported time (seconds)\" is the time the script logs for itself. "
                     "It can be much shorter than \"took\" in Task detail. Some scripts time only "
                     "one step of their work, and \"took\" also includes Python start-up.",
                     "A count that decreases slowly over weeks passes each daily check. This chart shows the slow decrease.",
                     "Select a row in Metric change to draw its line. The Host, Task and Metric filters "
                     "narrow Metric change first."),
    "Latest Metric Observations": ("The 60 newest metric values of all tasks.",
                                   "It shows the numbers that the newest runs logged. "
                                   "Tasks that run often fill most of the list.",
                                   "Pick a task in the Filters panel to show only its values."),
    # The Metric filter narrows this table too, to one metric of the selected task -- the old
    # text and the "All values of the selected task" title named only the Task filter
    # (critic-all-values-title-ignores-metric-filter).
    "Metric Observations": ("All logged values of the selected task, with the log file of each value. "
                            "The Metric filter narrows it further, to one metric.",
                            "It gives the exact value and the log that it came from.",
                            None),
    "Heartbeat": (f"The time since the collector (the Task Dash task) last saved data. {_HEARTBEAT_STATES} "
                  + _HEARTBEAT_COMPUTERS,
                  f"If this time is long, all other data on this dashboard is old. {_HEARTBEAT_LIVE}",
                  _HEARTBEAT_TODO),
    "Collector Runs": (
        "The 48 newest runs of the Task Dash task on all computers: the result, the duration and the rows sent. "
        "To read one computer, pick it in the Host filter. "
        "Task rows sent counts every row sent, new or unchanged. "
        "Metric rows sent counts the rows from log files that changed since the last run. "
        # Neither column was defined before, though they sit next to the two that were
        # (collector_tab-columns-not-defined).
        "Events read counts every Task Scheduler event read since the last run, also for unlisted tasks. "
        "Runs saved counts task runs added or updated. "
        # Named for what it means, not what it was called: "Alert sent" read as a problem
        # (collector_tab-alert-sent-undefined / wording-alert-sent-means-daily-email).
        f"{EMAIL_SENT_TEXT}",
        "A run can send an e-mail but not save the data. This table shows that, also when the heartbeat is OK.",
        # "<time>" parsed as an HTML tag and vanished (wording-collector-help-log-name-eaten);
        # help_html() now escapes every HELP string, and the text itself names no angle brackets.
        "For a Failed row, read the Error column. It shows the first 300 characters. "
        "The full error is in the Task Dash log on that computer, at the Started time. "
        "The file name is taskdash_YYYYMMDD_HHMMSS.log, in its log folder. "
        # A run that cannot reach ArcGIS Online saves its row on the host, and the next run that
        # can writes it (taskdash_publish._write_collector_run_row). Only a killed run leaves no row
        # (audit 0/9: the 2026-10-16 01:00 failure had no row, so the table showed only successes).
        "A run that cannot reach ArcGIS Online appears here after the next run that can. "
        "A run that Task Scheduler stopped has no row. A gap in Started shows it."),
    # The time is the collector's find time, not the edit time (operations-change-time-is-
    # detection-time / collector_tab-change-time-is-detection-time): the collector runs hourly,
    # so a change can show up to 1 h later than it happened. A cleared setting note can mean the
    # setting was fixed, OR that it was only acknowledged in workflow_descriptions.json, OR that
    # TaskDash stopped raising that check -- the row looks the same for all three
    # (operations-acknowledged-note-reads-as-fixed / wording-ack-reads-as-setting-fixed).
    "Recent Changes": ("The 50 newest changes to the tasks: added, removed, started failing, recovered, and more. "
                       "The time shown is when the collector found the change, up to 1 hour after it happened. "
                       "After an outage it can be later, and a change that began and ended in the outage has no row. "
                       "A \"Setting note removed\" row with settings unchanged means the problem went "
                       "away (for example a missing file was restored). It can also mean the note was "
                       "acknowledged, or TaskDash no longer raises it. It does not mean the setting was fixed. "
                       "The tasks that exist when TaskDash is installed are the baseline. "
                       "They are not listed as added, and the list stays empty until a task changes.",
                       "A changed task is a common cause of new failures. This list also keeps the recoveries.",
                       # Wording pinned by test_dashboard_phase4.test_help_never_points_at_a_control_the_view_lacks
                       # and test_round2_review.test_recent_changes_help_points_at_the_filters_panel.
                       "Pick a task in the Filters panel, or type its name in the search box of this list. "
                       "A removed task can be found only with the search box."),
    "Setting Notes": ("Each task with a setting note: a task setting that can cause a problem, for example a skipped run.",
                     "A setting note is a risk in the task setup, not a failure. "
                     "A setting that is off on purpose is not listed.",
                     # "the setting name" matched more than one note (collector_tab-ack-setting-
                     # name-hides-other-notes): MultipleInstancesPolicy is in three of them, so
                     # acknowledging it by that name hid all three, not just one.
                     "Read each note. If the setting is wrong, change it in Windows Task Scheduler. "
                     "If it is on purpose, ask the dashboard owner to accept it. The owner adds a word "
                     "that appears only in that note, for example StartWhenAvailable. "
                     "The word goes under acknowledge_config_warnings in the task descriptions file."),
    "Attention Tasks": ("The tasks that need attention now, or are disabled.",
                        "It names each task that the tiles count.",
                        None),
    # Does not repeat the panel text: that gives the facts, this says why the version is there.
    "About TaskDash": ("The version of TaskDash that built this dashboard.",
                       "A problem report is easier to match to a release when it names the version.",
                       None),
    "About Overview": ("A short note on what this dashboard is and what the tiles count.",
                       "A reader who opens only the Overview tab still learns what the tiles mean. "
                       "The full text is in About TaskDash on the Collector & Changes tab.",
                       None),
}

METRIC_TREND_PICKER_PROMPT = "Pick a Task and a Metric in the Filters panel to see the trend."
PROMPTS = {  # noFilterState text, shown on a requiresSelection target until something is picked
    # Dashboards draws a target only once EVERY source that requires a selection has one, so the
    # Task Health row click still gates these two even after a Task pick in Filters: the old
    # "narrows the table/chart only" parenthetical claimed a Task pick did something visible here,
    # but it draws nothing on its own (interactions-r2regress-duration-prompt-false / r2regress-critic-06).
    "Task Detail": "Select a row in Task health to see its detail.",
    "Duration Trend": "Select a row in Task health to see its run durations.",
    "Metric Trend": "Select a row in the Metric change table to see the trend of that metric.",
    # A short title, not just the prompt: the idle state hid topCaption entirely (critic-07).
    # The prompt replaces the table's top caption, so it carries the title: bare, the idle band
    # had no header and looked like a leftover (phase 0 design item 10). Plain text: Dashboards
    # drops the markup in a prompt, and <strong>..</strong><br> rendered as "taskPick" (TEST render).
    # A Metric change row click draws only Metric trend (audit 32), so the prompt says so.
    "Metric Observations": ("All values of the selected task. Open the Filters panel at the left edge and pick a task. "
                            "A click in Metric change does not select a task."),
}

# noDataState text (and noValueState on an indicator). Each one is true with or without a filter.
EMPTY_STATES = {
    "7-Day Success Rate": "No runs in the last 7 days.",
    "Task Health": "No task matches the filters. Clear a filter to see more tasks.",
    # True with or without the row/Task filter (failures_trend-07), and does not overstate the
    # kept history (interactions-90-day-claim: the purge limit is 90 days, not the data's age).
    # One retention phrase everywhere (wording-11): "saved history (up to 90 days)".
    # Named the filters that actually narrow this list (Host, Task): Schedule and Status do not,
    # though the old text's "or filters" implied they did (interactions-schedule-status-skip-failed-runs).
    # The empty state replaces the list's subtitle and search box, so the scope line moves here
    # (audit 28: Dashboards drops them when there is no data).
    "Recent Failures": ("No failed runs for the selected computer or task in the saved history (up to 90 days). "
                        "Schedule and Status do not filter this list."),
    # A filter can hide the selected task; Task Detail then has no row (audit 31).
    "Task Detail": "No detail to show. The filters may hide the selected task. Clear a filter or select another row.",
    # Notes the active filters: Host, Task and Schedule all narrow this chart
    # (failures_trend-top-empty-reads-global), so "no failed runs" alone read as a fleet all-clear.
    "Top Failing Tasks": "No failed runs in the last 7 days for the selected task or filters.",
    # "timed" was jargon and did not match Task detail's own wording for the same fact
    # (wording-duration-trend-empty-timed), which made a monthly task's genuine successful run
    # with no recorded duration read as a contradiction (failures_trend-r2regress-monthly-empty-state).
    "Duration Trend": "No successful runs with a recorded duration in the saved history (up to 90 days).",
    "Metric Change": "No metric values to show.",
    "Metric Summary": "No metric values to show.",
    "Metric Trend": "No values to show for this task and metric.",
    "Latest Metric Observations": "No metric values to show.",
    "Metric Observations": "This task has no logged metric values.",
    "Collector Runs": "No collector runs to show.",
    "Recent Changes": ("No task changes to show. The tasks that exist when TaskDash is installed are the "
                       "baseline, so only later changes appear here."),
    "Setting Notes": "No setting notes to show.",
    # Matches the widened title/filter below: the tile above counts only needs_attention, so a
    # lone disabled task used to leave an unexplained row under an empty-looking tile
    # (interactions-mobile-attention-title-vs-disabled).
    "Attention Tasks": "No task needs attention or is disabled.",
}

MOBILE_SUFFIX = " (mobile)"
# Their "What to do" points at a task selector, which only the Filters panel provides.
SELECTOR_TODO = {"Recent Failures", "Latest Metric Observations", "Recent Changes"}
# The phone view has no selectors and no row source, so the desktop empty state's "for the
# selected task or filters" named a control the phone does not have
# (failures_trend-mobile-empty-mentions-filters / interactions-mobile-text-points-at-desktop-controls).
MOBILE_EMPTY_STATES = {"Recent Failures": "No failed runs in the saved history (up to 90 days)."}
# The phone view has only 8 tiles and the two lists; it has no Most failures chart, no Metric
# change table, and only one heartbeat tile. Two "Why it matters" texts still named those
# desktop-only widgets (indicators-mobile-why-names-desktop-widgets); these mobile-only
# overrides replace them instead of pointing a phone reader at a control that is not there.
MOBILE_WHY = {
    "Collector Heartbeat": ("If this time is long, all other data on this dashboard is old. "
                            "Examine this tile first. Only this tile updates while the page is open. "
                            "Reload the page to update the rest."),
    "7-Day Success Rate": "One failure does not count here. With no such task the tile shows 100%.",
}


# The help text above is written for the reference install's setup. These are its site-specific literals; a
# config that names other values has them swapped in at build time, so the default text is
# unchanged (test_phase0_dashboard proves it byte for byte).
DEFAULT_RETENTION_DAYS = config_default("retention.runs_days")
NO_DAILY_EMAIL_TEXT = "There is no daily e-mail."
# install_task.ps1's -TaskName default, so a new install needs no config key to find its own task.
DEFAULT_COLLECTOR_TASK_NAME = config_default("scheduler.collector_task_name")
DEFAULT_GRACE_MINUTES = int(config_default("scheduler.overdue_grace_minutes"))  # whole minutes, as the Overdue help prints them


def help_terms(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Site values for the help text and the heartbeat thresholds, from the config.

    scheduler.collector_interval_minutes (default 60) sets Late at 1.5 and Down at 3 intervals;
    alerting.heartbeat_hour is the daily e-mail hour (unset = no daily e-mail, daily is None);
    alerting.enabled, alerting.to and alerting.realert_hours set the E-mail sent help;
    scheduler.overdue_grace_minutes the grace; retention.runs_days the saved-history days;
    scheduler.collector_task_name the Windows task that runs the collector.
    """
    config = config or {}
    scheduler, alerting = config.get("scheduler") or {}, config.get("alerting") or {}
    interval = scheduler.get("collector_interval_minutes", DEFAULT_COLLECTOR_INTERVAL_MINUTES)
    problem = config_range_problem("scheduler.collector_interval_minutes", interval)
    if problem:
        raise SystemExit(f"{problem}. Set it to the minutes between collector runs, or remove the key for 60.\n"
                         + see_troubleshooting("The collector interval is not above 0"))
    late, down = heartbeat_thresholds(interval)
    ignore_days = scheduler.get("heartbeat_ignore_after_days", DEFAULT_HEARTBEAT_IGNORE_DAYS)
    problem = config_range_problem("scheduler.heartbeat_ignore_after_days", ignore_days)
    if problem:
        raise SystemExit(f"{problem}. Set it to the days of silence after which the dashboard calls a "
                         "computer silent, or remove the key for 7.\n"
                         + see_troubleshooting("The heartbeat ignore time is not above 0"))
    hour = alerting.get("heartbeat_hour")
    recipients = alerting.get("to") or []
    recipients = [recipients] if isinstance(recipients, str) else recipients
    # Mirrors maybe_send_alert: nothing is sent unless alerting is enabled and has an address.
    sends_email = bool(alerting.get("enabled")) and any(str(r).strip() for r in recipients)
    realert = alerting.get("realert_hours", config_default("alerting.realert_hours"))
    realert = float(realert) if isinstance(realert, (int, float)) and not isinstance(realert, bool) else None
    daily = None if hour is None else f"{int(hour) % 12 or 12} {'AM' if int(hour) % 24 < 12 else 'PM'}"
    days = (config.get("retention") or {}).get("runs_days", DEFAULT_RETENTION_DAYS)
    metric_days = (config.get("retention") or {}).get("metrics_days", DEFAULT_RETENTION_DAYS)
    return {"late": late, "down": down, "ignore_days": ignore_days, "daily": daily,
            "sends_email": sends_email, "realert": realert,
            "days": int(days), "metric_days": int(metric_days),
            "grace": scheduler.get("overdue_grace_minutes", DEFAULT_GRACE_MINUTES),
            "collector": scheduler.get("collector_task_name") or DEFAULT_COLLECTOR_TASK_NAME}


def localize_retention(text: str, terms: Optional[Dict[str, Any]]) -> str:
    if not terms:
        return text
    # The metric caption describes TaskMetrics, which keeps retention.metrics_days, not runs_days.
    text = text.replace("of each metric in the saved history (up to 90 days)",
                        f"of each metric in the saved history (up to {terms.get('metric_days', terms['days'])} days)")
    return text.replace("(up to 90 days)", f"(up to {terms['days']} days)")


def localize_help(text: str, terms: Optional[Dict[str, Any]]) -> str:
    if not terms:
        return text
    collector = terms["collector"]
    interval = terms["late"] / 1.5
    delay = "1 hour" if interval == 60 else f"{interval / 1440:g} days" if interval >= 1440 else f"{interval:g} min"
    daily = terms["daily"]
    text = localize_retention(text, terms)
    if not terms["sends_email"]:
        text = text.replace(EMAIL_SENT_TEXT, NO_ALERTING_TEXT)
    elif terms["realert"] is None:
        text = text.replace(", or when a problem is still open after 24 hours", "")
    elif terms["realert"] != 24:
        hours = terms["realert"]
        text = text.replace("after 24 hours", f"after {hours:g} {'hour' if hours == 1 else 'hours'}")
    if daily is None:
        text = text.replace("It is also Yes for the 7 AM daily check, sent every day.", NO_DAILY_EMAIL_TEXT)
    return (text.replace(_HEARTBEAT_STATES, heartbeat_states_text(terms["late"], terms["down"]))
            .replace("up to 1 hour after it happened", f"up to {delay} after it happened")
            .replace("The grace time is 15 min", f"The grace time is {terms['grace']:g} min")
            .replace("for the 7 AM daily check", f"for the {daily} daily check")
            .replace("Task Dash task", f"{collector} task").replace("Task Dash log", f"{collector} log"))


def help_html(name: str, with_todo: bool = True, why: Optional[str] = None,
              terms: Optional[Dict[str, Any]] = None) -> str:
    # Escaped: "(taskdash_<time>.log)" once parsed as an HTML <time> element and the file name
    # vanished from the rendered help (wording-collector-help-log-name-eaten). No HELP string
    # holds intended markup, so escaping every one is safe. todo can be None (no "What to do").
    shows, why_text, todo = (html.escape(localize_help(t, terms), quote=False) if t else t for t in HELP[name])
    if why is not None:
        why_text = html.escape(why, quote=False)
    parts = [f"<p><strong>Shows:</strong> {shows}</p>", f"<p><strong>Why it matters:</strong> {why_text}</p>"]
    if todo and with_todo:
        parts.append(f"<p><strong>What to do:</strong> {todo}</p>")
    return "".join(parts)


def apply_help(widget: Dict[str, Any], terms: Optional[Dict[str, Any]] = None) -> None:
    """Help, prompt and empty-state text, each behind its own flag (F3/F4 are unproven)."""
    base = widget["name"].removesuffix(MOBILE_SUFFIX)
    if widget.get("topCaption"):
        widget["topCaption"] = localize_retention(widget["topCaption"], terms)
    if FEATURE_FLAGS["widget_description"] and base in HELP:
        # The mobile view has no selectors and no Task Health table, so every desktop "What to
        # do" (select a task, open the table) would point at something the phone does not show.
        mobile = widget["name"] != base
        no_selectors = base in SELECTOR_TODO and not FEATURE_FLAGS["sidebar_selectors"]
        why = MOBILE_WHY.get(base) if mobile else None
        widget["description"] = help_html(base, with_todo=not (mobile or no_selectors), why=why, terms=terms)
        widget["showDescription"] = True
    # Keep TaskDash's own 5.0.0 show-keys and add only `text`: pasting S-PAVE/S-EECA blocks
    # would bring showCaption/showDescription, the key names of another version.
    if FEATURE_FLAGS["no_filter_prompt_text"] and base in PROMPTS:
        prompt = PROMPTS[base]
        if base == "Metric Trend" and not FEATURE_FLAGS["cross_table_row_filter"]:
            prompt = METRIC_TREND_PICKER_PROMPT  # wire_events' fallback: the Task AND Metric pickers
        widget.setdefault("noFilterState", {})["text"] = f'<p style="text-align:center">{prompt}</p>'
    if FEATURE_FLAGS["no_data_text"] and base in EMPTY_STATES:
        mobile = widget["name"] != base
        empty = MOBILE_EMPTY_STATES.get(base) if mobile and base in MOBILE_EMPTY_STATES else EMPTY_STATES[base]
        empty = localize_retention(empty, terms)
        text = f'<p style="text-align:center">{empty}</p>'
        widget.setdefault("noDataState", {})["text"] = text
        # A MAX over rows that are all NULL returns one row with no value, so an indicator can
        # show noValueState rather than noDataState; which one drew the bare "No data" at the
        # 1920 px render is unproven, so both carry the text.
        if widget["type"] == "indicatorWidget":
            widget.setdefault("noValueState", {})["text"] = text


# --------------------------------------------------------------------------- widgets per tab

# Four tiles per column, so the edges line up. Left: the total and its three parts.
TILE_ROW_1 = ("Needs Attention", "Failed (last run)", "Overdue", "Other Warnings")
TILE_ROW_2 = ("Collector Heartbeat", "7-Day Success Rate", "Disabled", "Tasks Tracked")
# "Disabled" was missing: a disabled task changes no other mobile tile and never creates a
# failed run, so it was invisible on a phone (interactions-mobile-disabled-invisible).
# Two tiles side by side in a "col" stack, the way two harvested 5.1.0 mobile layouts pair their
# indicators. Dashboards scales the mobile heights to one screen (rendered 2026-09-28: at a 1.96
# total each 0.18 tile got ~80 px and "7 min ago" overlapped "OK"), so the heights sum to 1.0 and
# a tile row keeps ~110 px of an 860 px phone.
# Needs Attention, Failed, Overdue and Other Warnings are the tile and the three that add up to
# it (see indicator_tiles); the old pairing split them across two rows with Disabled and Tasks
# Tracked between them, so the three parts never sat next to the total (critic-05).
MOBILE_TILE_PAIRS = (("Collector Heartbeat", "7-Day Success Rate"), ("Needs Attention", "Failed (last run)"),
                     ("Overdue", "Other Warnings"), ("Disabled", "Tasks Tracked"))
MOBILE_TILES = tuple(name for pair in MOBILE_TILE_PAIRS for name in pair)
MOBILE_TILE_HEIGHT = 0.13
# Round-5 moved these from 0.20/0.28 to 0.32/0.16 as a fault-replay trade-off, but on a healthy
# day that left "Failed runs (newest 10)" showing only 1 of its 10 cards below an empty
# attention list holding 32% of the screen for one line (interactions-mobile-failed-runs-one-card).
# Equal halves show about 2 failure cards while the tile rows (4 x 0.13 = 0.52) are unchanged.
MOBILE_ATTENTION_HEIGHT = 0.24
MOBILE_FAILURES_HEIGHT = 0.24


def arcade_dataset(source_id: str, out_fields: List[str], max_features: Optional[int] = None) -> Dict[str, Any]:
    """The S-TRAF (5.1.0) dataset shape for an Arcade data source: no statistics, no ordering."""
    dataset: Dict[str, Any] = {
        "type": "serviceDataset", "name": "main",
        "dataSource": {"type": "arcadeDataSource", "itemId": source_id},
        "groupByFields": [], "orderByFields": [], "statisticDefinitions": [],
        "outFields": out_fields, "returnDistinctValues": False,
        "allowSourceDownload": False, "allowSummaryDownload": False,
    }
    if max_features is not None:
        dataset["maxFeatures"] = max_features
    return dataset


def add_arcade_source(arcade_items: List[Dict[str, Any]], name: str, script: str, refresh_minutes: int) -> str:
    source_id = new_id()
    arcade_items.append({"type": "arcadeItem", "itemId": source_id, "name": name,
                         "refreshInterval": refresh_minutes, "script": script})
    return source_id


def heartbeat_widget(item_id: str, layer_id: int, portal_url: str,
                     arcade_items: List[Dict[str, Any]],
                     late: float = HEARTBEAT_LATE_MINUTES, down: float = HEARTBEAT_DOWN_MINUTES,
                     runs_layer_id: int = DEFAULT_LAYER_IDS[TABLE_COLLECTOR_RUNS],
                     ignore_after_days: float = DEFAULT_HEARTBEAT_IGNORE_DAYS) -> Dict[str, Any]:
    widget = indicator(item_id, "Collector Heartbeat", layer_id, stat_field="collected_at_utc",
                       stat_type="min", expression=heartbeat_expression("MIN_collected_at_utc", late, down))
    if not FEATURE_FLAGS["heartbeat_arcade_refresh"]:
        return widget
    source_id = add_arcade_source(arcade_items, "TaskDash heartbeat (oldest computer data)",
                                  heartbeat_source_script(portal_url, item_id, layer_id,
                                                          runs_layer_id, ignore_after_days),
                                  HEARTBEAT_REFRESH_MINUTES)
    # The S-TRAF (5.1.0) shape for an indicator on an Arcade data source: valueType "feature",
    # one feature, no statistics, and valueField naming the feature's field (S-TRAF sets it on
    # every feature-mode indicator).
    widget["datasets"] = [arcade_dataset(
        source_id, [HEARTBEAT_SOURCE_FIELD, HEARTBEAT_HOST_FIELD, HEARTBEAT_SILENT_FIELD], max_features=1)]
    widget["valueType"] = "feature"
    widget["valueField"] = HEARTBEAT_SOURCE_FIELD
    widget["expression"] = heartbeat_expression(HEARTBEAT_SOURCE_FIELD, late, down, detail=True)
    return widget


def success_rate_widget(item_id: str, layer_id: int, portal_url: str,
                        arcade_items: List[Dict[str, Any]],
                        runs_layer_id: int = DEFAULT_LAYER_IDS[TABLE_COLLECTOR_RUNS],
                        ignore_after_days: float = DEFAULT_HEARTBEAT_IGNORE_DAYS) -> Dict[str, Any]:
    # Every host stamps the fleet-wide lowest-per-task value (taskdash_publish.stamp_success_rate).
    # MIN, not MAX: the tile must err toward the alarm, not toward the healthiest stamp. A silent
    # computer keeps its last stamp for ever, so the Arcade source leaves it out (the heartbeat
    # reports it). A widget-side per-task ratio is not possible: an indicator surfaces one statistic
    # to $datapoint, and $reference was never render-proven.
    widget = indicator(item_id, "7-Day Success Rate", layer_id, stat_field="lowest_success_rate_7d",
                       stat_type="min",
                       expression=success_rate_expression('var pct = $datapoint["MIN_lowest_success_rate_7d"];\n'))
    if not FEATURE_FLAGS["heartbeat_arcade_refresh"]:
        return widget
    source_id = add_arcade_source(arcade_items, "TaskDash lowest success rate (active computers)",
                                  success_rate_source_script(portal_url, item_id, layer_id,
                                                             runs_layer_id, ignore_after_days),
                                  HEARTBEAT_REFRESH_MINUTES)
    widget["datasets"] = [arcade_dataset(source_id, [SUCCESS_SOURCE_FIELD], max_features=1)]
    widget["valueType"] = "feature"
    widget["valueField"] = SUCCESS_SOURCE_FIELD
    widget["expression"] = success_rate_expression(f'var pct = $datapoint[{_s(SUCCESS_SOURCE_FIELD)}];\n')
    return widget


# Shown titles in sentence case, like every other title; the names stay the stable keys.
TILE_TITLES = {"Collector Heartbeat": "Collector heartbeat", "Needs Attention": "Needs attention",
               "Other Warnings": "Other warnings",
               "7-Day Success Rate": "Lowest 7-day success",
               # "(all)": the tiles ignore every filter, and this says so on screen.
               "Tasks Tracked": "Tasks tracked (all)"}


def indicator_tiles(item_id: str, layers: Dict[str, int], portal_url: str,
                    arcade_items: List[Dict[str, Any]],
                    late: float = HEARTBEAT_LATE_MINUTES, down: float = HEARTBEAT_DOWN_MINUTES,
                    ignore_after_days: float = DEFAULT_HEARTBEAT_IGNORE_DAYS) -> Dict[str, Dict[str, Any]]:
    """The 8 tiles. No selector ever targets them, so a filter cannot hide a failure here.

    Failed, Overdue and Other Warnings partition Needs Attention by attention_kind, which
    enrich_inventory sets to exactly one kind per needs_attention row: their sum equals it, by
    construction. Other Warnings also takes a NULL kind: SQL drops NULL from NOT IN, and a row
    stays NULL after --migrate until its host collects again (for good on a retired host).
    """
    inv = layers[TABLE_INVENTORY]
    unset_or_other = {"type": "filterGroup", "condition": "OR", "rules": [
        rule("attention_kind", None, operator="is_null"),
        rule("attention_kind", ["FAILED", "OVERDUE"], operator="is_not_in")]}

    # Needs attention is the one large number; its three parts and Disabled draw smaller, and
    # Tasks tracked draws small and grey so it is not the biggest figure on the page.
    def count_tile(name, where, status, word, size="medium"):
        return indicator(item_id, name, inv, where=where,
                         expression=count_tile_expression("COUNT_OBJECTID", status, word, size))

    tasks_tracked_expression = (
        'return {\n'
        f'  textColor: {_s(fg("Disabled"))},\n'
        '  middleText: Text(DefaultValue($datapoint["COUNT_OBJECTID"], 0), "#,##0"),\n'
        '  middleTextMaxSize: "medium"\n'
        '};\n')
    tiles = {w["name"]: w for w in (
        heartbeat_widget(item_id, inv, portal_url, arcade_items, late, down,
                         layers[TABLE_COLLECTOR_RUNS], ignore_after_days),
        count_tile("Needs Attention", [rule("needs_attention", 1)], "Warning", "Open", "large"),
        count_tile("Failed (last run)", [rule("attention_kind", "FAILED")], "Error", PALETTE["Error"][0]),
        count_tile("Overdue", [rule("attention_kind", "OVERDUE")], "Warning", "Overdue"),
        count_tile("Other Warnings", [rule("needs_attention", 1), unset_or_other], "Warning", "Warning"),
        success_rate_widget(item_id, inv, portal_url, arcade_items, layers[TABLE_COLLECTOR_RUNS], ignore_after_days),
        indicator(item_id, "Tasks Tracked", inv, expression=tasks_tracked_expression),
        count_tile("Disabled", [rule("enabled", 0)], "Disabled", PALETTE["Disabled"][0]),
    )}
    for name, title in TILE_TITLES.items():
        tiles[name]["title"] = title
    return tiles


def duration_trend_scoped() -> bool:
    """A trend only exists with a control that scopes it to one task -- unscoped is unreadable."""
    return FEATURE_FLAGS["trend_charts"] and (
        FEATURE_FLAGS["sidebar_selectors"] or FEATURE_FLAGS["cross_table_row_filter"])


def recent_failures_list(item_id: str, layers: Dict[str, int], templates: Dict[str, Any], *,
                         max_features: int, name: str = "Recent Failures") -> Dict[str, Any]:
    widget = list_widget(
        item_id, name, layers[TABLE_RUNS], templates["list"],
        order_by=["start_time_utc DESC"], where=[rule("is_error", 1, TABLE_RUNS)],
        expression=failure_list_expression(), max_features=max_features,
        title="Failed runs",
        text='<p><span style="font-size:15px"><strong>{task_name}</strong></span> '
             '<strong>{expression/statusText}</strong></p>'
             '<p>{expression/startText} ({expression/ageText}) | {expression/durationText}</p>'
             '<p>{expression/resultText}</p>')
    # The cap sits in `title`, the only key proven to draw on a list; topCaption has only ever
    # been rendered on tables, and a silent cap is what check_caps_and_help exists to block.
    widget["title"] = f"Failed runs - newest {max_features}"
    return widget


def overview_widgets(item_id: str, layers: Dict[str, int], templates: Dict[str, Any],
                     hidden: Sequence[str] = ()) -> Dict[str, Dict[str, Any]]:
    inv, runs = layers[TABLE_INVENTORY], layers[TABLE_RUNS]
    widgets = [table_widget(
        item_id, "Task Health", inv,
        columns=[column("attention_kind", "Status"), column("task_name", "Task"),
                 column("attention_reason", "Why"), column("last_run_time", "Last run"),
                 column("next_run_time", "Next run"),
                 column("failures_last_7_days", "Failures (7 days)", align="right"),
                 column("host_name", "Host")],
        template=templates["table"],
        # A CRITICAL-logging task that exits 0 has no attention reason and sorted by name, below
        # the fold (health_table-log-error-rows-not-sorted-first), so log errors sort next. A NULL
        # count (log not readable, or CSC's cumulative debug file) sorts after the 0s: accepted,
        # a problem row above the fold matters more than name order at the bottom. A disabled
        # task shares a healthy task's 0/0/0 keys (health_table-disabled-not-sorted-first).
        order_by=["last_run_failed DESC", "is_overdue DESC", "needs_attention DESC",
                  "enabled ASC", "latest_log_error_lines DESC", "failures_last_7_days DESC",
                  "task_name ASC"],
        expression=task_table_expression(),
        top_caption='<p><strong>Task health</strong><br>'
                    '<span style="font-size:12px;">Tasks with a problem are at the top. '
                    'Select a row to see its detail below. '
                    "Run times show in your device's time zone. Schedules use the computer's clock.</span></p>")]
    task_detail = list_widget(
        item_id, "Task Detail", inv, templates["list"], order_by=["task_name ASC"],
        expression=detail_list_expression(hidden), title="Task detail",
        text='<p><span style="font-size:15px"><strong>{task_name}</strong></span> '
             '<strong>{expression/statusText}</strong> <span style="color:#595959">{host_name}</span></p>'
             '<p><strong>{expression/whyText}</strong></p>'
             '<p>{expression/descText}</p>'
             '<p><strong>Folder:</strong> {task_path}<br>'
             '<strong>Schedule:</strong> {expression/scheduleText}<br>'
             '<strong>Last run:</strong> {expression/lastText}<br>'
             '<strong>Next run:</strong> {expression/nextText}<br>'
             '<strong>Last 7 days:</strong> {expression/weekText}<br>'
             '{expression/anomalyText}</p>'
             '<p><strong>Runs as:</strong> {expression/userText}<br>'
             '<strong>Script:</strong> {expression/pathText}<br>'
             '<strong>Working folder:</strong> {expression/wdText}<br>'
             '<strong>Settings:</strong> {expression/settingsText}<br>'
             '<strong>Latest log:</strong> {expression/logText}<br>'
             '{expression/logPathText}</p>'
             f'<p style="color:{fg("ConfigWarning")}">{{expression/cfgLabel}} {{expression/cfgText}}</p>')
    # The template's showFilter=true put a full-width "Search..." box over one selected task's
    # own card (task_detail-08): nothing here is ever more than one row.
    task_detail["showFilter"] = False
    widgets.append(task_detail)
    failed_runs = recent_failures_list(item_id, layers, templates, max_features=25)
    if FEATURE_FLAGS["sidebar_selectors"]:
        # TaskRuns has no health_status or schedule_bucket, and a filter cannot chain through
        # Task health, so the Status and Schedule pickers cannot narrow this list. The title says
        # so, or it contradicts Most failures below it when one of them is set.
        failed_runs["topCaption"] = subtitle_html("Schedule and Status do not filter it.")
    widgets.append(failed_runs)
    # TaskInventory.failures_last_7_days, not TaskRuns: that table keeps 90 days and a filter
    # rule has no relative-date constraint here, so the 7-day window must arrive precomputed.
    top = serial_chart(item_id, "Top Failing Tasks", inv, "task_name", templates["serial_config"],
                       stat_field="failures_last_7_days", stat_type="sum", max_features=10,
                       where=[rule("failures_last_7_days", 0, operator="greater")],
                       title="Most failures in 7 days")
    top["title"] = "Most failures in 7 days - top 10"
    red = [163, 42, 42, 255]  # palette red fg; value labels carry the count, not the colour
    # A light fill with a solid outline: a count of 1 filled the whole chart with dark red. A bar
    # width cannot be capped (no proven key) and the chart cannot know its max, so the weight is
    # lower for every count; the outline and the value label keep the bar and its number clear.
    light_red = [*red[:3], 110]
    for series in top["chartConfig"]["series"]:
        series["fillSymbol"] = {"type": "esriSFS", "style": "esriSFSSolid", "color": light_red,
                                "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": red, "width": 1}}
    # The cloned category axis inherits characterLimit 11 from Schedule Mix (fine for its own
    # short bucket words); 26 of 27 task names cut to "911 Edit Ve..." at that limit (critic-05).
    # 32 let a long name wrap to 3 lines in the ~70 px label strip of the 390 px widget, so the
    # lines of neighbouring bars overlapped; 24 keeps a label to 2 lines, then cuts with "...".
    top["chartConfig"]["axes"][0]["valueFormat"]["characterLimit"] = 24
    widgets.append(top)
    if duration_trend_scoped():
        # start_time_estimated rows are the 28.5% with sub-5 s durations; a failed run's brief
        # duration reads as a false dip the same way (failures_trend-02), so both are excluded.
        # Seconds, not minutes: every other widget (Task detail, Failed runs, Collector runs)
        # shows a duration in seconds, and a 2-decimal-minute axis flattened sub-minute runs to
        # 0.00-0.05 next to a "took < 1 s" card (failures_trend-r3regress-units-differ-from-detail).
        # The unit sits on the axis title, with the minute equivalent because Task detail says
        # "31 min 40 s" (audit 20); a static chart cannot switch unit per task. The cap sits in `title`, kept short so the 1920 px
        # header cannot clip it (failures_trend-r2regress-03-title-truncated).
        duration_trend = trend_chart(
            item_id, "Duration Trend", runs, "start_time_utc", "duration_seconds", y_title="Seconds (60 = 1 min)",
            where=[rule("is_completed", 1, TABLE_RUNS), rule("start_time_estimated", 0, TABLE_RUNS),
                   rule("is_success", 1, TABLE_RUNS)],
            max_features=DURATION_TREND_CAP, order_desc=True, title="Duration trend")
        duration_trend["title"] = f"Duration trend - newest {DURATION_TREND_CAP} runs"
        # Starts at 0: an axis that began at the data minimum drew a 17% change as a collapse.
        # Duration only: Metric trend shares the config, and a metric can be negative.
        # buffer False too: all 7 harvested axes that carry "minimum" have buffer False with
        # isLogarithmic/integerOnlyValues, none has buffer True. The live render ignored a
        # minimum beside buffer True and began at 1,500 s (audit 2/30). Cause inferred, not rendered.
        duration_axis = duration_trend["chartConfig"]["axes"][1]
        duration_axis.update({"minimum": 0, "buffer": False, "integerOnlyValues": False, "isLogarithmic": False})
        widgets.append(duration_trend)
    return {w["name"]: w for w in widgets}


METRIC_CHANGE_REFRESH_MINUTES = 60  # TaskMetrics changes at most once per hourly collector run


def metric_change_table(item_id: str, layer_id: int, templates: Dict[str, Any], portal_url: str,
                        arcade_items: List[Dict[str, Any]],
                        change_layer_id: Optional[int] = None) -> Dict[str, Any]:
    """Latest value against the usual value, per task and metric, on an Arcade data source.

    The Host, Task and Metric pickers narrow it, and a row click draws that metric's line in
    Metric Trend (both render-tested on TEST 2026-09-28). Flagged rows sort first. With
    change_layer_id, the layer of the MetricChange table, the source reads that small table
    instead of every TaskMetrics row.
    """
    script = (metric_change_source_script(portal_url, item_id, layer_id) if change_layer_id is None
              else metric_change_from_table_script(portal_url, item_id, change_layer_id))
    source_id = add_arcade_source(arcade_items, "TaskDash metric change (latest vs usual)", script,
                                  METRIC_CHANGE_REFRESH_MINUTES)
    widget = table_widget(
        item_id, "Metric Change", layer_id,
        columns=[column("task_name", "Task"), column("host_name", "Host"), column("display_name", "Metric"),
                 column("latest_value", "Latest", align="right"),
                 column("usual_value", "Usual", align="right"),
                 column("change_pct", "Change", align="right"),
                 column("last_seen", "Last seen"), column("flag", "Flag")],
        template=templates["table"], order_by=[], expression=metric_change_expression(),
        # flag_rank sorts the "--" rows (no usual value yet) last, but nothing said why they were
        # last until now (metrics_tab-r3regress-06-caption).
        top_caption='<p><strong>Metric change</strong><br><span style="font-size:12px;">'
                    'The latest value of each metric against its usual value. Flagged rows are at the top; '
                    'rows with no usual value (--) are at the bottom.'
                    '</span></p>')
    widget["datasets"] = [arcade_dataset(source_id, [name for name, _, _ in METRIC_CHANGE_FIELDS])]
    return widget


def metric_summary_table(item_id: str, layer_id: int, templates: Dict[str, Any]) -> Dict[str, Any]:
    # A grouped table renders natively only (see table_widget). It shows the range, not the
    # latest run: Metric Change answers "did the last run drop?" (audit D1).
    return table_widget(
        item_id, "Metric Summary", layer_id,
        columns=[column("task_full_name", "Task"), column("host_name", "Host"), column("display_name", "Metric"),
                 column("MIN_metric_value", "Lowest", align="right", decimals=2),
                 column("AVG_metric_value", "Average", align="right", decimals=2),
                 column("MAX_metric_value", "Highest", align="right", decimals=2),
                 column("COUNT_metric_value", "Times logged", align="right")],
        template=templates["table"], group_by=["task_full_name", "host_name", "display_name"],
        stats=[stat_def("metric_value", "min"), stat_def("metric_value", "avg"),
               stat_def("metric_value", "max"), stat_def("metric_value", "count")],
        order_by=["task_full_name ASC", "host_name ASC", "display_name ASC"],
        top_caption='<p><strong>Metric range</strong><br><span style="font-size:12px;">'
                    'The lowest, average and highest value of each metric in the saved history '
                    '(up to 90 days).</span></p>')


def metrics_widgets(item_id: str, layers: Dict[str, int], templates: Dict[str, Any], portal_url: str,
                    arcade_items: List[Dict[str, Any]],
                    hidden: Sequence[str] = ()) -> Dict[str, Dict[str, Any]]:
    met = layers[TABLE_METRICS]
    # Deliberately no "Observations by Metric"/"Observations by Task" charts here: they counted
    # how many rows the MONITOR collected, which is meta-data about TaskDash, not a signal about
    # any workflow. "Acme Stormwater: 152 observations" is not actionable.
    widgets = [metric_summary_table(item_id, met, templates)]
    if FEATURE_FLAGS["heartbeat_arcade_refresh"]:
        # The MetricChange layer id is in `layers` only when the service has that table.
        widgets.append(metric_change_table(item_id, met, templates, portal_url, arcade_items,
                                           layers.get(TABLE_METRIC_CHANGE)))
    # Metric Trend needs BOTH the Task and the Metric selector: one task logs several metrics,
    # and without the Metric pick the chart draws unrelated series on one line.
    if FEATURE_FLAGS["trend_charts"] and FEATURE_FLAGS["sidebar_selectors"]:
        # A fixed "Duration in seconds" axis title read wrong for every non-Duration metric
        # (critic-03); HELP['Metric Trend'] carries the seconds note instead.
        widgets.append(trend_chart(item_id, "Metric Trend", met, "observed_at_utc", "metric_value",
                                   y_title="Value",
                                   title="Metric trend"))
    latest_values = list_widget(
        item_id, "Latest Metric Observations", met, templates["list"],
        order_by=["observed_at_utc DESC"], expression=METRIC_LIST_EXPRESSION, max_features=60,
        title="Latest metric values",
        text='<p><span style="font-size:15px"><strong>{task_name}</strong></span> '
             '&mdash; {expression/metricText}</p>'
             '<p><span style="font-size:20px"><strong>{expression/valueText}</strong></span></p>'
             '<p style="color:#595959">{expression/whenText} | {expression/logText}</p>')
    latest_values["title"] = "Latest metric values - newest 60"
    widgets.append(latest_values)
    # No maxFeatures: the old 2,000 cap with task_name ASC silently dropped every task late in
    # the alphabet. The Task selector scopes it to one task's history instead.
    widgets.append(table_widget(
        item_id, "Metric Observations", met,
        columns=[column("task_name", "Task"), column("display_name", "Metric"),
                 column("metric_value", "Value", align="right"),
                 column("observed_at_utc", "Logged"), column("log_file_name", "Log file"),
                 *([] if "sample_line" in hidden else [column("sample_line", "Log line")])],
        template=templates["table"],
        # Sorted by the visible Metric column (display_name), not the hidden metric_key: the two
        # orders disagree once DISPLAY_NAMES renames a key, so the shown rows looked shuffled
        # (metrics_tab-09).
        order_by=["task_name ASC", "display_name ASC", "observed_at_utc DESC"],
        expression=FMT_HELPERS + (
            'return {cells: {\n'
            '  task_name: {displayText: DefaultValue($datapoint["task_name"], ""), textAlign: "left"},\n'
            '  display_name: {displayText: DefaultValue($datapoint["display_name"], ""), textAlign: "left"},\n'
            '  metric_value: {displayText: fmtMetric($datapoint["metric_key"], $datapoint["metric_value"]),'
            ' textAlign: "right"},\n'
            '  observed_at_utc: {displayText: fmtDateShort($datapoint["observed_at_utc"]), textAlign: "left"},\n'
            '  log_file_name: {displayText: DefaultValue($datapoint["log_file_name"], ""), textAlign: "left"}'
            + ('\n' if "sample_line" in hidden else ',\n'
            # The line a value was parsed from lets an operator check its meaning at a glance
            # (critic-07): several metric names turned out not to match their source line. 120
            # cut the value itself off a long log preamble + UNC path (metrics_tab-05); 300 keeps it.
               '  sample_line: {displayText: shortText(DefaultValue($datapoint["sample_line"], ""), 300),'
               ' textAlign: "left"}\n')
            + '}};\n'),
        # The Metric filter narrows this too, to one metric, though the title names only the task
        # (critic-all-values-title-ignores-metric-filter).
        top_caption='<p><strong>All values of the selected task</strong><br><span style="font-size:12px;">'
                    'One row for each logged value, with the log file that it came from. The '
                    'Metric filter narrows it further, to one metric.</span></p>'))
    return {w["name"]: w for w in widgets}


def collector_widgets(item_id: str, layers: Dict[str, int], templates: Dict[str, Any],
                      heartbeat: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    repeat = copy.deepcopy(heartbeat)  # this tab must stand alone; same data source, new id
    repeat.update(id=new_id(), name="Heartbeat", title="Collector heartbeat")
    runs = table_widget(
        item_id, "Collector Runs", layers[TABLE_COLLECTOR_RUNS],
        columns=[column("exit_status", "Status"), column("started_utc", "Started"),
                 column("duration_seconds", "Duration", align="right"),
                 column("events_read", "Events read", align="right"),
                 column("runs_written", "Runs saved", align="right"),
                 # "saved" read as "new": every row is re-sent and counted here, changed or not
                 # (collector_tab-02/wording-08). "Runs saved" keeps its old name; TaskRuns rows
                 # really are new or updated each pass, unlike the whole-table re-send of Task rows below.
                 column("inventory_written", "Task rows sent", align="right"),
                 column("metrics_written", "Metric rows sent", align="right"),
                 # "Alert sent" read as a problem alert; it is also Yes on the healthy 7 AM daily
                 # check e-mail (collector_tab-alert-sent-undefined / wording-alert-sent-means-daily-email).
                 column("alert_sent", "E-mail sent"), column("host_name", "Host"),
                 column("error_text", "Error")],
        template=templates["table"], order_by=["started_utc DESC"], max_features=48,
        expression=collector_runs_expression(),
        # "Two days of hourly runs" claimed coverage the 48 rows do not have on an outage
        # (wording-collector-caption-two-days): the collector runs hourly, but a gap in Started
        # still means it did not run for that stretch.
        top_caption='<p><strong>Collector runs</strong><br><span style="font-size:12px;">'
                    'Newest 48. Read Error for a failed run.</span></p>')
    changes = list_widget(
        item_id, "Recent Changes", layers[TABLE_TASK_CHANGES], templates["list"],
        order_by=["changed_utc DESC"], expression=changes_list_expression(), max_features=50,
        title="Recent changes",
        text='<p><strong>{expression/kindText}</strong> {task_name} '
             '<span style="color:#595959">{host_name}</span></p>'
             '<p>{expression/whenText}</p><p style="color:#595959">{expression/summaryText}</p>')
    changes["title"] = "Recent changes - newest 50"
    notes_where = [rule("config_warnings", None, operator="is_not_empty")] if FEATURE_FLAGS["config_notes_filter"] else None
    notes = list_widget(
        item_id, "Setting Notes", layers[TABLE_INVENTORY], templates["list"],
        order_by=["task_name ASC"], expression=config_notes_expression(), where=notes_where,
        title=badge('ConfigWarning', 'Setting notes'),
        text='<p><strong>{task_name}</strong> <span style="color:#595959">{host_name}</span></p>'
             '<p>{expression/noteText}</p>')
    notes["topCaption"] = subtitle_html("Risks in the task setup, not failures")
    return {w["name"]: w for w in (repeat, runs, changes, notes)}


ABOUT_NAME = "About TaskDash"
ABOUT_TEXT_STYLE = "color:#4c4c4c;font-size:12px;"
# The demo's dashboard shows invented failures, so a screenshot of it must say so on its face.
DEMO_NOTE = "<strong>DEMO - invented data.</strong> Nothing here comes from a real computer."


def about_widget(terms: Dict[str, Any], demo: bool = False) -> Dict[str, Any]:
    """Rich-text panel: the version, what TaskDash does, and how fresh the data is.

    Keys are those of the saved 5.1.0 rich-text widgets. The title is a heading in the text:
    no saved rich-text widget has a `title` key. No host, path, address or item id: the
    dashboard may be shown outside the team. The interval comes from the same config key as
    the heartbeat thresholds (Late is 1.5 intervals). demo adds the line that marks invented data.
    """
    interval = f"{round(terms['late'] / 1.5, 2):g}"
    lines = (
        f"<strong>TaskDash {TASKDASH_VERSION}</strong>",
        *([DEMO_NOTE] if demo else []),
        "TaskDash checks whether each scheduled Windows task ran and did its job.",
        "It reads Task Scheduler events, the task settings, and the log of each script.",
        ("The demo data was published once. The Collector heartbeat shows that run." if demo else
         f"The collector runs every {interval} min. The Collector heartbeat shows its last run."),
        # The definition's timeZone is "system": Dashboards shows each time in the viewer's zone.
        "Run times show in your device's time zone. Schedules use the computer's clock.",
        "Overview: task health. Workflow Metrics: logged values. "
        "Collector &amp; Changes: collector runs, recent changes and setting notes.",
    )
    return _about_panel(ABOUT_NAME, lines, heading=ABOUT_NAME)


def _about_panel(name: str, lines: Iterable[str], heading: str = "") -> Dict[str, Any]:
    body = "".join(f'<p><span style="{ABOUT_TEXT_STYLE}">{line}</span></p>' for line in lines)
    title = f'<h4><span style="color:#4c4c4c;"><strong>{heading}</strong></span></h4>' if heading else ""
    return {"id": new_id(), "dashboardItemType": "widget", "name": name,
            "noDataState": {}, "noFilterState": {}, "focusModeEnabled": False, "datasets": [],
            "type": "richTextWidget", "valueType": "none", "showFeatureMenu": False,
            "text": f"{title}{body}"}


# A short About on the first tab: a reader who never opens Collector & Changes still sees what the
# dashboard is, what the tiles count and where the full text is (TD-145). Not on the phone view.
OVERVIEW_ABOUT_NAME = "About Overview"


def overview_about_widget(demo: bool = False) -> Dict[str, Any]:
    lines = (
        f"<strong>TaskDash {TASKDASH_VERSION}</strong> checks whether each scheduled Windows task ran and did its job.",
        *([DEMO_NOTE] if demo else []),
        "The tiles count all tasks and ignore every filter. Tasks with a problem are at the top of Task health.",
        "The Collector heartbeat shows how old the data is. "
        "The Collector &amp; Changes tab has the full About text.",
    )
    return _about_panel(OVERVIEW_ABOUT_NAME, lines)


# --------------------------------------------------------------------------- selectors + events

SELECTOR_FIELDS = (("Host", TABLE_INVENTORY, "host_name"), ("Task", TABLE_INVENTORY, "task_name"),
                   ("Health", TABLE_INVENTORY, "health_status"),
                   ("Schedule", TABLE_INVENTORY, "schedule_bucket"), ("Metric", TABLE_METRICS, "display_name"))
INVENTORY_TARGETS = ("Task Health", "Task Detail", "Top Failing Tasks")
# Health and Schedule read TaskInventory, same as Setting notes, but did not narrow it: with
# Schedule = Every 15 min, Setting notes still listed every task's note (interactions-filter-coverage-gaps).
HEALTH_SCHEDULE_TARGETS = INVENTORY_TARGETS + ("Setting Notes",)
TASK_TARGETS = INVENTORY_TARGETS + (
    "Metric Change", "Recent Failures", "Duration Trend", "Metric Summary", "Metric Trend", "Latest Metric Observations",
    "Metric Observations", "Recent Changes", "Setting Notes")
HOST_TARGETS = TASK_TARGETS + ("Collector Runs",)  # CollectorRuns has no task field
# Needs a Task in scope before it has data -- from a Task Health row click OR the Task selector,
# not only a row click (critic-06 found the old "empty until a task is picked" comment implied
# only the row source, though wire_events lets the Task selector narrow these on its own too).
TASK_SCOPED = {"Duration Trend", "Metric Trend", "Metric Observations"}


def selector_labels(name: str) -> Dict[str, Any]:
    """The Health selector shows the status column's words, not raw health_status values (X1).
    Its name stays "Health": the name is the key the wiring and the help text use.
    The label also names the tabs where the pick does something: the sidebar is shared by all
    tabs, and a pick with no visible effect reads as broken. Scopes follow wire_events: Status
    and Schedule reach HEALTH_SCHEDULE_TARGETS (Overview + Setting notes), Metric only the
    Workflow Metrics tab. Host and Task reach every tab."""
    if name == "Health":
        return {"label": "Status (Overview and Setting notes)",
                "label_overrides": {v: badge(v) for v in HEALTH_VALUES}}
    if name == "Schedule":
        # "Scheduled" is the bucket for a time trigger whose repeat the collector could not read.
        return {"label": "Schedule (Overview and Setting notes)",
                "label_overrides": {"Scheduled": "Scheduled (frequency not read)"}}
    if name == "Metric":
        return {"label": "Metric (Workflow Metrics)"}
    return {}


def selectors(item_id: str, layers: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    if not FEATURE_FLAGS["sidebar_selectors"]:
        return {}
    return {name: category_selector(item_id, name, layers[table], field, **selector_labels(name))
            for name, table, field in SELECTOR_FIELDS}


def target(widget: Dict[str, Any], requires: bool = False, join: Optional[str] = None) -> Dict[str, Any]:
    """One 5.x filter target. A cross-table target MUST carry fieldMap: keyed on OBJECTID it
    matches nothing in the other table (it rendered "No data")."""
    out: Dict[str, Any] = {"targetId": f"{widget['id']}#main", "by": "whereClause", "requiresSelection": requires}
    if join:
        out["fieldMap"] = [{"sourceName": join, "targetName": join}]
    return out


def filter_events(targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"type": "selectionChanged", "actions": [{"type": "filter", "targets": targets}]}]


def wire_events(widgets: Dict[str, Dict[str, Any]], sels: Dict[str, Dict[str, Any]]) -> None:
    cross = FEATURE_FLAGS["cross_table_row_filter"]
    row = [target(widgets["Task Detail"], FEATURE_FLAGS["detail_requires_selection"])]
    bar = [target(widgets["Task Health"])]
    if cross:
        # A row is a feature, so join on task_full_name: task_name repeats across folders. The
        # same full name on two hosts still matches both; a second fieldMap entry for host_name
        # would fix that, but none of 490 harvested fieldMaps has two entries (unproven shape).
        # The bar is grouped by task_name, so task_name is all it can emit.
        row.append(target(widgets["Recent Failures"], join="task_full_name"))
        bar.append(target(widgets["Recent Failures"], join="task_name"))
        if "Duration Trend" in widgets:
            row.append(target(widgets["Duration Trend"], True, "task_full_name"))
    widgets["Task Health"]["events"] = filter_events(row)
    widgets["Top Failing Tasks"]["events"] = filter_events(bar)
    # One line needs a task AND a metric, and a filter applies only its first fieldMap entry, so
    # the row click joins on series_key. It is the trend's only required source: the pickers
    # narrow Metric change first (a picker pair used to be the only way, and nobody found it).
    metric_row = []
    if cross and "Metric Change" in widgets and "Metric Trend" in widgets:
        metric_row = [target(widgets["Metric Trend"], True, "series_key")]
        widgets["Metric Change"]["events"] = filter_events(metric_row)
        # Said only where the click is wired: check_events refuses a "Select" caption without events.
        widgets["Metric Change"]["topCaption"] = widgets["Metric Change"]["topCaption"].replace(
            "are at the bottom.", "are at the bottom. Select a row to see its trend.")
    if not sels:
        return

    def targets(names: Iterable[str], join: str, required: Iterable[str] = ()) -> List[Dict[str, Any]]:
        return [target(widgets[n], n in required, join) for n in names if n in widgets]

    # Dashboards draws a target only when EVERY source that requires a selection has one. Duration
    # Trend required both the Task Health row and the Task selector, so a row pick alone never
    # drew it (render at 1920 px). Where the row is required, the Task selector only narrows.
    row_required = {t["targetId"] for t in row + metric_row if t["requiresSelection"]}
    task_scoped = {n for n in TASK_SCOPED if n in widgets and target(widgets[n])["targetId"] not in row_required}
    sels["Host"]["events"] = filter_events(
        [target(sels["Task"], join="host_name"), target(sels["Metric"], join="host_name")]
        + targets(HOST_TARGETS, "host_name"))
    sels["Task"]["events"] = filter_events(
        [target(sels["Metric"], join="task_name")] + targets(TASK_TARGETS, "task_name", task_scoped))
    # Health/Schedule exist only on TaskInventory, and Dashboards filters do not chain through
    # a widget, so these two cannot reach TaskRuns/TaskMetrics widgets at all.
    # Neither used to narrow the Task selector, so a Health or Schedule pick left every task in
    # the Task dropdown, including ones outside the picked group
    # (interactions-r3regress-status-schedule-no-task-cascade): joined the same way Host already
    # narrows Task, on the field the two selectors share with it (TaskInventory).
    sels["Health"]["events"] = filter_events(
        [target(sels["Task"], join="health_status")] + targets(HEALTH_SCHEDULE_TARGETS, "health_status"))
    sels["Schedule"]["events"] = filter_events(
        [target(sels["Task"], join="schedule_bucket")] + targets(HEALTH_SCHEDULE_TARGETS, "schedule_bucket"))
    # A Metric pick alone used to change nothing visible: only Metric Trend and Metric
    # Observations listened, and both also need a Task (metrics_tab-06). Metric range and
    # Latest metric values need no Task, so a Metric pick alone now narrows them.
    sels["Metric"]["events"] = filter_events(
        targets(("Metric Change", "Metric Trend", "Metric Observations", "Metric Summary",
                 "Latest Metric Observations"), "display_name", () if metric_row else {"Metric Trend"}))


# --------------------------------------------------------------------------- assembly

def item_el(widget_id: str, width: float, height: float = 1.0) -> Dict[str, Any]:
    return {"type": "itemLayoutElement", "id": widget_id, "width": width, "height": height}


VALID_ORIENTATIONS = {"row", "col"}


def stack(elements: List[Dict[str, Any]], orientation: str, width: float, height: float) -> Dict[str, Any]:
    if orientation not in VALID_ORIENTATIONS:
        raise ValueError(f"orientation must be one of {sorted(VALID_ORIENTATIONS)}, got {orientation!r}; "
                         "an unrecognized value makes Dashboards flatten the entire layout")
    return {"type": "stackLayoutElement", "id": new_id(), "orientation": orientation,
            "width": width, "height": height, "elements": elements}


def items(widgets: Dict[str, Dict[str, Any]], pairs: Iterable[Tuple[str, float]],
          *, vertical: bool = False) -> List[Dict[str, Any]]:
    """Layout items for the widgets that were built; a flag-dropped widget's share is shared out.
    The share is a width, or a height when `vertical` (items stacked in a "row" stack)."""
    kept = [(n, w) for n, w in pairs if n in widgets]
    total = sum(w for _, w in kept)
    return [item_el(widgets[n]["id"], 1, round(w / total, 4)) if vertical
            else item_el(widgets[n]["id"], round(w / total, 4)) for n, w in kept]


def rows(pairs: Iterable[Tuple[List[Dict[str, Any]], float]], orientation: str) -> List[Dict[str, Any]]:
    """Row stacks for the rows that have widgets; their heights are rescaled to sum to 1."""
    kept = [(els, h) for els, h in pairs if els]
    total = sum(h for _, h in kept)
    return [stack(els, orientation, 1, round(h / total, 4)) for els, h in kept]


def tab(name: str, content: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "groupLayoutElement", "id": new_id(), "width": 1, "height": 1,
            "tabName": name, "orientation": "row", "elements": [content]}


def desktop_layout(w: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    # A "col" stack lays its children side by side (widths share out); a "row" stack piles them
    # top to bottom (heights share out). The render at 1920 px showed four equal columns here,
    # which left Task Health's columns ~390 px. It now takes half the content width, with
    # Task Detail under it and Duration Trend moved under Top Failing Tasks.
    # Both columns use 0.4/0.2/0.2/0.2, so the tile edges line up and the first tile of each is tall.
    tile_heights = (0.4, 0.2, 0.2, 0.2)
    tiles = stack([stack(items(w, list(zip(TILE_ROW_1, tile_heights)), vertical=True), "row", 1, 0.5),
                   stack(items(w, list(zip(TILE_ROW_2, tile_heights)), vertical=True), "row", 1, 0.5)],
                  "col", 0.25, 1)
    overview = stack([
        tiles,
        stack(items(w, [("Task Health", 0.50), ("Task Detail", 0.35), (OVERVIEW_ABOUT_NAME, 0.15)],
                    vertical=True), "row", 0.5, 1),
        stack(items(w, [("Recent Failures", 0.40), ("Top Failing Tasks", 0.30), ("Duration Trend", 0.30)],
                    vertical=True), "row", 0.25, 1)], "col", 1, 1)
    # Tabs 2 and 3 use the INVERSE orientations of tab 1, and that is not a mistake: built with
    # tab 1's "col"-outer shape, tab 2's rows came out as side-by-side columns (verified by
    # rendering). The "row"-outer form also matches the S-TRAF/S-FC exports. Re-render every tab
    # after any change here.
    # Metric Change and Metric Summary each get a full-width row: side by side at 0.55/0.45 their
    # Metric column cut to ~16 characters ("Adds pts feature ..."), and no proven columnSettings
    # width key exists to fix that instead (metrics_tab-05).
    # Metric change and Metric range are always full: at the old 0.20/0.15 they showed only 5 and
    # 3 of 90 rows apiece, while the always-idle-until-a-task-pick bottom panel took 35% of the
    # height (metrics_tab-02).
    metrics = stack(rows([
        (items(w, [("Metric Change", 1.0)]), 0.33),
        (items(w, [("Metric Summary", 1.0)]), 0.30),
        (items(w, [("Metric Trend", 0.60), ("Latest Metric Observations", 0.40)]), 0.22),
        (items(w, [("Metric Observations", 1.0)]), 0.15)], "col"), "row", 1, 1)
    collector = stack(rows([
        (items(w, [("Heartbeat", 0.15), ("Collector Runs", 0.85)]), 0.45),
        ([item_el(w["Recent Changes"]["id"], 0.6),
          # Setting notes keeps the top; the About panel takes the empty space below its cards.
          stack(items(w, [("Setting Notes", 0.75), (ABOUT_NAME, 0.25)], vertical=True), "row", 0.4, 1)], 0.55)],
        "col"), "row", 1, 1)
    return {"type": "dockingLayout", "rootElement": {
        "type": "tabsLayoutElement", "id": new_id(), "width": 1, "height": 1,
        "elements": [tab("Overview", overview), tab("Workflow Metrics", metrics),
                     tab("Collector & Changes", collector)]}}


def mobile_view(item_id: str, layers: Dict[str, int], templates: Dict[str, Any],
                desktop: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Tiles, then the tasks that need attention or are disabled, then the newest 10 failures.

    No selectors, tables or charts: a selector on a phone could hide a failure without the reader
    noticing. The attention list is the one exception: without it, a non-zero Overdue or Other
    warnings tile (or a disabled task) named no task at all on a phone
    (interactions-mobile-no-attention-list). Every widget is an independent copy with a new id
    (S-FC has zero id overlap between its desktop and mobile widgets).
    """
    widgets, mapping = [], {}
    for name in MOBILE_TILES:
        clone = copy.deepcopy(desktop[name])
        clone.update(id=new_id(), name=name + MOBILE_SUFFIX)
        # A phone tile is short, so "medium" values drew about as small as the title. "large"
        # is only a cap: Dashboards still shrinks the text to fit the tile.
        if "expression" in clone and name != "Tasks Tracked":  # kept small, as on desktop
            clone["expression"] = clone["expression"].replace(
                'middleTextMaxSize: "medium"', 'middleTextMaxSize: "large"')
        widgets.append(clone)
        mapping[desktop[name]["id"]] = clone["id"]
    inv = layers[TABLE_INVENTORY]
    attention_where = [{"type": "filterGroup", "condition": "OR", "rules": [
        rule("needs_attention", 1), rule("enabled", 0)]}]
    attention = list_widget(
        item_id, "Attention Tasks" + MOBILE_SUFFIX, inv, templates["list"],
        order_by=["last_run_failed DESC", "is_overdue DESC", "needs_attention DESC", "task_name ASC"],
        # The list also holds disabled tasks, which the Needs Attention tile above does not count
        # (interactions-mobile-attention-title-vs-disabled / wording-mobile-attention-list-scope):
        # the title now says so instead of only the info-icon help.
        expression=mobile_attention_expression(), where=attention_where,
        title="Tasks that need attention or are disabled",
        text='<p><span style="font-size:15px"><strong>{task_name}</strong></span> '
             '<strong>{expression/statusText}</strong></p>'
             '<p>{expression/reasonText}</p>')
    attention["showFilter"] = False
    widgets.append(attention)
    failures = recent_failures_list(item_id, layers, templates, max_features=10,
                                    name="Recent Failures" + MOBILE_SUFFIX)
    failures["showFilter"] = False
    widgets.append(failures)
    mapping[desktop["Recent Failures"]["id"]] = failures["id"]
    # S-FC mobile root: one "row" stack, children stacked top to bottom by height.
    by_name = {w["name"]: w["id"] for w in widgets}
    tile_rows = [stack([item_el(by_name[name + MOBILE_SUFFIX], 0.5) for name in pair], "col", 1, MOBILE_TILE_HEIGHT)
                 for pair in MOBILE_TILE_PAIRS]
    layout = {"type": "dockingLayout", "rootElement": stack(
        tile_rows
        + [item_el(attention["id"], 1, MOBILE_ATTENTION_HEIGHT),
           item_el(failures["id"], 1, MOBILE_FAILURES_HEIGHT)], "row", 1, 1)}
    return {"type": "mobile", "layout": layout, "widgets": widgets}, mapping


def _points_to_definition_docs(func):
    """Add the docs pointer to every refusal of a definition check that has none of its own.
    About thirty checks refuse with one shared section, so they get it here, not one by one."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SystemExit as exc:
            if not isinstance(exc.code, str) or "TROUBLESHOOTING.md" in exc.code:
                raise
            raise SystemExit(f"{exc.code}\n" + see_troubleshooting("The definition check refuses the dashboard")) from None
    return wrapper


@_points_to_definition_docs
def build(item_id: str, templates: Dict[str, Any], layer_ids: Optional[Dict[str, int]] = None,
          *, portal_url: str = DEFAULT_PORTAL_URL, config: Optional[Dict[str, Any]] = None,
          demo: bool = False) -> Dict[str, Any]:
    layers = {**DEFAULT_LAYER_IDS, **(layer_ids or {})}
    terms = help_terms(config)
    arcade_items: List[Dict[str, Any]] = []
    tiles = indicator_tiles(item_id, layers, portal_url, arcade_items, terms["late"], terms["down"],
                            terms["ignore_days"])
    import taskdash_publish as publish

    hidden = sorted({f for fields in publish.view_hidden_fields(config or {}).values() for f in fields})
    widgets = {**tiles, **overview_widgets(item_id, layers, templates, hidden),
               **metrics_widgets(item_id, layers, templates, portal_url, arcade_items, hidden),
               **collector_widgets(item_id, layers, templates, tiles["Collector Heartbeat"]),
               ABOUT_NAME: about_widget(terms, demo), OVERVIEW_ABOUT_NAME: overview_about_widget(demo)}
    sels = selectors(item_id, layers)
    wire_events(widgets, sels)
    mobile, mapping = mobile_view(item_id, layers, templates, widgets)
    for widget in [*widgets.values(), *sels.values(), *mobile["widgets"]]:
        apply_help(widget, terms)

    desktop: Dict[str, Any] = {
        "layout": desktop_layout(widgets),
        "type": "desktopView",
        "widgets": list(widgets.values()),
        "settings": {"allowElementResizing": True, "allowElementExpansion": True, "allowReset": True},
    }
    if sels:
        # S-TRAF (5.1.0) places the sidebar with this key alone; no layout element points at it.
        desktop["sidebar"] = {"dashboardItemType": "panel", "type": "sidebar", "title": "Filters",
                              "allowSliding": True, "selectors": list(sels.values())}
    definition: Dict[str, Any] = {
        "version": "5.0.0",
        "authoringApp": "ArcGIS Dashboards",
        "authoringAppVersion": "5.0.0",
        "maxPaginationRecords": 50000,
        "maxChartRecords": 10000,
        "timeZone": "system",
        "theme": {"id": "light", "type": "default"},
        "numberPrefixOverrides": [],
        "desktopView": desktop,
        "mobileView": mobile,
        "elementMappings": mapping if FEATURE_FLAGS["mobile_element_mappings"] else {},
    }
    if arcade_items:
        definition["arcadeDataSourceItems"] = arcade_items
    return definition


# --------------------------------------------------------------------------- validation

def desktop_items(definition: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Desktop widgets plus sidebar selectors: both can be event sources and targets."""
    desktop = definition["desktopView"]
    return desktop["widgets"] + (desktop.get("sidebar") or {}).get("selectors", [])


def all_items(definition: Dict[str, Any]) -> List[Dict[str, Any]]:
    return desktop_items(definition) + definition.get("mobileView", {}).get("widgets", [])


def layer_datasets(widget: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Datasets bound to a hosted table; Arcade data sources carry no layerId."""
    return [d for d in widget["datasets"] if d["dataSource"].get("type") == "layerDataSource"]


def widget_table(widget: Dict[str, Any], id_to_table: Dict[int, str]) -> Optional[str]:
    datasets = layer_datasets(widget)
    return id_to_table[datasets[0]["dataSource"]["layerId"]] if datasets else None


def _walk_rules(group: Optional[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for r in (group or {}).get("rules", []):
        if r.get("type") == "filterGroup":
            yield from _walk_rules(r)
        else:
            yield r


def check_chart_series(definition: Dict[str, Any], id_to_table: Dict[int, str]) -> None:
    # Every chart series must be bound to the fields its own dataset actually returns, or the
    # widget renders "Cannot access data" while the underlying query is perfectly healthy.
    for widget in all_items(definition):
        if not widget["datasets"]:
            continue  # the About panel is rich text with no data
        dataset = widget["datasets"][0]
        for series in (widget.get("chartConfig") or {}).get("series", []):
            if widget.get("categoryType") == "features":
                # Feature-bound trend charts plot raw row values, not group-by/statistic
                # aliases, so confirm x/y are real fields and the dataset really is ungrouped.
                if dataset.get("groupByFields") or dataset.get("statisticDefinitions"):
                    raise SystemExit(
                        f"Refusing to publish: '{widget['name']}' is categoryType 'features' but "
                        "its dataset still declares groupByFields/statisticDefinitions -- a "
                        "features chart must read raw rows, not a grouped/aggregated query.")
                for field_name in (series.get("x"), series.get("y")):
                    field_filter_type(widget_table(widget, id_to_table), field_name)
                continue
            expected_x = (dataset.get("groupByFields") or [None])[0]
            aliases = {s["outStatisticFieldName"] for s in dataset.get("statisticDefinitions", [])}
            if series.get("x") != expected_x or series.get("y") not in aliases:
                raise SystemExit(
                    f"Refusing to publish: '{widget['name']}' series is bound to "
                    f"x={series.get('x')!r}/y={series.get('y')!r} but its dataset returns "
                    f"x={expected_x!r}/y={sorted(aliases)}.")
    emit("All chart series bound to their dataset fields.")


def check_filter_types(definition: Dict[str, Any], id_to_table: Dict[int, str]) -> None:
    for widget in all_items(definition):
        for dataset in layer_datasets(widget):
            table = id_to_table[dataset["dataSource"]["layerId"]]
            for r in _walk_rules(dataset.get("filter")):
                declared = r["field"]["type"]
                actual = field_filter_type(table, r["field"]["name"])
                if declared != actual:
                    raise SystemExit(
                        f"Refusing to publish: '{widget['name']}' filters {r['field']['name']!r} as "
                        f"{declared!r} but the schema says {actual!r}. Dashboards renders a wrong "
                        f"filter type as 'Cannot access data'.")
    emit("All filter rules typed against the schema.")


def arcade_source_fields(definition: Dict[str, Any], widget: Dict[str, Any]) -> Optional[List[str]]:
    """Field names an Arcade-sourced widget's script declares, or None for a layer widget."""
    sources = {a["itemId"]: a for a in definition.get("arcadeDataSourceItems", [])}
    for dataset in widget["datasets"]:
        if dataset["dataSource"].get("type") == "arcadeDataSource":
            script = sources.get(dataset["dataSource"].get("itemId"), {}).get("script", "")
            return re.findall(r'\{name: "(\w+)"', script)
    return None


def check_table_columns(definition: Dict[str, Any], id_to_table: Dict[int, str]) -> None:
    for widget in all_items(definition):
        if widget["type"] != "tableWidget":
            continue
        dataset = widget["datasets"][0]
        value_fields = widget["dataSettings"]["valueFields"]
        if value_fields != [c["fieldName"] for c in widget["columnSettings"]]:
            raise SystemExit(f"'{widget['name']}': valueFields and columnSettings disagree.")
        declared = arcade_source_fields(definition, widget)
        if declared is not None:
            # An Arcade source has no layer schema: its script's field list is the schema.
            missing = [n for n in value_fields if n not in declared]
            if missing:
                raise SystemExit(f"Refusing to publish: '{widget['name']}' shows {missing}, which its "
                                 "Arcade data source does not return. Add them to the script's fields.")
            continue
        # A grouped table can only return its own groupByFields and its statistic aliases
        # (synthetic names like AVG_metric_value that do not exist on the layer).
        group_by_fields = set(dataset.get("groupByFields") or [])
        stat_aliases = {s["outStatisticFieldName"] for s in dataset.get("statisticDefinitions", [])}
        for name in value_fields:
            if name in stat_aliases:
                continue
            field_filter_type(widget_table(widget, id_to_table), name)
            if group_by_fields and name not in group_by_fields:
                raise SystemExit(
                    f"Refusing to publish: '{widget['name']}' column {name!r} is a raw layer "
                    "field but is neither a groupByField nor a statistic alias of this grouped "
                    "dataset -- Dashboards can only return group-by fields and aggregated "
                    "statistics from a grouped query.")
        orphans = stat_aliases - set(value_fields)
        if orphans:
            raise SystemExit(
                f"'{widget['name']}' computes {sorted(orphans)} but shows no column for them. "
                f"Dashboards renders an undisplayed statistic as an extra unlabelled column with "
                f"an error marker on every row.")
        if FEATURE_FLAGS["column_number_format"]:
            for setting in widget["columnSettings"]:
                if setting["fieldName"].startswith("AVG_") and COLUMN_FORMAT_KEY not in setting:
                    raise SystemExit(f"'{widget['name']}' column {setting['fieldName']} has no number "
                                     "format; an unrounded average renders with 10+ decimals.")
    emit("Table columns validated against their layers.")


def _check_target(src: Dict[str, Any], t: Dict[str, Any], by_id: Dict[str, Dict[str, Any]],
                  id_to_table: Dict[int, str]) -> None:
    widget_id, _, dataset_name = t["targetId"].partition("#")
    dest = by_id.get(widget_id)
    where = f"'{src['name']}' -> {t['targetId']}"
    if dest is None:
        raise SystemExit(f"Refusing to publish: {where} targets a widget that does not exist in this view.")
    if dataset_name not in {d["name"] for d in dest["datasets"]}:
        raise SystemExit(f"Refusing to publish: {where} names dataset {dataset_name!r}, which "
                         f"'{dest['name']}' does not have.")
    if t.get("by") != "whereClause" or "requiresSelection" not in t:
        raise SystemExit(f"Refusing to publish: {where} is not a 5.x whereClause target; a legacy "
                         "flat action filters nothing and shows no error.")
    src_table, dest_table = widget_table(src, id_to_table), widget_table(dest, id_to_table)
    if src_table != dest_table and not t.get("fieldMap"):
        raise SystemExit(f"Refusing to publish: {where} crosses from {src_table} to {dest_table} "
                         "without a fieldMap; keyed on OBJECTID it matches nothing.")
    if len(t.get("fieldMap", [])) > 1:
        raise SystemExit(f"Refusing to publish: {where} maps {len(t['fieldMap'])} fields. Dashboards applies "
                         "only the first (probed 2026-09-28); join on one key field instead.")
    for fm in t.get("fieldMap", []):
        _check_field(src, src_table, fm["sourceName"], where)
        _check_field(dest, dest_table, fm["targetName"], where)


def _check_field(widget: Dict[str, Any], table: Optional[str], name: str, where: str) -> None:
    """A layer field is checked against the schema; an Arcade data source must return the field."""
    if table:
        field_filter_type(table, name)
    elif name not in widget["datasets"][0].get("outFields", []):
        raise SystemExit(f"Refusing to publish: {where} maps {name!r}, which the Arcade data source "
                         f"of '{widget['name']}' does not return.")


def check_events(definition: Dict[str, Any], id_to_table: Dict[int, str]) -> None:
    views = [desktop_items(definition), definition.get("mobileView", {}).get("widgets", [])]
    for view in views:  # a mobile target must name a mobile widget, never a desktop one
        by_id = {w["id"]: w for w in view}
        for src in view:
            for event in src.get("events", []):
                for action in event["actions"]:
                    for t in action["targets"]:
                        _check_target(src, t, by_id, id_to_table)
            caption = src.get("topCaption", "")
            if "Select" in caption and not src.get("events"):
                raise SystemExit(f"Refusing to publish: '{src['name']}' tells the reader to select "
                                 "a row, but selecting does nothing.")
    emit("Every event target resolves to a widget and a schema field.")


def check_required_sources(definition: Dict[str, Any]) -> None:
    """A target draws only when EVERY source that requires a selection has one. That AND is
    wanted only between sidebar pickers (Metric Trend: a Task AND a Metric, and its prompt says
    so); with a row source among them, one click never draws the widget (Duration Trend)."""
    for view in (desktop_items(definition), definition.get("mobileView", {}).get("widgets", [])):
        by_id = {w["id"]: w for w in view}
        required: Dict[str, List[Dict[str, Any]]] = {}
        for src in view:
            for event in src.get("events", []):
                for action in event["actions"]:
                    for t in action["targets"]:
                        if t.get("requiresSelection"):
                            required.setdefault(t["targetId"].partition("#")[0], []).append(src)
        for widget_id, sources in required.items():
            if len(sources) > 1 and any(s["type"] != "categorySelectorWidget" for s in sources):
                raise SystemExit(
                    f"Refusing to publish: '{by_id[widget_id]['name']}' requires a selection from "
                    f"{', '.join(sorted(s['name'] for s in sources))}; one row click can never satisfy all of "
                    "them. Keep requiresSelection true on the row source only.")
    emit("No widget waits on a row selection plus another required source.")


def check_caps_and_help(definition: Dict[str, Any]) -> None:
    for widget in all_items(definition):
        stated = widget.get("title", "") + widget.get("topCaption", "")
        for dataset in layer_datasets(widget):
            cap = dataset.get("maxFeatures")
            if cap is not None and str(cap) not in stated:
                raise SystemExit(f"Refusing to publish: '{widget['name']}' caps its query at {cap} "
                                 "rows without saying so in its title (no silent caps).")
        if FEATURE_FLAGS["widget_description"] and not widget.get("description"):
            raise SystemExit(f"Refusing to publish: '{widget['name']}' has no help text. Add it to HELP.")
        expression = widget.get("expression") or ""
        if widget["type"] == "indicatorWidget" and expression and (
                "attributes:" in expression or "middleText" not in expression):
            raise SystemExit(f"Refusing to publish: '{widget['name']}' indicator Arcade must return "
                             "a flat object with middleText; the list contract renders blank.")
    emit("No silent row caps; help text and indicator Arcade contracts present.")


def check_arcade_sources(definition: Dict[str, Any]) -> None:
    sources = {a["itemId"]: a for a in definition.get("arcadeDataSourceItems", [])}
    for widget in all_items(definition):
        for dataset in widget["datasets"]:
            source_id = dataset["dataSource"].get("itemId")
            if dataset["dataSource"].get("type") != "arcadeDataSource":
                continue
            if source_id not in sources or not sources[source_id].get("refreshInterval"):
                raise SystemExit(f"Refusing to publish: '{widget['name']}' reads Arcade data source "
                                 f"{source_id} with no refreshInterval; the heartbeat would never age.")
    emit("Arcade data sources resolve and refresh.")


def data_item_id(agol: Dict[str, Any], target: str) -> str:
    """The item the dashboard reads: the target's read-only view when one is configured, else
    the service itself (today's behaviour). The collector always writes to the service."""
    import taskdash_publish as publish

    view_id = normalize_text(agol.get(publish.view_key(target)))
    if not view_id:
        emit(f"WARNING: arcgis.{publish.view_key(target)} is not set, so the dashboard reads the editable "
             f"service. Create a read-only view with {command_text('collect', '--create-view', '--apply')} and set "
             f"arcgis.{publish.view_key(target)}.")
    return view_id or normalize_text(agol.get(publish.service_key(target)))


def check_data_item(definition: Dict[str, Any], expected_id: str) -> None:
    """Every layer data source and Arcade script must name expected_id; a stray service id
    would leave part of a view-backed dashboard reading the writable service."""
    ids = {d["dataSource"].get("itemId") for w in all_items(definition) for d in layer_datasets(w)}
    stray = sorted(ids - {expected_id})
    if stray:
        raise SystemExit(f"Refusing to publish: widget data sources name item(s) {', '.join(stray)}, "
                         f"not the configured {expected_id}.")
    for source in definition.get("arcadeDataSourceItems", []):
        if f'"{expected_id}"' not in source.get("script", ""):
            raise SystemExit(f"Refusing to publish: Arcade source {source.get('name', '?')!r} "
                             f"does not read the configured item {expected_id}.")
    emit(f"All data sources read {expected_id}.")


def check_hidden_fields(definition: Dict[str, Any], config: Optional[Dict[str, Any]]) -> None:
    """The view hides the fields the privacy switches name; a widget that reads one fails to
    render there, so none may."""
    import taskdash_publish as publish

    blob = json.dumps(definition)
    for table, fields in publish.view_hidden_fields(config or {}).items():
        for field in fields:
            if f'$datapoint[\\"{field}\\"]' in blob or f'"name": "{field}"' in blob:
                raise SystemExit(f"Refusing to publish: a widget reads {table}.{field}, which the "
                                 f"view hides. Turn the privacy switch off or remove the widget's read.")


@_points_to_definition_docs
def validate_definition(definition: Dict[str, Any], layer_ids: Optional[Dict[str, int]] = None,
                        config: Optional[Dict[str, Any]] = None,
                        data_item: str = "") -> None:
    id_to_table = {v: k for k, v in {**DEFAULT_LAYER_IDS, **(layer_ids or {})}.items()}
    blob = json.dumps(definition)
    # A substring test, not an exact quoted string: a host inside a where clause or an Arcade
    # literal is a hardcoded filter too. Hosts under 3 characters would match unrelated text.
    hosts = {normalize_text((config or {}).get("host_name_override")), socket.gethostname()}
    # Only a quoted literal is a filter ('HOST' in a where clause, "HOST" in Arcade, escaped \"HOST\"
    # in the JSON), so a host called TASKS or DASH does not match ordinary words in the help text.
    if any(len(h) >= 3 and re.search(rf"(?<=['\"]){re.escape(h)}(?=\\?['\"])", blob, re.IGNORECASE)
           for h in hosts) or '"server_name"' in blob:
        raise SystemExit("Refusing to publish: definition still contains a hardcoded host filter.")
    emit("No hardcoded host filters present.")
    check_chart_series(definition, id_to_table)
    check_filter_types(definition, id_to_table)
    check_table_columns(definition, id_to_table)
    check_events(definition, id_to_table)
    check_required_sources(definition)
    check_caps_and_help(definition)
    check_arcade_sources(definition)
    if data_item:
        check_data_item(definition, data_item)
        check_hidden_fields(definition, config)


# --------------------------------------------------------------------------- AGOL

TEMPLATE_FILE = "dashboard_templates.json"


def load_templates(gis: Any, source_dashboard_id: str) -> Dict[str, Any]:
    """Borrow proven widget structures, preferring the local snapshot.

    These structures originally came from the old Task Scheduler Audit dashboard. Reading them
    live made this script depend on the very item the retirement plan deletes -- delete it and
    the new dashboard becomes unrebuildable. The snapshot in TEMPLATE_FILE is authoritative;
    AGOL is only a fallback for a working copy that has not got the file yet.
    """
    local = data_dir() / TEMPLATE_FILE
    if local.exists():
        data = json.loads(local.read_text(encoding="utf-8"))
        emit(f"Widget templates loaded from {TEMPLATE_FILE} (no dependency on any AGOL item).")
    elif source_dashboard_id:
        item = gis.content.get(source_dashboard_id)
        if item is None:
            raise SystemExit(
                f"Widget templates unavailable: {TEMPLATE_FILE} is missing and source dashboard "
                f"{source_dashboard_id} no longer exists. Reinstall the TaskDash wheel to restore {TEMPLATE_FILE}.\n"
                + see_troubleshooting("The widget templates are missing")
            )
        data = item.get_data()
        local.write_text(json.dumps(data, indent=1), encoding="utf-8")
        emit(f"Widget templates fetched from {source_dashboard_id} and cached to {TEMPLATE_FILE}.")
    else:
        raise SystemExit(
            f"No widget templates: {TEMPLATE_FILE} is missing. Reinstall the TaskDash wheel to restore it, or pass "
            f"--source-dashboard <id> of any dashboard whose widget structures to borrow.\n"
            + see_troubleshooting("The widget templates are missing")
        )

    widgets = {w["name"]: w for w in data["desktopView"]["widgets"]}
    missing = {"Task Inventory", "Recent Runs", "Schedule Mix"} - set(widgets)
    if missing:
        raise SystemExit(f"{TEMPLATE_FILE} is missing required template widget(s): {', '.join(sorted(missing))}. "
                         "Reinstall the TaskDash wheel to restore the file.\n"
                         + see_troubleshooting("The widget templates are missing"))
    return {
        "table": widgets["Task Inventory"],
        "list": widgets["Recent Runs"],
        "serial_config": widgets["Schedule Mix"]["chartConfig"],
    }


def resolve_layer_ids(gis: Any, service_item_id: str, target: str) -> Dict[str, int]:
    """Layer ids by table name, read from the service (read-only). MetricChange is optional: it
    is in the result only when the item has it, and then Metric Change reads it."""
    item = gis.content.get(service_item_id)
    if item is None:
        raise SystemExit(f"Service item {service_item_id} not found or not visible to this account. Check "
                         f"the item id in the config.\n" + see_troubleshooting("The service or view item cannot be found"))
    tables = get_tables(item)
    missing = sorted(set(DEFAULT_LAYER_IDS) - set(tables))
    if missing:
        raise SystemExit(f"The {target} service has no {', '.join(missing)} table(s). Create them first: "
                         f"{command_text('collect', '--migrate', '--target', target, '--apply')}\n"
                         + see_troubleshooting("The service has no tables"))
    wanted = [*DEFAULT_LAYER_IDS, *([TABLE_METRIC_CHANGE] if TABLE_METRIC_CHANGE in tables else [])]
    return {name: int(tables[name].properties.id) for name in wanted}


def resolve_dashboard(gis: Any, target: str, agol: Dict[str, Any], title_override: str = "") -> Tuple[Any, str]:
    """(existing item or None, title). Prod resolves by id only -- a title search can pick the
    wrong item or a copy (audit dashboard-05). Test resolves by its own title, and may never
    land on the prod item."""
    prod_id = normalize_text(agol.get("dashboard_id"))
    if target == "prod":
        if not prod_id:
            title = title_override or PROD_DASHBOARD_TITLE
            # The title search only refuses; it never picks the item (audit dashboard-05). An empty id
            # with a dashboard already there would add a second, unshared copy in the root folder.
            owner = gis.users.me.username
            found = [i for i in gis.content.search(f'title:"{title}" AND owner:{owner}', item_type="Dashboard")
                     if i.title == title]
            if found:
                raise SystemExit(f"arcgis.dashboard_id is empty, but a dashboard titled {title!r} already exists "
                                 f"({', '.join(i.id for i in found)}). A new one would be an unshared second copy. "
                                 "Put that id in arcgis.dashboard_id in the config, then run this again; "
                                 "this script never creates a second prod dashboard.\n"
                                 + see_troubleshooting("The dashboard id is empty but one exists"))
            return None, title
        item = gis.content.get(prod_id)
        if item is None or item.type != "Dashboard":
            raise SystemExit(f"arcgis.dashboard_id {prod_id} is not a Dashboard visible to this account. "
                             "Correct the id in the config; this script never creates a second prod dashboard.\n"
                             + see_troubleshooting("The dashboard item is wrong"))
        return item, title_override or item.title
    title = title_override or TEST_DASHBOARD_TITLE
    owner = gis.users.me.username
    matches = [i for i in gis.content.search(f'title:"{title}" AND owner:{owner}', item_type="Dashboard")
               if i.title == title]
    if prod_id and any(i.id == prod_id for i in matches):
        raise SystemExit(f"Refusing: the test title {title!r} resolves to the prod dashboard {prod_id}. "
                         "Rename one of them or pass a different --title.\n"
                         + see_troubleshooting("The test title matches the production dashboard"))
    if len(matches) > 1:
        raise SystemExit(f"{len(matches)} dashboards are titled {title!r} ({', '.join(i.id for i in matches)}). "
                         "Delete the extras so --apply cannot update the wrong one.\n"
                         + see_troubleshooting("More than one dashboard has the title"))
    return (matches[0] if matches else None), title


def backup_dashboard(item: Any, log_dir: Path) -> Path:
    """Save the live definition before --apply overwrites it (audit dashboard-04)."""
    data = item.get_data()
    if not data:
        raise SystemExit(f"Could not read dashboard {item.id} for a backup; not overwriting it. Open it in "
                         f"ArcGIS Online to check that it opens, then run the command again.\n"
                         + see_troubleshooting("The backup could not be read"))
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"dashboard_backup_{item.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    emit(f"Backed up the current definition of {item.id} to {path}")
    return path


def _reply_text(response: Any) -> str:
    """The code and message of an ArcGIS error reply, not the whole dict. Anything else is shown as text."""
    error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return str(response)[:300]
    details = "; ".join(str(d) for d in error.get("details") or [])
    return f"error {error.get('code', '?')}: {error.get('message', 'no message')}" + (f" ({details})" if details else "")


def post_dashboard(gis: Any, definition: Dict[str, Any], title: str, existing: Any,
                   config: Optional[Dict[str, Any]] = None, demo: bool = False) -> str:
    configured_tags = normalize_text((config or {}).get("arcgis", {}).get("dashboard_tags"))
    snippet = ("DEMO - invented data. " if demo else "") + "Scheduled task health and workflow metrics."
    # Posted straight to the REST API: gis.content.add() is broken in arcgis 2.4.3, which
    # dereferences arcgis.features.geo._is_geoenabled on every add and that no longer exists.
    # Web items carry their definition in the item's `text` property.
    params = {
        "f": "json",
        "title": title,
        "type": "Dashboard",
        "typeKeywords": "Dashboard,ArcGIS Dashboards,Data,Web Application,Ready To Use",
        "text": json.dumps(definition),
    }
    # A rebuild must not overwrite the summary and tags an owner edited on the item: the update
    # call changes only the parameters it receives. A configured dashboard_tags still wins.
    if existing is None:
        params.update(tags=configured_tags or "taskdash,monitoring", snippet=snippet)
    elif configured_tags:
        params["tags"] = configured_tags
    rest = gis._portal.resturl
    owner = gis.users.me.username
    if existing is not None:
        # The update path names the item's owner, not the signed-in user: the prod dashboard is
        # owned by a person while the collector account publishes, and the signed-in user's path
        # answered "Error Code: 400" (phase 0 prod deploy). An admin may update another's item.
        response = gis._con.post(f"{rest}content/users/{existing.owner}/items/{existing.id}/update", params)
        verb, target = "Updated", existing.id
        emit("The summary and tags of the item stay as they are.")
    else:
        response = gis._con.post(f"{rest}content/users/{owner}/addItem", params)
        verb, target = "Created", response.get("id")
    if not (response.get("success") or response.get("id")):
        raise SystemExit(f"Dashboard publish failed: {_reply_text(response)}. Check that the account may create "
                         "items and may edit the dashboard, then run the command again.\n"
                         + see_troubleshooting("The dashboard could not be published"))
    emit(f"{verb} dashboard '{title}' -> {target}")
    emit(f"Open: {gis.url}/apps/dashboards/{target}")
    return target


def print_plan(definition: Dict[str, Any]) -> None:
    for view, entries in (("desktop", desktop_items(definition)),
                          ("mobile", definition["mobileView"]["widgets"])):
        for widget in entries:
            layers = [str(d["dataSource"].get("layerId", "arcade")) for d in widget["datasets"]]
            emit(f"    {view:7s} {widget['type']:24s} layer={','.join(layers):7s} {widget['name']}")
    emit("Feature flags: " + ", ".join(f"{k}={'on' if v else 'off'}" for k, v in FEATURE_FLAGS.items()))


def write_definition(definition: Dict[str, Any], item_id: str, layer_ids: Dict[str, int]) -> None:
    out_path = runtime_dir() / OUTPUT_JSON
    out_path.write_text(json.dumps(definition, indent=1), encoding="utf-8")
    emit(f"Wrote {out_path} ({len(desktop_items(definition))} desktop items, "
         f"{len(definition['mobileView']['widgets'])} mobile widgets), service {item_id}, layers {layer_ids}")
    print_plan(definition)


def build_offline(args: argparse.Namespace) -> int:
    """Build and validate without AGOL. A config is read only when --config names one: CI has none."""
    if args.apply:
        raise SystemExit("--offline never signs in, so it cannot --apply. Drop one of the two flags.\n"
                         + see_troubleshooting("Offline and apply cannot be combined"))
    config = load_config(Path(args.config)) if args.config else {}
    layer_ids = dict(DEFAULT_LAYER_IDS)
    definition = build(OFFLINE_ITEM_ID, load_templates(None, ""), layer_ids, config=config)
    write_definition(definition, OFFLINE_ITEM_ID, layer_ids)
    validate_definition(definition, layer_ids, config)
    emit("OFFLINE - nothing was signed in to or posted.")
    return 0


def _record_dashboard_id(args: argparse.Namespace, config: Dict[str, Any], dashboard_id: str) -> None:
    """Save a new prod dashboard's id as arcgis.dashboard_id, after a backup of the config, so the
    next run updates this item and the alert e-mail links to it. A failed write prints the manual step."""
    from taskdash_setup import backup_config, write_config_key  # setup imports this module at load

    path = Path(args.config) if args.config else runtime_dir() / DEFAULT_CONFIG_FILE
    try:
        backup_config(path)
        write_config_key(path, config, "dashboard_id", dashboard_id)
    except (OSError, ValueError) as exc:
        emit(f"Could not write the config ({type(exc).__name__}). Put {dashboard_id} in arcgis.dashboard_id "
             "by hand, so the next run and the alert e-mail use this dashboard.")
        return
    emit("The next run and the alert e-mail use this dashboard.")


def main(argv: Optional[List[str]] = None) -> int:
    import taskdash_publish as publish

    parser = argparse.ArgumentParser(prog="taskdash dashboard", description="Build the TaskDash dashboard.",
                                     allow_abbrev=False)
    parser.add_argument("--apply", action="store_true", help="Create or update the dashboard item.")
    parser.add_argument("--target", choices=("prod", "test"), default=None,
                        help="Which dashboard to build: prod or test. Default: test if the config has "
                             "a test service, otherwise prod (check mode only: --apply then needs --target).")
    parser.add_argument("--title", default="", help="Dashboard title. Default: the standard title for the target.")
    parser.add_argument("--config", default="", metavar="PATH", help=CONFIG_ARG_HELP)
    parser.add_argument("--offline", action="store_true",
                        help="Build and check the definition with default layer ids and a placeholder "
                             "item id. Never signs in or posts; for CI.")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    # Default is empty on purpose: the old id here pointed at the retired dashboard, which has
    # been deleted, so it read as a live dependency that no longer existed. dashboard_templates.json
    # is authoritative; this flag is only an escape hatch if that file is ever lost.
    parser.add_argument("--source-dashboard", default="",
                        help="Existing dashboard to borrow widget structure from (fallback only).")
    args = parser.parse_args(argv)
    if args.offline:
        return build_offline(args)

    config = load_config(Path(args.config) if args.config else None)
    agol = config.get("arcgis", {})
    if args.target is None:
        # A new install has no test service, so a test default would fail there.
        has_test = normalize_text(agol.get(publish.service_key("test")))
        # Check mode may pick prod, but a write must never reach prod unless the caller names it:
        # most installs have no test service, and --apply replaces the live dashboard.
        if args.apply and not has_test:
            raise SystemExit("The config has no test service, so --apply without --target would "
                             "update the production dashboard. To do that, run "
                             f"{command_text('dashboard', '--target', 'prod', '--apply')}.\n"
                             + see_troubleshooting("The dashboard command will not apply without a target"))
        args.target = "test" if has_test else "prod"
        emit(f"No --target given: using {args.target}"
             f"{'' if has_test else ' (the config has no test service)'}.")
    service_key = publish.service_key(args.target)
    item_id = normalize_text(agol.get(service_key))
    if not item_id:
        raise SystemExit(f"No arcgis.{service_key} in the config: the {args.target} dashboard needs its service. "
                         f"Set the key, or run {command_text('setup')} for a new install.\n"
                         + see_troubleshooting("The dashboard has no service to read"))

    read_id = data_item_id(agol, args.target)
    gis = connect_agol(config)
    templates = load_templates(gis, args.source_dashboard)
    layer_ids = resolve_layer_ids(gis, read_id, args.target)
    if TABLE_METRIC_CHANGE in layer_ids:
        emit(f"Metric Change reads the small {TABLE_METRIC_CHANGE} table.")
    else:
        emit(f"The item has no {TABLE_METRIC_CHANGE} table, so Metric Change reads all of {TABLE_METRICS}. "
             f"To read the small table, run {command_text('collect', '--migrate', '--target', args.target, '--apply')}. A view made before "
             f"this release needs a new view; docs/UPGRADE.md has the steps.")
    definition = build(read_id, templates, layer_ids, portal_url=gis.url, config=config)

    write_definition(definition, item_id, layer_ids)
    validate_definition(definition, layer_ids, config, data_item=read_id if read_id != item_id else "")

    existing, title = resolve_dashboard(gis, args.target, agol, args.title)
    emit(f"Plan: {'update ' + existing.id if existing is not None else 'CREATE a new item'} titled '{title}'.")
    if not args.apply:
        emit("DRY RUN - dashboard item not written. Re-run with --apply.")
        return 0

    if existing is not None:
        log_dir = publish.resolve_log_dir(config)
        emit(f"This rebuild replaces any hand edits made in the Dashboards editor. "
             f"The current definition is saved first in {log_dir}.")
        backup_dashboard(existing, log_dir)
    target = post_dashboard(gis, definition, title, existing, config)
    if args.target == "prod" and existing is None:
        _record_dashboard_id(args, config, target)
    return 0


if __name__ == "__main__":
    sys.exit(main())

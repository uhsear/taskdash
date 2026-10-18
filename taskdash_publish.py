"""TaskDash entry point: collect scheduled tasks, runs and log metrics, publish to AGOL.

Check-by-default. A plain run collects, validates and reports; `--apply` is the only path
that writes to ArcGIS Online.

    python taskdash_publish.py                     # dry run, prints what would change
    python taskdash_publish.py --apply             # writes to the configured item
    python taskdash_publish.py --capture-fixture f.json   # host-local capture for off-host testing
    python taskdash_publish.py --fixture f.json --apply --target test
    python taskdash_publish.py --migrate            # reports missing table(s)/field(s)/index(es)
    python taskdash_publish.py --migrate --apply    # creates/adds them (additive only)
    python taskdash_publish.py --preview-alert      # the alert e-mail it would send now; sends nothing
    python taskdash_publish.py --refresh-result-text          # reports stored runs with outdated text
    python taskdash_publish.py --refresh-result-text --apply  # rewrites that text from the result code
    python taskdash_publish.py --scrub-history      # counts stored rows the privacy rules would change
    python taskdash_publish.py --scrub-history --apply        # blanks or redacts those values
    python taskdash_publish.py --check-anonymous    # exit 1 if any table answers without a token

Runs under either interpreter; arcpy is deliberately blocked (see taskdash_common.disable_arcpy_import).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import taskdash_collect as collect
import taskdash_metrics as metrics
from taskdash_common import (
    DEFAULT_CHUNK_SIZE,
    STALE_LOCK_HOURS,
    TABLE_COLLECTOR_RUNS,
    TABLE_INVENTORY,
    TABLE_METRICS,
    TABLE_RUNS,
    TABLE_TASK_CHANGES,
    SCHEMA_VERSION,
    TASKDASH_VERSION,
    RunLockHeld,
    apply_edits_chunked,
    atomic_write_json,
    chunked,
    cleanup_old_logs,
    max_field_value,
    connect_agol,
    emit,
    escape_sql_string,
    get_host_name,
    get_tables,
    load_config,
    make_field,
    normalize_text,
    object_id_field,
    purge_older_than,
    query_all_rows,
    raise_on_edit_failure,
    run_lock,
    short_duration,
    runtime_dir,
    safe_float,
    safe_int,
    setup_logging,
    to_epoch_millis,
    parse_iso_datetime,
    utc_now,
    with_retry,
)

DATE_FIELDS = {
    TABLE_INVENTORY: {"next_run_time", "last_run_time", "latest_log_time_utc", "collected_at_utc"},
    TABLE_RUNS: {"start_time_utc", "end_time_utc", "collected_at_utc"},
    TABLE_METRICS: {"observed_at_utc", "collected_at_utc"},
    TABLE_COLLECTOR_RUNS: {"started_utc", "ended_utc"},
    TABLE_TASK_CHANGES: {"changed_utc"},
}


# --------------------------------------------------------------------------- schema

def inventory_fields() -> List[Dict[str, Any]]:
    return [
        make_field("OBJECTID", "esriFieldTypeOID", "OBJECTID", nullable=False, editable=False),
        make_field("host_name", "esriFieldTypeString", "Host", length=120),
        make_field("collected_at_utc", "esriFieldTypeDate", "Collected At (UTC)"),
        make_field("task_name", "esriFieldTypeString", "Task", length=255),
        make_field("task_path", "esriFieldTypeString", "Task Path", length=255),
        make_field("task_full_name", "esriFieldTypeString", "Task Full Name", length=500),
        make_field("description", "esriFieldTypeString", "Description", length=2000),
        make_field("enabled", "esriFieldTypeInteger", "Enabled"),
        make_field("state", "esriFieldTypeString", "State", length=60),
        make_field("schedule_bucket", "esriFieldTypeString", "Schedule", length=60),
        make_field("trigger_types", "esriFieldTypeString", "Trigger Types", length=255),
        make_field("schedule_detail", "esriFieldTypeString", "Schedule Detail", length=1000),
        make_field("settings_detail", "esriFieldTypeString", "Settings Detail", length=255),
        make_field("next_run_time", "esriFieldTypeDate", "Next Run"),
        make_field("next_run_in_hours", "esriFieldTypeDouble", "Next Run In (hours)"),
        make_field("run_as_user", "esriFieldTypeString", "Run As", length=255),
        make_field("command_target_path", "esriFieldTypeString", "Script Path", length=500),
        make_field("command_target_name", "esriFieldTypeString", "Script", length=255),
        make_field("command_target_type", "esriFieldTypeString", "Script Type", length=60),
        make_field("command_target_working_directory", "esriFieldTypeString", "Working Directory", length=500),
        make_field("command_target_arguments", "esriFieldTypeString", "Script Arguments", length=500),
        make_field("actions", "esriFieldTypeString", "Actions", length=2000),
        make_field("last_run_time", "esriFieldTypeDate", "Last Run"),
        make_field("hours_since_last_run", "esriFieldTypeDouble", "Hours Since Last Run"),
        make_field("last_result_code", "esriFieldTypeDouble", "Last Result Code"),
        make_field("last_result_text", "esriFieldTypeString", "Last Result", length=500),
        make_field("last_run_failed", "esriFieldTypeInteger", "Last Run Failed"),
        make_field("last_duration_seconds", "esriFieldTypeDouble", "Last Duration (s)"),
        make_field("duration_ratio", "esriFieldTypeDouble", "Duration Ratio (vs baseline)"),
        make_field("is_duration_anomaly", "esriFieldTypeInteger", "Duration Anomaly"),
        make_field("number_of_missed_runs", "esriFieldTypeInteger", "Missed Runs"),
        make_field("is_overdue", "esriFieldTypeInteger", "Overdue"),
        make_field("health_status", "esriFieldTypeString", "Health", length=60),
        make_field("needs_attention", "esriFieldTypeInteger", "Needs Attention"),
        make_field("attention_kind", "esriFieldTypeString", "Attention Kind", length=20),
        make_field("attention_reason", "esriFieldTypeString", "Attention Reason", length=1000),
        make_field("runs_last_7_days", "esriFieldTypeInteger", "Runs (7d)"),
        make_field("failures_last_7_days", "esriFieldTypeInteger", "Failures (7d)"),
        make_field("skipped_last_7_days", "esriFieldTypeInteger", "Skipped (7d)"),
        make_field("success_rate_7d", "esriFieldTypeDouble", "Success Rate 7d (%)"),
        make_field("lowest_success_rate_7d", "esriFieldTypeDouble", "Lowest task success (7d)"),
        make_field("latest_log_name", "esriFieldTypeString", "Latest Log", length=255),
        make_field("latest_log_path", "esriFieldTypeString", "Latest Log Path", length=1000),
        make_field("latest_log_time_utc", "esriFieldTypeDate", "Latest Log Time"),
        make_field("latest_log_age_hours", "esriFieldTypeDouble", "Latest Log Age (hours)"),
        make_field("latest_log_error_lines", "esriFieldTypeInteger", "Log Error Lines"),
        make_field("latest_log_warning_lines", "esriFieldTypeInteger", "Log Warning Lines"),
        make_field("log_files_scanned", "esriFieldTypeInteger", "Log Files Scanned"),
        make_field("config_warnings", "esriFieldTypeString", "Config Warnings", length=1000),
        make_field("logon_type", "esriFieldTypeString", "Logon Type", length=60),
        make_field("run_level", "esriFieldTypeString", "Run Level", length=60),
        make_field("definition_hash", "esriFieldTypeString", "Definition Hash", length=64),
    ]


def runs_fields() -> List[Dict[str, Any]]:
    return [
        make_field("OBJECTID", "esriFieldTypeOID", "OBJECTID", nullable=False, editable=False),
        make_field("host_name", "esriFieldTypeString", "Host", length=120),
        make_field("collected_at_utc", "esriFieldTypeDate", "Collected At (UTC)"),
        make_field("task_name", "esriFieldTypeString", "Task", length=255),
        make_field("task_path", "esriFieldTypeString", "Task Path", length=255),
        make_field("task_full_name", "esriFieldTypeString", "Task Full Name", length=500),
        make_field("run_key", "esriFieldTypeString", "Run Key", length=255),
        make_field("instance_id", "esriFieldTypeString", "Instance ID", length=100),
        make_field("start_time_utc", "esriFieldTypeDate", "Start (UTC)"),
        make_field("start_time_estimated", "esriFieldTypeInteger", "Start Estimated"),
        make_field("end_time_utc", "esriFieldTypeDate", "End (UTC)"),
        make_field("duration_seconds", "esriFieldTypeDouble", "Duration (s)"),
        make_field("duration_minutes", "esriFieldTypeDouble", "Duration (min)"),
        make_field("result_code", "esriFieldTypeDouble", "Result Code"),
        make_field("result_text", "esriFieldTypeString", "Result", length=500),
        make_field("run_status", "esriFieldTypeString", "Run Status", length=50),
        make_field("is_completed", "esriFieldTypeInteger", "Completed"),
        make_field("is_success", "esriFieldTypeInteger", "Success"),
        make_field("is_error", "esriFieldTypeInteger", "Error"),
        make_field("user_context", "esriFieldTypeString", "User", length=255),
        make_field("event_count", "esriFieldTypeInteger", "Event Count"),
        make_field("max_event_record_id", "esriFieldTypeDouble", "Max Event Record ID"),
    ]


def metrics_fields() -> List[Dict[str, Any]]:
    return [
        make_field("OBJECTID", "esriFieldTypeOID", "OBJECTID", nullable=False, editable=False),
        make_field("host_name", "esriFieldTypeString", "Host", length=120),
        make_field("collected_at_utc", "esriFieldTypeDate", "Collected At (UTC)"),
        make_field("task_name", "esriFieldTypeString", "Task", length=255),
        make_field("task_full_name", "esriFieldTypeString", "Task Full Name", length=500),
        make_field("metric_key", "esriFieldTypeString", "Metric Key", length=150),
        make_field("display_name", "esriFieldTypeString", "Metric", length=255),
        make_field("metric_value", "esriFieldTypeDouble", "Value"),
        make_field("observed_at_utc", "esriFieldTypeDate", "Observed At (UTC)"),
        make_field("log_file_name", "esriFieldTypeString", "Log File", length=255),
        make_field("sample_line", "esriFieldTypeString", "Sample Line", length=500),
        # host|task_full_name|metric_key: the one field a Metric change row click filters on.
        make_field("series_key", "esriFieldTypeString", "Series Key", length=800),
    ]


def collector_runs_fields() -> List[Dict[str, Any]]:
    """3.6: one row per TaskDash --apply run -- audit trail independent of any one task."""
    return [
        make_field("OBJECTID", "esriFieldTypeOID", "OBJECTID", nullable=False, editable=False),
        make_field("run_id", "esriFieldTypeString", "Run ID", length=160),
        make_field("host_name", "esriFieldTypeString", "Host", length=120),
        make_field("started_utc", "esriFieldTypeDate", "Started (UTC)"),
        make_field("ended_utc", "esriFieldTypeDate", "Ended (UTC)"),
        make_field("duration_seconds", "esriFieldTypeDouble", "Duration (s)"),
        make_field("exit_status", "esriFieldTypeString", "Exit Status", length=20),
        make_field("error_text", "esriFieldTypeString", "Error", length=2000),
        make_field("tasks_seen", "esriFieldTypeInteger", "Tasks Seen"),
        make_field("events_read", "esriFieldTypeInteger", "Events Read"),
        make_field("watermark_from", "esriFieldTypeDouble", "Watermark From"),
        make_field("watermark_to", "esriFieldTypeDouble", "Watermark To"),
        make_field("runs_written", "esriFieldTypeInteger", "Runs Written"),
        make_field("inventory_written", "esriFieldTypeInteger", "Inventory Written"),
        make_field("metrics_written", "esriFieldTypeInteger", "Metrics Written"),
        make_field("alert_sent", "esriFieldTypeInteger", "Alert Sent"),
        make_field("taskdash_version", "esriFieldTypeString", "TaskDash Version", length=20),
        make_field("schema_version", "esriFieldTypeInteger", "Schema Version"),
    ]


def task_changes_fields() -> List[Dict[str, Any]]:
    """3.7: one row per detected task state transition (added/removed/definition/health)."""
    return [
        make_field("OBJECTID", "esriFieldTypeOID", "OBJECTID", nullable=False, editable=False),
        make_field("host_name", "esriFieldTypeString", "Host", length=120),
        make_field("task_full_name", "esriFieldTypeString", "Task Full Name", length=500),
        make_field("task_name", "esriFieldTypeString", "Task", length=255),
        make_field("change_kind", "esriFieldTypeString", "Change Kind", length=40),
        make_field("changed_utc", "esriFieldTypeDate", "Changed (UTC)"),
        make_field("old_summary", "esriFieldTypeString", "Old", length=1000),
        make_field("new_summary", "esriFieldTypeString", "New", length=1000),
    ]


TABLE_SPECS = {
    TABLE_INVENTORY: (inventory_fields, "task_name", "One row per scheduled task: current state and last-run outcome."),
    TABLE_RUNS: (runs_fields, "task_name", "One row per task run, derived from the TaskScheduler Operational log."),
    TABLE_METRICS: (metrics_fields, "display_name", "Metric time series mined from each workflow's log files."),
    TABLE_COLLECTOR_RUNS: (collector_runs_fields, "run_id", "One row per TaskDash collector run: timing, counts, and outcome."),
    TABLE_TASK_CHANGES: (task_changes_fields, "change_kind", "One row per detected task state transition."),
}


# Field sets match the upsert/sync keys actually used in publish(): TaskRuns.run_key
# (upsert_by_key), TaskInventory (host_name, task_full_name) (sync_inventory), TaskMetrics
# (host_name, task_full_name, metric_key, log_file_name) (upsert_by_key). The non-unique
# entries back the dashboard's health/recency filters. TaskMetrics carries host_name in its key
# (unlike a plain task_full_name+metric_key+log_file_name) because two hosts can legitimately
# run a task of the same full name, metric and log file name; without it, the second host's
# fetch_keys (scoped to its own host_name) never sees the first host's row and sends a
# colliding add every pass once the unique index exists.
TABLE_INDEXES: Dict[str, List[Dict[str, Any]]] = {
    TABLE_INVENTORY: [
        {"fields": ["host_name", "task_full_name"], "unique": True},
        {"fields": ["health_status"], "unique": False},
        {"fields": ["needs_attention"], "unique": False},
    ],
    TABLE_RUNS: [
        {"fields": ["run_key"], "unique": True},
        {"fields": ["start_time_utc"], "unique": False},
        {"fields": ["is_error"], "unique": False},
    ],
    TABLE_METRICS: [
        {"fields": ["host_name", "task_full_name", "metric_key", "log_file_name"], "unique": True},
        {"fields": ["series_key"], "unique": False},
    ],
    TABLE_COLLECTOR_RUNS: [
        {"fields": ["run_id"], "unique": True},
    ],
    TABLE_TASK_CHANGES: [
        {"fields": ["host_name", "task_full_name", "change_kind", "changed_utc"], "unique": True},
    ],
}


def table_definition(name: str) -> Dict[str, Any]:
    fields_fn, display_field, description = TABLE_SPECS[name]
    return {
        "name": name,
        "type": "Table",
        "displayField": display_field,
        "description": description,
        "hasAttachments": False,
        "htmlPopupType": "esriServerHTMLPopupTypeNone",
        "objectIdField": "OBJECTID",
        "globalIdField": "",
        "typeIdField": "",
        "fields": fields_fn(),
        "indexes": [{
            "name": f"PK__{name}__OBJECTID",
            "fields": "OBJECTID",
            "isAscending": True,
            "isUnique": True,
            "description": "clustered, unique, primary key",
            "indexType": "Attribute",
        }],
        "types": [], "relationships": [], "templates": [], "extent": None,
    }


# --------------------------------------------------------------------------- schema drift / migration

def diff_schema(table: Any, expected_fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expected fields (from inventory_fields() etc.) not present on the live table.

    Matched by name only, case-insensitively (AGOL does not guarantee round-trip case). A
    same-named field of a different type is reported separately by type_mismatches(), not
    here: --migrate only ever adds -- it cannot retype it, so there is nothing this function can
    act on beyond the name. The OID field is excluded; a table always has one and it cannot be
    added.
    """
    live_names = set()
    for f in getattr(table.properties, "fields", None) or []:
        name = getattr(f, "name", None)
        if name is None and hasattr(f, "get"):
            name = f.get("name")
        live_names.add(normalize_text(name).lower())
    return [f for f in expected_fields
            if f["type"] != "esriFieldTypeOID" and f["name"].lower() not in live_names]


def type_mismatches(table: Any, expected_fields: List[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """Same-named fields whose live type disagrees with what the code writes.

    (field name, expected type, live type) tuples. A mismatch (e.g. max_event_record_id stored
    as Integer instead of Double) otherwise passes diff_schema silently and only surfaces later
    as a failed or truncated write mid-publish. --migrate never retypes, so this is reported,
    not raised -- there is nothing to fix automatically.
    """
    live_types: Dict[str, str] = {}
    for f in getattr(table.properties, "fields", None) or []:
        name = getattr(f, "name", None)
        ftype = getattr(f, "type", None)
        if name is None and hasattr(f, "get"):
            name = f.get("name")
            ftype = f.get("type")
        live_types[normalize_text(name).lower()] = normalize_text(ftype)
    mismatches = []
    for f in expected_fields:
        if f["type"] == "esriFieldTypeOID":
            continue
        live_type = live_types.get(f["name"].lower())
        if live_type and live_type != f["type"]:
            mismatches.append((f["name"], f["type"], live_type))
    return mismatches


def _existing_index_field_sets(table: Any) -> List[Tuple[frozenset, bool]]:
    sets = []
    for idx in getattr(table.properties, "indexes", None) or []:
        fields = getattr(idx, "fields", None)
        unique = getattr(idx, "isUnique", None)
        if hasattr(idx, "get"):
            fields = idx.get("fields") if fields is None else fields
            unique = idx.get("isUnique") if unique is None else unique
        field_set = frozenset(f.strip().lower() for f in normalize_text(fields).split(",") if f.strip())
        sets.append((field_set, bool(unique)))
    return sets


def missing_indexes(table: Any, expected: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expected index specs (TABLE_INDEXES[name]) whose exact field set isn't already indexed."""
    # A non-unique index on the right fields (say, one made in the AGOL UI) must not pass for
    # the unique one: the duplicate protection would then be missing without a word.
    existing = _existing_index_field_sets(table)

    def present(spec: Dict[str, Any]) -> bool:
        wanted = frozenset(f.lower() for f in spec["fields"])
        return any(fields == wanted and (unique or not spec["unique"]) for fields, unique in existing)

    return [spec for spec in expected if not present(spec)]


def _index_definition(table_name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    fields = spec["fields"]
    prefix = "UQ" if spec["unique"] else "IDX"
    return {
        "name": f"{prefix}__{table_name}__{'_'.join(fields)}",
        "fields": ",".join(fields),
        "isAscending": True,
        "isUnique": spec["unique"],
        "description": f"{'unique ' if spec['unique'] else ''}index on {', '.join(fields)}",
        "indexType": "Attribute",
    }


def find_duplicate_keys(table: Any, key_fields: Sequence[str], limit: int = 5) -> List[Tuple[str, ...]]:
    """Existing key combinations that repeat -- a unique index over them would be refused by AGOL.

    Esri does not document what add_to_definition does with a unique index laid over duplicate
    data, so this is checked and refused here up front instead of discovered live. `having`
    filters the group-by server-side to just the duplicate groups (arcgis 2.4.3's Table.query
    supports it): outStatistics results are not paged, so without `having` a table with more
    distinct keys than maxRecordCount could have duplicates past the first page go undetected.
    Counting the OID field rather than key_fields[0] also catches a repeated NULL key, which
    COUNT(key_fields[0]) reports as 0 for every occurrence. The client-side count check is kept
    as a second line of defense in case a server/test double does not honour `having`.
    """
    oid_field = object_id_field(table)
    result = with_retry(lambda: table.query(
        where="1=1", out_fields=",".join(key_fields), return_geometry=False,
        group_by_fields_for_statistics=",".join(key_fields),
        out_statistics=[{"statisticType": "count", "onStatisticField": oid_field,
                         "outStatisticFieldName": "n"}],
        having=f"COUNT({oid_field}) > 1"),
        label="duplicate key check")
    dupes: List[Tuple[str, ...]] = []
    for feature in getattr(result, "features", []) or []:
        attrs = feature.attributes
        if (safe_int(attrs.get("n")) or 0) > 1:
            dupes.append(tuple(normalize_text(attrs.get(f)) for f in key_fields))
            if len(dupes) >= limit:
                break
    return dupes


def check_schema_drift(tables: Dict[str, Any]) -> None:
    """Refuse to publish against a table missing field(s), or a unique index a keyed upsert
    relies on to survive a resent write, that the current code needs.

    Caught once here at connect time, rather than mid-publish as an opaque AGOL 400 or a
    silent duplicate row from a retried write. A type mismatch is reported but not fatal --
    --migrate never retypes, so there is nothing actionable beyond the warning. A table that is
    missing entirely (CollectorRuns/TaskChanges before --migrate --apply has ever run against
    this service, or -- for the original three -- a service that predates them) is reported the
    same way: a warning, not a refusal, so a not-yet-migrated new table never blocks an ordinary
    publish. The original three are additionally hard-blocked by publish()'s own check right
    before it writes to them.
    """
    missing_tables = [name for name in TABLE_SPECS if name not in tables]
    if missing_tables:
        emit(f"WARNING: table(s) not found on the service: {', '.join(missing_tables)} -- "
            f"run --migrate --apply to create them.")

    problems = []
    type_warnings = []
    for name in TABLE_SPECS:
        if name not in tables:
            continue
        expected_fields = TABLE_SPECS[name][0]()
        missing = diff_schema(tables[name], expected_fields)
        if missing:
            problems.append(f"{name}: {', '.join(f['name'] for f in missing)}")
        for field_name, expected_type, live_type in type_mismatches(tables[name], expected_fields):
            type_warnings.append(f"{name}.{field_name}: live type is {live_type}, code writes {expected_type}")
    if problems:
        raise SystemExit(
            "Schema drift -- table(s) are missing field(s) the current code writes:\n  "
            + "\n  ".join(problems) + "\nRun `--migrate --apply` to add them."
        )
    for warning in type_warnings:
        emit(f"WARNING: schema type mismatch -- {warning}. --migrate never retypes; fix manually "
            f"if this causes a write to fail or truncate.")

    index_problems = []
    for name in TABLE_SPECS:
        if name not in tables:
            continue
        missing_unique = [s for s in missing_indexes(tables[name], TABLE_INDEXES[name]) if s["unique"]]
        if missing_unique:
            index_problems.append(f"{name}: {', '.join('+'.join(s['fields']) for s in missing_unique)}")
    if index_problems:
        raise SystemExit(
            "Schema drift -- table(s) are missing the unique index a retried write relies on to "
            "avoid duplicating rows:\n  " + "\n  ".join(index_problems)
            + "\nRun `--migrate --apply` to add them."
        )


def _schema_report(table: Any, name: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    return diff_schema(table, TABLE_SPECS[name][0]()), missing_indexes(table, TABLE_INDEXES[name])


def describe_migration(tables: Dict[str, Any]) -> bool:
    """Report what `--migrate --apply` would add. Returns True if anything is missing."""
    changed = False
    for name in TABLE_SPECS:
        if name not in tables:
            changed = True
            emit(f"{name}: table not found -- would create it.")
            continue
        missing_fields, missing_idx = _schema_report(tables[name], name)
        if missing_fields:
            changed = True
            emit(f"{name}: would add field(s) {', '.join(f['name'] for f in missing_fields)}.")
        for spec in missing_idx:
            changed = True
            kind = "unique " if spec["unique"] else ""
            emit(f"{name}: would add {kind}index on {', '.join(spec['fields'])}.")
    if not changed:
        emit("Schema is up to date; nothing to migrate.")
    return changed


def apply_migration(tables: Dict[str, Any], item: Any = None) -> None:
    """Add missing fields, then missing indexes. Additive only -- never drops or retypes.

    With `item` (the service's Item, from run_migration), a table entirely absent from `tables`
    (CollectorRuns/TaskChanges on a service that predates them) is created first via
    table_definition(), then falls through into the same field/index pass below. `item` is
    optional and defaults to None so existing callers that hand a partial tables dict scoped to
    one table -- by design, to keep a test about that table only -- keep working unchanged: a
    table missing from `tables` is silently skipped, exactly as before, when no item is given.
    """
    for name in TABLE_SPECS:
        if name not in tables:
            if item is None:
                continue
            from arcgis.features import FeatureLayerCollection

            flc = FeatureLayerCollection.fromitem(item)
            res = flc.manager.add_to_definition({"tables": [table_definition(name)]}) or {}
            if not res.get("success", True):
                raise SystemExit(f"{name}: add_to_definition failed creating the table: {res}")
            emit(f"{name}: created table.")
            tables = get_tables(item)
            if name not in tables:
                raise SystemExit(
                    f"{name}: created but not found on re-query -- re-run --migrate --apply."
                )
        table = tables[name]
        missing_fields, missing_idx = _schema_report(table, name)
        if missing_fields:
            res = table.manager.add_to_definition({"fields": missing_fields}) or {}
            if not res.get("success", True):
                raise SystemExit(
                    f"{name}: add_to_definition failed adding field(s) "
                    f"{', '.join(f['name'] for f in missing_fields)}: {res}"
                )
            emit(f"{name}: added field(s) {', '.join(f['name'] for f in missing_fields)}.")
        for spec in missing_idx:
            if spec["unique"]:
                dupes = find_duplicate_keys(table, spec["fields"])
                if dupes:
                    raise SystemExit(
                        f"{name}: cannot add a unique index on {', '.join(spec['fields'])} -- "
                        f"duplicate key(s) exist, e.g. {dupes}. Resolve the duplicates first."
                    )
            res = table.manager.add_to_definition({"indexes": [_index_definition(name, spec)]}) or {}
            if not res.get("success", True):
                raise SystemExit(
                    f"{name}: add_to_definition failed adding {'unique ' if spec['unique'] else ''}"
                    f"index on {', '.join(spec['fields'])}: {res}"
                )
            emit(f"{name}: added {'unique ' if spec['unique'] else ''}index on "
                 f"{', '.join(spec['fields'])}.")
    if item is not None:
        stamp_service_version(item)


def service_schema_version(item: Any) -> Optional[int]:
    """The schema version stamped on the service item, or None for an unstamped service."""
    properties = getattr(item, "properties", None) or {}
    return safe_int(properties.get("taskdash_schema_version")) if hasattr(properties, "get") else None


def stamp_service_version(item: Any) -> None:
    """Record which TaskDash and schema version last migrated the service, in the item's
    properties. Other keys in properties are kept. A newer stamp is never lowered."""
    current = service_schema_version(item)
    if current is not None and current > SCHEMA_VERSION:
        raise SystemExit(f"The service {item.id} has schema version {current}, newer than this "
                         f"TaskDash ({TASKDASH_VERSION}, schema {SCHEMA_VERSION}). Upgrade TaskDash.")
    properties = dict(getattr(item, "properties", None) or {})
    properties.update({"taskdash_version": TASKDASH_VERSION, "taskdash_schema_version": SCHEMA_VERSION})
    if not item.update(item_properties={"properties": json.dumps(properties)}):
        raise SystemExit(f"Could not stamp the version on service {item.id}. Re-run --migrate --apply.")
    emit(f"Service stamped: TaskDash {TASKDASH_VERSION}, schema {SCHEMA_VERSION}.")


# --------------------------------------------------------------------------- row prep

# Free-text fields that can carry a password (a task argument, a log line, an error). Scrubbed
# at write time on every published copy, always -- there is no switch. The raw value still feeds
# compute_definition_hash, which runs before prepare_rows, so scrubbing never fakes a change.
_SCRUBBED_FIELDS = {
    # description and config_warnings too: an owner-written description or a note can quote a
    # command line, and a note quotes paths.
    TABLE_INVENTORY: ("actions", "command_target_arguments", "description", "config_warnings"),
    TABLE_METRICS: ("sample_line",),
    TABLE_TASK_CHANGES: ("old_summary", "new_summary"),
    TABLE_COLLECTOR_RUNS: ("error_text",),
}
# privacy.<key> display switches. Read only through privacy_hides: a missing key hides.
_HIDDEN_FIELDS = {
    "hide_command_lines": {TABLE_INVENTORY: ("actions", "command_target_arguments")},
    "hide_sample_lines": {TABLE_METRICS: ("sample_line",)},
    "hide_accounts": {TABLE_INVENTORY: ("run_as_user",), TABLE_RUNS: ("user_context",)},
    # latest_log_name stays: it is a file name only, and TaskMetrics log_file_name shows it anyway.
    "hide_paths": {TABLE_INVENTORY: ("command_target_path", "command_target_working_directory",
                                     "latest_log_path")},
}


def privacy_hides(privacy: Optional[Dict[str, Any]], switch: str) -> bool:
    """privacy.<switch>: only an explicit false shows the fields. A missing key hides them, so a
    new install publishes no command line, account, path or log line until its owner opts in."""
    value = (privacy or {}).get(switch)
    return True if value is None else bool(value)


def _hidden_fields(table_name: str, privacy: Optional[Dict[str, Any]]) -> set:
    return {f for switch, tables in _HIDDEN_FIELDS.items() if privacy_hides(privacy, switch)
            for f in tables.get(table_name, ())}


def _published_value(key: str, value: Any, hidden: set, scrubbed: Sequence[str],
                     lengths: Dict[str, int], privacy: Optional[Dict[str, Any]] = None) -> Any:
    """The value a published copy may hold: blank when a switch hides it, else scrubbed (and, for
    a TaskChanges summary, masked per the switches)."""
    if key in hidden:
        return ""
    if key in scrubbed and isinstance(value, str):
        # Scrubbed whole, then cut: a cut first could leave half a secret with no flag before it,
        # and a redaction can lengthen the text past the field length.
        value = _scrub_secrets(value)
        if key in _SUMMARY_FIELDS:
            value = mask_summary(value, privacy)
        return value[:lengths.get(key)]
    return value


# TaskChanges summaries quote the hidden fields in free text: "Runs as: DOMAIN\user", "Actions: ...",
# "Working folder: C:\...", and notes that name a missing file. A blanked field does not reach them.
_SUMMARY_FIELDS = ("old_summary", "new_summary")
# The account runs up to its " (LogonType, RunLevel)" or the next " | " part.
_SUMMARY_ACCOUNT_PATTERN = re.compile(r"(Runs as: )(?:(?! \(| \| ).)+")
# Actions holds " | " between actions itself, so it ends only at a label that follows it in
# _definition_parts, or at the end.
_SUMMARY_ACTIONS_PATTERN = re.compile(
    r"(Actions: ).*?(?= \| (?:Runs as|Enabled|If already running|Time limit|Catch-up): |\Z)")
# A drive or UNC path. Inner folders may hold spaces (C:\Program Files\...), since a backslash
# follows them; the last part stops at a space. ':' is excluded, so two paths never merge.
_PATH_PART = r"""[^\\\s"'|;,()<>:*?]"""
_WINDOWS_PATH_PATTERN = re.compile(
    rf"(?:\b[A-Za-z]:|\\\\{_PATH_PART}+)\\(?:{_PATH_PART}+(?: {_PATH_PART}+)*\\)*{_PATH_PART}*")


def mask_summary(text: Any, privacy: Optional[Dict[str, Any]]) -> Any:
    """A change summary with what the privacy switches hide masked out of the text: the account
    after "Runs as:", the "Actions:" command line (both "(hidden)", as _definition_parts writes
    them) and Windows paths ("(path hidden)", as the config notes write them). With every switch
    false it returns the text unchanged."""
    if not isinstance(text, str) or not text:
        return text
    if privacy_hides(privacy, "hide_accounts"):
        text = _SUMMARY_ACCOUNT_PATTERN.sub(r"\1(hidden)", text)
    if privacy_hides(privacy, "hide_command_lines"):
        text = _SUMMARY_ACTIONS_PATTERN.sub(r"\1(hidden)", text)
    if privacy_hides(privacy, "hide_paths"):
        text = _WINDOWS_PATH_PATTERN.sub("(path hidden)", text)
    return text


def prepare_rows(rows: Sequence[Dict[str, Any]], table_name: str, collected_at: str,
                 privacy: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Convert ISO timestamps to epoch millis, stamp collection time, scrub secrets from
    free-text fields and blank the fields the privacy switches hide."""
    date_fields = DATE_FIELDS[table_name]
    specs = TABLE_SPECS[table_name][0]()
    valid = {f["name"] for f in specs} - {"OBJECTID"}
    lengths = {f["name"]: f["length"] for f in specs if f.get("length")}
    scrubbed = _SCRUBBED_FIELDS.get(table_name, ())
    hidden = _hidden_fields(table_name, privacy)
    prepared: List[Dict[str, Any]] = []

    for row in rows:
        clean: Dict[str, Any] = {}
        merged = dict(row)
        merged.setdefault("collected_at_utc", collected_at)
        for key, value in merged.items():
            if key not in valid:
                continue
            value = _published_value(key, value, hidden, scrubbed, lengths, privacy)
            clean[key] = to_epoch_millis(value) if key in date_fields else value
        prepared.append(clean)
    return prepared


DURATION_BASELINE_WINDOW_DAYS = 30
BLOCKED_WINDOW_HOURS = 24


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy's default "linear" method) over an already-sorted
    sequence. Stdlib-only -- statistics.quantiles' cut-point semantics don't map cleanly onto a
    named p5/p50/p95 at small sample sizes, so this is the plain, transparent version instead.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] * (hi - k) + sorted_values[hi] * (k - lo)


def summarize_runs(run_rows: Sequence[Dict[str, Any]],
                   now: Optional[dt.datetime] = None) -> Dict[str, Dict[str, Any]]:
    """Per-task last duration, 7-day counts, a 30-day duration baseline, and the inputs for
    stuck/blocked detection.

    `now` is the audit time the windows end at: the live collection's audit clock, or a replayed
    fixture's capture time. On the real clock a fixture's runs fall out of the 7-day window
    while its inventory is judged at capture time, and the two disagree.

    duration_seconds is only populated once a run has an end time, so "completed" here means
    duration_seconds is not None -- no separate is_completed field needs to be threaded through.
    last_completed_start_utc is that same finished run's own start, paired with
    last_duration_seconds, last_completed_result_text and last_completed_result_code
    (apply_own_run_view needs all four to describe one run, not the collector's own
    still-running current start -- task_detail-r3regress-own-task-running-previous-duration).
    The 30-day baseline (median/p95/p5, min 5 runs -- see enrich_inventory) is built only from
    completed, non-estimated runs in that window: an estimated start (keep_stored_start) makes
    duration_seconds unreliable, and a run from outside the window says nothing about "usual"
    duration today. Skipped-AlreadyRunning rows are excluded from all of that (as Missed already
    was) but are still counted, in the last 24h, for blocked-task detection.

    A Skipped-AlreadyRunning row whose instance_id matches a real run of the same task that
    started up to RUN_FAILURE_TOLERANCE_SECONDS earlier is a refused duplicate launch (Task
    Scheduler's daily-start and hourly-repeat firing in the same second), not a lost run: it is
    excluded from skipped_last_7_days and skipped_already_running_24h too
    (task_detail-taskdash-benign-skip-counted).
    """
    from taskdash_common import parse_iso_datetime

    now_ts = (now or utc_now()).timestamp()
    cutoff_7d = now_ts - (7 * 86400)
    cutoff_30d = now_ts - (DURATION_BASELINE_WINDOW_DAYS * 86400)
    cutoff_24h = now_ts - (BLOCKED_WINDOW_HOURS * 3600)
    summary: Dict[str, Dict[str, Any]] = {}

    real_run_starts: Dict[Tuple[str, str], dt.datetime] = {}
    for run in run_rows:
        if run.get("run_status") in collect.NON_RUN_STATUSES:
            continue
        task_key = run.get("task_full_name")
        instance_id = run.get("instance_id")
        if not task_key or not instance_id:
            continue
        started_at = parse_iso_datetime(run.get("start_time_utc"))
        if started_at is None:
            continue
        existing = real_run_starts.get((task_key, instance_id))
        if existing is None or started_at < existing:
            real_run_starts[(task_key, instance_id)] = started_at

    for run in run_rows:
        key = run.get("task_full_name")
        if not key:
            continue
        status = run.get("run_status")
        entry = summary.setdefault(key, {"last_duration_seconds": None, "_last_start": None,
                                         "_last_completed_start": None,
                                         "last_completed_is_error": 0,
                                         "last_completed_result_text": "",
                                         "last_completed_result_code": None,
                                         "runs_last_7_days": 0, "failures_last_7_days": 0,
                                         "skipped_last_7_days": 0,
                                         "completed_run_count": 0, "_duration_sum": 0.0,
                                         "last_run_is_error": 0, "last_run_status": "",
                                         "last_run_result_text": "", "_baseline_durations": [],
                                         "_running_since": None,
                                         "skipped_already_running_24h": 0})
        started = parse_iso_datetime(run.get("start_time_utc"))

        if status in collect.NON_RUN_STATUSES:  # Missed, Skipped-AlreadyRunning: not a run
            if status == "Skipped-AlreadyRunning" and started:
                instance_id = run.get("instance_id")
                real_start = real_run_starts.get((key, instance_id)) if instance_id else None
                if (real_start is not None
                        and 0 <= (started - real_start).total_seconds() <= RUN_FAILURE_TOLERANCE_SECONDS):
                    continue  # refused duplicate launch of a run that did start; not a lost run
            if started and started.timestamp() >= cutoff_7d:
                entry["skipped_last_7_days"] += 1
            if status == "Skipped-AlreadyRunning" and started and started.timestamp() >= cutoff_24h:
                entry["skipped_already_running_24h"] += 1
            continue

        if status == "Running" and started:
            if entry["_running_since"] is None or started > entry["_running_since"]:
                # newest Running row, not the earliest: a killed run from before collect-07
                # (2026-09-25) can be left stuck "Running" in history for up to 90 days, and
                # the earliest such row would then look like an ever-growing running time even
                # after the task has since completed many later runs cleanly.
                entry["_running_since"] = started

        if started and status != "Running" and (
                entry["_last_completed_start"] is None or started > entry["_last_completed_start"]):
            # The newest FINISHED run gives the duration: a run still going has none yet, and
            # Task Dash always sees its own run in progress, so its duration was always blank.
            entry["_last_completed_start"] = started
            entry["last_duration_seconds"] = run.get("duration_seconds")
            entry["last_completed_is_error"] = 1 if run.get("is_error") else 0
            entry["last_completed_result_text"] = normalize_text(run.get("result_text"))
            entry["last_completed_result_code"] = safe_int(run.get("result_code"))
        if started:
            if entry["_last_start"] is None or started > entry["_last_start"]:
                entry["_last_start"] = started
                entry["last_run_start"] = started
                entry["last_run_is_error"] = 1 if run.get("is_error") else 0
                entry["last_run_status"] = normalize_text(status)
                entry["last_run_result_text"] = normalize_text(run.get("result_text"))
            if started.timestamp() >= cutoff_7d:
                entry["runs_last_7_days"] += 1
                entry["failures_last_7_days"] += 1 if run.get("is_error") else 0
        duration = run.get("duration_seconds")
        if duration is not None:
            entry["completed_run_count"] += 1
            entry["_duration_sum"] += duration
            if (not run.get("start_time_estimated") and started
                    and started.timestamp() >= cutoff_30d):
                entry["_baseline_durations"].append(duration)

    for entry in summary.values():
        entry.pop("_last_start", None)
        duration_sum = entry.pop("_duration_sum")
        count = entry["completed_run_count"]
        entry["mean_duration_seconds"] = (duration_sum / count) if count else None

        baseline = sorted(entry.pop("_baseline_durations"))
        entry["baseline_run_count"] = len(baseline)
        if len(baseline) >= MIN_RUNS_FOR_DURATION_BASELINE:
            entry["baseline_median_seconds"] = _percentile(baseline, 50)
            entry["baseline_p95_seconds"] = _percentile(baseline, 95)
            entry["baseline_p5_seconds"] = _percentile(baseline, 5)
        else:
            entry["baseline_median_seconds"] = None
            entry["baseline_p95_seconds"] = None
            entry["baseline_p5_seconds"] = None

        running_since = entry.pop("_running_since")
        entry["running_since"] = running_since.isoformat() if running_since else None

        last_completed_start = entry.pop("_last_completed_start")
        entry["last_completed_start_utc"] = last_completed_start.isoformat() if last_completed_start else None
    return summary


def summarize_runs_from_agol(runs_table: Any, host_name: str,
                             now: Optional[dt.datetime] = None) -> Dict[str, Dict[str, Any]]:
    """Same summary, but over the accumulated table.

    Necessary because collection is incremental: a given pass may contain no runs at all for a
    task, so per-pass counts would report zero for a task that ran fine yesterday.
    """
    from taskdash_common import escape_sql_string

    where = f"host_name = '{escape_sql_string(host_name)}'"
    rows = query_all_rows(runs_table, where=where,
                          out_fields="task_full_name,start_time_utc,duration_seconds,is_error,"
                                     "run_status,result_text,result_code,start_time_estimated,"
                                     "instance_id")
    for row in rows:  # epoch millis -> ISO so the shared summarizer can parse it
        millis = row.get("start_time_utc")
        if millis:
            row["start_time_utc"] = dt.datetime.fromtimestamp(
                millis / 1000, tz=dt.timezone.utc).isoformat()
    return summarize_runs(rows, now)


MIN_RUNS_FOR_DURATION_BASELINE = 5
DURATION_ANOMALY_HIGH_RATIO = 2.0
DURATION_ANOMALY_LOW_RATIO = 0.33
STUCK_MIN_SECONDS = 30 * 60
STUCK_P95_MULTIPLIER = 3.0
BLOCKED_SKIPPED_THRESHOLD = 2


RUN_FAILURE_TOLERANCE_SECONDS = 60


def is_own_task(task: Dict[str, Any]) -> bool:
    """True for the scheduled task that runs this collector: its script is in this folder."""
    target = normalize_text(task.get("command_target_path"))
    if not target:
        return False
    folder = os.path.dirname(os.path.abspath(target))
    return os.path.normcase(folder) == os.path.normcase(str(runtime_dir()))


def apply_own_run_view(task: Dict[str, Any], stats: Dict[str, Any]) -> str:
    """Judge Task Dash by its previous finished run while it collects.

    The collector runs inside its own scheduled task, so Task Scheduler always reports that task
    as Running and its row read "Running" forever. Call after apply_run_level_failure has
    restored the collected base, so both enrich_inventory passes give the same result.
    Returns the attention sentence, or "".

    last_run_time also moves to that previous run's own start: left alone, it stays the
    collector's current (still-running) start from collect.py, and enrich_inventory pairs it with
    last_duration_seconds from the previous run -- two different runs shown as one
    (task_detail-r3regress-own-task-running-previous-duration).
    """
    if task.get("health_status") != "Running" or not is_own_task(task):
        return ""
    text = stats.get("last_completed_result_text")
    if not text:  # no finished run stored yet: Running is all we know
        return ""
    failed = 1 if stats.get("last_completed_is_error") else 0
    task.update(health_status="Error" if failed else "Success", last_run_failed=failed,
                needs_attention=failed, last_result_text=text[:500])
    completed_start = stats.get("last_completed_start_utc")
    if completed_start:
        task["last_run_time"] = completed_start
    completed_code = stats.get("last_completed_result_code")
    if completed_code is not None:
        # Otherwise last_result_code stays 267009 ("currently running", collect.py's own read of
        # this still-running task) next to last_result_text/last_run_time from the previous
        # finished run (operations-own-task-result-code-stale).
        task["last_result_code"] = completed_code
    return f"{_LIVE_STATE_PREFIX}{text}"


def apply_finished_run(task: Dict[str, Any], stats: Dict[str, Any]) -> None:
    """Show a task as finished when its newest collected run has already ended.

    collect.py reads the task list before the event log. A task that starts a second before the
    collector is Running in that read, but the events read later hold its end (seen 2026-10-16:
    a task shown Running for an hour after a 17 s run). The newest run only counts when it
    is at least as new as LastRunTime, so an old finished run cannot end a run still going.
    A failed finish is apply_run_level_failure's (it runs first); this only clears the stale
    state and, for a successful finish, the Running health. Task Dash keeps apply_own_run_view.
    """
    if normalize_text(task.get("state")).lower() != "running" or is_own_task(task):
        return
    newest = stats.get("last_run_start")
    if newest is None or stats.get("last_run_status") in ("", "Running"):
        return  # no run seen yet, or the newest run has no end event: it is still going
    last_run = parse_iso_datetime(task.get("last_run_time"))
    if last_run is None or newest < last_run - dt.timedelta(seconds=RUN_FAILURE_TOLERANCE_SECONDS):
        return
    if task.get("health_status") == "Error":
        task["state"] = "Ready"  # apply_run_level_failure judged the finish; only the state is stale
        return
    if task.get("health_status") != "Running" or stats.get("last_run_status") != "Success":
        return  # ponytail: a Warning finish stays Running until the next read; rare
    task.update(state="Ready", health_status="Success",
                last_result_text=normalize_text(stats.get("last_completed_result_text"))[:500]
                or task.get("last_result_text"))
    if stats.get("last_completed_start_utc"):
        task["last_run_time"] = stats["last_completed_start_utc"]
    if stats.get("last_completed_result_code") is not None:
        task["last_result_code"] = stats["last_completed_result_code"]


# The health sentence collect.build_attention_reason adds for a task with no problem. It must go
# when a failure or warning leads the reason: "last run failed: ...; The operation completed
# successfully." contradicted itself.
_BASE_STATUS_SENTENCES = ("task is currently running", "task has not yet run")


def _without_status_sentence(base: str, result_text: str) -> str:
    drop = {s.casefold() for s in _BASE_STATUS_SENTENCES}
    drop.add(normalize_text(result_text).rstrip(".").casefold())
    return "; ".join(p for p in base.split("; ") if p.strip().rstrip(".").casefold() not in drop)


# The same words as taskdash_dashboard.PALETTE, so the reason and the Status column agree.
# Kept here because publish does not import the dashboard module.
RUN_STATUS_WORDS = {"Error": "Failed", "StartFailed": "Did not start",
                    "NotLoggedOn": "Account not signed in", "KilledTimeLimit": "Stopped at time limit",
                    "Terminated": "Terminated", "Stopped": "Stopped"}


def apply_run_level_failure(task: Dict[str, Any], stats: Dict[str, Any]) -> str:
    """Fail the task when its newest run row failed, even if LastTaskResult says success.

    A launch that never happened (not logged on, launch failure, killed on its time limit) is a
    TaskRuns error row, but Task Scheduler may leave LastTaskResult at the previous run's
    success. Health, last_run_failed and the alert e-mail would then miss it. The run row only
    counts when it is at least as new as LastRunTime, so an old failure cannot resurface.
    Idempotent: enrich_inventory runs twice per publish, so the collected values are stashed.
    Returns the attention sentence to lead with, or "".
    """
    # "state" too: apply_finished_run sets it to Ready, and the second pass must start from the
    # collected Running again or it would skip the task.
    fields = ("health_status", "last_run_failed", "needs_attention", "last_result_text", "last_run_time",
              "last_result_code", "state")
    base = task.setdefault("_health_base", {f: task.get(f) for f in fields})
    task.update(base)
    started = stats.get("last_run_start")
    if not stats.get("last_run_is_error") or task.get("last_run_failed") or started is None:
        return ""
    if task.get("health_status") == "Disabled":
        return ""  # switched off on purpose: its last failure is history, not an alert
    last_run = parse_iso_datetime(task.get("last_run_time"))
    if last_run and started < last_run - dt.timedelta(seconds=RUN_FAILURE_TOLERANCE_SECONDS):
        return ""
    status = stats.get("last_run_status") or "failed"
    text = stats.get("last_run_result_text") or status
    if text == collect.describe_hresult(0):
        # A launch-failure event can carry result code 0, which describes as success.
        text = (collect.OUTCOME_EVENT_STATUS[103][2] if status == "StartFailed"
                else "The run did not end normally.")
    task.update(health_status="Error", last_run_failed=1, needs_attention=1,
                last_result_text=text[:500])
    return f"Last run: {RUN_STATUS_WORDS.get(status, 'Failed')}. {text}"


def apply_stuck_or_blocked_warning(
    task: Dict[str, Any], stats: Dict[str, Any], execution_time_limit_seconds: Optional[float],
    now: Optional[dt.datetime] = None,
) -> str:
    """Warning-only overlays a clean exit code can't see: a run stuck well past how long this
    task usually takes (or past its own ExecutionTimeLimit), and a task whose new runs are being
    skipped because an earlier run of it is still going.

    Never raises to Error, and never fires over a real run-level failure or a task switched off
    on purpose -- apply_run_level_failure has already reset task["health_status"] to this call's
    base value (see its own idempotency note), so reading it here is enough to defer to Error.
    Elapsed time is only evaluated while the live task is actually Running: TaskRuns can hold a
    Running row up to 90 days old (a pre-collect-07 kill left stuck forever, or just today's
    newest row from a task that has since finished), and trusting it regardless of live state
    would flag an already-healthy task. execution_time_limit_seconds of 0 (ExecutionTimeLimit
    PT0S) means "never times out", not "always past its limit" -- treated as falsy here, same as
    None, so it can never trip past_limit on its own.
    Returns the attention sentence to add, or "".

    Also stashes _is_stuck/_is_blocked (0/1) on the task -- private, not an inventory_fields()
    column -- so enrich_inventory can pick attention_kind without re-deriving these conditions
    or parsing the sentence text. Recomputed fresh every call, same as the sentence itself, so
    it stays idempotent across enrich_inventory's two passes with no stashed base needed.
    """
    task["_is_stuck"] = 0
    task["_is_blocked"] = 0
    if task.get("health_status") in ("Error", "Disabled"):
        return ""

    reasons = []
    running_since = parse_iso_datetime(stats.get("running_since"))
    is_live_running = normalize_text(task.get("state")).lower() == "running"
    if is_live_running and running_since:
        elapsed = ((now or utc_now()) - running_since).total_seconds()
        p95 = stats.get("baseline_p95_seconds")
        threshold = max(STUCK_P95_MULTIPLIER * p95, STUCK_MIN_SECONDS) if p95 else STUCK_MIN_SECONDS
        past_limit = bool(execution_time_limit_seconds) and elapsed > execution_time_limit_seconds
        if elapsed > threshold or past_limit:
            usual = stats.get("baseline_median_seconds") or stats.get("mean_duration_seconds")
            # ponytail: falls back to "" (no "usually" clause) when neither baseline stat is
            # known yet (a brand-new task); good enough until it has run a few times.
            usually = f", usually about {short_duration(usual)}" if usual else ""
            reasons.append(f"Still running {short_duration(elapsed)}{usually}")
            task["_is_stuck"] = 1

    if (stats.get("skipped_already_running_24h") or 0) >= BLOCKED_SKIPPED_THRESHOLD:
        reasons.append("Blocked: new runs skipped because an earlier run is still going")
        task["_is_blocked"] = 1

    if not reasons:
        return ""
    task["health_status"] = "Warning"
    task["needs_attention"] = 1
    return "; ".join(reasons)


def _run_duration(seconds: float) -> str:
    """One run's measured length, the same as the dashboard's Arcade fmtDur ("24 min 14 s").

    short_duration rounds to "24 min", so Recent changes and Failed runs showed one run two
    ways (audit 7). short_duration stays for usual values and elapsed times, which are rough.
    """
    if seconds < 1:
        return "< 1 s"
    total = int(seconds + 0.5)  # Arcade Round(s, 0) rounds half up, Python round() to even
    if total < 60:
        return f"{total} s"
    if total < 3600:
        sec = total % 60
        return f"{total // 60} min" + (f" {sec} s" if sec else "")
    mins = (total % 3600) // 60
    return f"{total // 3600} h" + (f" {mins} min" if mins else "")


def _versus_usual(ratio: float) -> str:
    """Wording for a duration ratio. A fast run's ratio is below 1, and "0.0 times" read as zero.

    Must agree with the Task Detail card's anomalyText (taskdash_dashboard.py): both floor the
    same stored duration_ratio (rounded to 3 places) so the two never differ on a boundary.
    """
    ratio = round(ratio, 3)
    if ratio >= 1:
        return f"about {ratio:.1f} times the usual"
    pct = int(round(ratio * 100, 6))  # 0.29 * 100 is 28.999...: floor the rounded product
    return "under 1% of the usual" if pct < 1 else f"about {pct}% of the usual"


def enrich_inventory(
    inventory: List[Dict[str, Any]],
    run_summary: Dict[str, Dict[str, Any]],
    log_summary: Dict[str, Dict[str, Any]],
    execution_time_limits: Optional[Dict[str, Optional[float]]] = None,
    now: Optional[dt.datetime] = None,
) -> None:
    """Fold last-run duration, 7-day counts, a duration-anomaly flag, stuck/blocked warnings,
    and log summary onto each row. `now` is the audit time (see summarize_runs).

    The duration-anomaly baseline is this task's own 30-day median/p95/p5 (summarize_runs),
    gated on >=5 baseline runs so a new task's first couple of runs can't trip it; it stays
    advisory (health_status untouched). Stuck/blocked instead set health_status to Warning (never
    Error) -- see apply_stuck_or_blocked_warning.
    """
    execution_time_limits = execution_time_limits or {}
    for task in inventory:
        key = task["task_full_name"]
        stats = run_summary.get(key, {})
        run_failure = apply_run_level_failure(task, stats) or apply_own_run_view(task, stats)
        apply_finished_run(task, stats)
        warning = apply_stuck_or_blocked_warning(task, stats, execution_time_limits.get(key), now)
        task["last_duration_seconds"] = stats.get("last_duration_seconds")
        task["runs_last_7_days"] = stats.get("runs_last_7_days", 0)
        task["failures_last_7_days"] = stats.get("failures_last_7_days", 0)
        task["skipped_last_7_days"] = stats.get("skipped_last_7_days", 0)

        last_duration = stats.get("last_duration_seconds")
        median = stats.get("baseline_median_seconds")
        p95 = stats.get("baseline_p95_seconds")
        p5 = stats.get("baseline_p5_seconds")
        baseline_count = stats.get("baseline_run_count", 0)
        ratio = None
        is_anomaly = 0
        if (baseline_count >= MIN_RUNS_FOR_DURATION_BASELINE and median
                and last_duration is not None):
            ratio = last_duration / median
            high = ratio > DURATION_ANOMALY_HIGH_RATIO and p95 is not None and last_duration > p95
            low = ratio < DURATION_ANOMALY_LOW_RATIO and p5 is not None and last_duration < p5
            is_anomaly = 1 if (high or low) else 0
        task["duration_ratio"] = round(ratio, 3) if ratio is not None else None
        task["is_duration_anomaly"] = is_anomaly
        # Rebuild from a stashed base rather than appending: enrich_inventory runs twice on the
        # same list (once in collect_everything, again in publish once the stored runs are
        # known), so a plain append duplicated the sentence and could push the real result text
        # past the 1000-char field limit. warning is recomputed fresh from stats every call too,
        # so it stays idempotent the same way.
        base = task.setdefault("_attention_base", task.get("attention_reason", "") or "")
        if run_failure or warning:
            base = _without_status_sentence(base, task["_health_base"]["last_result_text"])
        # wording-16: matches the Task Detail card's anomalyText ("Duration: 1.9 times the
        # usual"), which already shows the same fact in a shorter form -- the two used to read
        # as two separate anomalies with different numbers reported.
        sentence = (f"Last run took {_run_duration(last_duration)}, {_versus_usual(ratio)} "
                    f"{short_duration(median)}") if is_anomaly else ""
        # Parts often end in "." (result text, "Last run: Failed."); joining with "; " left ".;".
        # Strip only at the join so a lone part keeps its final period.
        task["attention_reason"] = "; ".join(
            p for p in (run_failure, warning, base, sentence) if p).replace(".; ", "; ")[:1000]

        task.update(log_summary.get(key, {}))

        # Single primary reason, by precedence, after every health rule above has run (config
        # warnings never set it -- those are definition-risk, reported separately). Recomputed
        # fresh from the fields those rules just set, so it stays idempotent across
        # enrich_inventory's two passes the same way attention_reason does. Must agree with
        # needs_attention: "" exactly when needs_attention == 0 -- see test_attention_kind.py.
        # Gated on needs_attention first: collect.py computes is_overdue apart from health, so a
        # 0x41301 "Running" row with a stale state can be overdue yet need no attention.
        # OVERDUE outranks STUCK/BLOCKED per the owner-approved layout (phase 4, section 7).
        if not task.get("needs_attention"):
            kind = ""
        elif task.get("last_run_failed"):
            kind = "FAILED"
        elif task.get("is_overdue"):
            kind = "OVERDUE"
        elif task.get("_is_stuck"):
            kind = "STUCK"
        elif task.get("_is_blocked"):
            kind = "BLOCKED"
        else:
            kind = "WARNING"
        task["attention_kind"] = kind

    stamp_success_rate(inventory)


MIN_FAILURES_FOR_LOWEST_SUCCESS_RATE = 2


def stamp_success_rate(inventory: List[Dict[str, Any]],
                       other_hosts: Sequence[Dict[str, Any]] = ()) -> Optional[float]:
    """Stamp the fleet-wide 7-day success rate on every row this host writes; return it.

    A dashboard indicator surfaces only one statistic to $datapoint (a second
    statisticDefinition is dropped -- found by render), so the ratio arrives precomputed and the
    tile reads MIN(lowest_success_rate_7d). other_hosts are the other hosts' stored TaskInventory
    rows (fetch_other_host_counts): adding their per-task 7-day ratios to this host's makes every
    host stamp the same fleet value, so MIN makes a silent host's old stamp err toward the alarm
    for any number of hosts. With one host other_hosts is empty and the value is exactly the
    per-host rate it always was. success_rate_7d (the mean, below) is kept for the collector log
    line; no widget reads it.

    The rate is the mean of each task's own (runs - fails) / runs over tasks with
    runs_last_7_days > 0, not a run-weighted ratio: a 15-minute task with hundreds of runs would
    otherwise swamp a daily task that failed every night (indicators-01).

    lowest_success_rate_7d is the lowest of those same per-task ratios, but only over tasks with
    at least MIN_FAILURES_FOR_LOWEST_SUCCESS_RATE failures in the window
    (indicators-lowest-rate-flags-single-failure): without that floor, one accidental failure of
    a daily or weekly task (9 or fewer runs in 7 days) drops it below the OK threshold for a full
    week, while a 15-minute task can fail 30 times and still read OK. 100.0 when tasks ran but
    none has that many failures; None when no task in the window ran.
    """
    rows = [*inventory, *other_hosts]
    ratios = []
    qualifying_ratios = []
    for t in rows:
        runs = safe_int(t.get("runs_last_7_days")) or 0
        if runs <= 0:
            continue
        fails = safe_int(t.get("failures_last_7_days")) or 0
        ratio = (runs - fails) / runs
        ratios.append(ratio)
        if fails >= MIN_FAILURES_FOR_LOWEST_SUCCESS_RATE:
            qualifying_ratios.append(ratio)
    rate = round(100.0 * sum(ratios) / len(ratios), 2) if ratios else None
    if not ratios:
        lowest = None
    elif qualifying_ratios:
        lowest = round(100.0 * min(qualifying_ratios), 2)
    else:
        lowest = 100.0
    for task in inventory:
        task["success_rate_7d"] = rate
        task["lowest_success_rate_7d"] = lowest
    return rate


def fetch_other_host_counts(table: Any, host_name: str, now: dt.datetime) -> List[Dict[str, Any]]:
    """Other hosts' stored per-task 7-day counts, for stamp_success_rate.

    A host that has not collected for 7 days is left out: its whole window has passed, so its
    counts describe no run in the current one. Its rows (and its old stamp, which the tile's MIN
    can still pick) stay until they are deleted -- the heartbeat already shows that host DOWN.
    """
    rows = query_all_rows(table, where=f"host_name <> '{escape_sql_string(host_name)}'",
                          out_fields="host_name,collected_at_utc,runs_last_7_days,failures_last_7_days")
    cutoff = to_epoch_millis(now - dt.timedelta(days=7))
    return [r for r in rows if (safe_float(r.get("collected_at_utc")) or 0) >= cutoff]


# --------------------------------------------------------------------------- publishing

def service_key(target: str) -> str:
    return "item_id" if target == "prod" else "test_item_id"


def ensure_service(gis: Any, config: Dict[str, Any], target: str) -> Any:
    agol = config.get("arcgis", {})
    key = service_key(target)
    item_id = normalize_text(agol.get(key))
    if not item_id:
        raise SystemExit(
            f"No arcgis.{key} in the config. A new install runs `taskdash setup --apply`, which "
            f"creates the service and fills the id in. To add a service to an existing install, "
            f"run --create-service, then paste the returned item id into the config."
        )
    item = gis.content.get(item_id)
    if item is None:
        raise SystemExit(f"ArcGIS item {item_id} not found or not visible to this account.")
    return item


def create_service(gis: Any, service_name: str, config: Optional[Dict[str, Any]] = None,
                   folder: Optional[str] = None) -> Any:
    """Create an empty hosted table service with the three original TaskDash tables.

    CollectorRuns/TaskChanges are additive (3.6/3.7) and deliberately not created here --
    apply_migration adds them to a service made by this function too, so there is one path
    for "this service is missing a table" regardless of when the table was introduced. Callers
    run it right after (--create-service and taskdash_setup do).
    """
    from arcgis.features import FeatureLayerCollection

    existing = gis.content.search(f'title:"{service_name}" AND owner:{gis.users.me.username}',
                                  item_type="Feature Service")
    for candidate in existing:
        if candidate.title == service_name:
            raise SystemExit(
                f"A feature service named '{service_name}' already exists ({candidate.id}). "
                f"Delete it or choose another name with --service-name."
            )

    agol = (config or {}).get("arcgis", {})
    description = normalize_text(agol.get("service_description")) or "TaskDash - scheduled task monitoring"
    tags = normalize_text(agol.get("service_tags")) or "taskdash,monitoring,scheduled tasks"

    # No is_table kwarg in arcgis 2.4.3: create a bare feature service, then add the tables.
    item = gis.content.create_service(
        name=service_name,
        service_description=description,
        has_static_data=False,
        max_record_count=10000,
        capabilities="Query,Editing,Create,Update,Delete",
        create_params={
            "name": service_name,
            "serviceDescription": description,
            "hasStaticData": False,
            "maxRecordCount": 10000,
            "supportedQueryFormats": "JSON",
            "capabilities": "Query,Editing,Create,Update,Delete",
            "allowGeometryUpdates": False,
            "units": "esriMeters",
            "layers": [],
            "tables": [],
            "xssPreventionInfo": {"xssPreventionEnabled": True,
                                  "xssPreventionRule": "InputOnly",
                                  "xssInputRule": "rejectInvalid"},
        },
        tags=tags,
        snippet="Scheduled task health, run history and log metrics.",
        folder=folder,
    )

    flc = FeatureLayerCollection.fromitem(item)
    flc.manager.add_to_definition({"tables": [table_definition(n) for n in
                                              (TABLE_INVENTORY, TABLE_RUNS, TABLE_METRICS)]})
    emit(f"Created service '{service_name}' -> item id {item.id}")
    return item


# --------------------------------------------------------------------------- read-only view

VIEW_CAPABILITIES = "Query"


def refuse_public_sharing(everyone: Any) -> None:
    """The one gate for every sharing call: TaskDash data never goes to everyone, no override."""
    if everyone:
        raise SystemExit("TaskDash never shares an item with everyone (public). Share it with the "
                         "organization or named groups instead; there is no override flag.")


def share_item(item: Any, *, everyone: bool = False, org: bool = False,
               groups: Sequence[Any] = ()) -> None:
    refuse_public_sharing(everyone)
    if not org and not groups:
        # Nothing to share, so share() is skipped: it raises for an admin account that does not
        # own the item (a view lands in the source owner's content). Confirm the item really is
        # private, or a share made outside this call would go unnoticed.
        access = getattr(item, "access", None)
        if access != "private":
            raise SystemExit(f"Item {item.id} should be private but its access is '{access}'. Set its "
                             f"sharing to Owner in ArcGIS Online, then re-run.")
        return None
    # Item.sharing, not Item.share: share() is deprecated since arcgis 2.3 and gone in 3.0.
    # Raising the level to ORG keeps the groups; nothing here ever lowers a level.
    if org:
        item.sharing.sharing_level = "ORG"
    for group in groups:
        item.sharing.groups.add(group)
    # groups.add() returns False both for a failed share and for a group already shared, so the
    # item's group list is the one answer that tells the two apart. list() reads the org group
    # search index, which can lag a group created seconds earlier, so a miss is re-read first.
    for attempt in range(4):
        have = {g.id for g in item.sharing.groups.list()}
        not_shared = [getattr(g, "id", g) for g in groups if getattr(g, "id", g) not in have]
        if not not_shared or attempt == 3:
            break
        time.sleep(5 * (attempt + 1))
    if not_shared:
        raise SystemExit(f"Item {item.id} was not shared with group(s) {', '.join(map(str, not_shared))}. "
                         f"Share it with them by hand, or add the collector account to those groups "
                         f"and re-run.")


def item_sharing(item: Any) -> Dict[str, Any]:
    """{'everyone', 'org', 'groups'} from Item.sharing; Item.shared_with is gone in arcgis 3.0.
    Makes portal requests, so the hourly check (check_source_sharing) never calls it."""
    shared = item.sharing.shared_with
    level = getattr(shared.get("level"), "name", "PRIVATE")
    return {"everyone": level == "EVERYONE", "org": level in ("ORG", "EVERYONE"),
            "groups": list(shared.get("groups") or [])}


def view_key(target: str) -> str:
    return "view_item_id" if target == "prod" else "test_view_item_id"


# Hidden on the view whatever the switches say. definition_hash is an unsalted SHA-256 of the raw
# command line, so a short or guessable secret in it could be confirmed offline; no widget reads it.
VIEW_ALWAYS_HIDDEN = {TABLE_INVENTORY: ("definition_hash",)}


def view_hidden_fields(config: Dict[str, Any]) -> Dict[str, List[str]]:
    """Fields the view hides, by table: the privacy.hide_* switches (a missing switch hides),
    then VIEW_ALWAYS_HIDDEN."""
    privacy = config.get("privacy")
    hidden: Dict[str, List[str]] = {}
    groups = [tables for switch, tables in _HIDDEN_FIELDS.items() if privacy_hides(privacy, switch)]
    for tables in groups + [VIEW_ALWAYS_HIDDEN]:
        for table, fields in tables.items():
            hidden.setdefault(table, []).extend(f for f in fields if f not in hidden.get(table, ()))
    return hidden


def _definition_update(manager: Any, payload: Dict[str, Any], what: str) -> None:
    res = manager.update_definition(payload) or {}
    if not res.get("success", True):
        raise SystemExit(f"View {what}: update_definition failed: {res}")


def resend_view_capabilities(view_item: Any, view_tables: Dict[str, Any]) -> None:
    """Query only, on the view and on every layer/table. Re-sent after ANY definition change:
    hiding fields through updateDefinition once turned Query on for anonymous users."""
    from arcgis.features import FeatureLayerCollection

    payload = {"capabilities": VIEW_CAPABILITIES}
    _definition_update(FeatureLayerCollection.fromitem(view_item).manager, payload, "capabilities")
    for name, table in view_tables.items():
        _definition_update(table.manager, payload, f"{name} capabilities")


def view_missing_fields(view_tables: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, List[str]]:
    """Expected fields absent from the view, by table; deliberately hidden fields do not count."""
    hidden = view_hidden_fields(config)
    missing: Dict[str, List[str]] = {}
    for name in TABLE_SPECS:
        if name not in view_tables:
            continue
        names = [f["name"] for f in diff_schema(view_tables[name], TABLE_SPECS[name][0]())
                 if f["name"] not in hidden.get(name, ())]
        if names:
            missing[name] = names
    return missing


def view_shown_hidden_fields(view_tables: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, List[str]]:
    """Fields the view should hide but still shows, by table (a hidden field is absent there)."""
    shown: Dict[str, List[str]] = {}
    for name, fields in view_hidden_fields(config).items():
        if name not in view_tables:
            continue
        absent = {f["name"] for f in diff_schema(view_tables[name], TABLE_SPECS[name][0]())}
        names = [f for f in fields if f not in absent]
        if names:
            shown[name] = names
    return shown


def migrate_view(view_item: Any, config: Dict[str, Any], apply: bool) -> None:
    """Report (or with apply, fix) the view's fields: expose service fields it lacks, and hide
    fields the switches hide that it still shows. Whether AGOL views pick up new source fields on
    their own is unverified, so this reads the view as it is now and acts only on the difference."""
    view_tables = get_tables(view_item)
    absent = sorted(set(TABLE_SPECS) - set(view_tables))
    if absent:
        emit(f"WARNING: the view has no table(s) {', '.join(absent)}; a view cannot gain a "
             f"table here. Create a new view with --create-view and update the config.")
    missing = view_missing_fields(view_tables, config)
    exposed = view_shown_hidden_fields(view_tables, config)
    if not missing and not exposed:
        emit("View: fields are up to date.")
        return
    for name, names in missing.items():
        emit(f"View {name}: {'exposing' if apply else 'would expose'} field(s) {', '.join(names)}.")
    for name, names in exposed.items():
        emit(f"View {name}: {'hiding' if apply else 'would hide'} field(s) {', '.join(names)}.")
    if not apply:
        return

    def update() -> None:
        for name in TABLE_SPECS:
            fields = ([{"name": n, "visible": True} for n in missing.get(name, ())]
                      + [{"name": n, "visible": False} for n in exposed.get(name, ())])
            if fields:
                _definition_update(view_tables[name].manager, {"fields": fields}, f"{name} fields")

    update_then_resend(update, view_item, lambda: get_tables(view_item))


def update_then_resend(update: Callable[[], None], view_item: Any,
                       view_tables: Callable[[], Dict[str, Any]]) -> None:
    """Run a view field update, then re-send the capabilities even after a failed update: a partly
    applied field change can still have reopened Query. A re-send that fails while the update's
    own error propagates is a WARNING, so it never masks that error."""
    try:
        update()
    except BaseException:
        try:
            resend_view_capabilities(view_item, view_tables())
        except Exception as exc:
            emit(f"WARNING: re-sending the view capabilities ({VIEW_CAPABILITIES}) also failed "
                 f"({type(exc).__name__}: {exc}). Re-run --migrate --apply, then --check-anonymous.")
        raise
    resend_view_capabilities(view_item, view_tables())


def refuse_public_source(item: Any, shared: Optional[Dict[str, Any]] = None) -> None:
    """A service shared with everyone exposes every field and the edit path; no view fixes that.
    shared: item_sharing(item), only where the caller has already paid for it (create_view)."""
    if getattr(item, "access", None) == "public" or (shared or {}).get("everyone"):
        raise SystemExit(f"The service {item.id} is shared with everyone. Set its sharing to Owner "
                         f"in ArcGIS Online, then re-run. Share only the read-only view.")


def warn_shared_source(item: Any) -> None:
    # Only the two shared values warn: a missing access (an item read without it) proves nothing.
    access = getattr(item, "access", None)
    if access in ("org", "shared"):
        emit(f"WARNING: the editable service {item.id} is shared ('{access}'). Set its sharing to "
             f"Owner in ArcGIS Online and share only the read-only view.")


def check_source_sharing(item: Any, config: Dict[str, Any], target: str) -> None:
    """With a view configured, the service itself must not be shared: Esri's view pattern keeps the
    editable source owner-only. Reads only item.access, loaded with the item: item_sharing
    makes several portal requests, and this runs hourly outside the outage-tolerant block."""
    if not normalize_text(config.get("arcgis", {}).get(view_key(target))):
        return
    refuse_public_source(item)
    warn_shared_source(item)


def create_view(gis: Any, item: Any, config: Dict[str, Any], apply: bool,
                target: str = "prod", folder: Optional[str] = None,
                on_create: Optional[Callable[[Any], None]] = None) -> Optional[Any]:
    """Plan (default) or create the read-only view of the service, then print its item id.
    on_create gets the view as soon as it exists, so a caller can record it before a later step fails."""
    from arcgis.features import FeatureLayerCollection

    name = f"{item.title}_view"
    hidden = view_hidden_fields(config)
    # access first: a public service stops here before the sharing requests.
    refuse_public_source(item)
    shared = item_sharing(item)
    refuse_public_source(item, shared)
    org = bool(shared.get("org"))
    groups = [g.id for g in shared.get("groups") or []]
    emit(f"Plan: view '{name}' of {item.id}, capabilities {VIEW_CAPABILITIES}.")
    emit(f"Sharing copied from the service: org={org}, groups={groups or 'none'}.")
    emit("Fields hidden: " + ("; ".join(f"{t}: {', '.join(f)}" for t, f in hidden.items()) or "none"))
    if not apply:
        return None

    # The view lands in the SOURCE item owner's content even when an admin account creates it.
    owner = getattr(item, "owner", None) or gis.users.me.username
    for candidate in gis.content.search(f'title:"{name}" AND owner:{owner}',
                                        item_type="Feature Service"):
        if candidate.title == name:
            raise SystemExit(f"A feature service named '{name}' already exists ({candidate.id}). "
                             f"It may be half-configured by an earlier failed run, with no fields "
                             f"hidden. Delete it in ArcGIS Online and re-run; do not set "
                             f"arcgis.{view_key(target)} to it.")
    # updateable=False: the library default makes an updatable view, one setting from an edit path.
    extra = {"folder": folder} if folder else {}
    view = FeatureLayerCollection.fromitem(item).manager.create_view(name=name, updateable=False, **extra)
    if on_create:
        on_create(view)
    # The item create_view returns lists no tables (seen on ArcGIS Online, 2026-10-17); a fresh
    # get of the same id does, once the new service has finished provisioning.
    view_tables = {}
    for attempt in range(6):
        view = gis.content.get(view.id)
        view_tables = get_tables(view)
        if set(TABLE_SPECS) <= set(view_tables):
            break
        time.sleep(5 * (attempt + 1))
    absent = sorted(set(TABLE_SPECS) - set(view_tables))
    if absent:
        raise SystemExit(f"The new view {view.id} has no table(s) {', '.join(absent)}. Run "
                         f"--migrate --apply on the service first, delete the view, and retry.")
    def update() -> None:
        for table_name, fields in hidden.items():
            _definition_update(view_tables[table_name].manager,
                               {"fields": [{"name": f, "visible": False} for f in fields]},
                               f"{table_name} hidden fields")

    update_then_resend(update, view, lambda: view_tables)
    share_item(view, org=org, groups=groups)
    warn_shared_source(item)
    return view


# Anonymous refusals: ArcGIS answers a token-less query of a private table with JSON error 499
# (token required) or 403, in an HTTP 200 or an HTTP 401/403. A bare HTTP 401/403 is NOT a refusal:
# a proxy or WAF can 403 the urllib user agent without ArcGIS ever seeing the query.
ANONYMOUS_DENIED_CODES = {403, 499}
_INCONCLUSIVE = "inconclusive, not a proven refusal"


def _arcgis_error_code(raw: bytes) -> Optional[int]:
    """The error.code of an ArcGIS JSON error body, else None."""
    try:
        body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return safe_int(error.get("code")) if isinstance(error, dict) else None


def _anonymous_answer(url: str, timeout: int) -> str:
    """'' when an anonymous query of the table was refused, else what came back instead."""
    import urllib.error
    import urllib.request

    query = f"{url.rstrip('/')}/query?where=1%3D1&resultRecordCount=1&f=json"
    try:
        with urllib.request.urlopen(query, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            return f"HTTP {exc.code}, {_INCONCLUSIVE}"
        try:
            code = _arcgis_error_code(exc.read() or b"")
        except OSError:
            code = None
        return "" if code in ANONYMOUS_DENIED_CODES else (
            f"HTTP {exc.code} without an ArcGIS error body (a proxy or firewall may have answered), "
            f"{_INCONCLUSIVE}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"no readable answer ({type(exc).__name__}), {_INCONCLUSIVE}"
    try:
        body = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return f"an answer that is not JSON, {_INCONCLUSIVE}"
    if not isinstance(body, dict):
        return f"an answer that is not a JSON object, {_INCONCLUSIVE}"
    if "features" in body:
        return "rows, without signing in"
    code = _arcgis_error_code(raw)
    return "" if code in ANONYMOUS_DENIED_CODES else f"error code {code}, {_INCONCLUSIVE}"


def check_anonymous_access(urls: Iterable[str], timeout: int = 30) -> List[str]:
    """Query each table with no token. Returns one problem line per table that did not refuse."""
    problems = []
    for url in urls:
        answer = _anonymous_answer(url, timeout)
        if answer:
            problems.append(f"{url}: {answer}")
    return problems


def table_urls(*items: Any) -> List[str]:
    return [table.url for item in items if item is not None for table in get_tables(item).values()]


ANONYMOUS_FIX = ("Set the service sharing to Owner and the view to your organization or groups in "
                 "ArcGIS Online. Then run --check-anonymous. An inconclusive line means the answer "
                 "did not come from ArcGIS: open the query URL in a private browser window to "
                 "confirm it asks for a sign-in.")


def require_no_anonymous_access(urls: Iterable[str]) -> None:
    problems = check_anonymous_access(urls)
    if problems:
        raise SystemExit("Anonymous users may read TaskDash data, or the check could not prove they "
                         "cannot:\n  " + "\n  ".join(problems)
                         + "\n" + ANONYMOUS_FIX)
    emit("Anonymous check: every table refused a query without a token.")


def run_create_view(config: Dict[str, Any], args: argparse.Namespace) -> int:
    key = view_key(args.target)
    existing = normalize_text(config.get("arcgis", {}).get(key))
    if existing:
        raise SystemExit(f"arcgis.{key} is already {existing}; the view exists. Remove the key "
                         f"only if you mean to create another view.")
    gis = connect_agol(config)
    item = ensure_service(gis, config, args.target)
    view = create_view(gis, item, config, args.apply, args.target)
    if view is None:
        emit("DRY RUN - nothing written. Re-run with --create-view --apply to create the view.")
        return 0
    emit(f"Created view '{view.title}' -> item id {view.id}")
    emit(f"Add this to the config: \"{key}\": \"{view.id}\", then rebuild the dashboard.")
    require_no_anonymous_access(table_urls(item, view))
    return 0


def run_check_anonymous(config: Dict[str, Any], args: argparse.Namespace) -> int:
    """--check-anonymous: read-only. 0 when every table of the service and view refuses an
    anonymous query, else 1."""
    gis = connect_agol(config)
    item = ensure_service(gis, config, args.target)
    view_id = normalize_text(config.get("arcgis", {}).get(view_key(args.target)))
    view = gis.content.get(view_id) if view_id else None
    if view_id and view is None:
        raise SystemExit(f"View item {view_id} not found or not visible to this account.")
    problems = check_anonymous_access(table_urls(item, view))
    for problem in problems:
        emit(f"FAIL: {problem}")
    if problems:
        emit(ANONYMOUS_FIX)
        return 1
    emit("Anonymous check passed: every table refused a query without a token.")
    return 0


def fetch_keys(table: Any, rows: Sequence[Dict[str, Any]], key_fields: Sequence[str],
              oid_field: str) -> Dict[Tuple[str, ...], Any]:
    """OBJECTIDs for exactly the given rows' keys -- not a read of the whole table.

    Single-field keys (TaskRuns.run_key) use a chunked IN clause. Multi-field keys
    (TaskMetrics) fall back to a host-scoped query: one publish() pass's rows are always for
    the single host it is running against, so that read is already small and bounded, and it
    avoids quoting an AND/OR key-list built from three string columns.
    """
    keys = {tuple(normalize_text(row.get(f)) for f in key_fields) for row in rows}
    keys.discard(tuple("" for _ in key_fields))
    if not keys:
        return {}
    out_fields = ",".join([oid_field, *key_fields])
    index: Dict[Tuple[str, ...], Any] = {}
    if len(key_fields) == 1:
        field = key_fields[0]
        for chunk in chunked(sorted(keys), 100):
            values = ",".join(f"'{escape_sql_string(k[0])}'" for k in chunk)
            for row in query_all_rows(table, where=f"{field} IN ({values})", out_fields=out_fields):
                index[(normalize_text(row.get(field)),)] = row.get(oid_field)
        return index
    hosts = {normalize_text(row.get("host_name")) for row in rows if row.get("host_name")}
    where = ("host_name IN (" + ",".join(f"'{escape_sql_string(h)}'" for h in hosts) + ")"
            if hosts else "1=1")
    for row in query_all_rows(table, where=where, out_fields=out_fields):
        index[tuple(normalize_text(row.get(f)) for f in key_fields)] = row.get(oid_field)
    return index


def merge_duplicate_keys(rows: List[Dict[str, Any]], key_fields: Sequence[str],
                         label: str) -> List[Dict[str, Any]]:
    """Keep one row per key; the later row wins.

    Two rows with one key in a single pass (the same log file name in a task's script folder
    and its Logs folder) were both sent as adds. With the unique index the whole chunk then
    rolls back and the run fails every hour, because the stored row never ages out. Keys fold
    case, since the hosted table's database compares text case-insensitively.
    """
    merged: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for row in rows:
        merged[tuple(normalize_text(row.get(f)).casefold() for f in key_fields)] = row
    if len(merged) < len(rows):
        emit(f"{label}: merged {len(rows) - len(merged)} row(s) that repeated a key in this pass.")
    return list(merged.values())


def upsert_by_key(
    table: Any,
    rows: List[Dict[str, Any]],
    key_fields: Sequence[str],
    *,
    chunk_size: int,
    label: str,
    order_field: str = "",
) -> Dict[str, int]:
    """Insert new rows, update existing ones matched on key_fields. Idempotent by construction.

    With order_field, rows are written in ascending order of it, adds and updates together, one
    rollback-on-failure request per chunk. TaskRuns needs this: the next pass's watermark is
    max(max_event_record_id) over whatever landed, so a failure midway must leave only a low,
    contiguous prefix written -- never a high row that would lift the watermark past unwritten ones.
    The guarantee is per row, not per event: an unwritten run whose events straddle the landed
    maximum loses its earlier events, so after a failed pass run once with --full-history.

    Each chunk's matched keys are fetched fresh via fetch_keys just before it is sent (not the
    whole table up front), and a retried chunk re-fetches its still-unmatched keys before
    resending its adds, so an add that actually landed during an ambiguous failure (e.g. a 504)
    is not sent again as a duplicate. A single-field key's per-chunk fetch is already a narrow
    IN-clause read, so it stays per-chunk; a multi-field key's fetch_keys falls back to a
    host-scoped read of the whole table (see fetch_keys), so that one is built once for every
    row up front and reused across chunks -- an N-chunk metrics pass otherwise re-read this
    host's whole stored history N times.
    """
    oid_field = object_id_field(table)
    rows = merge_duplicate_keys(rows, key_fields, label)
    if order_field:
        rows = sorted(rows, key=lambda r: safe_float(r.get(order_field)) or 0.0)

    multi_key = len(key_fields) > 1
    full_index = fetch_keys(table, rows, key_fields, oid_field) if multi_key else None

    counts = {"added": 0, "updated": 0, "deleted": 0}
    total = (len(rows) + max(1, chunk_size) - 1) // max(1, chunk_size)
    for number, chunk in enumerate(chunked(rows, chunk_size), start=1):
        index = full_index if multi_key else fetch_keys(table, chunk, key_fields, oid_field)
        state: Dict[str, List[Dict[str, Any]]] = {"adds": [], "updates": []}
        for row in chunk:
            oid = index.get(tuple(normalize_text(row.get(f)) for f in key_fields))
            if oid is None:
                state["adds"].append(row)
            else:
                state["updates"].append({**row, oid_field: oid})
        emit(f"  {label} chunk {number}/{total} ({len(state['adds'])} adds, "
            f"{len(state['updates'])} updates)")
        added, updated = len(state["adds"]), len(state["updates"])

        def send(state=state):
            result = table.edit_features(
                adds=[{"attributes": r} for r in state["adds"]] or None,
                updates=[{"attributes": r} for r in state["updates"]] or None,
                rollback_on_failure=True)
            raise_on_edit_failure(result, "adds", label)
            raise_on_edit_failure(result, "updates", label)
            return result

        def before_retry(state=state):
            if not state["adds"]:
                return
            before = len(state["adds"])
            landed = fetch_keys(table, state["adds"], key_fields, oid_field)
            state["adds"] = [r for r in state["adds"]
                             if tuple(normalize_text(r.get(f)) for f in key_fields) not in landed]
            if multi_key:
                full_index.update(landed)  # keep the cached snapshot current for later chunks
            emit(f"  {label} adds retry: {len(state['adds'])}/{before} row(s) still missing "
                f"after re-query.")

        with_retry(send, label=f"{label} chunk {number}/{total}", before_retry=before_retry)
        counts["added"] += added
        counts["updated"] += updated
    return counts


STORED_RUN_FIELDS = ("run_key", "start_time_utc", "start_time_estimated", "event_count",
                     "max_event_record_id", "result_code", "result_text", "run_status",
                     "is_error", "is_success")
RESULT_FIELDS = ("result_code", "result_text", "run_status", "is_error", "is_success")


def fetch_stored_runs(table: Any, run_keys: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Stored start fields for the given run keys (queried by key, not the whole table)."""
    stored: Dict[str, Dict[str, Any]] = {}
    for chunk in chunked(sorted(set(run_keys)), 100):
        where = "run_key IN (" + ",".join(f"'{escape_sql_string(k)}'" for k in chunk) + ")"
        for row in query_all_rows(table, where=where,
                                  out_fields=",".join(STORED_RUN_FIELDS)):
            stored[normalize_text(row.get("run_key"))] = row
    return stored


def run_result_text(result_code: Any, stored_text: Any = "", run_status: Any = "") -> str:
    """A run's result text from its code, described the way build_run_rows describes it now.

    A row with no code keeps its stored text: that text came from the outcome event (a launch
    failure, a skip), and the code alone cannot rebuild it. Every launch-failure event gives the
    status StartFailed, so any other status means the code came from the finished action.
    """
    code = safe_int(result_code)
    if code is None:
        return normalize_text(stored_text)[:500]
    completed = normalize_text(run_status) != "StartFailed"
    return collect.describe_task_result(code, action_completed=completed)[:500]


def carry_stored_failure(run: Dict[str, Any], prior: Dict[str, Any]) -> None:
    """Keep a failure the stored row already knew about.

    A multi-action run can report its failing action in one pass and a later, successful action
    in the next; the keyed upsert would then replace the failure with the success. While the run
    was in flight the stored row said Running (is_error 0) but already held the failing 201
    code, so that code is the verdict once the run has ended.
    """
    if run.get("is_error"):
        return
    if safe_int(prior.get("is_error")):
        run.update({f: prior.get(f) for f in RESULT_FIELDS})
        # The stored text dates from when the row was first written; describe the code again
        # so a later mapping fix reaches it (a stale "Unmapped task result code" stayed forever).
        run["result_text"] = run_result_text(prior.get("result_code"), prior.get("result_text"),
                                             prior.get("run_status"))
        return
    code = safe_int(prior.get("result_code"))
    if code is None or collect.normalize_hresult_code(code) in collect.NON_ERROR_RESULT_CODES:
        return
    status = collect.get_run_status(run.get("end_time_utc"), code)
    run.update(result_code=code, result_text=run_result_text(code, run_status=status), run_status=status,
               is_error=1 if status == "Error" else 0, is_success=1 if status == "Success" else 0)


def keep_stored_start(runs: List[Dict[str, Any]], stored: Dict[str, Dict[str, Any]]) -> int:
    """Keep a run's stored start when this pass only saw its end.

    A run still in flight at collection time is stored with its real start (event 100). The next
    incremental pass reads only its 201/102, so build_run_rows estimates the start from the first
    event it saw -- about 0 s before the end -- and the keyed upsert replaced the real start with
    it. That had corrupted over a quarter of the stored runs: every job that starts on the
    hour, when TaskDash itself fires. A stored estimate still beats a later one, so the earlier
    of two estimates is kept too (with no duration). Returns the number of runs merged.
    """
    merged = 0
    for run in runs:
        prior = stored.get(normalize_text(run.get("run_key")))
        # A start taken from LastRunTime is real, but the rest of the run still needs the merge.
        reconciled = bool(run.get("_start_from_last_run_time"))
        if not (run.get("start_time_estimated") or reconciled) or not prior or not prior.get("start_time_utc"):
            continue
        start = dt.datetime.fromtimestamp(prior["start_time_utc"] / 1000, tz=dt.timezone.utc)
        prior_estimated = bool(safe_int(prior.get("start_time_estimated")))
        current = parse_iso_datetime(run.get("start_time_utc"))
        if not reconciled and prior_estimated and current is not None and current <= start:
            continue

        if not (reconciled and prior_estimated):
            run["start_time_utc"] = start.isoformat()
            run["start_time_estimated"] = 1 if prior_estimated else 0
        # Add the stored count only when this pass's events are all newer than the stored ones;
        # a re-read (--full-history, window read) sees some of the same events again.
        prior_count = safe_int(prior.get("event_count")) or 0
        count = safe_int(run.get("event_count")) or 0
        prior_max = safe_float(prior.get("max_event_record_id"))
        low = safe_float(run.get("_min_event_record_id"))
        disjoint = prior_max is not None and low is not None and prior_max < low
        run["event_count"] = prior_count + count if disjoint else max(prior_count, count)
        carry_stored_failure(run, prior)
        end = parse_iso_datetime(run.get("end_time_utc"))
        if not prior_estimated and end and end >= start:
            seconds = round((end - start).total_seconds(), 2)
            run["duration_seconds"] = seconds
            run["duration_minutes"] = round(seconds / 60.0, 2)
        merged += 1
    return merged


def _inventory_index(table: Any, host_name: str, oid_field: str) -> Dict[str, List[Any]]:
    """This host's stored task_full_name -> every OID stored under it (normally one; more than
    one is a duplicate row left by an earlier resent write, healed by sync_inventory below)."""
    host_where = f"host_name = '{escape_sql_string(host_name)}'"
    existing = query_all_rows(table, where=host_where, out_fields=f"{oid_field},task_full_name")
    index: Dict[str, List[Any]] = {}
    for row in existing:
        index.setdefault(normalize_text(row.get("task_full_name")), []).append(row.get(oid_field))
    return index


def _inventory_edits(
    rows: List[Dict[str, Any]], index: Dict[str, List[Any]], oid_field: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], List[Any]]:
    """adds, updates, this host's stale keys (stored but not in rows), and duplicate OIDs (every
    OID past the first stored under a key) -- the latter are folded into deletes by the caller
    to self-heal a duplicate even though its key is still present in this pass."""
    seen = set()
    adds, updates = [], []
    for row in rows:
        key = normalize_text(row.get("task_full_name"))
        seen.add(key)
        oids = index.get(key) or []
        if not oids:
            adds.append(row)
        else:
            updates.append({**row, oid_field: oids[0]})
    stale_keys = [k for k in index if k not in seen]
    dup_oids = [oid for oids in index.values() for oid in oids[1:]]
    return adds, updates, stale_keys, dup_oids


def sync_inventory(table: Any, rows: List[Dict[str, Any]], host_name: str, *,
                   chunk_size: int = DEFAULT_CHUNK_SIZE, label: str = TABLE_INVENTORY,
                   force: bool = False) -> Dict[str, int]:
    """Diff-sync TaskInventory keyed on (host_name, task_full_name); replaces truncate+reload.

    Updates matched rows, adds new ones, deletes this host's stored rows that were not
    collected this pass. The table is never empty mid-write and OBJECTIDs stay stable. Only
    rows already stamped with this host_name are read, matched or deleted -- another host's
    inventory is never touched. Guarded the way the old truncate was: a drop of more than half
    of this host's stored rows is the signature of collecting from the wrong machine; refuse
    rather than destroy, unless --force says the shrink is real. Inventory is well under 250
    rows, so the normal case is one rollback_on_failure request covering adds+updates+deletes
    together; past chunk_size, apply_edits_chunked splits it, deletes last, at the cost of that
    cross-request atomicity.

    On a retry (after an ambiguous failure such as a 504 whose write may already have
    committed), both the single-request and chunked paths re-query this host's rows and rebuild
    adds/updates/deletes from scratch before resending, so a landed add becomes an update and an
    already-applied delete drops out. Until the (host_name, task_full_name) unique index exists
    (check_schema_drift refuses --apply without it, but --migrate --apply may not have run yet),
    a duplicate from an earlier resent write would otherwise sit invisible to every future sync;
    every OID past the first stored under a key is added to deletes here so it heals regardless.
    """
    oid_field = object_id_field(table)
    index = _inventory_index(table, host_name, oid_field)
    adds, updates, stale_keys, dup_oids = _inventory_edits(rows, index, oid_field)

    if stale_keys and not force and len(stale_keys) > len(index) * 0.5:
        raise SystemExit(
            f"{label}: refusing to delete {len(stale_keys)} of {len(index)} stored task(s) for "
            f"host '{host_name}' -- a drop of more than half usually means collection ran on "
            f"the wrong host. Re-run on the right machine, or pass --force if the shrink is real."
        )
    deletes = [index[k][0] for k in stale_keys] + dup_oids

    counts = {"added": len(adds), "updated": len(updates), "deleted": len(deletes)}
    if len(adds) + len(updates) + len(deletes) <= chunk_size:
        state = {"adds": adds, "updates": updates, "deletes": deletes}

        def send(state=state):
            result = table.edit_features(
                adds=[{"attributes": r} for r in state["adds"]] or None,
                updates=[{"attributes": r} for r in state["updates"]] or None,
                deletes=",".join(str(o) for o in state["deletes"]) if state["deletes"] else None,
                rollback_on_failure=True)
            for kind in ("adds", "updates", "deletes"):
                raise_on_edit_failure(result, kind, label)
            return result

        def before_retry(state=state):
            fresh_index = _inventory_index(table, host_name, oid_field)
            new_adds, new_updates, new_stale, new_dup_oids = _inventory_edits(rows, fresh_index, oid_field)
            state["adds"], state["updates"] = new_adds, new_updates
            state["deletes"] = [fresh_index[k][0] for k in new_stale] + new_dup_oids
            emit(f"  {label} sync retry: {len(state['adds'])} adds, {len(state['updates'])} "
                f"updates, {len(state['deletes'])} deletes after re-query.")

        with_retry(send, label=f"{label} sync", before_retry=before_retry)
    else:
        def dedupe_adds(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            landed = _inventory_index(table, host_name, oid_field)
            return [r for r in pending if normalize_text(r.get("task_full_name")) not in landed]

        apply_edits_chunked(table, adds=adds, updates=updates, deletes=deletes,
                            chunk_size=chunk_size, label=label, dedupe_adds=dedupe_adds)
    return counts


# --------------------------------------------------------------------------- orchestration

def event_watermark(runs_table: Any, host_name: str, newest: Optional[int]) -> int:
    """Highest stored record id for this host that the live log can still contain.

    Per host, because another host's record ids say nothing about this log. Bounded by the log's
    newest record id, because ids restart low when the log is cleared or the host is rebuilt
    under the same host_name_override: an unbounded max stays above every new id until the
    90-day purge, so each pass would read nothing (or re-read the whole window) for months. The
    bound makes the first pass after a reset a window read and later passes normal again.
    """
    host_where = f"host_name = '{escape_sql_string(host_name)}'"
    stored = int(max_field_value(runs_table, "max_event_record_id", where=host_where) or 0)
    if newest is None or stored <= newest:
        return stored
    bounded = int(max_field_value(runs_table, "max_event_record_id",
                                  where=f"{host_where} AND max_event_record_id <= {int(newest)}") or 0)
    emit(f"WARNING: the Operational log's newest record id ({newest}) is below the stored "
         f"watermark ({stored}) -- the log was cleared or the host rebuilt. Using {bounded}.")
    return bounded


def resolve_log_dir(config: Dict[str, Any]) -> Path:
    """TaskDash's own log folder; metrics mining must skip it, so both sides use this."""
    return Path(normalize_text(config.get("logging", {}).get("log_dir")) or (runtime_dir() / "Logs"))


def read_run_events(history: Dict[str, Any], timeout: int, min_record_id: int,
                    max_events: int) -> Tuple[List[Dict[str, Any]], bool]:
    """Operational-log events past the watermark, or by time window when there is none.

    Returns (events, stalled_risk): True when an incremental read filled its budget, so the
    caller can tell a backlog still being walked from one that can never move (see below).
    """
    if max_events:
        event_budget, budget_setting = max_events, "history.first_run_max_events"
    else:
        event_budget = int(history.get("max_events_per_run", 5000))
        budget_setting = "history.max_events_per_run"
    if min_record_id:
        emit(f"Incremental fetch: events newer than record id {min_record_id}.")
    raw_events = collect.collect_run_events(
        int(history.get("days_back", 30)), event_budget, timeout, min_record_id=min_record_id)
    emit(f"Collected {len(raw_events)} Operational log event(s).")
    full = len(raw_events) >= event_budget
    if full and min_record_id:
        emit(f"WARNING: hit the {event_budget}-event budget. Older events are read first, so "
             f"the rest of the backlog is picked up by the next run(s); raise "
             f"history.max_events_per_run if this repeats.")
    elif full:
        emit(f"WARNING: hit the {event_budget}-event budget on a time-window read, so only the "
             f"newest {event_budget} events were kept and older runs in the window are missing. "
             f"Raise {budget_setting} to backfill further.")
    return raw_events, bool(full and min_record_id)


# Bump this prefix whenever a build_config_warnings check is added or removed, or an existing
# check's note wording changes, not just when compute_definition_hash's own inputs change: the
# bump is what makes detect_task_changes treat the next pass as a silent baseline for
# config_warning_added/cleared rows too (see the "not old_hash.startswith(...)" guard around both
# note loops there), instead of dating a brand-new rule, or a reworded note, as a task change on
# every task it already applies to (operations-rule-deploy-note-dated-as-task-change).
#
# Bump when the hash inputs change. A stored hash from another scheme is a baseline, not a
# change: v1 (bare 64-hex, no prefix) hashed a trigger string that held only the CIM class
# name, so moving to v2 would otherwise log DEFINITION CHANGED for every task at once. Hex never
# contains "v", so an unprefixed v1 hash can never be mistaken for this scheme. The run-as user
# is casefolded: Task Scheduler reports "svc_gisjobs" to one session and "svc_gisjobs" to
# another, and that alone changed all 27 hashes. Casefolding needed no bump: the scheduled
# session sees only "svc_gisjobs", so its stored v2 hashes are the same either way.
# v3 (collector_tab-04 / wording-06): _trigger_recurrence dropped "every day"/"every week" for
# an interval of 1 and reworded an all-days-of-week Weekly trigger as "on every day" -- both feed
# describe_triggers, which this hash covers, so every task's hash would otherwise change and log
# a spurious definition_changed row for a wording-only edit. detect_task_changes only compares
# equality once old_hash.startswith(DEFINITION_HASH_PREFIX), so a stored v2 hash (or v1) is a
# baseline under v3, the same way v1 was under v2.
# v4 (round 3): describe_trigger now reads an all-days-of-week Weekly trigger with no multi-week
# interval as "Daily", not "Weekly on every day" (task_detail-r2regress-taskdash-schedule-wording
# / collector_tab-08 / r2regress-wording-06-taskdash), and its time/date moved from 24-hour ISO
# to 12-hour plain text (task_detail-mixed-time-formats / wording-10) -- both feed
# describe_triggers. collect.describe_task_settings (MultipleInstancesPolicy, ExecutionTimeLimit,
# StartWhenAvailable) is also now hashed: those settings could change without moving the hash or
# the change summary (collector_tab-03). A stored v3 hash (or v2, or v1) is a baseline under v4.
# v5 (round-3 review): the settings text uses the Settings-tab words ("Do not start a new
# instance", not IgnoreNew) and an end date reads like the start date. TEST had stored v4
# hashes of the old text; a v4 hash is a baseline under v5.
# v6 (round 6): describe_trigger appends " (fixed UTC offset)" after a trigger start that
# carries a fixed UTC offset ("Synchronize across time zones") (operations-dst-synchronized-
# triggers). A stored v5 hash (or v4, v3, v2, v1) is a baseline under v6, so the first pass after
# the deploy records no definition_changed row for any task.
# v7 (round 7): describe_trigger's offset suffix changed from " (fixed UTC offset)" to ", fixed
# to UTC-04:00" (or ", fixed to UTC" for Z) (wording-utc-offset-note-vague). This also feeds the
# note text in check_dst_offset_trigger, but that no longer matters to detect_task_changes: the
# per-note loops there are now skipped outright for a baseline row (see the comment above this
# prefix), so this bump makes the next pass silent for both definition_changed and
# config_warning_added/cleared rows. A stored v6 hash (or v5, v4, v3, v2, v1) is a baseline
# under v7.
# v8 (round-7 review): the offset note drops its US month ranges and is not raised on a host
# with no daylight-saving time; TEST had stored v7 notes in the old wording.
# v9 (phase 2.5): the LogonType note names the Single Use licence, and privacy.hide_paths (on by
# default) puts "(path hidden)" in the not-found notes. Both change stored note text.
DEFINITION_HASH_PREFIX = "v9"


def compute_definition_hash(raw: Dict[str, Any]) -> str:
    """Versioned SHA-256 over what defines a task's behaviour: its actions, its schedule (every
    trigger's type, boundaries, interval, days, repetition and delays -- collect.describe_triggers),
    principal (run-as user, logon type, run level), enabled flag, and the Settings-tab values
    that govern silent skips (collect.describe_task_settings). 64 chars: prefix + 62 hex.

    Used by 3.7's TaskChanges to detect definition_changed without storing the full definition
    twice -- a changed hash means something here changed; last-run outcome (result code,
    duration, health) is deliberately excluded, that's what became_failing/recovered/
    became_overdue already cover separately.
    """
    parts = "\x1f".join([
        normalize_text(raw.get("actions")), collect.describe_triggers(raw.get("trigger_details")),
        normalize_text(raw.get("run_as_user")).casefold(), normalize_text(raw.get("logon_type")),
        normalize_text(raw.get("run_level")), "1" if raw.get("enabled") else "0",
        collect.describe_task_settings(raw),
    ])
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()
    return DEFINITION_HASH_PREFIX + digest[:64 - len(DEFINITION_HASH_PREFIX)]


def reconcile_estimated_starts(runs: List[Dict[str, Any]], inventory: Sequence[Dict[str, Any]],
                               execution_time_limits: Dict[str, Optional[float]]) -> int:
    """Give each task's newest run the real start from LastRunTime when that start is estimated.

    When event 100 falls outside the read, build_run_rows estimates the start from the first
    event seen and leaves the duration blank. LastRunTime is the real start of the newest run.
    It is used only if it is no later than the first event seen (60 s tolerance), no later than
    the end, and within the ExecutionTimeLimit. Returns the number of runs fixed.
    """
    newest: Dict[str, Dict[str, Any]] = {}
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    for run in runs:
        if run.get("run_status") in collect.NON_RUN_STATUSES:
            continue
        key = run.get("task_full_name")
        start = parse_iso_datetime(run.get("start_time_utc")) or floor
        if key not in newest or start > (parse_iso_datetime(newest[key].get("start_time_utc")) or floor):
            newest[key] = run
    tolerance = dt.timedelta(seconds=RUN_FAILURE_TOLERANCE_SECONDS)
    fixed = 0
    for task in inventory:
        key = task.get("task_full_name")
        run = newest.get(key)
        last_run = parse_iso_datetime(task.get("last_run_time"))
        if not run or not run.get("start_time_estimated") or last_run is None:
            continue
        seen = parse_iso_datetime(run.get("start_time_utc"))
        end = parse_iso_datetime(run.get("end_time_utc"))
        limit = execution_time_limits.get(key)
        if seen is None or seen < last_run - tolerance or (end and end < last_run):
            continue
        if limit and (seen - last_run).total_seconds() > limit + tolerance.total_seconds():
            continue
        run["start_time_utc"] = last_run.isoformat()
        run["start_time_estimated"] = 0
        # publish() still merges these with the stored row: a run in flight at the previous
        # pass is seen now only by its 201/102, and the stored row may hold its failing action.
        run["_start_from_last_run_time"] = 1
        if end:
            seconds = round((end - last_run).total_seconds(), 2)
            run["duration_seconds"] = seconds
            run["duration_minutes"] = round(seconds / 60.0, 2)
        fixed += 1
    return fixed


def mine_metrics(config: Dict[str, Any], inventory: Sequence[Dict[str, Any]],
                 host_name: str, examined: Optional[set] = None,
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Mine whatever log paths resolve on this machine. Off-host that is legitimately zero."""
    metrics_cfg = config.get("metrics", {})
    return metrics.build_metric_rows(
        inventory,
        host_name=host_name,
        days_back=int(metrics_cfg.get("days_back", 30)),
        max_files_per_task=int(metrics_cfg.get("max_log_files_per_task", 40)),
        max_lines_per_file=int(metrics_cfg.get("max_lines_per_file", 5000)),
        log_directory_overrides=metrics_cfg.get("log_directory_overrides") or {},
        own_log_dir=resolve_log_dir(config),
        examined=examined,
    )


def interactive_pattern(scheduler: Dict[str, Any]) -> str:
    """scheduler.interactive_required_pattern: only the boolean false turns the LogonType check off,
    for an install whose Pro licence is not Single Use. A missing key, null or "" keeps the ArcGIS
    Pro default, as before Phase 2.5: a blanked value must not silently drop the check."""
    value = scheduler.get("interactive_required_pattern")
    if value is False:
        return ""
    return normalize_text(value) or collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN


def collect_everything(config: Dict[str, Any], fixture: Optional[Path],
                       min_record_id: int = 0, max_events: int = 0) -> Dict[str, Any]:
    host_name = get_host_name(config)
    scheduler = config.get("scheduler", {})
    history = config.get("history", {})
    timeout = int(config.get("powershell_timeout_seconds", 180))
    # Taken before the slow task and event queries: a task due at 10:00 that starts at 10:00:05
    # must not look overdue because the audit clock was read a minute later.
    audit_dt = utc_now()

    if fixture:
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        raw_tasks = payload.get("tasks", [])
        raw_events = payload.get("events", [])
        stalled_risk = False
        host_name = normalize_text(payload.get("host_name")) or host_name
        # A replayed capture is judged at the moment it was taken, or every task looks overdue.
        audit_dt = parse_iso_datetime(payload.get("captured_at_utc")) or audit_dt
        emit(f"Loaded fixture {fixture} ({len(raw_tasks)} tasks, {len(raw_events)} events)")
    else:
        emit("Collecting scheduled tasks...")
        raw_tasks = collect.collect_tasks(timeout)
        emit(f"Collected {len(raw_tasks)} task row(s) before filtering.")
        emit("Collecting run events...")
        raw_events, stalled_risk = read_run_events(history, timeout, min_record_id, max_events)

    filtered = [r for r in raw_tasks if collect.should_include_task(r, scheduler)]
    emit(f"Tasks after filtering: {len(filtered)}")

    descriptions = collect.load_descriptions()
    inventory = collect.build_inventory_rows(
        filtered, host_name=host_name, descriptions=descriptions, audit_dt=audit_dt,
        overdue_grace_minutes=float(scheduler.get("overdue_grace_minutes",
                                                  collect.DEFAULT_OVERDUE_GRACE_MINUTES)),
        interactive_required_pattern=interactive_pattern(scheduler),
        hide_paths=privacy_hides(config.get("privacy"), "hide_paths"),
        # A replayed capture's script paths belong to the captured host, not this one: checking
        # them here wrote false "script not found" config notes. LogonType and silent-skip checks
        # read only captured data and still run.
        exists=(lambda _path: True) if fixture else os.path.exists)

    # definition_hash (3.7's TaskChanges) and each task's ExecutionTimeLimit (3.5's stuck
    # detection) both come from the raw PowerShell row, not the inventory row, so they are
    # built here, keyed on task_full_name, and threaded through -- enrich_inventory only sees
    # the inventory rows, and publish()'s second enrich pass needs the same limits again.
    hash_by_task = {normalize_text(r.get("task_full_name")): compute_definition_hash(r) for r in filtered}
    execution_time_limits = {
        normalize_text(r.get("task_full_name")):
            collect.parse_iso8601_duration_seconds(r.get("execution_time_limit"))
        for r in filtered
    }
    for row in inventory:
        row["definition_hash"] = hash_by_task.get(row["task_full_name"], "")

    runs = collect.build_run_rows(raw_events, inventory, host_name=host_name)
    emit(f"Derived {len(runs)} run row(s).")
    fixed = reconcile_estimated_starts(runs, inventory, execution_time_limits)
    if fixed:
        emit(f"Took the real start from LastRunTime for {fixed} run(s) whose start event was not read.")
    # The watermark only advances past runs that are written. If the oldest budget-full of
    # events past it holds no monitored run (an Edge-updater or WER flood), every pass re-reads
    # the same events and never moves. Silent forever is worse than a failed run.
    if stalled_risk and not runs:
        raise SystemExit(
            "Run collection is stalled: the oldest events past the watermark filled the whole "
            "budget and none belongs to a monitored task. Raise history.max_events_per_run, or "
            "run once with --full-history to jump to the current log window.")

    metric_rows, log_summary = mine_metrics(config, inventory, host_name)
    emit(f"Mined {len(metric_rows)} metric observation(s) from {len(log_summary)} task log set(s).")

    # Enrich from this pass only; publish() re-does it from the accumulated table when writing.
    enrich_inventory(inventory, summarize_runs(runs, audit_dt), log_summary, execution_time_limits,
                     audit_dt)
    return {"host_name": host_name, "raw_tasks": raw_tasks, "raw_events": raw_events,
            "inventory": inventory, "runs": runs, "metrics": metric_rows,
            "log_summary": log_summary, "execution_time_limits": execution_time_limits,
            "audit_dt": audit_dt}


def report(collected: Dict[str, Any]) -> None:
    inventory = collected["inventory"]
    runs = collected["runs"]
    failing = [t for t in inventory if t["last_run_failed"]]
    attention = [t for t in inventory if t["needs_attention"]]
    overdue = [t for t in inventory if t["is_overdue"]]

    emit("")
    emit("=" * 68)
    emit(f"Host              : {collected['host_name']}")
    emit(f"Tasks             : {len(inventory)}")
    emit(f"Runs              : {len(runs)}  (errors: {sum(r['is_error'] for r in runs)})")
    emit(f"Metric rows       : {len(collected['metrics'])}")
    emit(f"Last run failed   : {len(failing)}")
    emit(f"Needs attention   : {len(attention)}")
    emit(f"Overdue           : {len(overdue)}")
    emit("=" * 68)
    for task in attention:
        emit(f"  [{task['health_status']:9s}] {task['task_name'][:40]:41s} {task['attention_reason'][:60]}")
    if not attention:
        emit("  All tasks healthy.")
    emit("")


_CHANGE_DETECTION_FIELDS = ("task_full_name", "task_name", "definition_hash", "last_run_failed",
                           "is_overdue", "config_warnings", "actions", "run_as_user",
                           "logon_type", "run_level", "enabled", "schedule_detail",
                           "settings_detail", "attention_reason", "health_status", "state")


def _fetch_prior_inventory_for_changes(table: Any, host_name: str) -> Dict[str, Dict[str, Any]]:
    """This host's stored TaskInventory rows, queried before sync_inventory overwrites them --
    the "before" side of detect_task_changes' diff. Keyed on task_full_name."""
    host_where = f"host_name = '{escape_sql_string(host_name)}'"
    rows = query_all_rows(table, where=host_where, out_fields=",".join(_CHANGE_DETECTION_FIELDS))
    return {normalize_text(r.get("task_full_name")): r for r in rows}


def _fetch_failing_history(table: Any, host_name: str) -> Dict[str, Tuple[bool, str]]:
    """Per task, whether its newest became_failing/recovered TaskChanges row says failing, and
    that row's reason. detect_task_changes reads it when the stored inventory row is mid-run."""
    where = (f"host_name = '{escape_sql_string(host_name)}' "
             f"AND change_kind IN ('became_failing', 'recovered')")
    rows = query_all_rows(table, where=where,
                          out_fields="task_full_name,change_kind,changed_utc,new_summary")
    rows.sort(key=lambda r: safe_float(r.get("changed_utc")) or 0.0)
    return {normalize_text(r.get("task_full_name")):
            (r.get("change_kind") == "became_failing", normalize_text(r.get("new_summary")))
            for r in rows}


def _run_in_progress(row: Dict[str, Any]) -> bool:
    """A run is still going, so the row says nothing yet about failed or not.

    collect.py stores last_run_failed 0 for a Running task, so a failed task that started
    again read as recovered 23 s into the run (audit 4). A stuck run is Warning while live
    Running. Task Dash itself is judged by its previous run (apply_own_run_view), so it is
    Success or Error here and counts as finished.
    """
    health = normalize_text(row.get("health_status"))
    return health == "Running" or (health == "Warning" and normalize_text(row.get("state")).lower() == "running")


def _definition_parts(row: Dict[str, Any], brief: bool = False,
                      hide_commands: bool = False, hide_accounts: bool = False) -> List[Tuple[str, str]]:
    """The ordered (label, value) pairs behind _definition_summary.

    Also the input to detect_task_changes' definition_changed diff, which keeps only the parts
    that actually differ instead of two ~300-char full definitions an operator must diff by eye
    (collector_tab-02). "(wd=" is expanded to "(Working folder: " here only -- display, not the
    hashed actions string that feeds compute_definition_hash.
    """
    # Plain text, not repr: the Recent Changes list shows this verbatim, and repr doubled every
    # backslash in a Windows path and wrapped each value in quotes.
    # Scrubbed before the quotes are stripped and the text is cut: a quoted password with spaces
    # needs its quotes to be found, and a cut must not leave half a secret behind.
    actions = _scrub_secrets(normalize_text(row.get("actions"))).replace('"', "")[:200].replace(
        "(wd=", "(Working folder: ")
    if hide_commands:
        actions = "(hidden)"
    # run_level is in definition_hash, so it must show here too: a run-level-only edit otherwise
    # logged DEFINITION CHANGED with identical old and new summaries.
    principal = ", ".join(v for v in (normalize_text(row.get("logon_type")),
                                      normalize_text(row.get("run_level"))) if v)
    run_as = ("(hidden)" if hide_accounts else (normalize_text(row.get("run_as_user")) or "unknown")) + (f" ({principal})" if principal else "")
    enabled = "yes" if safe_int(row.get("enabled")) else "no"
    # Omitted, not "unknown", when absent: rows stored before schedule_detail/settings_detail
    # existed have none.
    schedule = normalize_text(row.get("schedule_detail"))[:500]
    settings = normalize_text(row.get("settings_detail"))[:255]
    parts: List[Tuple[str, str]] = []
    if schedule:
        parts.append(("Schedule", schedule))
    if not brief:
        parts.append(("Actions", actions or "none"))
    elif normalize_text(row.get("command_target_name")):
        # A task_added row shows the script file only: the full command line and working folder
        # are long and say nothing a reader needs on the day a task appears. Display text only --
        # compute_definition_hash reads the raw actions, not these parts.
        parts.append(("Script", normalize_text(row.get("command_target_name"))))
    parts.append(("Runs as", run_as))
    parts.append(("Enabled", enabled))
    # Each Settings-tab value (collect.describe_task_settings) is its own top-level part, not one
    # "Settings" part holding all three behind the same " | " that separates parts here: nested,
    # a one-setting edit still showed "Settings: <all three>" as changed, and the two separator
    # levels were hard to tell apart (collector_tab-change-summary-separator-and-jargon).
    for item in settings.split(" | "):
        if not item:
            continue
        label, _, value = item.partition(": ")
        parts.append((label, value))
    return parts


def _join_definition_parts(parts: Sequence[Tuple[str, str]]) -> str:
    return " | ".join(f"{label}: {value}" for label, value in parts)[:1000]


def _definition_summary(row: Dict[str, Any], brief: bool = False, hide_commands: bool = False,
                        hide_accounts: bool = False) -> str:
    return _join_definition_parts(_definition_parts(row, brief, hide_commands, hide_accounts))


# The live-state lead apply_own_run_view puts on attention_reason. History keeps only the
# reason: the lead is true for the moment of the run, not for the day the row is read.
_LIVE_STATE_PREFIX = "Running now. Status is from the previous run: "


def _history_reason(text: Any) -> str:
    reason = normalize_text(text)
    return reason[len(_LIVE_STATE_PREFIX):] if reason.startswith(_LIVE_STATE_PREFIX) else reason


def _definition_change_summaries(old: Dict[str, Any], new: Dict[str, Any],
                                 hide_commands: bool = False,
                                 hide_accounts: bool = False) -> Tuple[str, str]:
    """Only the (label, value) parts that differ between old and new (collector_tab-02).

    Falls back to the full old/new summaries when nothing in the tracked parts differs, so a
    definition_changed row is never logged with two empty summaries. With hide_commands the
    command line reads "(hidden)" on both sides, so an edit to it never shows two look-alike texts.
    """
    old_parts = dict(_definition_parts(old, hide_commands=hide_commands, hide_accounts=hide_accounts))
    new_parts = dict(_definition_parts(new, hide_commands=hide_commands, hide_accounts=hide_accounts))
    labels = list(dict.fromkeys([*old_parts, *new_parts]))
    changed = [l for l in labels if old_parts.get(l) != new_parts.get(l)]
    old_diff = [(l, old_parts[l]) for l in changed if l in old_parts]
    new_diff = [(l, new_parts[l]) for l in changed if l in new_parts]
    if not old_diff and not new_diff:
        return (_definition_summary(old, hide_commands=hide_commands, hide_accounts=hide_accounts),
                _definition_summary(new, hide_commands=hide_commands, hide_accounts=hide_accounts))
    return _join_definition_parts(old_diff), _join_definition_parts(new_diff)


def detect_task_changes(
    old_by_key: Dict[str, Dict[str, Any]],
    new_rows: Sequence[Dict[str, Any]],
    host_name: str,
    changed_utc: str,
    failing_history: Optional[Dict[str, Tuple[bool, str]]] = None,
    hide_commands: bool = False,
    hide_accounts: bool = False,
    hide_paths: bool = False,
) -> List[Dict[str, Any]]:
    """One row per state transition between the previously-stored TaskInventory (queried by the
    caller before sync_inventory overwrites it) and this pass's freshly collected inventory.

    became_failing/recovered compare finished runs only. A run in progress (_run_in_progress)
    records neither. When the stored row is mid-run, its "before" is the task's newest
    became_failing/recovered row (failing_history), so a recovery needs a finished good run.

    Every row in one publish shares changed_utc, so the (host_name, task_full_name, change_kind,
    changed_utc) unique key still holds even when a task has two transitions in the same pass
    (e.g. definition_changed and became_failing together). A stored row with no definition_hash
    yet (the first sync after --migrate added the field), or one from an older hash scheme (see
    DEFINITION_HASH_PREFIX), is a baseline, not a change: reporting it would log a
    definition_changed row for every task on the first run.

    A host with no stored rows at all (its first-ever sync) is the same kind of baseline: every
    key would otherwise be "new" and log 27 task_added rows dated today, even for tasks whose
    real trigger start is years old (collector_tab-01 / wording-15).
    """
    if not old_by_key:
        return []
    changes: List[Dict[str, Any]] = []
    new_by_key = {normalize_text(r.get("task_full_name")): r for r in new_rows}
    mask = {"hide_command_lines": hide_commands, "hide_accounts": hide_accounts, "hide_paths": hide_paths}

    def published_notes(row: Dict[str, Any]) -> List[str]:
        # Both sides through the transform the stored copy went through (scrub, the 1000-character
        # cut) plus the summary masking: compared raw, a note holding a secret, a long note, or a
        # path that hide_paths now writes as "(path hidden)" logged cleared/added every hour.
        text = _published_value("config_warnings", normalize_text(row.get("config_warnings")), set(),
                                ("config_warnings",), {"config_warnings": 1000})
        return [n for n in mask_summary(text, mask).split("; ") if n]

    def add(key: str, task_name: str, kind: str, old_summary: str = "", new_summary: str = "") -> None:
        changes.append({
            "host_name": host_name, "task_full_name": key, "task_name": task_name,
            "change_kind": kind, "changed_utc": changed_utc,
            "old_summary": _scrub_secrets(old_summary)[:1000],
            "new_summary": _scrub_secrets(new_summary)[:1000],
        })

    for key, new in new_by_key.items():
        old = old_by_key.get(key)
        task_name = normalize_text(new.get("task_name"))
        if old is None:
            add(key, task_name, "task_added", new_summary=_definition_summary(new, brief=True))
            continue
        old_hash = normalize_text(old.get("definition_hash"))
        if old_hash.startswith(DEFINITION_HASH_PREFIX) and old_hash != normalize_text(new.get("definition_hash")):
            old_summary, new_summary = _definition_change_summaries(
                old, new, hide_commands, hide_accounts)
            add(key, task_name, "definition_changed", old_summary, new_summary)
        old_failed, new_failed = bool(safe_int(old.get("last_run_failed"))), bool(safe_int(new.get("last_run_failed")))
        old_reason = _history_reason(old.get("attention_reason"))
        if _run_in_progress(old) and key in (failing_history or {}):
            old_failed, old_reason = failing_history[key]
        # collector_tab-05: the reason (attention_reason / last result text) goes in the summary
        # so the row has a detail line instead of an empty grey one.
        if _run_in_progress(new):
            pass
        elif new_failed and not old_failed:
            add(key, task_name, "became_failing", new_summary=_history_reason(new.get("attention_reason")))
        elif old_failed and not new_failed:
            # Stored bare: the dashboard prefixes "Was: " (collector_tab-recovered-row-shows-old-
            # failure), which also covers rows stored before; a stored prefix read "Was: Was:".
            add(key, task_name, "recovered", old_summary=old_reason)
        if safe_int(new.get("is_overdue")) and not safe_int(old.get("is_overdue")):
            add(key, task_name, "became_overdue", new_summary=_history_reason(new.get("attention_reason")))
        # Per-note, not per-field: comparing the whole "; "-joined string only caught an
        # empty<->non-empty transition, so a second note landing on a task that already had one
        # stayed silent (collector_tab-r3regress-03-note-split-not-landed). A stored hash that
        # does not start with DEFINITION_HASH_PREFIX is itself a baseline (see the comment above
        # it): its notes were never compared against this scheme, so both loops below are skipped
        # for it. Otherwise a new TaskDash rule, or a note-wording change, would date every task
        # that already matches it as "Setting note added" today (operations-rule-deploy-note-
        # dated-as-task-change).
        if old_hash.startswith(DEFINITION_HASH_PREFIX):
            old_notes = published_notes(old)
            new_notes = published_notes(new)
            for note in new_notes:
                if note not in old_notes:
                    add(key, task_name, "config_warning_added", new_summary=note)
            # A vanished note reads as "fixed" with no explanation (wording-ack-reads-as-setting-
            # fixed / operations-acknowledged-note-reads-as-fixed): the definition_hash (settings,
            # triggers, actions, principal) tells whether the task definition moved in the same pass.
            new_hash = normalize_text(new.get("definition_hash"))
            if old_hash == new_hash:
                cleared_summary = ("The task settings did not change: the problem went away (for "
                                    "example a missing file was restored), the note was "
                                    "acknowledged, or TaskDash no longer raises it.")
            else:
                cleared_summary = "The task definition was changed in the same hour."
            for note in old_notes:
                if note not in new_notes:
                    add(key, task_name, "config_warning_cleared", old_summary=note, new_summary=cleared_summary)

    for key, old in old_by_key.items():
        if key not in new_by_key:
            add(key, normalize_text(old.get("task_name")), "task_removed", old_summary=_definition_summary(old))

    return changes


def publish(gis: Any, item: Any, collected: Dict[str, Any], config: Dict[str, Any],
            force: bool = False) -> Dict[str, int]:
    chunk_size = int(config.get("upload", {}).get("chunk_size", DEFAULT_CHUNK_SIZE))
    retention = config.get("retention", {})
    collected_at = utc_now().isoformat()
    tables = get_tables(item)

    missing = {TABLE_INVENTORY, TABLE_RUNS, TABLE_METRICS} - set(tables)
    if missing:
        raise SystemExit(f"Target service is missing table(s): {sorted(missing)}. Run --create-service.")

    # Runs go first: the inventory's 7-day counts are derived from the accumulated run table,
    # which only holds this pass's rows once they are written.
    estimated = [r["run_key"] for r in collected["runs"]
                 if r.get("start_time_estimated") or r.get("_start_from_last_run_time")]
    if estimated:
        repaired = keep_stored_start(collected["runs"], fetch_stored_runs(tables[TABLE_RUNS], estimated))
        emit(f"{TABLE_RUNS}: kept the stored start of {repaired} of {len(estimated)} run(s) seen only by their end.")
    privacy = config.get("privacy") or {}
    runs = prepare_rows(collected["runs"], TABLE_RUNS, collected_at, privacy)
    result = upsert_by_key(tables[TABLE_RUNS], runs, ["run_key"], chunk_size=chunk_size,
                           label=TABLE_RUNS, order_field="max_event_record_id")
    emit(f"{TABLE_RUNS}: added {result['added']}, updated {result['updated']}.")
    counts = {"runs_added": result["added"], "runs_updated": result["updated"]}

    audit_dt = collected.get("audit_dt") or utc_now()
    run_summary = summarize_runs_from_agol(tables[TABLE_RUNS], collected["host_name"], audit_dt)
    enrich_inventory(collected["inventory"], run_summary, collected.get("log_summary", {}),
                     collected.get("execution_time_limits"), audit_dt)
    emit(f"Inventory roll-ups recomputed from {len(run_summary)} task(s) of stored run history.")
    others = fetch_other_host_counts(tables[TABLE_INVENTORY], collected["host_name"], audit_dt)
    rate = stamp_success_rate(collected["inventory"], others)
    hosts = 1 + len({normalize_text(r.get("host_name")) for r in others})
    emit(f"Fleet 7-day success rate: {'n/a' if rate is None else f'{rate}%'} across {hosts} host(s).")

    # 3.7: snapshot this host's stored inventory before sync_inventory overwrites it, so
    # detect_task_changes has a "before" to diff against. Best-effort/additive: skipped
    # entirely until --migrate --apply has created TaskChanges on this service.
    prior_inventory: Dict[str, Dict[str, Any]] = {}
    if TABLE_TASK_CHANGES in tables:
        prior_inventory = _fetch_prior_inventory_for_changes(tables[TABLE_INVENTORY], collected["host_name"])

    inventory = prepare_rows(collected["inventory"], TABLE_INVENTORY, collected_at, privacy)
    result = sync_inventory(tables[TABLE_INVENTORY], inventory, collected["host_name"],
                            chunk_size=chunk_size, label=TABLE_INVENTORY, force=force)
    emit(f"{TABLE_INVENTORY}: added {result['added']}, updated {result['updated']}, "
        f"deleted {result['deleted']}.")
    counts.update(inventory_added=result["added"], inventory_updated=result["updated"])

    if TABLE_TASK_CHANGES in tables:
        # Best-effort, like the 3.7 snapshot above: the metrics upsert and the TaskRuns/
        # TaskMetrics purges below must still run even if this write or purge fails -- a lost
        # pass of state transitions is preferable to aborting the rest of publish() over it.
        try:
            # Read only when a stored row is mid-run: most passes then skip the extra query.
            failing_history = (_fetch_failing_history(tables[TABLE_TASK_CHANGES], collected["host_name"])
                               if any(_run_in_progress(r) for r in prior_inventory.values()) else {})
            changes = detect_task_changes(prior_inventory, inventory, collected["host_name"], collected_at,
                                          failing_history, privacy_hides(privacy, "hide_command_lines"),
                                          privacy_hides(privacy, "hide_accounts"),
                                          privacy_hides(privacy, "hide_paths"))
            if changes:
                prepared_changes = prepare_rows(changes, TABLE_TASK_CHANGES, collected_at, privacy)
                upsert_by_key(tables[TABLE_TASK_CHANGES], prepared_changes,
                             ["host_name", "task_full_name", "change_kind", "changed_utc"],
                             chunk_size=chunk_size, label=TABLE_TASK_CHANGES)
                emit(f"{TABLE_TASK_CHANGES}: recorded {len(changes)} change(s).")
            purge_older_than(tables[TABLE_TASK_CHANGES], "changed_utc",
                             int(retention.get("changes_days", 365)), TABLE_TASK_CHANGES)
        except Exception as exc:
            emit(f"WARNING: {TABLE_TASK_CHANGES} write/purge failed ({type(exc).__name__}: {exc}); "
                f"this pass's state transitions are lost, not deferred.")

    metric_rows = prepare_rows(collected["metrics"], TABLE_METRICS, collected_at, privacy)
    counts.update(metrics_added=0, metrics_updated=0)
    if metric_rows:
        result = upsert_by_key(
            tables[TABLE_METRICS], metric_rows,
            ["host_name", "task_full_name", "metric_key", "log_file_name"],
            chunk_size=chunk_size, label=TABLE_METRICS,
        )
        emit(f"{TABLE_METRICS}: added {result['added']}, updated {result['updated']}.")
        counts.update(metrics_added=result["added"], metrics_updated=result["updated"])

    purge_older_than(tables[TABLE_RUNS], "start_time_utc",
                     int(retention.get("runs_days", 90)), TABLE_RUNS)
    purge_older_than(tables[TABLE_METRICS], "observed_at_utc",
                     int(retention.get("metrics_days", 90)), TABLE_METRICS)
    return counts


# --------------------------------------------------------------------------- cli

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect scheduled-task health and publish to ArcGIS Online.",
        allow_abbrev=False,  # never let --apply be reachable by abbreviation
    )
    parser.add_argument("--apply", action="store_true", help="Write to ArcGIS Online (default is a dry run).")
    parser.add_argument("--force", action="store_true",
                        help="Allow the inventory sync to delete stored task(s) for this host even "
                             "when that is more than half of what is stored.")
    parser.add_argument("--allow-host-mismatch", action="store_true",
                        help="Permit --apply when this machine is not host_name_override.")
    parser.add_argument("--target", choices=("prod", "test"), default="prod",
                        help="Which configured item id to write to.")
    parser.add_argument("--config", default="", help="Path to TaskDash.config.json.")
    parser.add_argument("--fixture", default="", help="Use a captured JSON fixture instead of live collection.")
    parser.add_argument("--capture-fixture", default="",
                        help="Write a host-local collection to this JSON path and exit.")
    parser.add_argument("--create-service", action="store_true",
                        help="Print what the hosted table service would hold, then exit. With "
                             "--apply, create it and print its item id.")
    parser.add_argument("--service-name", default="TaskDash", help="Service name for --create-service.")
    parser.add_argument("--create-view", action="store_true",
                        help="Print the plan for a read-only hosted view of the service (Query only, "
                             "never shared with everyone). With --apply, create it and print its id.")
    parser.add_argument("--full-history", action="store_true",
                        help="Ignore the watermark once and re-read the whole event-log window. "
                             "Safe (the run upsert is keyed); use after a gap or to repair history.")
    parser.add_argument("--migrate", action="store_true",
                        help="Report (or, with --apply, create/add) table(s)/field(s)/index(es) "
                             "missing versus the current schema, then exit without publishing.")
    parser.add_argument("--preview-alert", action="store_true",
                        help="Collect, then write Logs/alert_preview_<ts>.txt with the alert e-mail "
                             "the hourly prod run would send now. Sends nothing, writes nothing else.")
    parser.add_argument("--refresh-result-text", action="store_true",
                        help="Report stored runs whose result text is out of date with the current "
                             "code-to-text mapping, then exit. With --apply, update that text.")
    parser.add_argument("--refresh-metrics", action="store_true",
                        help="Report stored metric rows under keys the log miner no longer makes, and "
                             "outdated metric names, then exit. With --apply, delete and rename them. "
                             "Run it on the task host: it reads the task logs.")
    parser.add_argument("--scrub-history", action="store_true",
                        help="Report how many stored rows in each table still hold a value the "
                             "privacy switches hide or the secret scrubber redacts, then exit. With "
                             "--apply, blank or redact those values.")
    parser.add_argument("--check-anonymous", action="store_true",
                        help="Query every table of the service and view without signing in. Exit 0 "
                             "when all refuse, 1 when any answers. Reads only.")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    args = parser.parse_args(argv)
    others = {"--migrate": args.migrate, "--preview-alert": args.preview_alert,
              "--capture-fixture": args.capture_fixture, "--create-service": args.create_service,
              "--create-view": args.create_view, "--fixture": args.fixture,
              "--refresh-result-text": args.refresh_result_text,
              "--refresh-metrics": args.refresh_metrics, "--scrub-history": args.scrub_history,
              "--check-anonymous": args.check_anonymous}
    for flag in ("--scrub-history", "--check-anonymous"):
        if others[flag] and any(on for name, on in others.items() if name != flag):
            parser.error(f"{flag} runs on its own; leave out the other action flags and --fixture.")
    if args.check_anonymous and args.apply:
        parser.error("--check-anonymous only reads; run it without --apply.")
    for flag, chosen in (("--refresh-result-text", args.refresh_result_text),
                         ("--refresh-metrics", args.refresh_metrics)):
        if chosen and (args.migrate or args.preview_alert or args.capture_fixture
                       or args.create_service or args.fixture
                       or (args.refresh_result_text and args.refresh_metrics)):
            parser.error(f"{flag} runs on its own; leave out --migrate, --preview-alert, "
                         "--capture-fixture, --create-service, --fixture and the other --refresh flag.")
    if args.create_view and (args.migrate or args.preview_alert or args.capture_fixture
                             or args.create_service or args.fixture or args.refresh_result_text
                             or args.refresh_metrics):
        parser.error("--create-view runs on its own; leave out --migrate, --preview-alert, "
                     "--capture-fixture, --create-service, --fixture and the --refresh flags.")
    if args.preview_alert and (args.apply or args.migrate or args.capture_fixture
                               or args.create_service or args.target != "prod"):
        parser.error("--preview-alert only reads; run it without --apply, --migrate, "
                     "--capture-fixture, --create-service and --target test (test never alerts).")
    return args


def run_migration(config: Dict[str, Any], args: argparse.Namespace) -> int:
    """The --migrate action: report or apply schema drift, then exit (never publishes)."""
    gis = connect_agol(config)
    item = ensure_service(gis, config, args.target)
    tables = get_tables(item)
    if args.apply:
        apply_migration(tables, item=item)
    else:
        describe_migration(tables)
    view_id = normalize_text(config.get("arcgis", {}).get(view_key(args.target)))
    view_item = None
    if view_id:
        view_item = gis.content.get(view_id)
        if view_item is None:
            raise SystemExit(f"View item {view_id} not found or not visible to this account.")
        migrate_view(view_item, config, args.apply)
    if args.apply:
        emit("Migration complete.")
        # A definition change once opened Query to anonymous users; prove it did not this time.
        require_no_anonymous_access(table_urls(item, view_item))
    else:
        emit("DRY RUN - nothing written. Re-run with --migrate --apply to apply.")
    return 0


def refresh_result_text(table: Any, apply: bool, chunk_size: int = DEFAULT_CHUNK_SIZE) -> int:
    """--refresh-result-text: describe every stored run's result code again.

    A run row gets its text once, when it is first written. A later fix to the code-to-text
    mapping reaches only runs read again, and runs older than the event log are never read
    again. Reports each old -> new text with its row count. Only apply=True writes, and it
    changes result_text only. Returns the number of rows with outdated text.
    """
    oid_field = object_id_field(table)
    rows = query_all_rows(table, where="result_code IS NOT NULL",
                          out_fields=f"{oid_field},result_code,result_text,run_status")
    updates: List[Dict[str, Any]] = []
    changes: Dict[Tuple[str, str], int] = {}
    for row in rows:
        old = normalize_text(row.get("result_text"))
        new = run_result_text(row.get("result_code"), old, row.get("run_status"))
        if new != old:
            updates.append({oid_field: row.get(oid_field), "result_text": new})
            changes[(old, new)] = changes.get((old, new), 0) + 1
    for (old, new), count in sorted(changes.items(), key=lambda kv: -kv[1]):
        emit(f"  {count} row(s): '{old}' -> '{new}'")
    emit(f"{TABLE_RUNS}: {len(updates)} of {len(rows)} stored run(s) have outdated result text.")
    if apply and updates:
        apply_edits_chunked(table, updates=updates, chunk_size=chunk_size,
                            label=f"{TABLE_RUNS} result_text")
        emit(f"{TABLE_RUNS}: updated the result text of {len(updates)} run(s).")
    return len(updates)


def run_refresh_result_text(config: Dict[str, Any], args: argparse.Namespace) -> int:
    """The --refresh-result-text action: report (or, with --apply, write) outdated run text."""
    gis = connect_agol(config)
    item = ensure_service(gis, config, args.target)
    tables = get_tables(item)
    if TABLE_RUNS not in tables:
        raise SystemExit(f"Target service has no {TABLE_RUNS} table. Run --migrate --apply first.")
    chunk_size = int(config.get("upload", {}).get("chunk_size", DEFAULT_CHUNK_SIZE))
    outdated = refresh_result_text(tables[TABLE_RUNS], args.apply, chunk_size)
    if outdated and not args.apply:
        emit("DRY RUN - nothing written. Re-run with --refresh-result-text --apply to update them.")
    return 0


SCRUB_HISTORY_TABLES = (TABLE_INVENTORY, TABLE_RUNS, TABLE_METRICS, TABLE_COLLECTOR_RUNS,
                        TABLE_TASK_CHANGES)


def plan_scrub_history(rows: Sequence[Dict[str, Any]], table_name: str,
                       privacy: Optional[Dict[str, Any]], oid_field: str) -> List[Dict[str, Any]]:
    """--scrub-history: the update (OID and changed fields only) that gives each stored row the
    values prepare_rows would publish now. Rows written before a switch was turned on, or before
    the scrubber learned a form, keep the old value until this rewrites them."""
    lengths = {f["name"]: f["length"] for f in TABLE_SPECS[table_name][0]() if f.get("length")}
    scrubbed = _SCRUBBED_FIELDS.get(table_name, ())
    hidden = _hidden_fields(table_name, privacy)
    updates: List[Dict[str, Any]] = []
    for row in rows:
        change = {}
        for key in set(scrubbed) | hidden:
            if key not in row:
                continue
            value = _published_value(key, row[key], hidden, scrubbed, lengths, privacy)
            # None and "" are both blank; rewriting one as the other is churn, not privacy.
            if normalize_text(value) != normalize_text(row[key]):
                change[key] = value
        if change:
            updates.append({oid_field: row.get(oid_field), **change})
    return updates


def scrub_history(tables: Dict[str, Any], privacy: Optional[Dict[str, Any]], apply: bool,
                  chunk_size: int = DEFAULT_CHUNK_SIZE) -> Dict[str, int]:
    """Count (and with apply, write) the stored rows plan_scrub_history would change, per table.
    Prints counts only, never a value: the values are what is being hidden."""
    counts: Dict[str, int] = {}
    for name in SCRUB_HISTORY_TABLES:
        if name not in tables:
            emit(f"{name}: table not found; skipped.")
            continue
        fields = sorted(set(_SCRUBBED_FIELDS.get(name, ())) | _hidden_fields(name, privacy))
        if not fields:
            continue
        table = tables[name]
        oid_field = object_id_field(table)
        rows = query_all_rows(table, out_fields=",".join([oid_field, *fields]))
        updates = plan_scrub_history(rows, name, privacy, oid_field)
        counts[name] = len(updates)
        emit(f"{name}: {len(updates)} of {len(rows)} stored row(s) "
             f"{'to rewrite' if apply else 'would change'}.")
        if apply and updates:
            apply_edits_chunked(table, updates=updates, chunk_size=chunk_size, label=f"{name} scrub")
            emit(f"{name}: rewrote {len(updates)} row(s).")
    return counts


def run_scrub_history(config: Dict[str, Any], args: argparse.Namespace) -> int:
    """The --scrub-history action: report (or, with --apply, write) the stored-row clean-up."""
    gis = connect_agol(config)
    item = ensure_service(gis, config, args.target)
    tables = get_tables(item)
    # The query names every scrubbed field; refuse an unmigrated service with the fix, not a 400.
    check_schema_drift(tables)
    chunk_size = int(config.get("upload", {}).get("chunk_size", DEFAULT_CHUNK_SIZE))
    counts = scrub_history(tables, config.get("privacy"), args.apply, chunk_size)
    if any(counts.values()) and not args.apply:
        emit("DRY RUN - nothing written. Re-run with --scrub-history --apply to rewrite them.")
    return 0


def plan_metric_refresh(stored: Sequence[Dict[str, Any]], fresh: Sequence[Dict[str, Any]],
                        oid_field: str, examined: Optional[set] = None,
                        own_tasks: Iterable[str] = ()) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """--refresh-metrics: the stored TaskMetrics rows to delete, and the display names to rewrite.

    TaskMetrics is upserted by (host, task, metric_key, log file), so a row under a key the miner
    no longer makes (features_max_oid, the bare Stormwater counts, inserted_rows_in) stays for 90
    days, and Metric Change flags it "Not in last run" all that time. A key is retired for a task
    when a log file mined again now held it, and no log of that task gives it now. Then every row
    of that key is deleted, also rows older than the mining window. A key that only older logs
    hold is kept: the script may have stopped logging it, and that is a real signal.
    Row by row, a stored row is also deleted when the miner examined its log this pass (read it,
    or ignored it as a manual run) and that log no longer gives its key -- Keep-Alive's dry-run
    "would touch" rows -- and every row of TaskDash's own task (own_tasks), which is not mined.
    Returns (OIDs to delete, display_name updates).
    """
    def task_of(row: Dict[str, Any]) -> Tuple[str, str]:
        return normalize_text(row.get("host_name")), normalize_text(row.get("task_full_name"))

    produced: Dict[Tuple[str, str], set] = {}
    remined: set = set()
    for row in fresh:
        produced.setdefault(task_of(row), set()).add(normalize_text(row.get("metric_key")))
        remined.add(task_of(row) + (normalize_text(row.get("log_file_name")),))
    retired = {task_of(r) + (normalize_text(r.get("metric_key")),) for r in stored
               if task_of(r) + (normalize_text(r.get("log_file_name")),) in remined
               and normalize_text(r.get("metric_key")) not in produced[task_of(r)]}

    examined_logs = {(normalize_text(t), normalize_text(f)) for t, f in (examined or ())}
    fresh_rows = {task_of(r) + (normalize_text(r.get("log_file_name")), normalize_text(r.get("metric_key")))
                  for r in fresh}
    own = {normalize_text(t) for t in own_tasks}

    deletes: List[Any] = []
    updates: List[Dict[str, Any]] = []
    for row in stored:
        key = normalize_text(row.get("metric_key"))
        log = normalize_text(row.get("log_file_name"))
        gone = ((task_of(row)[1], log) in examined_logs and task_of(row) + (log, key) not in fresh_rows)
        if task_of(row) + (key,) in retired or gone or task_of(row)[1] in own:
            deletes.append(row.get(oid_field))
            continue
        # A renamed display name split each series in two in the Metric selector (X5); a row
        # stored before series_key existed is invisible to the Metric change row click.
        change = {}
        name = metrics.metric_display_name(key)
        if normalize_text(row.get("display_name")) != name:
            change["display_name"] = name
        series = metrics.metric_series_key(row.get("host_name"), row.get("task_full_name"), row.get("metric_key"))
        if row.get("series_key") != series:
            change["series_key"] = series
        if change:
            updates.append({oid_field: row.get(oid_field), **change})
    return deletes, updates


def run_refresh_metrics(config: Dict[str, Any], args: argparse.Namespace) -> int:
    """The --refresh-metrics action: report (or, with --apply, write) the metric row clean-up."""
    gis = connect_agol(config)
    item = ensure_service(gis, config, args.target)
    tables = get_tables(item)
    if TABLE_METRICS not in tables or TABLE_INVENTORY not in tables:
        raise SystemExit(f"Target service has no {TABLE_METRICS} or {TABLE_INVENTORY} table. "
                         "Run --migrate --apply first.")
    # The stored-row query names series_key: on a service not yet migrated it failed as an
    # opaque AGOL 400 ("'outFields' parameter is invalid"), so refuse with the fix instead.
    check_schema_drift(tables)
    host_name = get_host_name(config)
    # The stored inventory holds each task's script path and working directory, so no
    # PowerShell or event read is needed to find the logs again -- unless privacy.hide_paths
    # blanked those paths, in which case read the tasks from Task Scheduler again.
    if privacy_hides(config.get("privacy"), "hide_paths"):
        inventory = collect_everything(config, None)["inventory"]
    else:
        inventory = query_all_rows(tables[TABLE_INVENTORY],
                                   where=f"host_name = '{escape_sql_string(host_name)}'")
    examined: set = set()
    fresh, _ = mine_metrics(config, inventory, host_name, examined)
    if not fresh:
        emit("WARNING: no task log resolved on this machine, so no metric key can be judged retired. "
             "Run --refresh-metrics on the task host.")
    table = tables[TABLE_METRICS]
    oid_field = object_id_field(table)
    stored = query_all_rows(table, out_fields=f"{oid_field},host_name,task_full_name,metric_key,"
                                              "log_file_name,display_name,series_key")
    own_tasks = [r.get("task_full_name") for r in inventory if metrics.is_taskdash_command(r.get("command_target_name"))]
    deletes, updates = plan_metric_refresh(stored, fresh, oid_field, examined, own_tasks)
    emit(f"{TABLE_METRICS}: {len(deletes)} row(s) that the log miner no longer makes, {len(updates)} row(s) with an "
         f"outdated name or series key, of {len(stored)} stored.")
    if not (deletes or updates):
        return 0
    if not args.apply:
        emit("DRY RUN - nothing written. Re-run with --refresh-metrics --apply to write them.")
        return 0
    chunk_size = int(config.get("upload", {}).get("chunk_size", DEFAULT_CHUNK_SIZE))
    apply_edits_chunked(table, updates=updates, deletes=deletes, chunk_size=chunk_size,
                        label=f"{TABLE_METRICS} refresh")
    emit(f"{TABLE_METRICS}: deleted {len(deletes)} and renamed {len(updates)} row(s).")
    return 0


# A run that cannot reach AGOL saves its CollectorRuns row here; the next run that can writes it.
# One file per --target, so a test run's row never lands in the prod table.
PENDING_COLLECTOR_RUNS_FILE = "collector_runs_pending_{target}.json"
# ponytail: keeps the newest rows only; about 8 days of hourly failures before the oldest drop.
MAX_PENDING_COLLECTOR_RUNS = 200


def _collector_run_row(
    collected: Dict[str, Any], *,
    run_started: dt.datetime, run_ended: dt.datetime, exit_status: str, error_text: str,
    watermark_from: float, watermark_to: float, publish_counts: Dict[str, int], alert_sent: bool,
) -> Dict[str, Any]:
    """One CollectorRuns row, before prepare_rows. JSON-safe, so a pending file can hold it."""
    host_name = collected.get("host_name", "")
    return {
        "run_id": f"{host_name}_{run_started.strftime('%Y%m%dT%H%M%S%f')}", "host_name": host_name,
        "started_utc": run_started.isoformat(), "ended_utc": run_ended.isoformat(),
        "duration_seconds": round((run_ended - run_started).total_seconds(), 2),
        # Every dashboard viewer can read this table, so an error that quotes a URL or a token
        # is scrubbed the same way as the e-mail log tail.
        "exit_status": exit_status, "error_text": _scrub_secrets(error_text)[:2000],
        "tasks_seen": len(collected.get("inventory") or []),
        "events_read": len(collected.get("raw_events") or []),
        "watermark_from": watermark_from, "watermark_to": watermark_to,
        "runs_written": publish_counts.get("runs_added", 0) + publish_counts.get("runs_updated", 0),
        "inventory_written": publish_counts.get("inventory_added", 0) + publish_counts.get("inventory_updated", 0),
        "metrics_written": publish_counts.get("metrics_added", 0) + publish_counts.get("metrics_updated", 0),
        "alert_sent": 1 if alert_sent else 0,
        "taskdash_version": TASKDASH_VERSION, "schema_version": SCHEMA_VERSION,
    }


def _read_pending_collector_runs(path: Optional[Path]) -> List[Dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        emit(f"WARNING: could not read {path} ({exc}); its saved {TABLE_COLLECTOR_RUNS} rows are lost.")
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("run_id")] if isinstance(rows, list) else []


def _save_pending_collector_runs(path: Optional[Path], rows: List[Dict[str, Any]]) -> None:
    """Rewrite the pending file with rows (an empty list once they are written). Never raises:
    it runs on failure paths, and must not hide the run's own error."""
    if path is None:
        return
    if len(rows) > MAX_PENDING_COLLECTOR_RUNS:
        emit(f"WARNING: {len(rows) - MAX_PENDING_COLLECTOR_RUNS} unsent {TABLE_COLLECTOR_RUNS} row(s) dropped; "
             f"only the newest {MAX_PENDING_COLLECTOR_RUNS} are kept in {path}.")
    try:
        atomic_write_json(path, rows[-MAX_PENDING_COLLECTOR_RUNS:])
    except OSError as exc:
        emit(f"WARNING: could not save {TABLE_COLLECTOR_RUNS} rows to {path}: {exc}")


def _write_collector_run_row(
    item: Any, config: Dict[str, Any], collected: Dict[str, Any], *,
    run_started: dt.datetime, run_ended: dt.datetime, exit_status: str, error_text: str,
    watermark_from: float, watermark_to: float, publish_counts: Dict[str, int], alert_sent: bool,
    pending_path: Optional[Path] = None,
) -> None:
    """3.6: best-effort CollectorRuns audit row, written at the end of every --apply run.

    Best-effort by design: a service that has not run `--migrate --apply` yet simply has no
    CollectorRuns table (skipped with a note, same as any other not-yet-migrated table), and any
    other failure here is logged and swallowed -- this must never mask run_publish()'s real
    outcome, which is why the caller computes exit_status/error_text before calling this and
    re-raises its own stashed publish_error afterwards regardless of what happens here.

    A row that cannot reach AGOL goes to pending_path, and the next run that can writes it first.
    Without that, the table showed only successes: the 2026-10-16 01:00 run lost AGOL and left no
    row. The upsert is keyed on run_id, so a pending row sent twice updates, never duplicates.
    """
    row = _collector_run_row(
        collected, run_started=run_started, run_ended=run_ended, exit_status=exit_status,
        error_text=error_text, watermark_from=watermark_from, watermark_to=watermark_to,
        publish_counts=publish_counts, alert_sent=alert_sent)
    pending = _read_pending_collector_runs(pending_path)
    if item is None:
        # The pre-publish connect failed, so there is no service to write to. Calling get_tables
        # anyway only imported arcgis (~8 s) to fail on None.
        emit(f"{TABLE_COLLECTOR_RUNS}: no AGOL connection this run; saving the audit row for the next run.")
        _save_pending_collector_runs(pending_path, pending + [row])
        return
    try:
        tables = get_tables(item)
    except Exception as exc:
        emit(f"WARNING: could not write {TABLE_COLLECTOR_RUNS} row (get_tables failed): {exc}")
        _save_pending_collector_runs(pending_path, pending + [row])
        return
    if TABLE_COLLECTOR_RUNS not in tables:
        # Not saved: until --migrate --apply adds the table, no later run can write it either.
        emit(f"{TABLE_COLLECTOR_RUNS}: table not found -- run --migrate --apply to add it; "
            f"skipping this run's audit row.")
        return
    try:
        prepared = prepare_rows(pending + [row], TABLE_COLLECTOR_RUNS, run_ended.isoformat())
        upsert_by_key(tables[TABLE_COLLECTOR_RUNS], prepared, ["run_id"],
                      chunk_size=DEFAULT_CHUNK_SIZE, label=TABLE_COLLECTOR_RUNS)
    except Exception as exc:
        emit(f"WARNING: could not write {TABLE_COLLECTOR_RUNS} row: {exc}")
        _save_pending_collector_runs(pending_path, pending + [row])
        return
    if pending:
        emit(f"{TABLE_COLLECTOR_RUNS}: wrote {len(pending)} row(s) saved by earlier runs.")
        _save_pending_collector_runs(pending_path, [])
    try:
        # 90-day retention, like TaskRuns -- same config key, no new one needed.
        retention_days = int(config.get("retention", {}).get("runs_days", 90))
        purge_older_than(tables[TABLE_COLLECTOR_RUNS], "started_utc", retention_days, TABLE_COLLECTOR_RUNS)
    except Exception as exc:
        emit(f"WARNING: could not purge old {TABLE_COLLECTOR_RUNS} rows: {exc}")


def run_publish(args: argparse.Namespace, config: Dict[str, Any], log_dir: Path) -> int:
    """Collect, report, and (with --apply) publish. Split out of main() so --apply can run it
    inside run_lock() while a plain dry run -- read-only, safe to overlap with anything -- never
    has to take the lock at all.

    An --apply run that dies on an uncaught error still saves a failed CollectorRuns row to the
    pending file, so the table shows the failure once a later run reaches AGOL."""
    run_started = utc_now()
    progress = {"armed": False, "recorded": False}
    try:
        return _run_publish(args, config, log_dir, run_started, progress)
    except (Exception, SystemExit) as exc:
        if progress["armed"] and not progress["recorded"]:
            pending_path = log_dir / PENDING_COLLECTOR_RUNS_FILE.format(target=args.target)
            row = _collector_run_row(
                {"host_name": get_host_name(config)}, run_started=run_started, run_ended=utc_now(),
                exit_status="failed", error_text=normalize_text(exc), watermark_from=0.0,
                watermark_to=0.0, publish_counts={}, alert_sent=False)
            _save_pending_collector_runs(pending_path, _read_pending_collector_runs(pending_path) + [row])
        raise


def _run_publish(args: argparse.Namespace, config: Dict[str, Any], log_dir: Path,
                 run_started: dt.datetime, progress: Dict[str, bool]) -> int:
    # Connect before collecting so the incremental watermark is known, and so a credential or
    # item-id problem surfaces before a minute of collection rather than after it.
    gis = item = None
    watermark = 0
    preflight_error: Optional[BaseException] = None
    if args.apply:
        # A warning is enough for a dry run; for a write it is not. Publishing from the wrong
        # machine stamps that machine's tasks with the configured host and corrupts the real
        # inventory -- the same silent-wrong-data failure class as a host rename.
        import socket
        configured = normalize_text(config.get("host_name_override"))
        if configured and configured.lower() != socket.gethostname().lower() and not args.allow_host_mismatch:
            raise SystemExit(
                f"Refusing to publish: host_name_override is '{configured}' but this machine is "
                f"'{socket.gethostname()}'. Collected data would describe the wrong host. "
                f"Run this on '{configured}', or pass --allow-host-mismatch if you are certain."
            )
        # Armed only past the host check: a refused wrong-host run must not save a row that a
        # later run would publish under the configured host.
        progress["armed"] = True
        # An expired credential or an AGOL outage here must not also silence a failed-task
        # e-mail (same reasoning as publish() below) -- caught and stashed so collection and
        # alerting still happen on whatever we can get, then re-raised at the end.
        tables: Dict[str, Any] = {}
        try:
            gis = connect_agol(config)
            item = ensure_service(gis, config, args.target)
            tables = get_tables(item)
        except (Exception, SystemExit) as exc:
            preflight_error = exc
            gis = item = None
            emit(f"WARNING: could not reach AGOL before publishing ({type(exc).__name__}: {exc}); "
                f"still collecting and evaluating alerts before exiting.")

        if preflight_error is None:
            # A missing field/index is a deliberate refusal (run --migrate --apply first), not
            # a transient outage -- unlike the block above, this must keep failing fast.
            check_schema_drift(tables)
            # Same: a public source service is a setup fault to fix, not an outage to ride out.
            # It reads only item.access, loaded above, so it makes no request that an outage
            # could fail here, outside the try below.
            check_source_sharing(item, config, args.target)
            try:
                if TABLE_RUNS in tables and not args.full_history:
                    newest = None if args.fixture else collect.newest_record_id(
                        int(config.get("powershell_timeout_seconds", 180)))
                    watermark = event_watermark(tables[TABLE_RUNS], get_host_name(config), newest)
                emit(f"Publishing to '{item.title}' ({item.id}) [{args.target}], watermark={watermark}")
            except (Exception, SystemExit) as exc:
                preflight_error = exc
                gis = item = None
                watermark = 0
                emit(f"WARNING: could not reach AGOL before publishing ({type(exc).__name__}: {exc}); "
                    f"still collecting and evaluating alerts before exiting.")

    # An empty run table means there is no history to inherit, so spend a bigger one-off event
    # budget on the initial backfill. Measured: 5000 events reaches back only ~5 days here.
    history_cfg = config.get("history", {})
    max_events = 0
    if args.apply and not watermark:
        max_events = int(history_cfg.get("first_run_max_events", 30000))
        why = "--full-history" if args.full_history else "No usable watermark"
        emit(f"{why}: reading the event-log window with up to {max_events} events.")

    collected = collect_everything(config, Path(args.fixture) if args.fixture else None,
                                   min_record_id=watermark, max_events=max_events)
    report(collected)

    if not args.apply:
        emit("DRY RUN - nothing written. Re-run with --apply to publish.")
        return 0

    # Evaluate and send the alert from the collected data even when publish() fails (an AGOL
    # outage or expired credential must not also silence a failed-task e-mail) -- but a failed
    # write still has to fail the run, so the error is stashed and re-raised after alerting.
    # A preflight failure above already means there is no gis/item to publish with, so it is
    # carried straight through as this run's error instead of attempting publish().
    publish_error: Optional[BaseException] = preflight_error
    publish_counts: Dict[str, int] = {}
    if publish_error is None:
        try:
            publish_counts = publish(gis, item, collected, config, force=args.force) or {}
        except (Exception, SystemExit) as exc:
            publish_error = exc
            emit(f"WARNING: publish() failed ({type(exc).__name__}: {exc}); still evaluating "
                f"alerts from the collected data before exiting.")

    # A test publish must neither mail real recipients nor advance prod's alert state.
    alert_sent = False
    if args.target == "prod":
        alert_sent = bool(maybe_send_alert(collected, config, log_dir))
    else:
        emit("Alerting skipped: --target test never e-mails or touches alert_state.json.")

    # A failed publish (preflight or the write itself) means no run landed, so the watermark
    # did not actually advance -- report it unchanged rather than the highest id merely collected.
    watermark_to = (float(watermark) if publish_error is not None else
                    max((safe_float(r.get("max_event_record_id")) or 0.0
                        for r in collected.get("runs") or []), default=float(watermark)))
    _write_collector_run_row(
        item, config, collected, run_started=run_started, run_ended=utc_now(),
        exit_status="failed" if publish_error is not None else "success",
        error_text=normalize_text(publish_error) if publish_error is not None else "",
        watermark_from=float(watermark), watermark_to=watermark_to,
        publish_counts=publish_counts, alert_sent=alert_sent,
        pending_path=log_dir / PENDING_COLLECTOR_RUNS_FILE.format(target=args.target),
    )
    progress["recorded"] = True

    if publish_error is not None:
        raise publish_error

    emit("TaskDash publish complete.")
    return 0


def _run_under_lock(log_dir: Path, action: Callable[[], int]) -> int:
    """Run action() inside run_lock, translating contention into an exit code.

    0 for a normal hourly overlap; 1 (so Task Scheduler shows a failure) once the lock has been
    held past STALE_LOCK_HOURS -- a run stuck that long would otherwise leave every later hourly
    run exiting 0 without writing anything, going silently stale forever, which is worse than a
    failed run.
    """
    try:
        with run_lock(log_dir):
            return action()
    except RunLockHeld as exc:
        emit(f"Another TaskDash run is in progress; exiting. {exc}")
        if exc.held_since and (utc_now() - exc.held_since) > dt.timedelta(hours=STALE_LOCK_HOURS):
            emit(f"WARNING: the lock has been held since {exc.held_since.isoformat()} -- more "
                f"than {STALE_LOCK_HOURS}h, longer than any normal run. Treating it as stuck "
                f"and exiting nonzero so Task Scheduler flags this.")
            return 1
        return 0


def run_preview_alert(args: argparse.Namespace, config: Dict[str, Any], log_dir: Path) -> int:
    """The --preview-alert action. Reads the event log's full window, not the dry run's
    max_events_per_run (~5 days): the alert judges each task's newest run, and a weekly task's
    can be 7 days old. The hourly run gets it from the AGOL table; the preview stays off AGOL."""
    collected = collect_everything(
        config, Path(args.fixture) if args.fixture else None,
        max_events=int(config.get("history", {}).get("first_run_max_events", 30000)))
    report(collected)
    preview_alert(collected, config, log_dir)
    return 0


def _main_dispatch(args: argparse.Namespace, config: Dict[str, Any], log_dir: Path) -> int:
    """The CLI's normal branching, split out of main() so main() can wrap it in one place with
    the error-logging and healthcheck-ping handling that every branch needs alike."""
    if args.create_service:
        if not args.apply:
            emit(f"Plan: hosted table service '{args.service_name}' with the tables {TABLE_INVENTORY}, "
                 f"{TABLE_RUNS} and {TABLE_METRICS}.")
            emit(f"The same run then adds {TABLE_COLLECTOR_RUNS} and {TABLE_TASK_CHANGES}, "
                 f"the indexes and the version stamp.")
            emit("DRY RUN - nothing written. Re-run with --create-service --apply to create it.")
            return 0
        gis = connect_agol(config)
        item = create_service(gis, args.service_name, config)
        # Without this the first --apply publish needed --migrate --apply (Trap 1).
        apply_migration(get_tables(item), item=item)
        emit(f"Add this to the config: \"item_id\": \"{item.id}\" (all 5 tables, indexes and the "
             f"version stamp exist; no --migrate needed).")
        return 0

    if args.check_anonymous:
        return run_check_anonymous(config, args)

    if args.scrub_history:
        if not args.apply:
            return run_scrub_history(config, args)
        # It rewrites stored rows in every table, so it must not overlap an hourly publish.
        return _run_under_lock(log_dir, lambda: run_scrub_history(config, args))

    if args.capture_fixture:
        # Not scrubbed: the golden replay needs the raw values to reproduce the published rows.
        emit("WARNING: the fixture holds raw command lines and account names. Keep it private: "
             "never commit, share or upload it.")
        host_name = get_host_name(config)
        timeout = int(config.get("powershell_timeout_seconds", 180))
        history = config.get("history", {})
        payload = {
            "host_name": host_name,
            "captured_at_utc": utc_now().isoformat(),
            "tasks": collect.collect_tasks(timeout),
            "events": collect.collect_run_events(
                int(history.get("days_back", 30)),
                int(history.get("max_events_per_run", 5000)),
                timeout),
        }
        out = Path(args.capture_fixture)
        out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        emit(f"Fixture written to {out} ({len(payload['tasks'])} tasks, {len(payload['events'])} events).")
        return 0

    if args.create_view:
        if not args.apply:
            return run_create_view(config, args)
        # It creates and edits a service item, so it takes the single-writer lock like --migrate.
        return _run_under_lock(log_dir, lambda: run_create_view(config, args))

    if args.migrate:
        if not args.apply:
            return run_migration(config, args)
        # --migrate --apply writes schema to the live service; it must not overlap an hourly
        # --apply publish, so it takes the same single-writer lock.
        return _run_under_lock(log_dir, lambda: run_migration(config, args))

    if args.refresh_result_text:
        if not args.apply:
            return run_refresh_result_text(config, args)
        # It writes TaskRuns, so it must not overlap an hourly --apply publish.
        return _run_under_lock(log_dir, lambda: run_refresh_result_text(config, args))

    if args.refresh_metrics:
        if not args.apply:
            return run_refresh_metrics(config, args)
        # It deletes and updates TaskMetrics rows, so it must not overlap an hourly publish.
        return _run_under_lock(log_dir, lambda: run_refresh_metrics(config, args))

    if args.preview_alert:
        return run_preview_alert(args, config, log_dir)

    if not args.apply:
        return run_publish(args, config, log_dir)

    # One writer at a time: an overlapping manual run or a slow hourly run must never race the
    # same adds/updates/deletes/purges against AGOL together.
    return _run_under_lock(log_dir, lambda: run_publish(args, config, log_dir))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config, config_warnings = load_config(Path(args.config) if args.config else None,
                                          defer_warnings=True)

    log_dir = resolve_log_dir(config)
    log_path = setup_logging(log_dir, "taskdash")
    cleanup_old_logs(log_dir, int(config.get("logging", {}).get("max_log_age_days", 30)))
    for warning in config_warnings:
        emit(warning)
    emit(f"TaskDash started. Log: {log_path}")

    # Every fatal error must reach the timestamped log file, not just stderr/bootstrap.log: a
    # bare `raise SystemExit(msg)` or an uncaught exception only prints to stderr on its own,
    # and pythonw/Task Scheduler runs have no stderr to read. Logged here once, in the one place
    # every CLI branch passes through, then re-raised so the process still exits non-zero.
    #
    # The dead man's switch pings only for the actual hourly prod publish job (--apply, no
    # --migrate/--capture-fixture/--create-service/--fixture/--preview-alert/--refresh-*,
    # --target prod): a manual dry run, a --target test run, a fixture replay or a --migrate would otherwise reset
    # (or /fail) the switch for a run that is not the scheduled prod publish it is meant to watch.
    is_prod_publish = (args.apply and args.target == "prod" and not args.migrate
                       and not args.capture_fixture and not args.create_service
                       and not args.create_view
                       and not args.fixture and not args.preview_alert
                       and not args.refresh_result_text and not args.refresh_metrics
                       and not args.scrub_history and not args.check_anonymous)
    if is_prod_publish:
        ping_healthcheck(config, "/start")
    try:
        result = _main_dispatch(args, config, log_dir)
    except SystemExit as exc:
        logging.error(f"TaskDash exiting: {exc.code}")
        if is_prod_publish:
            ping_healthcheck(config, "/fail")
        raise
    except Exception:
        logging.exception("TaskDash failed with an uncaught exception.")
        if is_prod_publish:
            ping_healthcheck(config, "/fail")
        raise
    if is_prod_publish:
        ping_healthcheck(config, "" if result == 0 else "/fail")
    return result


# --------------------------------------------------------------------------- alerting

def _problem_key(task: Dict[str, Any]) -> str:
    """'FAILED: name' / 'OVERDUE: name' for a task worth an alert, else ''."""
    if task.get("last_run_failed"):
        return f"FAILED: {task['task_name']}"
    if task.get("is_overdue"):
        return f"OVERDUE: {task['task_name']}"
    return ""


def alert_signature(inventory: Sequence[Dict[str, Any]]) -> List[str]:
    """The set of problems worth an e-mail, as a stable sorted signature.

    Only hard signals: a failed last run, or an enabled task past its next run time. Warnings
    are deliberately excluded -- a monitor that mails about everything gets filtered to trash.
    Deliberately holds no changing number (duration, hours overdue): "OVERDUE: X by 3.2h" used
    to become "by 3.3h" the next hour, which looked like a new problem to should_alert and
    re-alerted every run. The number still reaches the e-mail body via alert_details().
    """
    return sorted(k for k in (_problem_key(t) for t in inventory) if k)


def alert_details(inventory: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Full one-line text (with the changing number) for each alert_signature() entry."""
    details: Dict[str, str] = {}
    for task in inventory:
        key = _problem_key(task)
        if not key:
            continue
        if task.get("last_run_failed"):
            details[key] = f"{key} -- {task.get('last_result_text', '')}".strip()
        else:
            details[key] = f"{key} by {short_duration(abs(task.get('next_run_in_hours') or 0) * 3600)}"
    return details


def _active_maintenance_windows(windows: Sequence[Dict[str, Any]], now: dt.datetime) -> List[Dict[str, Any]]:
    """This run's active alerting.maintenance_windows entries: start <= now <= end.

    Logged every run (not just when an e-mail is sent) so a suppressed alert always leaves a
    trace -- an expired window is reported and dropped rather than silently kept or silently
    dropped without a word.
    """
    active = []
    for window in windows:
        start = parse_iso_datetime(window.get("start"))
        end = parse_iso_datetime(window.get("end"))
        if start is None or end is None:
            emit(f"WARNING: alerting.maintenance_windows entry has an unparseable start/end "
                f"({window}); ignored.")
            continue
        if end < now:
            emit(f"Maintenance window for task pattern {window.get('task')!r} expired at "
                f"{end.isoformat()}; ignored.")
            continue
        if start <= now <= end:
            active.append(window)
    return active


def _task_in_maintenance(task_name: str, windows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for window in windows:
        pattern = normalize_text(window.get("task"))
        if not pattern:
            continue
        try:
            if re.search(pattern, task_name):
                return window
        except re.error:
            emit(f"WARNING: alerting.maintenance_windows task pattern {pattern!r} is not a "
                f"valid regex; ignored.")
    return None


def partition_maintenance(
    inventory: Sequence[Dict[str, Any]], windows: Sequence[Dict[str, Any]],
) -> Tuple[List[str], List[str]]:
    """Split alert_signature(inventory) into (alerted, suppressed-by-maintenance)."""
    alerted, suppressed = [], []
    for task in inventory:
        key = _problem_key(task)
        if not key:
            continue
        target = suppressed if _task_in_maintenance(task["task_name"], windows) else alerted
        target.append(key)
    return sorted(alerted), sorted(suppressed)



# No \b before the key, so it still matches after an underscore or an opening quote --
# ARCGIS_PASSWORD=, DB_PWD= and JSON-style "password": "x" all have a non-boundary character
# (_ or ") right before the key that a leading \b would refuse to cross. Nothing before the key is
# consumed either: a [\w"']* prefix there made a long word cost quadratic time (20,000 letters
# took 15 s). ["']? after the key absorbs a JSON-style closing quote before the colon.
#
# A secret value: quoted (an unclosed quote runs to the end, as after a cut) or one bare token.
# (?!<redacted>) in the patterns below keeps a later pattern from re-redacting an earlier one's work.
_SECRET_VALUE = r"""(?:"[^"]*(?:"\S*|\Z)|'[^']*(?:'\S*|\Z)|\S+)"""
_SECRET_KV_PATTERN = re.compile(
    r'(?i)(pass(?:word|wd|phrase)?|pwd|secret(?:[_-]?(?:access[_-]?)?key)?|token|api[_-]?key|accountkey'
    r'|sharedaccesskey)'
    r'["\']?\s*[:=]\s*(?!<redacted>)' + _SECRET_VALUE)
# A space-separated command-line flag ('-Password x', '--db-password x', '-pw x'): no = or : to
# key on, so the flag itself is the anchor. The lookbehind keeps it off words that merely end in
# the key. Only password/passwd take a prefix (-AdminPassword, -SqlPassword); a prefix on pw would
# catch unrelated flags.
_SECRET_FLAG_PATTERN = re.compile(
    r'(?i)(?<![\w-])(--?(?:[\w-]*(?:password|passwd)|pass|pwd|pw|secret|token|api[_-]?key))\s+'
    r'(?!<redacted>)' + _SECRET_VALUE)
# sqlcmd/bcp/osql take the password as an upper-case -P and a space. Case-sensitive, so ssh's
# lower-case -p port is left alone, and an all-digit value is skipped: scp, pscp and mysql take a
# port as -P 22. ponytail: an all-digit password after -P is missed; add a sqlcmd context if one appears.
_UPPER_P_PATTERN = re.compile(r'(?<![\w-])(-P)\s+(?!\d+(?:\s|\Z))(?!<redacted>)' + _SECRET_VALUE)
# Flags that are a password only after one command: (command, flag pattern, replacement). Each
# flag pattern runs on the rest of the line after the command's first match (_sub_after_command).
# A lazy '[^\n]*?' bridge inside one regex rescanned the line from every 'mysql' (quadratic time).
_CONTEXT_FLAG_PATTERNS = [
    # mysql's -p takes its password attached (-pSecret). An attached -p elsewhere on the line
    # (-path, -profile) is an ordinary option of another program, so those words are skipped.
    (re.compile(r'(?i)\bmysql\w*'),
     re.compile(r'(?<![\w-])(-p)(?=[^\s-])(?!(?:ath|rofile|ort|ipe|rotocol)\b)(?!<redacted>)'
                + _SECRET_VALUE), r"\1<redacted>"),
    # psexec/paexec -p <password>: lower-case, space-separated. Elsewhere -p 8080 is a port.
    (re.compile(r'(?i)\b(?:psexec|paexec)\w*'),
     re.compile(r'(?<![\w-])(-p)\s+(?!<redacted>)' + _SECRET_VALUE), r"\1 <redacted>"),
    # schtasks /RP <password> and /P <password>. A value starting with / is the next switch
    # (schtasks then prompts for the password).
    (re.compile(r'(?i)\bschtasks\w*'),
     re.compile(r'(?i)(?<![\w/])(/R?P)\s+(?![/<])' + _SECRET_VALUE), r"\1 <redacted>"),
    # sqlcmd/bcp/osql -PSecret, attached. _UPPER_P_PATTERN takes the space form.
    (re.compile(r'(?i)\b(?:sqlcmd|bcp|osql)\w*'),
     re.compile(r'(?<![\w-])(-P)(?=\S)(?!<redacted>)' + _SECRET_VALUE), r"\1<redacted>"),
    # 7-Zip -pSecret, attached.
    (re.compile(r'(?i)\b7z[ar]?(?:\.exe)?\b'),
     re.compile(r'(?<![\w-])(-p)(?=\S)(?!<redacted>)' + _SECRET_VALUE), r"\1<redacted>"),
    # curl -uuser:password, attached: outside curl an attached -u (-update:C:\x) is another option.
    (re.compile(r'(?i)\bcurl\w*'),
     re.compile(r'''(?<![\w-])(-u)(["']?[^\s:"']+):(?!//)(?!<redacted>)\S+'''), r"\1\2:<redacted>"),
    # net use ... /user:DOMAIN\name <password>: the password can also follow the /user switch.
    (re.compile(r'(?i)\bnet(?:\.exe)?\s+use\b'),
     re.compile(r'(?i)(/u(?:ser)?:\S+\s+)(?![/<])' + _SECRET_VALUE), r"\1<redacted>"),
]
# curl -u user:password and --user=user:password. The space-form user part needs 2+ characters, so
# python -u C:\x.py (a drive letter and a path) is not read as user C, and //host after the colon
# is a URL, not a password.
_USER_COLON_PATTERN = re.compile(
    r'(?i)(?<![\w-])(-u|--user|--proxy-user)(\s+["\']?[^\s:"\']{2,}|=["\']?[^\s:"\']+):(?!//)'
    r'(?!<redacted>)(\S+)')
# net use Z: \\server\share <password> /user:DOMAIN\name -- positional, before any /switch.
# The share may be quoted. The /user: name is not a secret and stays.
_NET_USE_PATTERN = re.compile(
    r'(?i)(\bnet(?:\.exe)?\s+use\s+(?:(?:[a-z]:|\*)\s+)?(?:"\\\\[^"]*"|\\\\\S+)\s+)(?![/<])'
    + _SECRET_VALUE)
# powershell -EncodedCommand <base64>: the whole script, credentials included, decodes from it.
# Exact flag forms only: a looser -enc\w* would also take -Encoding utf8.
_ENCODED_COMMAND_PATTERN = re.compile(r'(?i)(?<![\w-])(-(?:EncodedCommand|enc|ec))\s+(?!<redacted>)\S+')
# PowerShell $pw = 'x': a quoted literal assigned to a variable whose name holds a key word. The
# name is checked in _powershell_secret, not in the regex: \w*(?:pw|...)\w* backtracks
# quadratically on a long name.
_POWERSHELL_ASSIGN_PATTERN = re.compile(r'''(\$(\w+)\s*=\s*)(?!<redacted>)("[^"]*(?:"|\Z)|'[^']*(?:'|\Z))''')
_POWERSHELL_SECRET_NAME = re.compile(r'(?i)pw|pass|secret|token|key')
# /p:secret, /password:secret. A bare /p whose value holds = is an msbuild property
# (/p:Configuration=Release), left to the key=value pattern.
_SLASH_PASSWORD_PATTERN = re.compile(
    r'(?i)(?<![\w/])(/(?:password|passwd|pwd|pw|pass|p(?![:=][^\s=]*=)))[:=](?!<redacted>)' + _SECRET_VALUE)
# An Azure SAS URL signs with &sig=; the rest of the query string is not secret.
_SAS_SIGNATURE_PATTERN = re.compile(r'(?i)([?&]sig=)[^&\s"\']+')
_SECURE_STRING_PATTERN = re.compile(
    r'(?i)(\bConvertTo-SecureString(?:\s+-(?:AsPlainText|Force|String))*\s+)(?!-)(?!<redacted>)'
    + _SECRET_VALUE)
_BASIC_AUTH_PATTERN = re.compile(r"(?i)\bauthorization\s*[:=]\s*basic\s+\S+")
# 'Authorization: Bearer x' and bare 'Bearer x' -- not a key=value shape, so a separate pattern.
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+\S+")
# Any scheme, not just http(s) -- this repo's own FTP scripts carry plaintext ftp://user:pass@ URLs.
# The lookbehind starts a scheme only at its first character; without it a long word was rescanned
# from every letter (quadratic time).
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def _sub_after_command(command: Any, flag: Any, repl: str, text: str) -> str:
    """flag.sub on the part of each line after the first match of command. Linear in the text."""
    if not command.search(text):
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        match = command.search(line)
        if match:
            lines[i] = line[:match.end()] + flag.sub(repl, line[match.end():])
    return "\n".join(lines)


def _powershell_secret(match: Any) -> str:
    if _POWERSHELL_SECRET_NAME.search(match.group(2)):
        return f"{match.group(1)}<redacted>"
    return match.group(0)


def _scrub_secrets(text: str) -> str:
    """Redact secrets before the text reaches a published row or an e-mail: key=value and
    key: value pairs, password flags (sqlcmd, bcp, plink, psexec, schtasks, mysql, 7z, curl,
    net use, /p:), -EncodedCommand values, PowerShell secret variables, Authorization headers,
    SAS signatures, ConvertTo-SecureString literals and scheme://user:pass@ URLs.
    The flag or key stays; only the value becomes <redacted>."""
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1<redacted>@", text)
    text = _BASIC_AUTH_PATTERN.sub("Authorization: Basic <redacted>", text)
    text = _BEARER_TOKEN_PATTERN.sub("Authorization: Bearer <redacted>", text)
    text = _SAS_SIGNATURE_PATTERN.sub(r"\1<redacted>", text)
    text = _SECURE_STRING_PATTERN.sub(r"\1<redacted>", text)
    text = _NET_USE_PATTERN.sub(r"\1<redacted>", text)
    text = _USER_COLON_PATTERN.sub(r"\1\2:<redacted>", text)
    for command, flag, repl in _CONTEXT_FLAG_PATTERNS:
        text = _sub_after_command(command, flag, repl, text)
    text = _ENCODED_COMMAND_PATTERN.sub(r"\1 <redacted>", text)
    text = _POWERSHELL_ASSIGN_PATTERN.sub(_powershell_secret, text)
    text = _UPPER_P_PATTERN.sub(r"\1 <redacted>", text)
    text = _SLASH_PASSWORD_PATTERN.sub(r"\1:<redacted>", text)
    text = _SECRET_FLAG_PATTERN.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _SECRET_KV_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return text


def tail_log_for_email(latest_log_path: str, max_lines: int = 15, max_bytes: int = 2048) -> str:
    """Last ~15 lines (~2KB max) of a failing task's latest log, scrubbed of secrets.

    A read failure (missing file, permission, non-text) never breaks the alert -- returns "".
    # ponytail: reads the last max_bytes*8 raw bytes before line-splitting rather than streaming
    # backward line-by-line; generous enough for real task-server log lines, revisit if a log
    # ever has lines longer than ~2KB each.
    """
    path = normalize_text(latest_log_path)
    if not path:
        return ""
    try:
        p = Path(path)
        size = p.stat().st_size
        read_size = min(size, max_bytes * 8)
        with p.open("rb") as fh:
            fh.seek(size - read_size)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        if read_size < size:
            # The read began mid-line; that fragment can start inside a secret, past its flag.
            text = text.partition("\n")[2]
        # Scrubbed before the cut: a cut can drop a flag and leave its value behind.
        return _scrub_secrets("\n".join(text.splitlines()[-max_lines:]))[-max_bytes:]
    except OSError:
        return ""


def ping_healthcheck(config: Dict[str, Any], suffix: str) -> None:
    """Optional Healthchecks.io-style dead man's switch ping (alerting.healthcheck_url).

    Off unless configured. A failed ping -- any exception, not only the URLError/OSError/
    ValueError a normal network failure raises (http.client.HTTPException subclasses such as
    InvalidURL/BadStatusLine are none of those) -- is logged and never fails the run: monitoring
    the monitor must not become a new way for it to break. `with` closes the response either way.
    """
    url = normalize_text(config.get("alerting", {}).get("healthcheck_url"))
    if not url:
        return
    import urllib.request

    target = url.rstrip("/") + suffix
    try:
        with urllib.request.urlopen(target, timeout=10):
            pass
    except Exception as exc:
        emit(f"WARNING: healthcheck ping to {target} failed: {exc}")


def should_alert(previous: Dict[str, Any], current: List[str], now: datetime,
                 heartbeat_hour: Optional[int], realert_hours: Optional[float] = None) -> Tuple[bool, str]:
    """Decide whether to send, and why. Pure -- no I/O, so it is testable.

    Sends when the problem set CHANGES (new problem, or recovery), never on every run: an
    unchanged failure mailed hourly trains the reader to ignore it. The daily heartbeat exists
    so that silence is meaningful -- without it, "no e-mail" and "monitor is dead" look alike.
    realert_hours adds a per-problem cooldown on top of that: an unchanged problem set is
    resent once any still-open problem's last alert (previous["problem_last_alert_utc"]) is
    older than the cooldown, or was never recorded, so a long-lived failure is still repeated
    periodically instead of going silent for good when heartbeat_hour isn't configured.
    """
    prior = previous.get("signature") or []
    if current != prior:
        if current and not prior:
            return True, "problem"
        if not current and prior:
            return True, "recovered"
        return True, "changed"

    if current and realert_hours is not None:
        last_alert = previous.get("problem_last_alert_utc") or {}
        for key in current:
            ts = parse_iso_datetime(last_alert.get(key))
            if ts is None or (now - ts).total_seconds() >= realert_hours * 3600:
                return True, "realert"

    if heartbeat_hour is None:
        return False, ""
    last = parse_iso_datetime(previous.get("last_heartbeat_utc"))
    local = now.astimezone()
    if local.hour == int(heartbeat_hour) and (last is None or (now - last).total_seconds() > 12 * 3600):
        return True, "heartbeat"
    return False, ""


# Shared-cause classification for an incident (several tasks failing at once). Checked in order,
# most specific first; the first group whose pattern matches a task's result text, attention
# reason or the ERROR-LOOKING lines of its log tail wins. Only error-looking lines count: nearly
# every arcpy script here logs its geodatabase connection-file path on INFO lines, which would otherwise make
# every failure look like a database problem. A database storage outage once failed many tasks
# over two days with "Could not open SDE workspace", "Parameters are not valid", "Input layer does not
# exist: <connection file>" and similar: 13 separate e-mails, none naming the one cause. ERROR 000732 /
# "Parameters are not valid" mean arcpy could not open an input, so they are data-source evidence.
# The groups and their (what, todo) hint text are rules data (cause_patterns, cause_text): the
# generic groups in rules/default.json, the reference install's data-source wording in its
# profile. Both names are the metrics module's own objects, so they hold the configured rules.
_CAUSE_PATTERNS = metrics.CAUSE_PATTERNS
_EVIDENCE_LINE = re.compile(r"(?i)error|fail|exception|traceback|not found|does not exist|denied|"
                            r"unable|cannot|could not|not valid|not licensed")
_CAUSE_TEXT = metrics.CAUSE_TEXT


def _failure_cause(task: Dict[str, Any]) -> str:
    """The _CAUSE_PATTERNS group a failed task's own evidence points at, or ''."""
    tail = tail_log_for_email(task.get("latest_log_path"))
    evidence = [ln for ln in tail.splitlines() if _EVIDENCE_LINE.search(ln)]
    text = "\n".join([normalize_text(task.get("last_result_text")),
                       normalize_text(task.get("attention_reason")), *evidence])
    return next((name for name, pattern in _CAUSE_PATTERNS if pattern.search(text)), "")


def failure_causes(inventory: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """{task_name: cause} for every failed task, read once per alert decision: each cause costs a
    log-tail read, and a log on an unreachable share can block for an SMB timeout."""
    return {t["task_name"]: _failure_cause(t) for t in inventory if t.get("last_run_failed")}


def problem_first_seen(previous: Dict[str, Any], current: Sequence[str],
                       now: dt.datetime) -> Dict[str, str]:
    """When each open problem was first seen: kept from the alert state, else now. A task's
    last_run_time is no substitute: a task that fails again moves it to the newest failure."""
    seen = previous.get("problem_first_seen_utc") or {}
    return {k: seen.get(k) or now.isoformat() for k in current}


def shared_cause(inventory: Sequence[Dict[str, Any]], current: Sequence[str],
                 min_tasks: int, first_seen: Optional[Dict[str, str]] = None,
                 causes: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """{count, since, cause, counts} when at least min_tasks alerted problems are FAILED runs,
    else None. cause is the group that a strict majority of the failing tasks point at, or ''.
    since is the earliest first_seen among them: when the incident started."""
    if min_tasks <= 0:
        return None
    keys = [k for k in current if k.startswith("FAILED: ")]
    names = {k[len("FAILED: "):] for k in keys}
    # last_run_failed too: a healthy task can share a failing task's name under another path.
    failing = [t for t in inventory if t.get("task_name") in names and t.get("last_run_failed")]
    if len(failing) < min_tasks:
        return None
    causes = causes if causes is not None else failure_causes(failing)
    counts: Dict[str, int] = {}
    for task in failing:
        cause = causes.get(task["task_name"], "")
        counts[cause] = counts.get(cause, 0) + 1
    top = max((c for c in counts if c), key=lambda c: counts[c], default="")
    cause = top if top and counts[top] * 2 > len(failing) else ""
    starts = [s for s in (parse_iso_datetime((first_seen or {}).get(k)) for k in keys) if s]
    return {"count": len(failing), "since": min(starts) if starts else None,
            "cause": cause, "counts": counts}


def _coalesce_into_digest(reason: str, prior: Sequence[str], current: Sequence[str],
                          incident: Optional[Dict[str, Any]], previous: Dict[str, Any],
                          now: dt.datetime, digest_hours: Optional[float],
                          causes: Dict[str, str]) -> Optional[dt.datetime]:
    """During an open incident, a "changed" alert whose only change is new FAILED tasks with the
    cause the last e-mail ANNOUNCED, or no recognisable cause ("more of the same"), waits for the
    next digest: returns when that is due, or None to send now. Compared with the announced cause,
    not this run's majority: a batch of new failures of another kind could flip the majority and
    hide itself. A recovery (including of a held failure), an overdue task, a different cause,
    the first incident e-mail and a re-alert always send at once."""
    if reason != "changed" or not incident or not digest_hours or digest_hours <= 0:
        return None
    if not set(prior) <= set(current):
        return None
    last = parse_iso_datetime(previous.get("incident_alert_utc"))
    if last is None or "incident_cause" not in previous:
        return None
    # Every cause the last e-mail already showed in its evidence line, plus "no clear cause". With
    # only the winning cause, an "unclear" incident would mail each new database failure hourly.
    announced = set(previous.get("incident_causes") or [previous.get("incident_cause") or ""]) | {""}
    for key in (k for k in current if k not in prior):
        if not key.startswith("FAILED: ") or causes.get(key[len("FAILED: "):], "") not in announced:
            return None
    due = last + dt.timedelta(hours=digest_hours)
    return due if now < due else None


def _read_alert_state(state_path: Path) -> Dict[str, Any]:
    try:
        return json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        return {}


def compose_alert(collected: Dict[str, Any], config: Dict[str, Any], previous: Dict[str, Any],
                  now: dt.datetime) -> Dict[str, Any]:
    """Decide whether to send and build the message: {send, reason, current, suppressed,
    subject, body}. No I/O beyond reading failing tasks' log tails, so maybe_send_alert and
    --preview-alert share it and the preview is exactly the e-mail the hourly run would send.
    subject/body are built even when send is False, so a preview can still show them."""
    alerting = config.get("alerting", {})
    windows = _active_maintenance_windows(alerting.get("maintenance_windows") or [], now)
    current, suppressed = partition_maintenance(collected["inventory"], windows)
    heartbeat_hour = alerting.get("heartbeat_hour")
    send, reason = should_alert(previous, current, now,
                                None if heartbeat_hour is None else int(heartbeat_hour),
                                safe_float(alerting.get("realert_hours", 24)))
    prior = previous.get("signature") or []
    first_seen = problem_first_seen(previous, current, now)
    min_tasks = int(safe_float(alerting.get("shared_cause_min_tasks", 3)) or 0)
    # Causes cost a log-tail read each: only for open failures, and only with the feature on.
    open_failed = {k[len("FAILED: "):] for k in current if k.startswith("FAILED: ")}
    causes = failure_causes([t for t in collected["inventory"] if t.get("task_name") in open_failed]
                            if min_tasks > 0 else [])
    digest_hours = safe_float(alerting.get("incident_digest_hours", 6))
    incident = shared_cause(collected["inventory"], current, min_tasks, first_seen, causes)
    if incident:
        incident["digest"] = bool(digest_hours and digest_hours > 0)
    # Held failures that recovered before their digest was sent: say so, or they are never mailed.
    # A held task now in maintenance or overdue is still open, not recovered.
    open_names = {k.split(": ", 1)[-1] for k in list(current) + list(suppressed)}
    held = previous.get("digest_pending") or []
    held_recovered = [k for k in held if k.split(": ", 1)[-1] not in open_names]
    digest_due = (None if held_recovered else
                  _coalesce_into_digest(reason, prior, current, incident, previous, now, digest_hours, causes))
    pending = sorted({k for k in held if k in current} | {k for k in current if k not in prior})
    if send and digest_due is not None:
        send, reason = False, "digest"
    # The open set then equals the last e-mail's, so should_alert sees no change: force the send.
    if held_recovered and not send:
        send, reason = True, "changed"
    subject, body = _alert_message(collected, config, now, reason, current, suppressed,
                                   prior, incident, held_recovered)
    return {"send": send, "reason": reason, "current": current, "suppressed": suppressed,
            "subject": subject, "body": body, "incident": incident, "digest_due": digest_due,
            "first_seen": first_seen, "digest_pending": pending if reason == "digest" else []}


def maybe_send_alert(collected: Dict[str, Any], config: Dict[str, Any], log_dir: Path) -> bool:
    """Returns True only when an e-mail was actually sent -- run_publish's CollectorRuns row
    (3.6) reports this as alert_sent.
    """
    from taskdash_common import load_mail_settings, send_email

    alerting = config.get("alerting", {})
    if not alerting.get("enabled"):
        return False
    recipients = [r for r in (alerting.get("to") or []) if normalize_text(r)]
    if not recipients:
        emit("WARNING: alerting.enabled is true but alerting.to is empty; no alert sent.")
        return False

    state_path = log_dir / "alert_state.json"
    previous = _read_alert_state(state_path)
    now = utc_now()
    alert = compose_alert(collected, config, previous, now)
    current, suppressed = alert["current"], alert["suppressed"]
    if suppressed:
        emit(f"In maintenance (not alerted): {len(suppressed)} problem(s) -- "
            + "; ".join(suppressed))
    if not alert["send"]:
        if alert["reason"] == "digest":
            emit(f"No alert yet: new problem(s) during an open incident ({len(current)} open) wait "
                 f"for the digest due {alert['digest_due']:%Y-%m-%d %H:%M} UTC.")
            # Remember what is held, so a held failure that recovers first is still reported.
            try:
                atomic_write_json(state_path, {**previous, "digest_pending": alert["digest_pending"],
                                               "problem_first_seen_utc": alert["first_seen"]})
            except OSError as exc:
                emit(f"WARNING: could not persist the held digest ({exc}).")
        else:
            emit(f"No alert: problem set unchanged ({len(current)} open, {len(suppressed)} in maintenance).")
        return False

    mail = load_mail_settings(config)
    sent = bool(mail and send_email(mail, recipients, alert["subject"], alert["body"]))
    if sent:
        state = {"signature": current, "updated_utc": now.isoformat(),
                 "last_heartbeat_utc": now.isoformat() if alert["reason"] == "heartbeat"
                 else previous.get("last_heartbeat_utc"),
                 "problem_last_alert_utc": {k: now.isoformat() for k in current},
                 "problem_first_seen_utc": alert["first_seen"],
                 # Set while an incident is open, so the next additions wait for the digest, and
                 # compared with the cause this e-mail announced.
                 "incident_alert_utc": now.isoformat() if alert["incident"] else None,
                 "incident_cause": alert["incident"]["cause"] if alert["incident"] else None,
                 "incident_causes": sorted(alert["incident"]["counts"]) if alert["incident"] else [],
                 "digest_pending": []}
        try:
            atomic_write_json(state_path, state)
        except OSError as exc:
            emit(f"WARNING: could not persist alert state ({exc}); may re-alert next run.")
    return sent


def _other_attention(inventory: Sequence[Dict[str, Any]]) -> List[str]:
    """Open STUCK, BLOCKED and WARNING tasks: the dashboard counts them, the alert does not."""
    return sorted(f"{t.get('attention_kind') or 'WARNING'}: {t.get('task_name')}"
                  for t in inventory if t.get("needs_attention") and not _problem_key(t))


def _alert_subject(host: str, reason: str, current: List[str], suppressed: List[str],
                   others: List[str], task_count: int,
                   incident: Optional[Dict[str, Any]] = None,
                   now: Optional[dt.datetime] = None) -> str:
    if incident:
        since = ""
        if incident["since"]:
            local = incident["since"].astimezone()
            same_day = local.date() == (now or local).astimezone().date()
            since = f" since {local:%H:%M}" if same_day else f" since {local:%b %d %H:%M}"
        cause = (f"likely shared cause: {_CAUSE_TEXT[incident['cause']][0]}" if incident["cause"]
                 else "check for a shared cause")
        return f"[TaskDash] {incident['count']} tasks failing on {host}{since} - {cause}"
    if current:
        if reason == "changed":
            return f"[TaskDash] problem list changed on {host} ({len(current)} open)"
        if reason == "realert":
            return f"[TaskDash] {len(current)} problem(s) still open on {host}"
        return f"[TaskDash] {len(current)} problem(s) on {host}"
    if suppressed:
        # Not actually "all clear" -- these problems are still open, just in a maintenance
        # window. The body already lists them under "In maintenance"; the subject must not
        # claim otherwise.
        return f"[TaskDash] {len(suppressed)} problem(s) in maintenance on {host}"
    if others:
        # Never "healthy" or "all clear" while the dashboard still shows open warnings.
        lead = "failed and overdue tasks cleared" if reason == "recovered" else "daily check"
        return f"[TaskDash] {lead} on {host}: {len(others)} task(s) need attention"
    return (f"[TaskDash] all clear on {host}" if reason == "recovered"
            else f"[TaskDash] daily check: all {task_count} task{'' if task_count == 1 else 's'} healthy on {host}")


def _change_lines(prior: List[str], current: List[str], suppressed: List[str],
                  details: Dict[str, str]) -> List[str]:
    """New, still-open and cleared problems, so a changed list says what changed."""
    sections = (("New", [k for k in current if k not in prior]),
                ("Still open", [k for k in current if k in prior]),
                # A problem moved into a maintenance window is still open, not cleared.
                ("Cleared", [k for k in prior if k not in current and k not in suppressed]))
    lines: List[str] = []
    for title, keys in sections:
        if keys:
            lines += ["", f"{title}:", *(f"  {details.get(k, k)}" for k in keys)]
    return lines


def _alert_message(collected: Dict[str, Any], config: Dict[str, Any], now: dt.datetime,
                   reason: str, current: List[str], suppressed: List[str],
                   prior: Sequence[str] = (),
                   incident: Optional[Dict[str, Any]] = None,
                   held_recovered: Sequence[str] = ()) -> Tuple[str, str]:
    inventory = collected["inventory"]
    host = collected["host_name"]
    others = _other_attention(inventory)
    subject = _alert_subject(host, reason, current, suppressed, others, len(inventory), incident, now)

    details = alert_details(inventory)
    # Local time, like the dashboard: a UTC time here did not match what the reader saw there.
    lines = [f"Checked {len(inventory)} scheduled task(s) at {now.astimezone():%Y-%m-%d %H:%M %Z}."]
    if incident:
        what, todo = _CAUSE_TEXT[incident["cause"]]
        since = (f" since {incident['since'].astimezone():%Y-%m-%d %H:%M %Z}"
                 if incident["since"] else "")
        parts = []
        for i, (c, n) in enumerate(sorted(incident["counts"].items(), key=lambda x: (-x[1], x[0] == ""))):
            phrase = (f"at {_CAUSE_TEXT[c][0]}" if c else "with no clear cause")
            if i == 0:
                phrase = (f"task{'' if n == 1 else 's'} " + (f"point{'s' if n == 1 else ''} " if c else "")
                          + phrase)
            parts.append(f"{n} {phrase}")
        evidence = ", ".join(parts)
        lines += ["", f"Likely shared cause: {incident['count']} tasks have failed{since}.",
                  (f"Most of their logs point at {what}. {todo}" if incident["cause"]
                   else f"Their logs show {what}. {todo}"),
                  f"Log evidence: {evidence}."]
        if incident.get("digest"):
            lines.append("While the incident is open, new failures with the same cause are batched "
                         "into the next e-mail. A recovery or a different kind of problem is sent at once.")
    if held_recovered:
        lines += ["", "Failed and recovered before this e-mail:", *(f"  {k}" for k in held_recovered)]
    if prior:
        lines += _change_lines(list(prior), current, suppressed, details)
    elif current:
        lines += ["", *(details.get(k, k) for k in current)]
    if not current:
        lines += ["", "No failed or overdue tasks."]
    if others:
        lines += ["", f"{len(others)} other task(s) need attention:", *(f"  {o}" for o in others)]

    task_by_name = {t.get("task_name"): t for t in inventory}
    for key in current:
        if not key.startswith("FAILED: "):
            continue
        task = task_by_name.get(key[len("FAILED: "):])
        tail = tail_log_for_email(task.get("latest_log_path")) if task else ""
        if tail:
            lines += ["", f"  --- tail of {task.get('latest_log_name') or 'log'} ---"]
            lines += [f"    {ln}" for ln in tail.splitlines()]

    if suppressed:
        lines += ["", "In maintenance (not alerted):"]
        lines += [details.get(k, k) for k in suppressed]

    lines += ["", f"Dashboard: {alert_dashboard_link(config)}", ""]
    return subject, "\n".join(lines)


def alert_dashboard_link(config: Dict[str, Any]) -> str:
    """alerting.dashboard_url wins; otherwise the portal URL the collector signs in to."""
    explicit = normalize_text(config.get("alerting", {}).get("dashboard_url"))
    if explicit:
        return explicit
    agol = config.get("arcgis", {})
    base = (normalize_text(agol.get("url")) or "https://www.arcgis.com").rstrip("/")
    return f"{base}/apps/dashboards/{normalize_text(agol.get('dashboard_id', ''))}"


def _alert_timing_lines(previous: Dict[str, Any], current: List[str], now: dt.datetime,
                        alerting: Dict[str, Any]) -> List[str]:
    """Why the preview's decision came out as it did: cooldown per open problem, heartbeat."""
    realert_hours = safe_float(alerting.get("realert_hours", 24))
    last_alert = previous.get("problem_last_alert_utc") or {}
    lines = []
    for key in current:
        ts = parse_iso_datetime(last_alert.get(key))
        if ts is None:
            lines.append(f"  {key}: never alerted")
        elif realert_hours is None:
            lines.append(f"  {key}: last alerted {ts:%Y-%m-%d %H:%M} UTC (re-alert off)")
        else:
            due = ts + dt.timedelta(hours=realert_hours)
            lines.append(f"  {key}: last alerted {ts:%Y-%m-%d %H:%M} UTC, re-alert due "
                         f"{due:%Y-%m-%d %H:%M} UTC")
    hour = alerting.get("heartbeat_hour")
    last = parse_iso_datetime(previous.get("last_heartbeat_utc"))
    lines.append("Heartbeat: off (alerting.heartbeat_hour not set)" if hour is None else
                 f"Heartbeat: at local hour {int(hour)}; last sent "
                 f"{f'{last:%Y-%m-%d %H:%M} UTC' if last else 'never'}")
    return lines


def preview_alert(collected: Dict[str, Any], config: Dict[str, Any], log_dir: Path,
                  now: Optional[dt.datetime] = None) -> Path:
    """--preview-alert: write Logs/alert_preview_<ts>.txt with the decision and the exact e-mail
    the hourly run would send now, and print it. Reads alert_state.json but never writes it,
    never sends, never reads the SMTP settings and never touches ArcGIS Online."""
    alerting = config.get("alerting", {})
    now = now or utc_now()
    state_path = log_dir / "alert_state.json"
    previous = _read_alert_state(state_path)
    alert = compose_alert(collected, config, previous, now)
    recipients = [r for r in (alerting.get("to") or []) if normalize_text(r)]
    blocked = ("alerting.enabled is false" if not alerting.get("enabled")
               else "alerting.to is empty" if not recipients else "")
    if alert["reason"] == "digest":
        decision = (f"would NOT send yet: new problem(s) during an open incident wait for the digest "
                    f"due {alert['digest_due']:%Y-%m-%d %H:%M} UTC (alerting.incident_digest_hours)")
    elif not alert["send"]:
        decision = "would NOT send: no new, changed, recovered or re-alert-due problem, no heartbeat due"
    elif blocked:
        decision = f"would NOT send: {blocked} (otherwise: send, reason '{alert['reason']}')"
    else:
        decision = f"WOULD SEND to {', '.join(recipients)} (reason '{alert['reason']}')"
    lines = [f"TaskDash alert preview, {now:%Y-%m-%d %H:%M} UTC. Nothing was sent; "
             f"alert_state.json was not changed. SMTP settings were not checked.",
             f"Alert state: {state_path}" + ("" if state_path.exists() else " (none: never alerted)"),
             f"Decision: {decision}",
             f"Open problems: {len(alert['current'])}; in maintenance (not alerted): "
             f"{'; '.join(alert['suppressed']) or 'none'}",
             *_alert_timing_lines(previous, alert["current"], now, alerting),
             "", f"Subject: {alert['subject']}", "", alert["body"]]
    text = "\n".join(lines)
    path = log_dir / f"alert_preview_{now:%Y%m%d_%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    emit(text)
    emit(f"Preview written to {path}")
    return path


if __name__ == "__main__":
    sys.exit(main())

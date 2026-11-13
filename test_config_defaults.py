"""TD-118: every config default the code reads, pinned by value.

The numbers and switches below are what an absent key means. They are the values that
docs/CONFIGURE.md lists in its Default column. Moving them into one map must not change any.
Nothing here signs in or reaches a network.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import taskdash_collect as collect
import taskdash_common as common
import taskdash_dashboard as dash
import taskdash_metrics as metrics
import taskdash_preflight as pf

# Leaf key -> the value the code uses when the key is absent (numbers and switches only).
READ_DEFAULTS = {
    "powershell_timeout_seconds": 180,
    "max_events_per_run": 5000, "days_back": 30, "first_run_max_events": 30000,
    "max_log_files_per_task": 40, "max_lines_per_file": 5000,
    "runs_days": 90, "metrics_days": 90, "changes_days": 365, "max_log_age_days": 30,
    "realert_hours": 24, "shared_cause_min_tasks": 3, "incident_digest_hours": 6,
    "collector_interval_minutes": 60, "exclude_microsoft_tasks": True,
    "include_log_tail": True, "chunk_size": 200,
}

# What `preflight --config-diff` prints for each key that is absent.
DEFAULT_WORDING = {
    "host_name_override": "empty", "host_previous_names": "empty", "powershell_timeout_seconds": "180",
    "arcgis.url": "https://www.arcgis.com", "arcgis.profile": "empty", "arcgis.username": "empty",
    "arcgis.username_env": "empty", "arcgis.item_id": "empty", "arcgis.test_item_id": "empty",
    "arcgis.view_item_id": "empty", "arcgis.test_view_item_id": "empty", "arcgis.dashboard_id": "empty",
    "arcgis.service_description": "TaskDash - scheduled task monitoring",
    "arcgis.service_tags": "taskdash,monitoring,scheduled tasks",
    "arcgis.dashboard_tags": "taskdash,monitoring",
    "alerting.enabled": "false", "alerting.to": "empty", "alerting.mail_config_path": "empty",
    "alerting.healthcheck_url": "empty", "alerting.include_log_tail": "true",
    "alerting.dashboard_url": "empty", "alerting.heartbeat_hour": "none", "alerting.realert_hours": "24",
    "alerting.shared_cause_min_tasks": "3", "alerting.incident_digest_hours": "6",
    "alerting.maintenance_windows": "empty list",
    "scheduler.task_name_regex": "empty", "scheduler.task_path_regex": "empty",
    "scheduler.action_regex": "empty", "scheduler.exclude_task_name_regex": "empty",
    "scheduler.exclude_microsoft_tasks": "true", "scheduler.include_disabled": "true",
    "scheduler.exclude_command_target_types": "empty list",
    "scheduler.interactive_required_pattern": "\\ArcGIS\\Pro\\bin\\Python",
    "scheduler.collector_task_name": "TaskDash Collector", "scheduler.collector_interval_minutes": "60",
    "scheduler.overdue_grace_minutes": "15", "scheduler.heartbeat_ignore_after_days": "7",
    "history.max_events_per_run": "5000", "history.days_back": "30", "history.first_run_max_events": "30000",
    "metrics.days_back": "30", "metrics.max_log_files_per_task": "40", "metrics.max_lines_per_file": "5000",
    "metrics.log_directory_overrides": "empty object", "metrics.log_roots": "empty list",
    "metrics.tasks": "empty object", "upload.chunk_size": "200",
    "retention.runs_days": "90", "retention.metrics_days": "90", "retention.changes_days": "365",
    "logging.log_dir": "empty", "logging.max_log_age_days": "30",
    "smtp.host": "empty", "smtp.port": "587", "smtp.from": "empty", "smtp.user": "empty",
    "smtp.starttls": "true", "smtp.ssl": "true when smtp.port is 465, else false",
    "smtp.allow_plaintext_auth": "false",
    "privacy.hide_command_lines": "true", "privacy.hide_sample_lines": "true",
    "privacy.hide_accounts": "true", "privacy.hide_paths": "true",
    "rules.profiles": "none (required)",
}


def test_wording_of_every_default_is_unchanged():
    assert {key: pf.default_wording(value) for key, value in common.CONFIG_DEFAULTS.items()} == DEFAULT_WORDING


def test_named_constants_keep_their_values():
    assert dash.DEFAULT_COLLECTOR_INTERVAL_MINUTES == 60
    assert dash.DEFAULT_HEARTBEAT_IGNORE_DAYS == 7
    assert dash.DEFAULT_RETENTION_DAYS == 90
    assert dash.DEFAULT_GRACE_MINUTES == 15
    assert dash.DEFAULT_COLLECTOR_TASK_NAME == "TaskDash Collector"
    assert dash.DEFAULT_PORTAL_URL == "https://www.arcgis.com"
    assert collect.DEFAULT_OVERDUE_GRACE_MINUTES == 15.0
    assert collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN == "\\ArcGIS\\Pro\\bin\\Python"
    assert metrics.DEFAULT_MAX_LOG_FILES_PER_TASK == 40
    assert metrics.DEFAULT_MAX_LINES_PER_FILE == 5000


def test_help_terms_with_an_empty_config_uses_the_defaults():
    terms = dash.help_terms({})
    assert (terms["ignore_days"], terms["days"], terms["metric_days"]) == (7, 90, 90)
    assert (terms["grace"], terms["collector"], terms["realert"]) == (15, "TaskDash Collector", 24.0)
    assert (terms["late"], terms["down"]) == (90, 180)


def test_accessor_returns_each_pinned_value():
    for dotted_key, value in common.CONFIG_DEFAULTS.items():
        if dotted_key in ("smtp.ssl", "rules.profiles"):
            with pytest.raises(KeyError):
                common.config_default(dotted_key)
            continue
        got = common.config_default(dotted_key)
        assert got == value and type(got) is type(value), dotted_key
        if isinstance(value, (list, dict)):
            assert got is not value, dotted_key
    for dotted_key, value in common.CONFIG_DEFAULTS.items():
        leaf = dotted_key.split(".")[-1]
        if leaf in READ_DEFAULTS:
            assert value == READ_DEFAULTS[leaf] and type(value) is type(READ_DEFAULTS[leaf]), dotted_key


def test_no_module_repeats_a_literal_default():
    # A `.get("key", 180)` in these modules would drift from the map; read config_default() instead.
    pattern = re.compile(r'\.get\(\s*"(\w+)"\s*,\s*(\d+|True|False)\s*\)')
    for name in ("taskdash_publish.py", "taskdash_dashboard.py", "taskdash_preflight.py"):
        text = (Path(__file__).parent / name).read_text(encoding="utf-8")
        assert [m for m in pattern.findall(text) if m[0] in READ_DEFAULTS] == [], name


def test_default_wording_covers_each_kind_of_value():
    assert [pf.default_wording(v) for v in (None, True, False, "", [], {}, 15.0, 7, "x")] == [
        "none", "true", "false", "empty", "empty list", "empty object", "15", "7", "x"]

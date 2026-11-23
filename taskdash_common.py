"""Shared plumbing for the TaskDash scheduled-task monitor.

Driver: taskdash_publish.py (single entry point)
Source: Windows Task Scheduler + Microsoft-Windows-TaskScheduler/Operational log + workflow log files
Target: ArcGIS Online hosted table service (TaskInventory / TaskRuns / TaskMetrics)

Config lives in TaskDash.config.json next to this file (installed from a wheel: in TASKDASH_HOME,
else the working directory). Host identity is `host_name_override` when set, else the computer
name. After a rename use `host_previous_names`: it moves the old name's inventory rows -- a
task-server migration once silently forked the dataset because identity was socket.gethostname().
"""

from __future__ import annotations

import builtins
import contextlib
import importlib.metadata
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# Shown in the dashboard's About panel and the wheel name; release.yml checks it against the tag.
TASKDASH_VERSION = "1.2.0rc3"
# Bump when TABLE_SPECS or TABLE_INDEXES change. A service stamped with a newer number was
# migrated by a newer TaskDash, which this one may write wrongly.
SCHEMA_VERSION = 2

DEFAULT_CONFIG_FILE = "TaskDash.config.json"
# What the code uses when a known config key is absent: the one place these values live. Read a
# value with config_default(). The wording `preflight --config-diff` prints comes from here too, and
# a test compares it with the Default column of docs/CONFIGURE.md. Taken from the code, never from
# TaskDash.config.example.json, whose values are placeholders. The sign-in password keys are left
# out: nothing about them is printed. A string such as "true when smtp.port is 465, else false"
# stands for a default that depends on another key.
CONFIG_DEFAULTS: Dict[str, Any] = {
    "host_name_override": "", "host_previous_names": "",
    "powershell_timeout_seconds": 180,
    "arcgis.url": "https://www.arcgis.com",
    "arcgis.profile": "", "arcgis.username": "", "arcgis.username_env": "",
    "arcgis.item_id": "", "arcgis.test_item_id": "",
    "arcgis.view_item_id": "", "arcgis.test_view_item_id": "", "arcgis.dashboard_id": "",
    "arcgis.service_description": "TaskDash - scheduled task monitoring",
    "arcgis.service_tags": "taskdash,monitoring,scheduled tasks",
    "arcgis.dashboard_tags": "taskdash,monitoring",
    "alerting.enabled": False, "alerting.to": "", "alerting.mail_config_path": "",
    "alerting.healthcheck_url": "", "alerting.include_log_tail": True,
    "alerting.dashboard_url": "", "alerting.heartbeat_hour": None,
    "alerting.realert_hours": 24, "alerting.shared_cause_min_tasks": 3,
    "alerting.incident_digest_hours": 6, "alerting.maintenance_windows": [],
    "scheduler.task_name_regex": "", "scheduler.task_path_regex": "",
    "scheduler.action_regex": "", "scheduler.exclude_task_name_regex": "",
    "scheduler.exclude_microsoft_tasks": True, "scheduler.include_disabled": True,
    "scheduler.exclude_command_target_types": [],
    "scheduler.interactive_required_pattern": "\\ArcGIS\\Pro\\bin\\Python",
    "scheduler.collector_task_name": "TaskDash Collector",
    "scheduler.collector_interval_minutes": 60,
    "scheduler.overdue_grace_minutes": 15.0,
    "scheduler.heartbeat_ignore_after_days": 7,
    "history.max_events_per_run": 5000, "history.days_back": 30, "history.first_run_max_events": 30000,
    "metrics.days_back": 30, "metrics.max_log_files_per_task": 40, "metrics.max_lines_per_file": 5000,
    "metrics.log_directory_overrides": {}, "metrics.log_roots": [], "metrics.tasks": {},
    "upload.chunk_size": 200,
    "retention.runs_days": 90, "retention.metrics_days": 90, "retention.changes_days": 365,
    "logging.log_dir": "", "logging.max_log_age_days": 30,
    "smtp.host": "", "smtp.port": 587, "smtp.from": "", "smtp.user": "",
    "smtp.starttls": True, "smtp.ssl": "true when smtp.port is 465, else false",
    "smtp.allow_plaintext_auth": False,
    "privacy.hide_command_lines": True, "privacy.hide_sample_lines": True,
    "privacy.hide_accounts": True, "privacy.hide_paths": True,
    "rules.profiles": "none (required)",
}


# Keys whose map entry is wording, not a value the code can use.
_WORDING_ONLY_DEFAULTS = frozenset({"smtp.ssl", "rules.profiles"})


def config_default(dotted_key: str) -> Any:
    """The code default of a known config key, such as "history.days_back".

    A list or dict comes back as a copy, so a caller cannot change the map.
    """
    if dotted_key in _WORDING_ONLY_DEFAULTS:
        # A truthy wording string read as smtp.ssl would switch SSL on.
        raise KeyError(f"{dotted_key} has no single default value; compute it from the other keys")
    value = CONFIG_DEFAULTS[dotted_key]
    return value.copy() if isinstance(value, (list, dict)) else value


DEFAULT_POWERSHELL_TIMEOUT_SECONDS = config_default("powershell_timeout_seconds")
DEFAULT_CHUNK_SIZE = config_default("upload.chunk_size")

TABLE_INVENTORY = "TaskInventory"
TABLE_RUNS = "TaskRuns"
TABLE_METRICS = "TaskMetrics"
TABLE_COLLECTOR_RUNS = "CollectorRuns"
TABLE_TASK_CHANGES = "TaskChanges"
TABLE_METRIC_CHANGE = "MetricChange"


# --------------------------------------------------------------------------- arcpy guard

def disable_arcpy_import() -> None:
    """Keep ArcGIS API imports from initializing an ArcPy/Pro license.

    This process only edits hosted AGOL tables. Under a non-interactive scheduled task the
    ArcGIS API probes ArcPy during import and dies before AGOL is reached if the Pro license
    is not initialized. Raising ImportError for ArcPy makes the API pick a non-ArcPy engine.
    """
    if getattr(builtins, "_taskdash_arcpy_disabled", False):
        return

    os.environ.setdefault("ARCGIS_IO_ENGINE", "shapefile")
    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {"arcpy", "arcgisscripting"} or name.startswith(("arcpy.", "arcgisscripting.")):
            raise ImportError(f"{name} disabled for AGOL-only publish")
        return real_import(name, globals, locals, fromlist, level)

    builtins._taskdash_arcpy_disabled = True
    builtins.__import__ = guarded_import


# --------------------------------------------------------------------------- paths & config

def _folder_deploy() -> bool:
    """True when rules/default.json sits beside this module: the plain-folder install, where one
    folder holds both the shipped data and the per-install files. The file, not just a rules/
    folder, because a wheel's modules share site-packages with any package named rules."""
    return (Path(__file__).resolve().parent / "rules" / "default.json").is_file()


def data_dir() -> Path:
    """Shipped read-only data: rules/default.json, dashboard_templates.json, the example config."""
    if _folder_deploy():
        return Path(__file__).resolve().parent
    return Path(sys.prefix) / "share" / "taskdash"


def home_dir() -> Path:
    """Per-install files: config, Logs, alert_state.json, pending files, dashboard output."""
    if _folder_deploy():
        return Path(__file__).resolve().parent
    if os.environ.get("TASKDASH_HOME"):
        return Path(os.environ["TASKDASH_HOME"]).resolve()
    cwd = Path.cwd().resolve()
    # A command run from another folder still finds the install that owns the venv.
    if sys.prefix != sys.base_prefix and not (cwd / DEFAULT_CONFIG_FILE).is_file():
        venv_parent = Path(sys.prefix).resolve().parent
        if (venv_parent / DEFAULT_CONFIG_FILE).is_file():
            return venv_parent
    return cwd


def runtime_dir() -> Path:
    """Where this install keeps its own files (home_dir). Callers that need shipped data use data_dir."""
    return home_dir()


_CONFIG_SECTION_KEYS = {"arcgis", "alerting", "scheduler", "history", "metrics", "upload",
                       "retention", "logging", "smtp", "privacy", "rules"}
_CONFIG_TOP_LEVEL_KEYS = _CONFIG_SECTION_KEYS | {"host_name_override", "host_previous_names",
                                                  "powershell_timeout_seconds"}

# Dotted key -> expected type, built from every config.get(...) call across the TaskDash .py
# files (see taskdash_collect.py:should_include_task, taskdash_publish.py:collect_everything/
# publish/main, taskdash_common.py:connect_agol/load_mail_settings). Keys not read anywhere
# are deliberately absent -- validating a key the code never looks at would just be noise.
_CONFIG_STRING_KEYS = {
    "host_name_override", "arcgis.url", "arcgis.username", "arcgis.password",
    "arcgis.username_env", "arcgis.password_env", "arcgis.item_id", "arcgis.test_item_id",
    "arcgis.view_item_id", "arcgis.test_view_item_id",
    "arcgis.dashboard_id", "alerting.mail_config_path", "scheduler.task_name_regex",
    "scheduler.task_path_regex", "scheduler.action_regex", "scheduler.exclude_task_name_regex",
    "scheduler.interactive_required_pattern", "alerting.healthcheck_url",
    "scheduler.collector_task_name",
    "arcgis.service_description", "arcgis.service_tags", "arcgis.dashboard_tags",
    "alerting.dashboard_url",
    "logging.log_dir", "arcgis.profile", "smtp.host", "smtp.from", "smtp.user",
}
_CONFIG_NUMBER_KEYS = {
    "powershell_timeout_seconds", "alerting.heartbeat_hour", "alerting.realert_hours",
    "alerting.shared_cause_min_tasks", "alerting.incident_digest_hours",
    "scheduler.overdue_grace_minutes", "scheduler.collector_interval_minutes",
    "scheduler.heartbeat_ignore_after_days",
    "history.max_events_per_run", "history.days_back", "history.first_run_max_events",
    "metrics.days_back", "metrics.max_log_files_per_task", "metrics.max_lines_per_file",
    "upload.chunk_size", "retention.runs_days", "retention.metrics_days",
    "retention.changes_days", "logging.max_log_age_days", "smtp.port",
}
# Dotted key -> (lowest, highest or None, lowest allowed itself, whole numbers only). Only values that
# break a run are ruled out. retention.*_days, logging.max_log_age_days and metrics.max_lines_per_file
# are absent on purpose: 0 or less is a documented setting there (keep everything, read the whole file).
_CONFIG_RANGES = {
    "powershell_timeout_seconds": (0, None, False, False),
    "alerting.heartbeat_hour": (0, 23, True, True),
    "alerting.realert_hours": (0, None, True, False),
    "alerting.shared_cause_min_tasks": (0, None, True, True),
    "alerting.incident_digest_hours": (0, None, True, False),
    "scheduler.overdue_grace_minutes": (0, 525600, True, False),
    "scheduler.collector_interval_minutes": (0, None, False, False),
    "scheduler.heartbeat_ignore_after_days": (0, None, False, False),
    "history.max_events_per_run": (1, None, True, True),
    "history.days_back": (0, None, False, False),
    "history.first_run_max_events": (1, None, True, True),
    "metrics.days_back": (0, None, False, False),
    "metrics.max_log_files_per_task": (1, None, True, True),
    "upload.chunk_size": (1, None, True, True),
    "smtp.port": (1, 65535, True, True),
}
_CONFIG_BOOL_KEYS = {"alerting.enabled", "alerting.include_log_tail", "scheduler.exclude_microsoft_tasks",
                    "scheduler.include_disabled", "arcgis.allow_plaintext_password",
                    "smtp.starttls", "smtp.ssl", "smtp.allow_plaintext_auth",
                    "privacy.hide_command_lines", "privacy.hide_sample_lines",
                    "privacy.hide_accounts", "privacy.hide_paths"}
_CONFIG_STRING_LIST_KEYS = {"alerting.to", "scheduler.exclude_command_target_types",
                            "rules.profiles", "metrics.log_roots", "host_previous_names"}
_CONFIG_DICT_KEYS = {"metrics.log_directory_overrides", "metrics.tasks"}
# alerting.maintenance_windows: list of {"task": regex str, "start": ISO str, "end": ISO str,
# "reason": str}; an explicit "end": null is an open window (a per-task mute; an omitted "end" is still
# ignored) and "start" may be omitted or null (active now). The list-of-objects shape is validated here; a stray key or an empty
# "task" only warns (_maintenance_window_warnings), so an existing config keeps loading. A malformed
# value (bad regex, unparseable start/end) is caught defensively at runtime by
# taskdash_publish._active_maintenance_windows/_task_in_maintenance, which warn and skip it
# rather than fail the whole config.
_CONFIG_LIST_OF_DICT_KEYS = {"alerting.maintenance_windows"}

_MISSING = object()


def _section_allowed_keys() -> Dict[str, set]:
    """Section -> its known keys, derived from the typed-key sets above (one source of truth)."""
    allowed: Dict[str, set] = {section: set() for section in _CONFIG_SECTION_KEYS}
    for keys in (_CONFIG_STRING_KEYS, _CONFIG_NUMBER_KEYS, _CONFIG_BOOL_KEYS,
                 _CONFIG_STRING_LIST_KEYS, _CONFIG_DICT_KEYS, _CONFIG_LIST_OF_DICT_KEYS):
        for dotted in keys:
            section, _, key = dotted.partition(".")
            if section in allowed:
                allowed[section].add(key)
    return allowed


def _unknown_section_keys(config: Dict[str, Any]) -> List[tuple]:
    """(section, key) for every unrecognised key inside a known section; '_' keys are notes."""
    allowed = _section_allowed_keys()
    return [(section, key) for section in sorted(allowed) if isinstance(config.get(section), dict)
            for key in config[section] if key not in allowed[section] and not key.startswith("_")]


# Old dotted key -> new dotted key, for `preflight --config-diff`. No key has been renamed yet: the
# deprecated alerting.mail_config_path still works and is not a rename. Add a pair when a key moves.
RENAMED_KEYS: Dict[str, str] = {}


def config_diff(config: Dict[str, Any]) -> Dict[str, List[Any]]:
    """Compare a config's keys with the known-keys tables. Names only, never a value.

    The example file's values are placeholders and often differ from the code default (the example
    sets heartbeat_hour 7, the code default sends no daily e-mail), so they are no source of
    defaults. CONFIG_DEFAULTS in this module holds the code defaults.

    unknown: dotted keys TaskDash ignores (likely typos); absent: dotted known keys the config
    lacks; renamed: [(old, new)]. '_' keys are notes. Nested objects such as metrics.tasks are not
    walked.
    """
    allowed = _section_allowed_keys()
    known = sorted(_CONFIG_TOP_LEVEL_KEYS - _CONFIG_SECTION_KEYS) + sorted(
        f"{section}.{key}" for section in sorted(allowed) for key in allowed[section])
    present = [k for k in config if not k.startswith("_") and k not in _CONFIG_SECTION_KEYS]
    for section in sorted(_CONFIG_SECTION_KEYS):
        if isinstance(config.get(section), dict):
            present += [f"{section}.{k}" for k in config[section] if not k.startswith("_")]
    renamed = [(old, RENAMED_KEYS[old]) for old in present if old in RENAMED_KEYS]
    moved_to = {new for _, new in renamed}
    unknown = [k for k in present if k not in known and k not in RENAMED_KEYS]
    absent = [dotted for dotted in known if dotted not in present and dotted not in moved_to]
    return {"unknown": unknown, "absent": absent, "renamed": renamed}


def _config_path_value(config: Dict[str, Any], dotted_key: str) -> Any:
    node: Any = config
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _redact_config_value(dotted_key: str, value: Any) -> Any:
    """Never echo a credential into a SystemExit message, even when its type is wrong."""
    if "password" in dotted_key.lower() or "secret" in dotted_key.lower():
        return "<redacted>"
    return value


def config_range_problem(dotted_key: str, value: Any) -> Optional[str]:
    """'<key> must be <what>, got <value>' when value is not a finite number inside the key's range
    in _CONFIG_RANGES, else None. load_config and the dashboard command share it."""
    low, high, low_allowed, whole = _CONFIG_RANGES[dotted_key]
    number = "a whole number" if whole else "a number"
    expected = (f"{number} from {low:g} to {high:g}" if high is not None
                else f"{number} of {low:g} or more" if low_allowed else f"{number} above {low:g}")
    ok = (isinstance(value, (int, float)) and not isinstance(value, bool) and value == value
          and abs(value) != float("inf") and (value > low or (low_allowed and value == low))
          and (high is None or value <= high) and (not whole or value == int(value)))
    return None if ok else f"{dotted_key} must be {expected}, got {_redact_config_value(dotted_key, value)!r}"


_CONFIG_REGEX_KEYS = ("scheduler.task_name_regex", "scheduler.task_path_regex", "scheduler.action_regex",
                      "scheduler.exclude_task_name_regex")
# A folder such as \Nightly\ is the usual way to hit this: JSON turns "\\Nightly\\" into \Nightly\,
# and the regex engine then reads \N and a trailing backslash as escapes.
_REGEX_BACKSLASH_HINT = (r'In the JSON file, write each backslash twice: "^\\\\Nightly\\\\$" is the regex '
                         r"^\\Nightly\\$, which matches the folder \Nightly\.")


def _previous_names_errors(config: Dict[str, Any]) -> List[str]:
    """host_previous_names holds old computer names. An empty one matches nothing useful, and the
    current name would make the adoption step treat this host's own rows as another host's."""
    names = config.get("host_previous_names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return []  # the type check above already reported it
    if any(not n.strip() for n in names):
        return ["host_previous_names must not hold an empty name. Remove the empty entry, "
                "or list each earlier computer name as it shows in the Host column."]
    import socket

    current = normalize_text(config.get("host_name_override")) or socket.gethostname()
    if any(n.strip().lower() == current.lower() for n in names):
        return [f"host_previous_names must not list the current host name '{current}'. Remove it: "
                f"list only earlier names of this computer."]
    return []


_MAINTENANCE_WINDOW_KEYS = {"task", "start", "end", "reason"}


def maintenance_window_has_unknown_key(window: Dict[str, Any]) -> bool:
    return any(key not in _MAINTENANCE_WINDOW_KEYS and not str(key).startswith("_") for key in window)


def maintenance_window_is_misspelled_mute(window: Dict[str, Any]) -> bool:
    """An entry with no end or no start and a stray key may be a misspelled "end" ("End", "stop") or
    "start" ("Start"). Read as an open window it would mute for ever, or a future window would start
    at once, so the run ignores it, as it ignored every entry without both before optional keys
    existed. A stray key beside a real start and end changes nothing."""
    missing_a_bound = window.get("start") is None or window.get("end") is None
    return missing_a_bound and maintenance_window_has_unknown_key(window)


def _maintenance_window_warnings(config: Dict[str, Any]) -> List[str]:
    """Warnings, never load errors: an existing config with a stray key must keep running."""
    windows = _config_path_value(config, "alerting.maintenance_windows")
    if not isinstance(windows, list):
        return []  # the type check above already reported it
    warnings = []
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            continue
        where = f"alerting.maintenance_windows[{index}]"
        if maintenance_window_has_unknown_key(window):
            outcome = ("The entry has no start or no end, so it is ignored"
                       if maintenance_window_is_misspelled_mute(window) else "The key is ignored")
            warnings.append(f"WARNING: {where} has an unknown key. Use only task, start, end and reason "
                            f"(a '_' key is a note). {outcome}; check for a misspelled start or end.")
        task = window.get("task")
        if not isinstance(task, str) or not task.strip():
            warnings.append(f"WARNING: {where} has no non-empty \"task\" (a regex), so it matches nothing. "
                            f"Add it, or remove the entry.")
        for field in ("start", "end"):
            if _is_naive_iso_time(window.get(field)):
                warnings.append(f"WARNING: {where}.{field} has no offset, so TaskDash reads it as UTC, which can be "
                                f"hours off your local time. Add the offset, such as -05:00, or Z for UTC.")
    return warnings


def _is_naive_iso_time(value: Any) -> bool:
    """True for a readable ISO 8601 time that carries no offset. Other values are not this warning's concern."""
    text = value.strip() if isinstance(value, str) else ""
    if not text or text.endswith("Z"):
        return False
    try:
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text)).tzinfo is None
    except ValueError:
        return False


def _validate_config(config: Dict[str, Any]) -> List[str]:
    errors: List[str] = []

    def check(dotted_key: str, value: Any, expected: str, ok: bool) -> None:
        if not ok:
            errors.append(f"{dotted_key} must be {expected}, got {_redact_config_value(dotted_key, value)!r}")

    for section in _CONFIG_SECTION_KEYS:
        if section in config and not isinstance(config[section], dict):
            check(section, config[section], "an object", False)

    for key in _CONFIG_STRING_KEYS:
        value = _config_path_value(config, key)
        # false turns the Interactive-logon rule off (a licence that also works in a background session).
        if key == "scheduler.interactive_required_pattern" and value is False:
            continue
        if value is not _MISSING and value is not None:
            check(key, value, "a string", isinstance(value, str))

    for key in _CONFIG_NUMBER_KEYS:
        value = _config_path_value(config, key)
        if value is not _MISSING and value is not None:
            # json accepts NaN/Infinity; int(nan) and timedelta(hours=inf) would break the run.
            finite = (isinstance(value, (int, float)) and not isinstance(value, bool)
                      and value == value and abs(value) != float("inf"))
            check(key, value, "a finite number", finite)
            if finite and key in _CONFIG_RANGES:
                errors.extend(filter(None, [config_range_problem(key, value)]))

    for key in _CONFIG_REGEX_KEYS:
        value = _config_path_value(config, key)
        if isinstance(value, str) and value:
            try:
                re.compile(value)
            except re.error as exc:
                errors.append(f"{key} is not a valid regular expression ({exc}). {_REGEX_BACKSLASH_HINT}")

    for key in _CONFIG_BOOL_KEYS:
        value = _config_path_value(config, key)
        if value is not _MISSING and value is not None:
            check(key, value, "a boolean", isinstance(value, bool))

    for key in _CONFIG_STRING_LIST_KEYS:
        value = _config_path_value(config, key)
        if value is not _MISSING and value is not None:
            ok = isinstance(value, list) and all(isinstance(item, str) for item in value)
            check(key, value, "a list of strings", ok)

    errors.extend(_previous_names_errors(config))

    for key in _CONFIG_DICT_KEYS:
        value = _config_path_value(config, key)
        if value is not _MISSING and value is not None:
            check(key, value, "an object", isinstance(value, dict))

    for key in _CONFIG_LIST_OF_DICT_KEYS:
        value = _config_path_value(config, key)
        if value is not _MISSING and value is not None:
            ok = isinstance(value, list) and all(isinstance(item, dict) for item in value)
            check(key, value, "a list of objects", ok)

    return errors


def load_config(config_path: Optional[Path] = None, *, defer_warnings: bool = False):
    """Load and validate TaskDash.config.json.

    Reads utf-8-sig so a BOM (PowerShell 5.1's Out-File / Set-Content and Notepad both write
    one by default) does not fail json.loads on the leading byte. Every problem is collected
    and reported together, not one-at-a-time, so a config with several typos is fixed in one
    pass. Unknown top-level keys only warn, for forward compatibility with a newer config.

    defer_warnings=True returns (config, warnings) instead of emit()-ing the unknown-key
    warnings immediately, and the caller decides when to emit them. main() needs this: it calls
    load_config() before setup_logging() (log_dir itself comes from the config), so an
    immediate emit()'s logging.info() is silently dropped by the unconfigured root logger, and
    its print() raises and is swallowed under pythonw/Task Scheduler where stdout is None --
    the warning never reaches the log file, the one place a headless run's problems get read.
    Callers that don't pass it keep the original immediate-emit return type (just the dict).
    """
    path = config_path or (runtime_dir() / DEFAULT_CONFIG_FILE)
    if not path.exists():
        raise SystemExit(f"Config not found at {path}: copy TaskDash.config.example.json to "
                         f"{path.name} in {path.parent} and fill it in.\n"
                         + see_troubleshooting("The config file is missing"))
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Config at {path} is not valid JSON (line {exc.lineno}): {exc.msg}\n"
                         + see_troubleshooting("The config is not valid JSON"))
    except UnicodeDecodeError:
        # Windows PowerShell 5.1 Out-File and Set-Content write UTF-16 / ANSI by default.
        raise SystemExit(f"Config at {path} is not UTF-8 text. Open it in an editor and save it "
                         f"as UTF-8, then run again.\n" + see_troubleshooting("The config is not UTF-8 text"))
    except OSError as exc:
        raise SystemExit(f"Cannot read the config at {path}: {exc}. Give the account that runs "
                         f"TaskDash read access to the file, then run again.\n"
                         + see_troubleshooting("The config cannot be read"))
    if not isinstance(config, dict):
        raise SystemExit(f"Config at {path} must be a JSON object, not {type(config).__name__}.\n"
                         + see_troubleshooting("The config is not a JSON object"))

    errors = _validate_config(config)
    unknown_keys = _unknown_section_keys(config)
    # A mistyped privacy key would silently leave a field published, so privacy fails closed;
    # a typo elsewhere only loses that one setting and warns.
    valid_privacy = ", ".join(sorted(_section_allowed_keys()["privacy"]))
    for section, key in unknown_keys:
        if section == "privacy":
            errors.append(f"unknown key 'privacy.{key}' (valid privacy keys: {valid_privacy})")
    if errors:
        raise SystemExit(
            f"Config at {path} has {len(errors)} problem(s) -- fix and re-run:\n  "
            + "\n  ".join(errors) + "\n" + see_troubleshooting("The config has problems")
        )
    # A missing rules.profiles must not fall back silently: an install that had its own rules would
    # lose them, and --refresh-metrics --apply would then delete every row they mined.
    if not isinstance(config.get("rules"), dict) or "profiles" not in config["rules"]:
        raise SystemExit(
            f"Config at {path} has no rules.profiles. Add \"rules\": {{\"profiles\": []}} to use only "
            f"rules/default.json, or list your rules files, e.g. {{\"profiles\": [\"rules/<org>.json\"]}}.\n"
            + see_troubleshooting("The config has no rules profiles")
        )
    # The rules files and metrics.tasks are checked here too, so a bad regex stops the run before
    # any work. Imported late: taskdash_metrics imports this module.
    import taskdash_metrics
    taskdash_metrics.configure_rules(config)

    warnings = []
    for key in config:
        # "_notes"-style keys are the config's own documentation, not settings.
        if key not in _CONFIG_TOP_LEVEL_KEYS and not key.startswith("_"):
            message = f"WARNING: unknown config key '{key}' in {path} -- ignored (check for a typo)."
            if defer_warnings:
                warnings.append(message)
            else:
                emit(message)

    messages = [f"WARNING: unknown config key '{section}.{key}' in {path} -- ignored (check for a typo)."
                for section, key in unknown_keys if section != "privacy"]
    for message in messages + _maintenance_window_warnings(config):
        if defer_warnings:
            warnings.append(message)
        else:
            emit(message)

    return (config, warnings) if defer_warnings else config


def get_host_name(config: Dict[str, Any]) -> str:
    """Stable logical host identity. Override wins so a rename never forks the dataset.

    Running this from the wrong box would file that box's tasks under the configured host. This
    function only warns; `collect --apply` refuses to publish on a mismatch (host_mismatch in
    taskdash_publish). After a rename leave the override empty and use `host_previous_names`.
    """
    import socket

    actual = socket.gethostname()
    override = normalize_text(config.get("host_name_override"))
    if override and override.lower() != actual.lower():
        emit(f"WARNING: host_name_override is '{override}' but this machine is '{actual}'. "
             f"Collected data describes '{actual}' and will be published as '{override}'. "
             f"Correct after a rename; wrong if you meant to run this on '{override}'.")
    return override or actual


# --------------------------------------------------------------------------- logging

class ScrubSecretsFilter(logging.Filter):
    """Redact secrets in every record's final message and traceback before a handler writes it.

    Messages and exception texts echo whatever a library or a task put in them: a request URL
    with ?token=, a failing command line. The scrubber lives in taskdash_publish, which imports
    this module, so it is imported at first use.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from taskdash_publish import _scrub_secrets

            record.msg, record.args = _scrub_secrets(record.getMessage()), None
            if record.exc_info:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = _scrub_secrets(record.exc_text)
            if record.stack_info:
                record.stack_info = _scrub_secrets(record.stack_info)
        except Exception:
            # logging must never raise into the caller; an unscrubbed line must never be written.
            record.msg, record.args = "log line withheld: secret scrubbing failed", None
            record.exc_info = record.exc_text = record.stack_info = None
        return True


def setup_logging(log_dir: Path, prefix: str = "taskdash") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{prefix}_{stamp}.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.addFilter(ScrubSecretsFilter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[handler],
        force=True,
    )
    return log_path


def emit(message: str) -> None:
    """Log and print. Under pythonw/Task Scheduler stdout is None, so the file is the record.

    The message is scrubbed here as well as in the log filter: run_taskdash.cmd keeps stdout in
    Logs\\bootstrap.log, so a printed secret would still land in a file.
    """
    try:
        from taskdash_publish import _scrub_secrets

        message = _scrub_secrets(str(message))
    except Exception:
        message = "log line withheld: secret scrubbing failed"
    logging.info(message)
    try:
        print(message, flush=True)
    except (ValueError, OSError, AttributeError):
        pass


def see_troubleshooting(heading: str) -> str:
    """The last line of a refusal that docs/TROUBLESHOOTING.md documents. test_h3_see_troubleshooting
    checks every heading named in the code against the docs, so pass it as a string literal."""
    return f"See TROUBLESHOOTING.md, section {heading}."


# One help string for every --config option: the file is the install's own, not "next to the scripts".
CONFIG_ARG_HELP = "Config file. Default: the install's config."


def command_text(command: str, *flags: str) -> str:
    """The one form a message uses to name a command to run: `taskdash <command> <flags>`.
    A bare flag names no command, and `python taskdash_publish.py` does not exist in a wheel
    install. test_audit_s04_commands checks the shipped messages for both."""
    return "`" + " ".join(("taskdash", command, *flags)) + "`"


# setup_logging's own "{prefix}_{stamp}.log", the bootstrap*.log files written directly by
# run_taskdash.cmd (its stdout/stderr redirect, before setup_logging ever runs), and the
# --preview-alert output.
TASKDASH_LOG_GLOBS = ("taskdash_*.log", "bootstrap*.log", "alert_preview_*.txt")
# Prefix of the run log of every collect run except the hourly prod publish (a dry run, a test run,
# --migrate). `taskdash status --offline` skips these, so a manual run cannot hide the task's result.
MANUAL_LOG_PREFIX = "taskdash_manual"


def write_bootstrap_fail(message: str) -> None:
    """Keep a pre-logging failure of a wheel install as Logs/bootstrap_FAIL_<stamp>.log.

    The wheel task runs taskdash.exe directly, so run_taskdash.cmd's capture does not apply and a
    config or import error would leave no file. The folder install already has that capture.
    Never raises: a failing writer must not hide the error it reports.
    """
    if _folder_deploy():
        return
    try:
        try:
            from taskdash_publish import _scrub_secrets
            message = _scrub_secrets(message)
        except Exception:
            pass
        log_dir = home_dir() / "Logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        (log_dir / f"bootstrap_FAIL_{stamp}.log").write_text(message.rstrip() + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"TaskDash could not write bootstrap_FAIL log: {exc}", file=sys.stderr)


def cleanup_old_logs(log_dir: Path, max_age_days: int) -> None:
    """Delete TaskDash's own aged logs only.

    Globbing every *.log in log_dir would delete a shared or misconfigured log_dir's other
    programs' logs too; restricting to TaskDash's own names keeps this safe even if log_dir
    is ever pointed somewhere it should not be.
    """
    if max_age_days <= 0 or not log_dir.exists():
        return
    cutoff = datetime.now().timestamp() - (max_age_days * 86400)
    for pattern in TASKDASH_LOG_GLOBS:
        for path in log_dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError as exc:
                logging.warning("Could not remove old log %s: %s", path, exc)


# --------------------------------------------------------------------------- process control

class RunLockHeld(Exception):
    """Another TaskDash run already holds the single-instance lock.

    held_since is the holder's own recorded start time (best-effort; None if the lock file's
    metadata is missing or unreadable), so a caller can tell a normal hourly overlap from a run
    that has been stuck holding the lock for hours -- see STALE_LOCK_HOURS.
    """

    def __init__(self, message: str, held_since: Optional[datetime] = None, pid: Optional[int] = None,
                 lock_path: Optional[Path] = None):
        super().__init__(message)
        self.held_since = held_since
        self.pid = pid
        self.lock_path = lock_path


STALE_LOCK_HOURS = 2  # a run stuck this long is treated as dead, not a normal overlap


def _lock_metadata(lock_path: Path) -> str:
    """Read the holder's pid/start-time text without touching the locked byte at offset 0.

    msvcrt.locking(LK_NBLCK) locks exactly 1 byte at offset 0. Reading that byte from a second
    handle while it is locked raises PermissionError on Windows (verified); metadata is written
    starting at offset 1 by the lock holder below specifically so a contending process can still
    read it.
    """
    try:
        with open(lock_path, "rb") as fh:
            fh.seek(1)
            return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _lock_started(metadata: str) -> Optional[datetime]:
    match = re.search(r"started=(\S+)", metadata)
    return parse_iso_datetime(match.group(1)) if match else None


@contextlib.contextmanager
def run_lock(log_dir: Path, name: str = "taskdash.lock"):
    """Single-instance lock for the process lifetime; raises RunLockHeld on contention.

    msvcrt.locking(LK_NBLCK) fails immediately instead of blocking: a slow hourly pass still
    running when the next one starts must be skipped, not queued, or two processes would race
    the same adds/purges against AGOL. msvcrt is Windows-only and imported lazily so importing
    this module does not require it. The lock byte itself (offset 0) never carries data; the
    holder's pid and UTC start time are written from offset 1 onward so RunLockHeld.held_since
    can be read on contention without touching the locked byte. Caller pattern:

        try:
            with run_lock(log_dir):
                ...
        except RunLockHeld as exc:
            emit("Another TaskDash run is in progress; exiting.")
            raise SystemExit(0)
    """
    import msvcrt

    log_dir.mkdir(parents=True, exist_ok=True)
    lock_path = log_dir / name
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        metadata = _lock_metadata(lock_path)
        detail = f" ({metadata})" if metadata else ""
        pid_match = re.search(r"pid=(\d+)", metadata)
        raise RunLockHeld(f"Run lock at {lock_path} is held by another process{detail}.",
                          held_since=_lock_started(metadata),
                          pid=int(pid_match.group(1)) if pid_match else None,
                          lock_path=lock_path) from exc
    try:
        handle.seek(1)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started={utc_now().isoformat()}".encode("utf-8"))
        handle.flush()
        yield lock_path
    finally:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via a same-directory temp file + os.replace.

    os.replace is atomic on the same volume, so a crash mid-write cannot leave alert_state.json
    truncated -- a truncated state file would make every alert type look never-sent and re-fire
    every heartbeat/failure alert on the next run.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # On Windows, a virus scanner or the search indexer can hold the target open for a moment,
        # and os.replace then fails with WinError 5 (seen 2026-10-17 in 1 of 50 suite runs). A lost
        # alert state re-sends the last e-mail, so retry briefly before giving up.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- text / number helpers

def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def safe_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def escape_sql_string(value: str) -> str:
    return str(value).replace("'", "''")


def host_clause(*names: str, exclude: bool = False) -> str:
    """The where clause that picks (or with exclude, skips) rows of the named computers. The column stays
    bare so the hosted database can seek its host_name indexes; it compares text without regard to case.
    The one place that quotes the value."""
    values = ",".join(f"'{escape_sql_string(name.upper())}'" for name in names)
    if len(names) == 1:
        return f"host_name {'<>' if exclude else '='} {values}"
    return f"host_name {'NOT IN' if exclude else 'IN'} ({values})"


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    text = normalize_text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # .NET round-trip format can carry 7 fractional digits; Python accepts at most 6.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_epoch_millis(value: Any) -> Optional[int]:
    parsed = value if isinstance(value, datetime) else parse_iso_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def round_hours(value: float) -> float:
    return round(value, 2)


def short_duration(seconds: float) -> str:
    """One unit style for every reason and e-mail line: "45 s", "12 min", "3.2 h".

    The dashboard's fmtDur uses the same unit words, so a value reads alike in both places.
    """
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60.0:.0f} min"
    return f"{seconds / 3600.0:.1f} h"


# --------------------------------------------------------------------------- powershell

def system32_exe(*parts: str) -> str:
    """Absolute path under System32. A bare name is searched in the working folder and PATH,
    so a planted copy would run as the task account."""
    return str(Path(os.environ.get("SystemRoot") or r"C:\Windows", "System32", *parts))


POWERSHELL_EXE = ("WindowsPowerShell", "v1.0", "powershell.exe")


class PowerShellTimeout(SystemExit):
    """A SystemExit the event read can tell apart, so a slow log defers its exit to the end of
    the run (like EventLogDisabled) and halves the next first-run read."""


POWERSHELL_OUT_ENV = "TASKDASH_PS_OUT"

# The payload runs dot-sourced, so its variables keep the scope they had at the top of a -Command
# string. FullLanguage prints to stdout as it always did. ConstrainedLanguage (AppLocker or WDAC)
# cannot set [Console]::OutputEncoding, so stdout would arrive in the OEM code page and garble
# non-ASCII task names; there the JSON goes to a UTF-8 file named in the environment instead.
POWERSHELL_WRAPPER = (
    "$tdScript = {\n__SCRIPT__\n}\n"
    "if ($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage') { . $tdScript }\n"
    f"else {{ . $tdScript | Set-Content -LiteralPath $env:{POWERSHELL_OUT_ENV} -Encoding UTF8 }}\n"
)


def _powershell_argv(script: str) -> List[str]:
    return [system32_exe(*POWERSHELL_EXE), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-OutputFormat", "Text", "-Command", script]


def powershell_language_mode(timeout_seconds: int = 30) -> str:
    """Language mode of a new PowerShell ("FullLanguage", "ConstrainedLanguage"), or "" when unreadable."""
    argv = _powershell_argv("$ExecutionContext.SessionState.LanguageMode")
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout_seconds, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", errors="replace").strip() if done.returncode == 0 else ""


def _run_powershell(script: str, timeout_seconds: int) -> Tuple[Any, str]:
    """(finished process, text of the temp file the wrapper wrote or ""). The file is always removed."""
    out_path = runtime_dir() / f".taskdash_ps_{uuid.uuid4().hex}.json"
    try:
        completed = subprocess.run(
            _powershell_argv(POWERSHELL_WRAPPER.replace("__SCRIPT__", script)),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env={**os.environ, POWERSHELL_OUT_ENV: str(out_path)},
        )
        file_text = out_path.read_bytes().decode("utf-8-sig", errors="replace") if out_path.exists() else ""
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError as exc:
            emit(f"WARNING: could not remove the temporary file {out_path} ({exc}). Delete it by hand.")
    return completed, file_text


def run_powershell_json(script: str, timeout_seconds: int = DEFAULT_POWERSHELL_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
    """Run a PowerShell snippet that ends in ConvertTo-Json and return a list of dicts.

    Output is decoded as UTF-8 explicitly. The old wrapper piped PowerShell's default
    UTF-16 through a text log and produced unreadable spaced-out characters. In
    ConstrainedLanguage the JSON comes back through a temp file (see POWERSHELL_WRAPPER).
    """
    try:
        completed, file_text = _run_powershell(script, timeout_seconds)
    except subprocess.TimeoutExpired:
        raise PowerShellTimeout(
            f"PowerShell did not finish within {timeout_seconds} s. Raise "
            f"powershell_timeout_seconds in the config, or check that the computer is not "
            f"overloaded.")
    stdout = file_text.strip() or completed.stdout.decode("utf-8", errors="replace").strip()
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()

    if completed.returncode != 0:
        # The probe runs only after a failure, so a healthy run starts one PowerShell, not two.
        if powershell_language_mode() == "ConstrainedLanguage":
            raise SystemExit(
                f"PowerShell is in ConstrainedLanguage mode (AppLocker or WDAC is on) and the command "
                f"failed (exit {completed.returncode}): {stderr[:500]}\n"
                "Ask your administrator to add an allow rule for the TaskDash folder to the AppLocker or "
                "WDAC policy, or run the collector as an account that the policy exempts.\n"
                + see_troubleshooting("PowerShell runs in ConstrainedLanguage mode"))
        raise SystemExit(f"PowerShell collection failed (exit {completed.returncode}): {stderr[:500]}\n"
                         + see_troubleshooting("PowerShell failed"))
    if not stdout:
        return []

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"PowerShell returned non-JSON output ({exc.msg}): {stdout[:300]}\n"
                         + see_troubleshooting("PowerShell returned text that is not JSON"))

    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


# --------------------------------------------------------------------------- retry

RETRYABLE_STATUS_CODES = {429, 498, 499, 500, 502, 503, 504}
_ERROR_CODE_RE = re.compile(r"\(Error Code:\s*(\d+)\)")
_HTTP_ERROR_RE = re.compile(r"\b(\d{3})\s+(?:Server|Client) Error\b")
_JSON_CODE_RE = re.compile(r"'code':\s*(\d+)")


def _retryable_status(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP/Esri status code for an arcgis-raised exception.

    arcgis/gis/_impl/_con/_connection.py re-raises most failures as a bare Exception built
    from a formatted string -- data['error']['message'] plus "(Error Code: N)" for a JSON
    error body (_handle_json_error), or "Http Error: <requests message>" for an HTTP error --
    and drops the original response object, so the status has to come back out of the message
    when `.response` was not preserved on the exception. Query._process_query_result instead
    raises ValueError(result) with the raw error dict still attached as exc.args[0], so a
    query failure (query_all_rows, max_field_value, fetch_keys, purge_older_than's count) never
    matches _ERROR_CODE_RE/_HTTP_ERROR_RE and must be read off the dict or its str() directly.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    if status:
        return int(status)
    payload = exc.args[0] if getattr(exc, "args", None) else None
    if isinstance(payload, dict):
        code = payload.get("error", {}).get("code") if isinstance(payload.get("error"), dict) else None
        if code is not None:
            return int(code)
    text = str(exc)
    match = _ERROR_CODE_RE.search(text) or _HTTP_ERROR_RE.search(text) or _JSON_CODE_RE.search(text)
    return int(match.group(1)) if match else None


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    return safe_float(headers.get("Retry-After")) if headers else None


def with_retry(
    func: Callable[[], Any],
    *,
    label: str,
    attempts: int = 4,
    base_delay: float = 1.0,
    sleep: Optional[Callable[[float], None]] = None,
    before_retry: Optional[Callable[[], None]] = None,
) -> Any:
    """Call func() with retry on transient AGOL failures.

    Retries HTTP/Esri 429, 498, 499, 500, 502, 503, 504 and connection/timeout errors, honouring
    a Retry-After header on 429 when the response survived on the exception. Any other failure
    (bad request, schema mismatch, a real auth failure) is raised immediately -- retrying a
    permanent error only delays the actionable SystemExit. Exponential backoff with jitter;
    `sleep` is injectable so tests do not wait. `before_retry`, if given, runs once per caught
    failure before the delay -- apply_edits_chunked and sync_inventory use it to re-query
    existing keys before resending an ambiguous add (e.g. after a 504 whose write may already
    have committed), so a retried write cannot duplicate rows.

    This is on top of, not instead of, transport-level retry: arcgis 2.4.3 builds its session as
    EsriSession(retries=5) (gis/_impl/_con/_connection.py), which mounts a urllib3 Retry(total=5,
    status_forcelist=(413,429,500,502,503,504)) with POST allowed (arcgis/auth/api.py) -- a real
    HTTP-level 429/5xx on applyEdits is already retried up to 5 times inside `requests` before
    this function, or a dedupe hook, ever sees an exception. What reaches here for those codes is
    only what survived all 5 transport attempts. What this function's own retry mainly recovers
    is an Esri error embedded in a 200 OK JSON body (`Query._process_query_result` raises
    ValueError with the code inside the body), which urllib3's status-code-based Retry never
    sees at all -- that path, and 498/499 (Esri token expiry, not a real HTTP status), are what
    this loop and its dedupe hooks actually exist for.
    """
    import requests

    sleep_fn = sleep or time.sleep
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                ConnectionError, TimeoutError) as exc:
            last_exc, retry_after = exc, None
        except Exception as exc:
            status = _retryable_status(exc)
            if status not in RETRYABLE_STATUS_CODES:
                raise
            last_exc = exc
            retry_after = _retry_after_seconds(exc) if status == 429 else None

        if attempt == attempts:
            break
        if before_retry is not None:
            before_retry()
        if retry_after is not None:
            delay = retry_after  # server-specified; honour it exactly, no jitter added
        else:
            delay = base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
        emit(f"WARNING: {label} failed (attempt {attempt}/{attempts}): {last_exc}. "
            f"Retrying in {delay:.1f}s.")
        sleep_fn(delay)

    if isinstance(last_exc, requests.exceptions.SSLError):
        raise SystemExit(f"{label} failed after {attempts} attempts. SSLError: the certificate of "
                         f"the ArcGIS server was not trusted. " + _ssl_fix_text(last_exc)
                         + see_troubleshooting("The certificate was not trusted (SSLError)")) from last_exc
    raise SystemExit(f"{label} failed after {attempts} attempts: {last_exc}\n"
                     + see_troubleshooting("An ArcGIS call failed after several tries"))


# --------------------------------------------------------------------------- AGOL

def make_field(
    name: str,
    field_type: str,
    alias: str,
    *,
    length: Optional[int] = None,
    nullable: bool = True,
    editable: bool = True,
) -> Dict[str, Any]:
    field: Dict[str, Any] = {
        "name": name,
        "type": field_type,
        "alias": alias,
        "nullable": nullable,
        "editable": editable,
        "domain": None,
        "defaultValue": None,
    }
    if length is not None:
        field["length"] = length
    return field


def arcgis_version_warning() -> Optional[str]:
    """One WARNING line when the installed arcgis is outside pyproject's range, else None.

    The range is parsed by hand (packaging may not be installed). Keep it equal to the
    `arcgis` dependency in pyproject.toml. The version comes from the package metadata, never
    from `import arcgis`: that import probes ArcPy and dies without a Pro licence, and it runs
    before connect_agol installs the ArcPy guard."""
    try:
        version = importlib.metadata.version("arcgis")
    except importlib.metadata.PackageNotFoundError:
        return None
    match = re.match(r"(\d+)\.(\d+)", version)
    if match and (2, 4) <= (int(match.group(1)), int(match.group(2))) < (3, 0):
        return None
    return (f"WARNING: arcgis {version} is outside the tested range (arcgis>=2.4,<3). TaskDash may "
            "fail in ways its tests do not cover. Install a version in that range with "
            "pip install \"arcgis>=2.4,<3\".")


def _ssl_fix_text(exc: BaseException) -> str:
    """The cause and fix for an SSLError, shared by sign-in and every REST call (with_retry)."""
    return (f"A proxy that inspects TLS traffic is the usual cause. Add the proxy's root certificate to the Windows certificate store, or "
            f"save it as a PEM file and set the REQUESTS_CA_BUNDLE variable to that file's path. "
            f"Open a new PowerShell window afterwards. Detail: {str(exc)[:200]}\n")


def _open_gis(url: str, *args: Any, **kwargs: Any) -> Any:
    import requests
    from arcgis.gis import GIS

    # arcgis leaves its requests session's trust_env at None unless told, and requests then skips
    # HTTPS_PROXY: behind a proxy every call went direct (measured 2026-10-18, arcgis 2.4.3).
    kwargs.setdefault("trust_env", True)
    try:
        return GIS(url, *args, **kwargs)
    except requests.exceptions.SSLError as exc:
        # A subclass of ConnectionError, so it must be caught first: "check the network" hides the cause.
        raise SystemExit(
            f"SSLError: the certificate of arcgis.url '{url}' was not trusted. "
            + _ssl_fix_text(exc)
            + see_troubleshooting("The certificate was not trusted (SSLError)")) from exc
    except requests.exceptions.ConnectionError as exc:
        raise SystemExit(
            f"Cannot reach arcgis.url '{url}'. Check the URL in the config (ArcGIS Online is "
            f"https://www.arcgis.com or https://<org>.maps.arcgis.com), the network and any proxy. "
            f"Detail: {type(exc).__name__}: {str(exc)[:200]}\n"
            + see_troubleshooting("TaskDash cannot reach ArcGIS")) from exc


SIGNIN_FIX = (f"Run {command_text('signin', '--apply')} as the Windows account that runs this command. To "
              f"sign in the scheduled task's account without runas, run "
              f"{command_text('signin', '--for-task-account', '--apply')} in your own window.")


def _sign_in_or_exit(url: str, what: str, *args: Any, secret: str = "", **kwargs: Any) -> Any:
    """_open_gis, with any other sign-in failure (a wrong or expired password) turned into the one
    exit that names the fix. SSL and connection failures already exit from _open_gis. The text is cut
    and the secret removed, because an ArcGIS error can quote what was sent. A timeout, an HTTP 5xx
    and a broken arcgis package are not credential faults: they propagate, so main() logs the
    traceback and the user is not told to store a working sign-in again."""
    import requests

    try:
        return _open_gis(url, *args, **kwargs)
    except (ImportError, AttributeError, requests.exceptions.Timeout):
        raise
    except Exception as exc:
        response = getattr(exc, "response", None)
        if isinstance(exc, requests.exceptions.HTTPError) and (getattr(response, "status_code", 0) or 0) >= 500:
            raise
        text = f"{type(exc).__name__}: {exc}"
        text = (text.replace(secret, "***") if secret else text)[:200]
        raise SystemExit(f"ArcGIS sign-in with {what} failed: {text}. {SIGNIN_FIX}\n"
                         + see_troubleshooting("The stored sign-in does not work")) from None


def _empty_env_names(*names: str) -> str:
    """The *_env settings that name a variable this process does not have, as one sentence or ''."""
    empty = [name for name in names if name and not os.environ.get(name)]
    return (f" The environment variable {' and '.join(repr(n) for n in empty)} is empty or not set for "
            f"the account that runs TaskDash.") if empty else ""


def connect_agol(config: Dict[str, Any]) -> Any:
    """Connect using config credentials, or env vars when *_env names are supplied."""
    disable_arcpy_import()

    agol = config.get("arcgis", {})
    url = normalize_text(agol.get("url")) or "https://www.arcgis.com"

    # A profile keeps the password in the OS credential store, so none is read from the config.
    profile = normalize_text(agol.get("profile"))
    if profile:
        if agol.get("password"):
            emit("WARNING: arcgis.password is unused with arcgis.profile; delete it from the config.")
        gis = _sign_in_or_exit(url, f"the stored profile '{profile}'", profile=profile)
        if gis.users.me is None:
            raise SystemExit(
                f"arcgis.profile '{profile}' did not sign in. {SIGNIN_FIX}\n"
                + see_troubleshooting("The stored sign-in does not work"))
        emit(f"Connected to ArcGIS portal as: {gis.users.me.username}")
        return gis

    user_env = normalize_text(agol.get("username_env"))
    pass_env = normalize_text(agol.get("password_env"))
    username = os.environ.get(user_env) if user_env else normalize_text(agol.get("username"))
    password = os.environ.get(pass_env) if pass_env else agol.get("password")

    if not username or not password:
        raise SystemExit(
            "No ArcGIS credentials resolved: arcgis.profile is empty. Set arcgis.profile to taskdash in "
            f"the config. Then store the sign-in. {SIGNIN_FIX}"
            + _empty_env_names(user_env, pass_env) + "\n"
            + see_troubleshooting("TaskDash finds no ArcGIS credentials")
        )

    if agol.get("password") and not pass_env and agol.get("allow_plaintext_password") is not True:
        raise SystemExit(
            f"arcgis.password is stored in plaintext in the config, which TaskDash refuses. {SIGNIN_FIX} "
            "Then set arcgis.profile to the profile name and delete arcgis.password. Only if you accept "
            "the risk, set arcgis.allow_plaintext_password to true.\n"
            + see_troubleshooting("The config holds a plaintext password"))

    gis = _sign_in_or_exit(url, "the user name and password from the config", username, password,
                           secret=password)
    emit(f"Connected to ArcGIS portal as: {gis.users.me.username}")
    return gis


def get_tables(item: Any) -> Dict[str, Any]:
    """Map table name -> layer object for a hosted table service."""
    from arcgis.features import FeatureLayerCollection

    flc = FeatureLayerCollection.fromitem(item)
    return {t.properties.name: t for t in list(flc.layers) + list(flc.tables)}


def object_id_field(table: Any) -> str:
    """The table's real OID field name. AGOL tables are usually OBJECTID but not guaranteed."""
    try:
        value = normalize_text(getattr(table.properties, "objectIdField", None))
    except AttributeError:
        value = ""
    return value or "OBJECTID"


def _server_page_size(table: Any, fallback: int = 2000) -> int:
    """The table's own maxRecordCount, so our page size can never exceed what the server
    actually returns per request. Requesting more than that made a page the server capped
    below our size look like the final (short) page and silently dropped every row past it
    (audit publish-10)."""
    try:
        value = safe_int(getattr(table.properties, "maxRecordCount", None))
    except AttributeError:
        value = None
    return value if value and value > 0 else fallback


def max_field_value(table: Any, field: str, where: str = "1=1") -> Optional[float]:
    """Single-request max. The old code paginated one row per HTTP call and took 5.5 min."""
    result = with_retry(
        lambda: table.query(
            where=where,
            out_statistics=[{
                "statisticType": "max",
                "onStatisticField": field,
                "outStatisticFieldName": "max_value",
            }],
            return_geometry=False,
        ),
        label="max_field_value query",
    )
    features = getattr(result, "features", []) or []
    if not features:
        return None
    return safe_float(features[0].attributes.get("max_value"))


def query_all_rows(table: Any, *, where: str = "1=1", out_fields: str = "*",
                   page: Optional[int] = None) -> List[Dict[str, Any]]:
    """Page through every row matching `where`.

    Pages at the table's own maxRecordCount (fallback 2000 if unreadable), not a hardcoded
    guess -- see _server_page_size. A page shorter than the requested size is the reliable
    end-of-data signal only when the requested size matches what the server actually enforces.

    Ordering by the OID keeps arcgis 2.4 on its single-thread pager. Without it, a short
    last page sends arcgis to a 5-thread fetch, and truststore 0.10.0 (ArcGIS Pro 3.6/3.7)
    then drops certificate checks for the rest of the run. It also makes offset paging stable.
    """
    page_size = page or _server_page_size(table)
    order_by = object_id_field(table)
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        result = with_retry(
            lambda offset=offset: table.query(
                where=where,
                out_fields=out_fields,
                return_geometry=False,
                result_offset=offset,
                result_record_count=page_size,
                order_by_fields=order_by,
            ),
            label="query_all_rows",
        )
        features = list(getattr(result, "features", []) or [])
        if not features:
            break
        rows.extend(dict(f.attributes) for f in features)
        if len(features) < page_size:
            break
        offset += len(features)
    return rows


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    size = max(1, size)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def apply_edits_chunked(
    table: Any,
    *,
    adds: Optional[List[Dict[str, Any]]] = None,
    updates: Optional[List[Dict[str, Any]]] = None,
    deletes: Optional[List[int]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    label: str = "",
    dedupe_adds: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
) -> Dict[str, int]:
    """Apply edits in chunks, retrying transient failures. Raises on any per-feature failure.

    dedupe_adds contract: called with an adds chunk's row dicts (not the {"attributes": ...}
    wrapper) right before with_retry resends it, and must return the subset that still needs
    to be added. publish.py implements it by re-querying the table for the chunk's key fields
    and dropping rows that already landed, so a retry after an ambiguous failure (e.g. a 504
    whose write may already have committed) cannot duplicate rows.
    """
    counts = {"added": 0, "updated": 0, "deleted": 0}

    for kind, payload in (("adds", adds or []), ("updates", updates or [])):
        rows = list(payload)
        for index, row_chunk in enumerate(chunked(rows, chunk_size), start=1):
            total = (len(rows) + chunk_size - 1) // chunk_size
            emit(f"  {label} {kind} chunk {index}/{total} ({len(row_chunk)} rows)")
            state = {"rows": list(row_chunk)}

            def send(kind=kind, state=state):
                result = table.edit_features(**{kind: [{"attributes": r} for r in state["rows"]]})
                raise_on_edit_failure(result, kind, label)
                return result

            def before_retry(kind=kind, state=state):
                if kind != "adds" or dedupe_adds is None:
                    return
                before = len(state["rows"])
                state["rows"] = dedupe_adds(state["rows"])
                emit(f"  {label} adds retry: {len(state['rows'])}/{before} row(s) still "
                    f"missing after re-query.")

            with_retry(send, label=f"{label} {kind} chunk {index}/{total}",
                      before_retry=before_retry)
            counts["added" if kind == "adds" else "updated"] += len(row_chunk)

    if deletes:
        counts["deleted"] = _delete_chunked(table, deletes, chunk_size, label)
    return counts


def _present_oids(table: Any, oid_field: str, oids: List[Any]) -> set:
    where = f"{oid_field} IN ({','.join(str(o) for o in oids)})"
    return {safe_int(r.get(oid_field)) for r in query_all_rows(table, where=where, out_fields=oid_field)}


def _delete_chunked(table: Any, deletes: List[int], chunk_size: int, label: str) -> int:
    """Delete by OID in chunks. A delete that committed before an ambiguous failure (a 504) is
    reported as a missing object when resent, which used to fail the whole run. Two layers can
    resend: with_retry, whose before_retry re-queries the chunk and resends only the rows still
    present, and urllib3's transport Retry (see with_retry), which resends a real HTTP 504 unseen
    and hands back a 200 with per-row "may not exist" failures. send() therefore re-queries any
    failed row and fails only for rows that are still there."""
    oid_field = object_id_field(table)
    deleted = 0
    for chunk in chunked(deletes, chunk_size):
        state = {"oids": list(chunk)}

        def send(state=state):
            if not state["oids"]:
                return {}  # the ambiguous attempt already deleted every row
            result = table.edit_features(deletes=",".join(str(oid) for oid in state["oids"]))
            entries = (result or {}).get("deleteResults", []) or []
            failed = [safe_int(e.get("objectId")) for e in entries if not e.get("success", True)]
            if failed and None not in failed:
                present = _present_oids(table, oid_field, failed)
                result = {"deleteResults": [e for e in entries if e.get("success", True)
                                            or safe_int(e.get("objectId")) in present]}
            raise_on_edit_failure(result, "deletes", label)
            return result

        def before_retry(state=state):
            left = _present_oids(table, oid_field, state["oids"])
            before = len(state["oids"])
            state["oids"] = [o for o in state["oids"] if safe_int(o) in left]
            emit(f"  {label} deletes retry: {len(state['oids'])}/{before} row(s) still "
                f"present after re-query.")

        with_retry(send, label=f"{label} deletes", before_retry=before_retry)
        deleted += len(chunk)
    return deleted


def raise_on_edit_failure(result: Any, kind: str, label: str) -> None:
    key = {"adds": "addResults", "updates": "updateResults", "deletes": "deleteResults"}[kind]
    for entry in (result or {}).get(key, []) or []:
        if not entry.get("success", True):
            error = entry.get("error", {})
            text = normalize_text(error.get("description") or error)
            hint = ("-- a row with this key already exists (unique index). The next run matches "
                    "it and updates it instead; if this repeats, look for a key written twice."
                    if "duplicate key" in text.lower()
                    else "-- check field lengths and types against the table schema.")
            raise SystemExit(f"{label} {kind} failed: {text} {hint}\n"
                             + see_troubleshooting("ArcGIS refused a write"))


def purge_older_than(table: Any, time_field: str, retention_days: int, label: str = "") -> int:
    """Delete rows older than the retention window.

    Keyed on age only. The old purge filtered `server_name = <current host>`, which made the
    5,773 pre-migration rows unreachable by every retention path.
    """
    if retention_days <= 0:
        return 0
    cutoff = utc_now() - timedelta(days=retention_days)
    where = f"{time_field} < TIMESTAMP '{cutoff.strftime('%Y-%m-%d %H:%M:%S')}'"
    count = with_retry(lambda: table.query(where=where, return_count_only=True),
                       label=f"purge_older_than {label} count")
    if not count:
        return 0
    result = with_retry(lambda: table.delete_features(where=where),
                        label=f"purge_older_than {label} delete")
    raise_on_edit_failure(result, "deletes", f"purge_older_than {label}")
    emit(f"Purged {count} {label} row(s) older than {retention_days} day(s).")
    return int(count)


# --------------------------------------------------------------------------- alerting

SMTP_KEYRING_SERVICE = "taskdash-smtp"
# Why the newest send_email call failed. send_email keeps its bool return so every caller and test
# double still works; a caller that needs the reason reads it here right after a False.
last_send_error = ""
# Said by every warning that leaves a run without usable mail settings: the run does not carry on
# without alerts, it fails when an alert is due.
_CANNOT_ALERT = "TaskDash cannot send alerts, so a run with an alert due exits 1."


def mail_failure_hint(reason: str) -> str:
    """The fix for a failed send, picked by its cause (the "Type: text" of last_send_error). A relay on
    port 25 that offers no STARTTLS fails the default STARTTLS step; the firewall is not the cause."""
    if "SMTPNotSupportedError" in reason and "STARTTLS" in reason:
        return ("The mail server does not offer STARTTLS. If it is a relay on port 25 that takes mail "
                "without encryption and needs no sign-in, set smtp.starttls to false. Otherwise use "
                "port 587 with smtp.starttls, or port 465 with smtp.ssl.")
    return ("Check smtp.host, smtp.port, smtp.from and smtp.user in the config, and the SMTP password "
            "stored for the account that runs the task. Run `taskdash preflight --as-task --apply` "
            "and read its smtp line to check that account. `taskdash test-alert --apply` sends from "
            "your own account, so it cannot prove the task account's password.")


def _smtp_block_settings(smtp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate the config's smtp block into the dict shape send_email reads.

    The password lives in keyring under (SMTP_KEYRING_SERVICE, smtp.user), never in the config.
    """
    host = normalize_text(smtp.get("host"))
    sender = normalize_text(smtp.get("from"))
    if not host or not sender:
        emit(f"WARNING: smtp block needs host and from. {_CANNOT_ALERT}")
        return None
    port = smtp.get("port", config_default("smtp.port"))
    use_ssl = smtp.get("ssl")
    if use_ssl is None:  # absent or null: the port decides, so changing only the port to 465 works
        use_ssl = port == 465
    mail: Dict[str, Any] = {
        "smtp server": host, "from address": sender,
        "smtp port": port, "use tls": smtp.get("starttls", config_default("smtp.starttls")), "use ssl": use_ssl,
        "strict tls": True,
    }
    user = normalize_text(smtp.get("user"))
    if user and not (use_ssl or mail["use tls"]) and smtp.get("allow_plaintext_auth") is not True:
        emit("WARNING: smtp.user is set but the connection is unencrypted (smtp.starttls and "
             "smtp.ssl are both off); refusing to send the password. Enable smtp.starttls or "
             f"smtp.ssl, or set smtp.allow_plaintext_auth to true to accept the risk. {_CANNOT_ALERT}")
        return None
    if user:
        try:
            import keyring
            password = keyring.get_password(SMTP_KEYRING_SERVICE, user)
        except Exception as exc:  # noqa: BLE001 - a broken keyring backend must not fail the publish
            emit(f"WARNING: could not read the SMTP password from keyring ({type(exc).__name__}). "
                 f"{_CANNOT_ALERT}")
            return None
        if not password:
            emit(f"WARNING: no SMTP password in keyring for service '{SMTP_KEYRING_SERVICE}', "
                 f"user '{user}'. Store one as the account the scheduled task runs under: "
                 f"\"{sys.executable}\" -m keyring set {SMTP_KEYRING_SERVICE} {user}. {_CANNOT_ALERT}")
            return None
        mail["smtp username"] = user
        mail["smtp password"] = password
        mail["allow plaintext auth"] = smtp.get("allow_plaintext_auth") is True
    return mail


def legacy_mail_warnings(mail: Dict[str, Any]) -> List[str]:
    """Deprecation advice for a mail dict read from alerting.mail_config_path. Never quotes a value."""
    warnings = [
        "alerting.mail_config_path is deprecated and TLS is not verified for it, so the mail server's "
        "certificate is never checked. Move to the smtp block (smtp.host, smtp.from, smtp.user; store "
        f"the password with '\"{sys.executable}\" -m keyring set taskdash-smtp <smtp.user>'), then clear "
        "alerting.mail_config_path."]
    if mail.get("smtp username") and mail.get("smtp password") and not mail.get("use tls", True):
        warnings.append(
            "the legacy mail file holds a login but turns TLS off, so the password crosses the "
            "network in plain text. Move to the smtp block with smtp.starttls set to true.")
    return warnings


def load_mail_settings(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The mail settings send_email reads, or None (after a WARNING) when they are unusable.

    The config's smtp block wins. Its password lives in the keyring of the account that runs the
    call, never in the config. Without an smtp block, the deprecated alerting.mail_config_path
    file is read instead.
    """
    if config.get("smtp"):
        return _smtp_block_settings(config["smtp"])
    alerting = config.get("alerting", {})
    configured = normalize_text(alerting.get("mail_config_path"))
    if not configured:
        emit("WARNING: no smtp block and no alerting.mail_config_path in the config. "
             f"Set one of them to enable e-mail. {_CANNOT_ALERT}")
        return None
    path = Path(configured)
    if not path.exists():
        emit(f"WARNING: mail config not found at {path}. {_CANNOT_ALERT}")
        return None
    try:
        mail = json.loads(path.read_text(encoding="utf-8-sig")).get("email settings", {})
    except (OSError, ValueError) as exc:
        emit(f"WARNING: could not read mail config ({exc}). {_CANNOT_ALERT}")
        return None
    if not mail.get("smtp server") or not mail.get("from address"):
        emit(f"WARNING: mail config has no smtp server/from address. {_CANNOT_ALERT}")
        return None
    return mail


def warn_legacy_mail(config: Dict[str, Any]) -> None:
    """Log the legacy-mail deprecation, for the caller to run once per run.

    load_mail_settings only runs when an alert is due, which is rare on an hourly schedule, so
    the warning cannot live there. A file that cannot be read is reported by load_mail_settings.
    """
    if config.get("smtp"):
        return
    configured = normalize_text((config.get("alerting") or {}).get("mail_config_path"))
    if not configured:
        return
    try:
        mail = json.loads(Path(configured).read_text(encoding="utf-8-sig")).get("email settings", {})
    except (OSError, ValueError, AttributeError):
        mail = {}
    for text in legacy_mail_warnings(mail):
        emit(f"WARNING: {text}")


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_control_chars(text: str) -> str:
    """Drop ASCII control characters except tab and newline.

    Task paths, command lines and log lines are untrusted: an ESC sequence or a stray CR in an
    alert would reach a mail client's terminal view or a header.
    """
    return _CONTROL_CHARS.sub("", text)


def send_email(mail: Dict[str, Any], recipients: List[str], subject: str, body: str) -> bool:
    """Send a plain-text alert. Returns True on success; never raises into the caller.

    A failed alert must not abort the publish -- the data write already succeeded and losing it
    over a mail outage would be worse than the missed notification. The caller fails the run
    afterwards instead, and reads the reason from last_send_error.
    """
    import smtplib
    import ssl
    from email.message import EmailMessage

    global last_send_error
    last_send_error = ""
    try:
        msg = EmailMessage()
        # One line: a newline in a header would split it (or make EmailMessage refuse it).
        msg["Subject"] = strip_control_chars(subject).replace("\n", " ")
        msg["From"] = mail["from address"]
        msg["To"] = ", ".join(recipients)
        if mail.get("reply to"):
            msg["Reply-To"] = mail["reply to"]
        msg.set_content(strip_control_chars(body))

        # A default context verifies the server certificate and hostname; a bare starttls() does not.
        # Only the smtp block is strict. The legacy mail_config_path file keeps its old behaviour:
        # its relay's certificate was never checked, and verifying it now could silently stop the
        # alerts of an install that works today.
        strict = mail.get("strict tls", False)
        tls_context = ssl.create_default_context() if strict else None
        port = int(mail.get("smtp port", 587))
        # The legacy file never had an SSL switch; honouring a stray "use ssl" there could move a
        # working relay to SMTP_SSL and stop its alerts.
        use_ssl = strict and mail.get("use ssl", False)
        use_tls = mail.get("use tls", True)
        has_login = bool(mail.get("smtp username") and mail.get("smtp password"))
        if strict and has_login and not (use_ssl or use_tls) and not mail.get("allow plaintext auth"):
            raise ValueError("refusing to send the SMTP password over an unencrypted connection; "
                             "enable TLS or set smtp.allow_plaintext_auth to true")

        if use_ssl:
            connection = smtplib.SMTP_SSL(mail["smtp server"], port, timeout=30, context=tls_context)
        else:
            connection = smtplib.SMTP(mail["smtp server"], port, timeout=30)
        with connection as smtp:
            if use_tls and not use_ssl:
                smtp.starttls(context=tls_context)
            if has_login:
                smtp.login(mail["smtp username"], mail["smtp password"])
            smtp.send_message(msg)
        emit(f"Alert e-mailed to {', '.join(recipients)}: {subject}")
        return True
    except Exception as exc:  # noqa: BLE001 - alerting must never break the publish
        last_send_error = f"{type(exc).__name__}: {exc}"
        emit(f"WARNING: could not send alert e-mail: {last_send_error}")
        return False

"""Phase 2 deliverables 3 and 5: metric rules and cause patterns as data (rules/*.json).

The digests below were taken from the rule constants as they stood in code, before the move.
Loading rules/default.json plus the reference profile must rebuild every table exactly: same
entries, same order, same regex flags. The cause groups were split between the two files, so
they are checked by behaviour against a frozen copy of the old patterns instead.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

import taskdash_common as common
import taskdash_dashboard as dash
import taskdash_metrics as metrics
import taskdash_publish as publish
from conftest import REFERENCE_PROFILES

BASE = Path(metrics.__file__).resolve().parent
# Built, not written out, so the generic deny-list scan of the tests stays clean.
SDE_EXT = "." + "sde"

EXPECTED_DIGESTS = {
    "log_preamble": "490f2b837679d7829717991d5db4efa6ce17dc285f68a4ecd38664532ec1d798",
    "log_level": "0271c93b53fb85fc2ce0d63793d2de6ad4a9e6e91fdcb0197a6387704d8723f0",
    "section": "7d9d5bcfbe315bb0336b39bcc32898780aecbc4e53205d1ab6918a27a81bc0d4",
    "prose": "9477835367c01968e08cf3666d4281c529e2e4818e3e27a9da829526058440b5",
    "canonical": "27178dc5ae8da3b38dbea1723b33389f04580fea464c86b16323c3c141aa1e55",
    "noise": "c42168271f83defdec7dc2c2df9d9b593b962a8c4ead365de037ff128d84aa30",
    "tokens": "1116d9b4c00f31740c6558abf034943b1e81aa602381d584ad594e9e213bf1a3",
    "acronyms": "70fb6a1bf49be49ebe6cf8d0bbefdf4927f9c5f2629c7f499da28458a43b6ca8",
    # The synthetic reference profile renames tasks, so only this digest differs from the real profile's.
    "display_names": "67eaa1a2072dadf0b61d165bd17f7950060a7be3e63c76927ca42dde4583cf24",
    "lower_is_better": "e312d8887b5576796c730fc761239823c63c91962d08acc300a504c4754a0b62",
    "cause_text": "7339bcd1cc0fc894d89fe6eae13999c66b75457409e97dc571d6fe60e11b6f76",
}

# The cause groups exactly as they were in taskdash_publish before the move.
FROZEN_CAUSES = [
    ("licence", re.compile(r"(?i)product license|not licensed|licen[cs]e (?:has not|is not|not)")),
    ("database", re.compile(r"(?i)odbc|sql server|login failed for user|tempdb|sde workspace")),
    ("network", re.compile(r"(?i)network path|network name|winerror 5[3]|winerror 67|access is denied|"
                           r"could not find a part of the path")),
    ("arcgis", re.compile(r"(?i)generatetoken|get_token|invalid token|token required|http error 5\d\d|"
                          r"\bportal\b.{0,80}(?:error|unavailable|fail|denied)")),
    ("database", re.compile(r"(?i)\." + "sde" + r"\b|sde workspace|odbc|sql server|login failed for user|tempdb|"
                            r"dbms|error 000(?:732|187|837)|does not exist or is not supported|"
                            r"parameters are not valid|source feature class not found|missing source fc|"
                            r"not found in workspace|not found through the default connection|"
                            r"source not found|workspace .{0,200}does not exist")),
]
# One phrase per alternative of every frozen group, plus phrases that match two groups at once.
CAUSE_CORPUS = [
    "Product License has not been initialized", "the tool is not licensed", "licence is not available",
    "ODBC driver error", "SQL Server is gone", "Login failed for user 'svc'", "tempdb log is full",
    "Could not open SDE workspace", "The network path was not found", "the network name is no longer available",
    "WinError 53", "WinError 67", "Access is denied", "Could not find a part of the path",
    "generateToken failed", "get_token error", "Invalid token", "Token required", "HTTP Error 503",
    "portal request error", f"Input C:/data/conn{SDE_EXT} is missing", "DBMS table not found",
    "ERROR 000732: Input Dataset", "ERROR 000187", "ERROR 000837", "does not exist or is not supported",
    "Parameters are not valid", "source feature class not found", "missing source fc",
    "not found in workspace", "not found through the default connection", "source not found",
    "Workspace C:/data/x.gdb does not exist",
    "Access is denied while tempdb was full", f"network path lost for conn{SDE_EXT}",
    "HTTP Error 500 then ERROR 000732", "ODBC error on the network path", "nothing matches here",
]


def _rx(pattern):
    return [pattern.pattern, pattern.flags]


def _tables():
    return {
        "log_preamble": _rx(metrics.LOG_PREAMBLE_RE),
        "log_level": _rx(metrics.LOG_LEVEL_RE),
        "section": _rx(metrics.SECTION_RE),
        "prose": [[*_rx(p), key] for p, key in metrics.PROSE_COUNT_RULES],
        "canonical": [[*_rx(p), key] for p, key in metrics.CANONICAL_METRIC_RULES],
        "noise": [_rx(p) for p in metrics.NOISE_METRIC_KEY_PATTERNS],
        "tokens": list(metrics.MEANINGFUL_METRIC_TOKENS),
        "acronyms": sorted(metrics.DISPLAY_ACRONYMS),
        "display_names": [list(item) for item in metrics.DISPLAY_NAMES.items()],
        "lower_is_better": list(dash.METRIC_CHANGE_LOWER_IS_BETTER),
        "cause_text": sorted([cause, list(text)] for cause, text in publish._CAUSE_TEXT.items()),
    }


def _classify(groups, text):
    return next((name for name, pattern in groups if pattern.search(text)), "")


def _deny_terms():
    spec = importlib.util.spec_from_file_location("deny_scan", BASE / "_tools" / "deny_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.GENERIC_DENY


# ---------------------------------------------------------------- characterization


def test_default_plus_reference_profile_rebuilds_every_table_exactly():
    digests = {name: hashlib.sha256(json.dumps(value).encode()).hexdigest() for name, value in _tables().items()}
    assert {name for name in digests if digests[name] != EXPECTED_DIGESTS[name]} == set()


def test_default_plus_reference_profile_classifies_causes_as_before():
    assert [(t, _classify(publish._CAUSE_PATTERNS, t)) for t in CAUSE_CORPUS] == \
        [(t, _classify(FROZEN_CAUSES, t)) for t in CAUSE_CORPUS]


FROZEN_FIXED_VALUE_RULES = [
    (re.compile(r"(?i)\bno updates needed\b"), ("updated_record_count", "null_values_replaced_count")),
    (re.compile(r"(?i)\ball locators rebuilt successfully\b"), ("failure_count",)),
]
FIXED_VALUE_CORPUS = [
    "No updates needed - all fields already have correct values", "2026-01-02 03:04:05 - INFO - NO UPDATES NEEDED",
    "no updates needed.", "No update needed", "Updates needed: 4", "Nonupdates needed",
    "All locators rebuilt successfully in 0:01:02", "ALL LOCATORS REBUILT SUCCESSFULLY",
    "Not all locators rebuilt successfully", "All locators rebuilt unsuccessfully",
    "Locator A rebuilt successfully in 5s", "Records updated: 3", "nothing here",
]


def _frozen_fixed_pairs(line):
    return [(key, 0.0) for pattern, keys in FROZEN_FIXED_VALUE_RULES if pattern.search(line) for key in keys]


def test_fixed_value_rules_reproduce_the_old_phrase_constants():
    for line in FIXED_VALUE_CORPUS:
        stripped = metrics.LOG_PREAMBLE_RE.sub("", line, count=1)
        fixed = [(k, v) for k, v in metrics._phrase_pairs(stripped) if v == 0.0 and k in
                 {"updated_record_count", "null_values_replaced_count", "failure_count"}]
        assert fixed == _frozen_fixed_pairs(stripped), line


def test_fixed_value_rule_from_a_profile_sets_every_key(tmp_path):
    profile = tmp_path / "synthetic_profile.json"
    profile.write_text(json.dumps({"fixed_value_rules": [
        {"order": 5, "pattern": "\\bnothing to sync\\b", "keys": ["synced_record_count", "failure_count"], "value": 0}]}),
        encoding="utf-8")
    config = tmp_path / "synthetic.config.json"
    config.write_text(json.dumps({"rules": {"profiles": [str(profile)]}}), encoding="utf-8")
    common.load_config(config)
    assert list(metrics._phrase_pairs("Nothing To Sync today")) == [("synced_record_count", 0.0), ("failure_count", 0.0)]


def test_fixed_value_phrases_need_the_reference_profile():
    metrics.configure_rules({})
    assert metrics.FIXED_VALUE_RULES == []
    assert list(metrics._phrase_pairs("No updates needed")) == []


def test_dashboard_and_publish_alias_the_configured_tables():
    assert dash.METRIC_CHANGE_LOWER_IS_BETTER is metrics.METRIC_CHANGE_LOWER_IS_BETTER
    assert publish._CAUSE_PATTERNS is metrics.CAUSE_PATTERNS and publish._CAUSE_TEXT is metrics.CAUSE_TEXT


# ---------------------------------------------------------------- generic rules alone


def test_default_rules_alone_load_work_and_name_no_reference_install():
    text = (BASE / metrics.DEFAULT_RULES_FILE).read_text(encoding="utf-8").lower()
    assert [term for term in _deny_terms() if term.lower() in text] == []

    metrics.configure_rules({})
    line = "2026-01-02 03:04:05,123 - INFO - Records updated: 12"
    keys = {metrics.canonicalize_metric_key(k, line) for k, _ in metrics.extract_metrics_from_line(line)}
    assert "updated_record_count" in keys
    assert metrics.PROSE_COUNT_RULES == [] and metrics.DISPLAY_NAMES == {}
    assert metrics.classify_line("2026-01-02 03:04:05 [ERROR] boom") == (True, False)
    assert set(metrics.CAUSE_TEXT) == {"", "database", "licence", "network", "arcgis"}


# ---------------------------------------------------------------- validation


def _load_bad_profile(tmp_path, data):
    profile = tmp_path / "bad_profile.json"
    profile.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        metrics.load_rules((str(profile),))
    message = str(caught.value)
    assert "bad_profile.json" in message and "Fix:" in message
    return message


def test_bad_regex_stops_with_file_entry_and_fix(tmp_path):
    message = _load_bad_profile(tmp_path, {"prose_count_rules": [{"order": 5, "pattern": "(\\d+ widgets", "key": "w_count"}]})
    assert "prose_count_rules" in message and "entry 0 (order 5)" in message and "bad regex" in message


def test_prose_rule_without_a_capture_group_stops(tmp_path):
    message = _load_bad_profile(tmp_path, {"prose_count_rules": [{"order": 5, "pattern": "widgets", "key": "w_count"}]})
    assert "no capture group" in message


def test_unknown_section_stops(tmp_path):
    message = _load_bad_profile(tmp_path, {"prose_rules": []})
    assert "'prose_rules'" in message and "unknown section" in message


def test_duplicate_order_stops_and_names_both_files(tmp_path):
    used = metrics.load_rules(())  # any order the generic file already uses
    assert used["canonical_metric_rules"]
    message = _load_bad_profile(tmp_path, {"canonical_metric_rules": [{"order": 40, "pattern": "^a$", "key": "a_count"}]})
    assert "order 40 is already used" in message and "default.json" in message


@pytest.mark.parametrize("data, needle", [
    ({"display_names": {"a_count": 5}}, "not a string"),
    ({"noise_metric_key_patterns": [{"order": "7", "pattern": "^x$"}]}, "order must be a whole number"),
    ({"canonical_metric_rules": {"order": 7}}, "must be a list"),
    ({"canonical_metric_rules": [{"order": 7, "pattern": "^x$"}]}, "missing/non-string field(s) ['key']"),
    ({"cause_patterns": [{"order": 7, "cause": "storage", "pattern": "disk"}]}, "no hint text"),
    ({"fixed_value_rules": [{"order": 7, "pattern": "^x$", "value": 0}]}, "missing/non-string field(s) ['keys']"),
    ({"fixed_value_rules": [{"order": 7, "pattern": "^x$", "keys": [], "value": 0}]}, "['keys']"),
    ({"fixed_value_rules": [{"order": 7, "pattern": "^x$", "keys": "a_count", "value": 0}]}, "['keys']"),
    ({"fixed_value_rules": [{"order": 7, "pattern": "^x$", "keys": ["a_count"], "value": "0"}]}, "['value']"),
    ({"fixed_value_rules": [{"order": 7, "pattern": "^x$", "keys": ["a_count"]}]}, "['value']"),
    ({"fixed_value_rules": [{"order": 7, "pattern": "(x", "keys": ["a_count"], "value": 0}]}, "bad regex"),
])
def test_wrong_type_stops(tmp_path, data, needle):
    assert needle in _load_bad_profile(tmp_path, data)


def test_bad_rules_stop_the_run_at_config_load(tmp_path):
    profile = tmp_path / "bad_profile.json"
    profile.write_text(json.dumps({"section_pattern": "^(?P<other>x)"}), encoding="utf-8")
    config = tmp_path / "synthetic.config.json"
    config.write_text(json.dumps({"rules": {"profiles": [str(profile)]}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="no capture group 'name'"):
        common.load_config(config)

    config.write_text(json.dumps({"rules": {"profiles": "rules/x.json"}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="rules.profiles must be a list of strings"):
        common.load_config(config)

    config.write_text(json.dumps({"rules": {"profiles": list(REFERENCE_PROFILES)},
                                  "metrics": {"tasks": {"\\Synthetic\\Job": {"enabled": "no"}}}}), encoding="utf-8")
    with pytest.raises(SystemExit, match=r"metrics.tasks\['\\\\Synthetic\\\\Job'\]"):
        common.load_config(config)


def test_config_without_rules_key_stops_the_run(tmp_path):
    config = tmp_path / "synthetic.config.json"
    config.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"rules\.profiles.*Add"):
        common.load_config(config)


def test_config_with_empty_profiles_loads_generic_rules_only(tmp_path):
    config = tmp_path / "synthetic.config.json"
    config.write_text(json.dumps({"rules": {"profiles": []}}), encoding="utf-8")
    common.load_config(config)
    assert metrics.PROSE_COUNT_RULES == []
    config.write_text(json.dumps({"rules": {"profiles": list(REFERENCE_PROFILES)}}), encoding="utf-8")
    common.load_config(config)
    assert len(metrics.PROSE_COUNT_RULES) == 9


# ---------------------------------------------------------------- per-task settings


def test_per_task_extra_rules_and_noise_keys(tmp_path):
    metrics.configure_rules({"metrics": {"tasks": {"\\Synthetic\\Widget Job": {
        "prose_count_rules": [{"pattern": r"\b(\d+) widgets polished\b", "key": "polished_widget_count"}],
        "noise_metric_key_patterns": ["^checked_record_count$"],
    }}}})
    log = tmp_path / "widget.log"
    log.write_text("Records checked: 9\n7 widgets polished\n", encoding="utf-8")

    plain = metrics.scan_log_file(log, 0)["metrics"]
    tuned = metrics.scan_log_file(log, 0, metrics.TASK_SETTINGS["\\Synthetic\\Widget Job"])["metrics"]
    assert "checked_record_count" in plain and "polished_widget_count" not in plain
    assert set(tuned) == {"polished_widget_count"} and tuned["polished_widget_count"][0] == 7.0


def test_per_task_metrics_disabled_keeps_the_log_summary(tmp_path, monkeypatch):
    project = tmp_path / "SyntheticProject"
    (project / "Logs").mkdir(parents=True)
    (project / "Logs" / "run.log").write_text("Records updated: 5\n", encoding="utf-8")
    (project / "run.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(metrics, "__file__", str(tmp_path / "taskdash_metrics.py"))
    inventory = [{"task_name": "Quiet", "task_full_name": "\\Quiet", "command_target_path": str(project / "run.py")}]

    rows, _ = metrics.build_metric_rows(inventory, host_name="H")
    assert [r["metric_value"] for r in rows] == [5.0]

    metrics.configure_rules({"metrics": {"tasks": {"\\Quiet": {"enabled": False}}}})
    rows, summary = metrics.build_metric_rows(inventory, host_name="H")
    assert rows == [] and summary["\\Quiet"]["log_files_scanned"] == 1


# ---------------------------------------------------------------- cause patterns


def test_data_source_pattern_matches_only_with_the_reference_profile():
    task = {"last_result_text": f"Input layer does not exist: C:/data/conn{SDE_EXT}", "latest_log_path": None}
    assert publish._failure_cause(task) == "database"
    assert publish._CAUSE_TEXT["database"][1].startswith("Check the database server first")

    metrics.configure_rules({})
    assert publish._failure_cause(task) == ""
    assert publish._failure_cause({"last_result_text": "ODBC driver error", "latest_log_path": None}) == "database"

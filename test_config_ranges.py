"""Config load checks the scheduler regexes and the number ranges, so a bad value stops the run at
once with the key named, not mid-publish with a bare traceback. Nothing here touches the network."""

from __future__ import annotations

import json

import pytest

import taskdash_collect as collect
import taskdash_common as common
import taskdash_dashboard as dash


def _load(tmp_path, **sections):
    path = tmp_path / "TaskDash.config.json"
    path.write_text(json.dumps({"rules": {"profiles": []}, **sections}), encoding="utf-8")
    return common.load_config(path)


def _problem(tmp_path, **sections):
    with pytest.raises(SystemExit) as caught:
        _load(tmp_path, **sections)
    return str(caught.value)


# ------------------------------------------------------------------ regexes

@pytest.mark.parametrize("key", ["task_name_regex", "task_path_regex", "action_regex",
                                 "exclude_task_name_regex"])
def test_a_bad_scheduler_regex_names_the_key_and_the_backslash_rule(tmp_path, key):
    # The documented folder style \Nightly\ typed as a JSON string "\\Nightly\\" is a bad regex.
    message = _problem(tmp_path, scheduler={key: "\\Nightly\\"})
    assert f"scheduler.{key} is not a valid regular expression" in message
    assert "write each backslash twice" in message


def test_the_escaped_folder_regex_loads_and_matches_the_folder(tmp_path):
    config = _load(tmp_path, scheduler={"task_path_regex": "^\\\\Nightly\\\\$"})
    assert collect.should_include_task({"task_name": "A", "task_path": "\\Nightly\\"}, config["scheduler"])
    assert not collect.should_include_task({"task_name": "A", "task_path": "\\Other\\"}, config["scheduler"])


def test_an_empty_or_good_regex_loads(tmp_path):
    _load(tmp_path, scheduler={"task_name_regex": "", "action_regex": "(?i)python", "exclude_task_name_regex": "^A"})


# ------------------------------------------------------------------ number ranges

BAD_VALUES = [
    ("upload", "chunk_size", 0), ("upload", "chunk_size", 2.5),
    ("alerting", "heartbeat_hour", 24), ("alerting", "heartbeat_hour", -1),
    ("scheduler", "collector_interval_minutes", 0), ("scheduler", "collector_interval_minutes", -5),
    ("scheduler", "overdue_grace_minutes", -1), ("scheduler", "overdue_grace_minutes", 10 ** 9),
    ("scheduler", "heartbeat_ignore_after_days", 0),
    ("history", "max_events_per_run", 0), ("history", "days_back", 0),
    ("metrics", "max_log_files_per_task", 0), ("smtp", "port", 70000), ("smtp", "port", 0),
    ("alerting", "realert_hours", -1), ("alerting", "incident_digest_hours", -1),
    ("alerting", "shared_cause_min_tasks", -1),
]


@pytest.mark.parametrize(("section", "key", "value"), BAD_VALUES)
def test_an_out_of_range_number_is_refused_with_the_key_and_the_range(tmp_path, section, key, value):
    message = _problem(tmp_path, **{section: {key: value}})
    assert f"{section}.{key} must be a" in message and f"got {value!r}" in message


def test_powershell_timeout_must_be_above_zero(tmp_path):
    assert "powershell_timeout_seconds must be a number above 0, got 0" in _problem(tmp_path, powershell_timeout_seconds=0)


GOOD_VALUES = [
    ("upload", "chunk_size", 1), ("alerting", "heartbeat_hour", 0), ("alerting", "heartbeat_hour", 23),
    ("scheduler", "collector_interval_minutes", 0.5), ("scheduler", "overdue_grace_minutes", 0),
    ("alerting", "shared_cause_min_tasks", 0), ("alerting", "incident_digest_hours", 0),
    ("smtp", "port", 587),
    # Zero is a documented setting for these, so no range rule applies.
    ("retention", "runs_days", 0), ("logging", "max_log_age_days", 0), ("metrics", "max_lines_per_file", 0),
]


@pytest.mark.parametrize(("section", "key", "value"), GOOD_VALUES)
def test_an_in_range_number_loads(tmp_path, section, key, value):
    assert _load(tmp_path, **{section: {key: value}})[section][key] == value


def test_every_range_rule_names_a_known_number_key():
    assert set(common._CONFIG_RANGES) <= common._CONFIG_NUMBER_KEYS


def test_a_non_finite_number_still_gets_the_finite_message_only(tmp_path):
    path = tmp_path / "TaskDash.config.json"
    path.write_text('{"rules": {"profiles": []}, "upload": {"chunk_size": NaN}}', encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        common.load_config(path)
    assert str(caught.value).count("upload.chunk_size") == 1 and "a finite number" in str(caught.value)


# ------------------------------------------------------------------ the dashboard shares the rule

@pytest.mark.parametrize("key", ["collector_interval_minutes", "heartbeat_ignore_after_days"])
@pytest.mark.parametrize("value", [0, -1, True, "7", float("inf")])
def test_the_dashboard_help_terms_use_the_shared_rule(key, value):
    with pytest.raises(SystemExit, match=f"scheduler.{key} must be a number above 0"):
        dash.help_terms({"scheduler": {key: value}})

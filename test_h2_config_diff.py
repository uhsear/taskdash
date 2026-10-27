"""H2-7: `taskdash preflight --config-diff` reports unknown, absent and renamed config keys.

Every file lives under tmp_path. Nothing signs in and the diff never loads the rules files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import taskdash_common as common
import taskdash_preflight as pf

SECRET = "hunter2-not-a-real-secret"


def _run(tmp_path, config, capsys):
    path = tmp_path / "TaskDash.config.json"
    path.write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
    code = pf.main(["--config-diff", "--config", str(path)])
    return code, capsys.readouterr().out


def _section(out, title):
    """The lines under one heading, up to the next heading."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title)) + 1
    body = []
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        body.append(line.strip())
    return body


def test_unknown_key_is_reported(tmp_path, capsys):
    code, out = _run(tmp_path, {"scheduler": {"overdue_grace_minutess": 5}, "hostname_override": "x"}, capsys)
    assert code == 0
    unknown = _section(out, "Unknown keys")
    assert "scheduler.overdue_grace_minutess" in unknown and "hostname_override" in unknown


def test_absent_key_is_reported_with_its_default(tmp_path, capsys):
    code, out = _run(tmp_path, {"history": {"days_back": 7}}, capsys)
    assert code == 0
    absent = _section(out, "Absent keys")
    assert "history.max_events_per_run (default: 5000)" in absent
    assert "scheduler.collector_interval_minutes (default: 60)" in absent
    assert not any(line.startswith("history.days_back") for line in absent)


def test_absent_key_shows_the_code_default_not_the_example_placeholder(tmp_path, capsys):
    # The shipped example holds placeholders (heartbeat_hour 7, a sample host name) that differ from
    # the code default (no daily e-mail), so the default comes from the code, never the example.
    _, out = _run(tmp_path, {}, capsys)
    absent = _section(out, "Absent keys")
    assert "alerting.heartbeat_hour (default: none)" in absent
    assert "host_name_override (default: empty)" in absent
    assert "TASK-HOST-01" not in out and "example.com" not in out


def test_every_known_key_has_a_default_except_the_secret_named_ones():
    known = set(common.config_diff({})["absent"])
    assert known - set(pf.CONFIG_DEFAULTS) == {"arcgis.password", "arcgis.password_env",
                                               "arcgis.allow_plaintext_password"}
    assert set(pf.CONFIG_DEFAULTS) <= known


def test_each_default_matches_the_default_column_of_configure_md():
    # The map repeats literals the code reads in many places; the guide is what an admin reads.
    guide = (Path(__file__).parent / "docs" / "CONFIGURE.md").read_text(encoding="utf-8")
    cells = {m.group(1): m.group(2).replace("`", "")
             for m in re.finditer(r"^\| `([\w.]+)` \| [^|]+ \| ([^|]+) \|", guide, re.M)}
    for key, default in pf.CONFIG_DEFAULTS.items():
        assert cells[key] == str(default), key


def test_renamed_key_is_reported_with_the_new_name(tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(common.RENAMED_KEYS, "scheduler.old_name", "scheduler.task_name_regex")
    code, out = _run(tmp_path, {"scheduler": {"old_name": "x"}}, capsys)
    assert code == 0
    assert "scheduler.old_name -> scheduler.task_name_regex" in _section(out, "Renamed keys")
    assert "scheduler.old_name" not in _section(out, "Unknown keys")
    assert not any(line.startswith("scheduler.task_name_regex") for line in _section(out, "Absent keys"))


def test_secret_named_values_never_print(tmp_path, capsys):
    config = {"arcgis": {"password": SECRET, "passwrd_env": SECRET}, "smtp": {"token": SECRET}}
    code, out = _run(tmp_path, config, capsys)
    assert code == 0
    assert SECRET not in out
    assert {"arcgis.passwrd_env", "smtp.token"} <= set(_section(out, "Unknown keys"))


def test_absent_secret_named_key_shows_no_default(tmp_path, capsys):
    _, out = _run(tmp_path, {}, capsys)
    absent = _section(out, "Absent keys")
    assert "arcgis.password_env" in absent
    assert not any("password" in line and "default" in line for line in absent)


def test_unreadable_config_exits_1(tmp_path, capsys):
    code, out = _run(tmp_path, "{not json", capsys)
    assert code == 1 and "not valid JSON" in out


def test_missing_config_exits_1(tmp_path, capsys):
    code = pf.main(["--config-diff", "--config", str(tmp_path / "nope.json")])
    assert code == 1 and "Config not found" in capsys.readouterr().out


def test_config_diff_rejects_apply():
    with pytest.raises(SystemExit):
        pf.main(["--config-diff", "--apply"])


def test_a_complete_config_says_none_under_absent_keys(tmp_path, capsys):
    config: dict = {}
    for dotted in common.config_diff({})["absent"]:
        *sections, key = dotted.split(".")
        node = config
        for section in sections:
            node = node.setdefault(section, {})
        node[key] = ""
    code, out = _run(tmp_path, config, capsys)
    assert code == 0 and _section(out, "Absent keys") == ["none"]

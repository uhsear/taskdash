"""H2-1: a rollback must keep working, and the upgrade runbook must name real commands.

No network: AGOL, the collector's PowerShell and e-mail are all faked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import taskdash_cli as cli
import taskdash_collect as collect
import taskdash_publish as publish
from test_taskdash import _task

ROOT = Path(__file__).resolve().parent
UPGRADE = ROOT / "docs" / "UPGRADE.md"


def _args():
    args = publish.parse_args(["--apply"])
    args.target = "prod"
    return args


@pytest.fixture
def world(monkeypatch):
    """Real _run_publish over one synthetic task; the service stamp is set per test."""
    seen = {"published": [], "rows": [], "item": None}

    def make_item(stamp):
        properties = {} if stamp is None else {"taskdash_schema_version": stamp}
        seen["item"] = type("I", (), {"title": "x", "id": "svc", "access": "private",
                                      "properties": properties})()

    monkeypatch.setattr(collect, "collect_tasks", lambda timeout, *_: [_task()])
    monkeypatch.setattr(collect, "collect_run_events", lambda *a, **k: [])
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: seen["item"])
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "check_release_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "publish",
                        lambda gis, i, collected, *a, **k: seen["published"].append(collected) or {})
    monkeypatch.setattr(publish, "maybe_send_alert", lambda collected, *a: True)
    monkeypatch.setattr(publish, "_write_collector_run_row",
                        lambda i, cfg, collected, **kw: seen["rows"].append(kw))
    seen["make_item"] = make_item
    return seen


def _run(tmp_path):
    return publish._run_publish(_args(), {"logging": {"log_dir": str(tmp_path)}}, tmp_path, publish.utc_now(),
                                {"armed": False, "recorded": False})


def _newer_warnings(capsys):
    text = capsys.readouterr().out
    return [line for line in text.splitlines() if "newer than this TaskDash" in line]


def test_a_newer_service_stamp_warns_once_and_publishing_continues(world, tmp_path, capsys):
    world["make_item"](publish.SCHEMA_VERSION + 1)
    assert _run(tmp_path) == 0
    (line,) = _newer_warnings(capsys)
    assert line.startswith("WARNING:")
    assert f"schema {publish.SCHEMA_VERSION + 1}" in line and f"schema {publish.SCHEMA_VERSION}" in line
    assert "publishing continues" in line and "new fields stay empty" in line
    assert len(world["published"]) == 1, "a rollback must still publish"


@pytest.mark.parametrize("stamp", [None, publish.SCHEMA_VERSION, publish.SCHEMA_VERSION - 1])
def test_an_equal_older_or_missing_stamp_does_not_warn(world, tmp_path, capsys, stamp):
    world["make_item"](stamp)
    assert _run(tmp_path) == 0
    assert _newer_warnings(capsys) == []


def test_the_stamping_path_still_refuses_a_newer_stamp():
    item = type("I", (), {"id": "svc", "properties": {"taskdash_schema_version": publish.SCHEMA_VERSION + 1}})()
    with pytest.raises(SystemExit, match="Upgrade TaskDash"):
        publish.stamp_service_version(item)


# ---------------------------------------------------------------- UPGRADE.md names real commands

# The arguments end at a backtick or the end of the line, so one `taskdash ...` span never
# lends its flags to the next one.
_COMMAND = re.compile(r"(?:taskdash(?:\.exe)?|\$td)[ \t]+(?P<command>[a-z][a-z-]*)(?P<rest>(?:[ \t]+[^\s`]+)*)")


def _help_flags(command, capsys):
    capsys.readouterr()
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
    return set(re.findall(r"--[a-z][a-z-]*", capsys.readouterr().out))


def _documented_invocations(text):
    """(command, flag) pairs from every `taskdash <command> ... --flag` pattern in the guide."""
    pairs = set()
    for match in _COMMAND.finditer(text):
        flags = re.findall(r"--[a-z][a-z-]*", match.group("rest"))
        pairs.update((match.group("command"), flag) for flag in flags)
        if not flags:
            pairs.add((match.group("command"), None))
    return pairs


def test_every_command_and_flag_in_the_upgrade_guide_exists(capsys):
    pairs = _documented_invocations(UPGRADE.read_text(encoding="utf-8"))
    commands = {command for command, _ in pairs}
    assert {"collect", "dashboard", "preflight", "status"} <= commands, "the guide lost a step"
    known = set(cli.SUBCOMMAND_MODULES) | {"status", "test-alert"}
    assert commands <= known, f"not a taskdash command: {sorted(commands - known)}"
    help_flags = {command: _help_flags(command, capsys) for command in commands}
    for command, flag in sorted(pairs, key=str):
        if flag is not None:
            assert flag in help_flags[command], f"taskdash {command} has no {flag}"


def test_the_extractor_rejects_a_flag_that_does_not_exist(capsys):
    pairs = _documented_invocations("Run `taskdash collect --no-such-flag` now.")
    assert pairs == {("collect", "--no-such-flag")}
    assert "--no-such-flag" not in _help_flags("collect", capsys)


def test_the_upgrade_guide_is_linked_from_the_readme_and_the_install_guide():
    for path in (ROOT / "README.md", ROOT / "docs" / "INSTALL.md"):
        assert "UPGRADE.md" in path.read_text(encoding="utf-8"), path.name

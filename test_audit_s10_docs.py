"""Audit lane s10 (upgrade, move and remove): checks that keep the lifecycle guides honest. Text only;
nothing here runs a command, signs in or touches a task."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"


def _headings():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return set(re.findall(r"^## \[([^\]]+)\]", text, re.M))


def test_every_release_a_guide_names_has_a_changelog_heading():
    # "Release 1.4 adds MetricChange" sent admins to a release that does not exist; it shipped in 1.2.0.
    headings = _headings()
    named = {}
    for guide in sorted(DOCS.glob("*.md")):
        for match in re.finditer(r"\b[Rr]elease (\d+\.\d+(?:\.\d+)?)\b", guide.read_text(encoding="utf-8")):
            named.setdefault(match.group(1), guide.name)
    missing = {version: guide for version, guide in named.items() if version not in headings}
    assert not missing, f"guides name releases that CHANGELOG.md has no heading for: {missing}"


def test_upgrade_numbers_the_dashboard_rebuild_after_the_first_run():
    upgrade = (DOCS / "UPGRADE.md").read_text(encoding="utf-8")
    steps = dict(re.findall(r"^(\d+)\. (.*)$", upgrade.split("## Roll back")[0], re.M))
    run, rebuild = [n for n, text in steps.items() if text.startswith("In the administrator window, enable")], \
                   [n for n, text in steps.items() if text.startswith("Rebuild the dashboard")]
    assert run and rebuild and int(rebuild[0]) > int(run[0])


def test_the_task_command_names_the_scripts_and_changes_nothing(capsys, monkeypatch, tmp_path):
    import taskdash_cli as cli
    import taskdash_common as common

    monkeypatch.setattr(common, "data_dir", lambda: tmp_path)
    assert cli.main(["task"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "install_task.ps1") in out and "Install-TaskDash.ps1" in out
    assert "signin --forget" in out and "UPGRADE.md" in out
    assert "task" in cli.USAGE
    assert list(tmp_path.iterdir()) == []

"""H4-10: a live dry-run collect on a CI runner reads the odd tasks the workflow registered.

Opt-in. .github/workflows/matrix.yml registers the tasks with tests/ci/seed_tasks.ps1 and sets
TASKDASH_LIVE_SEEDED to the JSON manifest that script writes. Without the variable the live tests
skip, so the default suite never reads the real task list. check_seeded is the same code the live
tests run, and the default tests feed it fake rows. Nothing here signs in: a dry run never connects.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import taskdash_collect as collect
import taskdash_common as common

common.disable_arcpy_import()

import taskdash_publish as publish  # noqa: E402

MANIFEST = os.environ.get("TASKDASH_LIVE_SEEDED", "")
live = pytest.mark.skipif(not MANIFEST, reason="opt-in: TASKDASH_LIVE_SEEDED names the seed manifest (CI only)")


def missing_manifest(env):
    """Why a CI environment leg has no seed manifest, or "" when the live tests may skip.

    Only the environment legs set TASKDASH_EXPECT_*. On such a leg a lost GITHUB_ENV write would
    otherwise turn both live tests into skips and leave the leg green, as test_h4_ci_legs.py avoids.
    """
    named = [n for n in ("TASKDASH_EXPECT_LANGUAGE_MODE", "TASKDASH_EXPECT_CULTURE") if env.get(n)]
    if not named or env.get("TASKDASH_LIVE_SEEDED"):
        return ""
    return (f"{' and '.join(named)} name a CI environment leg, but TASKDASH_LIVE_SEEDED is empty, so the live "
            f"dry-run collect would silently skip. Check that the 'Register the seeded tasks' step wrote "
            f"TASKDASH_LIVE_SEEDED to GITHUB_ENV.")


def test_a_ci_leg_without_a_seed_manifest_fails_instead_of_skipping():
    assert not missing_manifest(os.environ), missing_manifest(os.environ)


def test_missing_manifest_names_a_leg_that_would_skip_vacuously():
    assert "TASKDASH_LIVE_SEEDED" in missing_manifest({"TASKDASH_EXPECT_CULTURE": "de-DE"})
    assert "TASKDASH_EXPECT_LANGUAGE_MODE" in missing_manifest({"TASKDASH_EXPECT_LANGUAGE_MODE": "FullLanguage"})
    assert missing_manifest({"TASKDASH_EXPECT_CULTURE": "de-DE", "TASKDASH_LIVE_SEEDED": "m.json"}) == ""
    assert missing_manifest({}) == ""
    assert missing_manifest({"TASKDASH_EXPECT_CULTURE": ""}) == ""


def load_manifest():
    cases = json.loads(Path(MANIFEST).read_text(encoding="utf-8-sig"))
    assert cases, "the seed manifest is empty, so this test would prove nothing"
    return cases


def _has_month_day(row, day):
    details = row.get("trigger_details") or []
    if isinstance(details, dict):
        details = [details]
    return any(str(t.get("days_of_month")) == str(day) for t in details)


def check_seeded(rows, cases):
    """Problems found comparing the collected task rows with the registered cases, as text."""
    problems = []
    by_full = {r.get("task_full_name"): r for r in rows}
    for case in cases:
        full = case["path"] + case["name"]
        row = by_full.get(full)
        if row is None:
            problems.append(f"missing: {full!r}")
        elif (row.get("task_name"), row.get("task_path")) != (case["name"], case["path"]):
            problems.append(f"altered: {full!r} came back as {row.get('task_path')!r} {row.get('task_name')!r}")
        elif not collect.should_include_task(row, {}):
            problems.append(f"filtered out by the default scheduler settings: {full!r}")
        elif case.get("days") and not _has_month_day(row, case["days"]):
            problems.append(f"wrong monthly days: {full!r} should fire on day {case['days']}, "
                            f"read {row.get('trigger_details')!r}")
    microsoft = [r["task_full_name"] for r in rows if r.get("task_path", "").lower().startswith("\\microsoft\\")]
    if microsoft:
        problems.append(f"{len(microsoft)} task(s) under \\Microsoft\\ were not excluded")
    return problems


# ------------------------------------------------------------------ the check itself (always runs)

CASES = [{"path": "\\Probe\\", "name": "Café [1] probe"}, {"path": "\\MicrosoftSync\\", "name": "Prefix probe"}]


def _row(case, **changes):
    row = {"task_full_name": case["path"] + case["name"], "task_name": case["name"], "task_path": case["path"]}
    return {**row, **changes}


def test_check_seeded_accepts_every_case_read_back_intact():
    assert check_seeded([_row(c) for c in CASES], CASES) == []


def test_check_seeded_names_a_missing_case():
    problems = check_seeded([_row(CASES[0])], CASES)
    assert len(problems) == 1 and problems[0].startswith("missing:") and "Prefix probe" in problems[0]


def test_check_seeded_names_a_garbled_name():
    garbled = _row(CASES[0], task_name="Caf? [1] probe")
    problems = check_seeded([garbled, _row(CASES[1])], CASES)
    assert len(problems) == 1 and problems[0].startswith("altered:")


def test_check_seeded_names_monthly_days_read_from_the_wrong_task():
    case = {"path": "\\Probe\\", "name": "Monthly [1] probe", "days": 15}
    right = _row(case, trigger_details=[{"days_of_month": "15"}])
    wrong = _row(case, trigger_details=[{"days_of_month": "3"}])
    assert check_seeded([right], [case]) == []
    problems = check_seeded([wrong], [case])
    assert len(problems) == 1 and problems[0].startswith("wrong monthly days:")


def test_check_seeded_flags_a_microsoft_task_that_was_not_excluded():
    leaked = {"task_full_name": "\\Microsoft\\Windows\\X", "task_name": "X", "task_path": "\\Microsoft\\Windows\\"}
    problems = check_seeded([*[_row(c) for c in CASES], leaked], CASES)
    assert problems == ["1 task(s) under \\Microsoft\\ were not excluded"]


# ------------------------------------------------------------------ the runner (opt-in)

@live
def test_the_live_task_read_returns_every_seeded_task_intact():
    collect.unreadable_tasks.clear()
    rows = collect.collect_tasks(180, True)
    cases = load_manifest()
    assert check_seeded(rows, cases) == []
    names = [case["name"] for case in cases]
    assert not [u for u in collect.unreadable_tasks if any(n in u for n in names)], collect.unreadable_tasks


@live
def test_the_live_dry_run_collect_exits_zero_and_keeps_the_seeded_tasks(tmp_path, capsys):
    config = tmp_path / "TaskDash.config.json"
    config.write_text(json.dumps({"logging": {"log_dir": str(tmp_path / "Logs")}, "rules": {"profiles": []}}),
                      encoding="utf-8")
    assert publish.main(["--config", str(config)]) == 0
    kept = re.search(r"Tasks after filtering: (\d+)", capsys.readouterr().out)
    assert kept and int(kept.group(1)) >= len(load_manifest())

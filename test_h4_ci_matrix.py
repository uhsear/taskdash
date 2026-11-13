"""H4-10: static checks of .github/workflows/matrix.yml and tests/ci/seed_tasks.ps1.

The workflow cannot run here, so these tests parse it (PyYAML, installed by the workflow's own pip
step) and assert the properties that make the matrix prove something: supported runner labels, the
legs the roadmap names, the cleanup that always runs, and the same pinning rules as the other
workflows. They never start the workflow and never register a task.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT / ".github" / "workflows" / "matrix.yml"
SEED = ROOT / "tests" / "ci" / "seed_tasks.ps1"
POWERSHELL = shutil.which("powershell.exe")
SUPPORTED_RUNNERS = {"windows-2022", "windows-2025"}


@pytest.fixture(scope="module")
def doc():
    # On CI a missing PyYAML is a failure, not a skip: a skip there would hide every structural check.
    if os.environ.get("CI"):
        import yaml
    else:
        yaml = pytest.importorskip("yaml", reason="PyYAML is not installed; CI installs it")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_every_workflow_that_runs_the_suite_installs_pyyaml_so_the_parse_checks_never_skip():
    for name in ("install.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        installs = [l for l in text.splitlines() if 'pip install "arcgis>=2.4,<3" pytest' in l]
        assert installs and all('"pyyaml>=6,<7"' in l for l in installs), name


def _step(job, name):
    return next(s for s in job["steps"] if s.get("name") == name)


def _legs(doc):
    return {leg["leg"]: leg for leg in doc["jobs"]["environment"]["strategy"]["matrix"]["include"]}


# ------------------------------------------------------------------ the same rules as the other workflows

def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment():
    uses = [l.strip() for l in WORKFLOW.read_text(encoding="utf-8").splitlines() if re.match(r"\s*-?\s*uses:", l)]
    assert len(uses) == 6
    for line in uses:
        assert re.search(r"uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line


def test_every_checkout_drops_the_token_and_the_workflow_can_only_read():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("persist-credentials: false") == text.count("uses: actions/checkout@") == 3
    assert re.search(r"^permissions:\n  contents: read\n(?!  )", text, flags=re.M)
    assert "id-token" not in text and "contents: write" not in text


def test_the_workflow_uses_no_secret_and_never_applies():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text and "--apply" not in text and "GH_TOKEN" not in text


def test_the_workflow_runs_weekly_on_demand_and_when_the_release_workflow_calls_it(doc):
    triggers = doc.get("on") or doc[True]    # PyYAML reads the bare key `on` as the boolean True
    assert set(triggers) == {"workflow_call", "schedule", "workflow_dispatch"}
    assert "push" not in triggers, "release.yml calls this file on a tag; a push trigger would run it twice"
    assert len(triggers["schedule"]) == 1


# ------------------------------------------------------------------ what the matrix covers

def test_no_job_asks_for_a_retired_or_unlisted_runner(doc):
    suite = doc["jobs"]["suite"]["strategy"]["matrix"]
    assert set(suite["os"]) == SUPPORTED_RUNNERS     # windows-2019 was retired; the header says so
    assert suite["python"] == ["3.11", "3.12", "3.13"]
    assert {leg["os"] for leg in _legs(doc).values()} <= SUPPORTED_RUNNERS
    assert doc["jobs"]["scale"]["runs-on"] in SUPPORTED_RUNNERS


def test_the_suite_grid_is_a_plain_cross_product_and_the_environment_legs_are_listed_one_by_one(doc):
    assert set(doc["jobs"]["suite"]["strategy"]["matrix"]) == {"os", "python"}
    assert set(doc["jobs"]["environment"]["strategy"]["matrix"]) == {"include"}, "a cross product would multiply the cost"
    assert doc["jobs"]["environment"]["strategy"]["fail-fast"] is False
    assert len(_legs(doc)) == len(doc["jobs"]["environment"]["strategy"]["matrix"]["include"]) == 7


def test_the_environment_legs_cover_the_cultures_the_zones_and_constrained_language(doc):
    legs = _legs(doc)
    assert {legs["de-DE"]["culture"], legs["fi-FI"]["culture"]} == {"de-DE", "fi-FI"}
    assert legs["tz-new-york"]["zone"] == "Eastern Standard Time" and legs["tz-new-york"]["offset"] == "-18000"
    assert legs["tz-sydney"]["zone"] == "AUS Eastern Standard Time" and legs["tz-sydney"]["offset"] == "36000"
    assert legs["constrained-language"]["language_mode"] == "ConstrainedLanguage"
    assert {l["language_mode"] for n, l in legs.items() if n != "constrained-language"} == {"FullLanguage"}
    assert {legs["live-2022"]["os"], legs["live-2025"]["os"]} == SUPPORTED_RUNNERS
    for leg in legs.values():
        assert set(leg) == {"leg", "os", "zone", "offset", "culture", "language_mode", "targets"}, leg["leg"]


def test_every_leg_exports_the_expectations_the_guard_tests_read(doc):
    env = doc["jobs"]["environment"]["env"]
    assert set(env) == {"TASKDASH_EXPECT_UTC_OFFSET_S", "TASKDASH_EXPECT_CULTURE", "TASKDASH_EXPECT_LANGUAGE_MODE"}
    for name in env:
        assert any(name in path.read_text(encoding="utf-8")
                   for path in (ROOT / "test_h4_ci_legs.py", ROOT / "test_phase3_install.py")), name


def test_the_constrained_language_leg_runs_files_that_exist_and_still_checks_the_mode(doc):
    targets = _legs(doc)["constrained-language"]["targets"].split()
    assert "test_h4_ci_legs.py" in targets and "test_h4_live_seeded.py" in targets
    assert all((ROOT / target).is_file() for target in targets), targets
    # The leg must not run a file that parses a script with the Language class: that is blocked there.
    for target in targets:
        assert "Language.Parser" not in (ROOT / target).read_text(encoding="utf-8"), target


def test_the_lock_is_a_machine_variable_and_every_step_after_it_uses_cmd(doc):
    steps = doc["jobs"]["environment"]["steps"]
    lock = _step(doc["jobs"]["environment"], "Lock PowerShell into ConstrainedLanguage")
    assert "__PSLockdownPolicy /t REG_SZ /d 4" in lock["run"] and "HKLM" in lock["run"]
    after = steps[steps.index(lock) + 1:steps.index(_step(doc["jobs"]["environment"], "Remove the ConstrainedLanguage lock"))]
    assert after and all(step.get("shell") == "cmd" for step in after)


def test_the_cleanup_steps_always_run_and_the_lock_comes_off_first(doc):
    job = doc["jobs"]["environment"]
    names = [s.get("name") for s in job["steps"]]
    unlock = _step(job, "Remove the ConstrainedLanguage lock")
    remove = _step(job, "Remove the seeded tasks")
    assert "always()" in unlock["if"] and remove["if"] == "always()"
    assert "reg delete" in unlock["run"] and "-Action Remove" in remove["run"]
    order = ["Register the seeded tasks and enable the task log", "Lock PowerShell into ConstrainedLanguage",
             "Run the tests", "Remove the ConstrainedLanguage lock", "Remove the seeded tasks"]
    assert [names.index(n) for n in order] == sorted(names.index(n) for n in order)


def test_the_live_tests_get_the_manifest_the_seed_step_writes(doc):
    job = doc["jobs"]["environment"]
    seed = _step(job, "Register the seeded tasks and enable the task log")["run"]
    assert "-Action Register" in seed and "TASKDASH_LIVE_SEEDED=$manifest" in seed and "GITHUB_ENV" in seed
    assert "TaskScheduler/Operational /e:true" in seed
    assert "TASKDASH_LIVE_SEEDED" in (ROOT / "test_h4_live_seeded.py").read_text(encoding="utf-8")
    run = _step(job, "Run the tests")["run"]
    assert run.startswith("python -m pytest -q -p no:cacheprovider") and "${{ matrix.targets }}" in run


def test_the_scale_job_runs_the_500_task_test_under_a_15_minute_budget(doc):
    job = doc["jobs"]["scale"]
    assert job["env"] == {"TASKDASH_SCALE_TASKS": "500", "TASKDASH_SCALE_BUDGET_SECONDS": "900"}
    assert job["timeout-minutes"] > 15
    assert "test_h4_scale_fixture.py" in _step(job, "Run the scale test")["run"]
    source = (ROOT / "test_h4_scale_fixture.py").read_text(encoding="utf-8")
    assert "TASKDASH_SCALE_TASKS" in source and "TASKDASH_SCALE_BUDGET_SECONDS" in source


def test_every_job_has_a_timeout_and_the_suite_installs_what_the_tests_import(doc):
    for name, job in doc["jobs"].items():
        assert job["timeout-minutes"] <= 45, name
    for name in ("suite", "environment"):
        install = _step(doc["jobs"][name], "Install test dependencies")["run"]
        assert 'pip install "arcgis>=2.4,<3" pytest' in install and "pyyaml" in install


# ------------------------------------------------------------------ the seed script

def test_the_seed_script_is_ascii_so_windows_powershell_5_1_reads_it_right():
    SEED.read_bytes().decode("ascii")


def test_the_seed_script_registers_the_odd_cases_and_removes_them_by_literal_name():
    text = SEED.read_text(encoding="ascii")
    for needle in ("[1]", "   inside", "Quote's", "[char]0xE9", "[char]$_", "\\MicrosoftSync\\", "SYSTEM",
                   "-Force", "schtasks.exe /Delete /TN", "WriteAllText", "UTF8Encoding($false)"):
        assert needle in text, needle
    assert text.count("@{ Path =") == 8
    # A monthly trigger is a base-class trigger, so the collector exports the task XML for it. The
    # bracket name and its look-alike without brackets prove that export reads the right task.
    assert text.count("Days =") == 2 and "Monthly [1] probe" in text and "Monthly 1 probe" in text
    assert "ScheduleByMonth" in text and "-Xml" in text
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert "Unregister-ScheduledTask" not in code, "it reads [ ] as a wildcard set and misses the bracket task"
    assert "Set-ScheduledTask" not in code and "-Password" not in code


@pytest.mark.skipif(not POWERSHELL, reason="powershell.exe not available")
def test_the_seed_script_parses_without_errors():
    # Parse only: ParseFile never runs the script, so no task is registered.
    command = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{SEED}', [ref]$null, [ref]$e); if ($e) {{ $e | ForEach-Object {{ $_.Message }}; exit 1 }}")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr

"""H4-5: whole-folder Microsoft filter, filtered inside PowerShell, default exclude regex for new installs.

No network and no real Task Scheduler: the PowerShell call is faked.
"""

from __future__ import annotations

import json

import pytest

import taskdash_collect as collect
from taskdash_common import data_dir
from test_taskdash import _task

ON = {"exclude_microsoft_tasks": True}

# Synthetic names shaped like the Edge, Google and OneDrive updater tasks, plus ordinary ones.
UPDATER_NAMES = [
    "MicrosoftEdgeUpdateTaskMachineCore{00000000-0000-0000-0000-000000000000}",
    "MicrosoftEdgeUpdateTaskMachineUA",
    "GoogleUpdateTaskMachineCore",
    "GoogleUpdaterTaskSystem 1.2.3.4",
    "OneDrive Standalone Update Task-S-1-5-21-0",
    "OneDrive Reporting Task-S-1-5-21-0",
    "OneDrive Startup Task-S-1-5-21-0",
    "OneDrive Per-Machine Standalone Update Task",
]
KEPT_NAMES = [
    "Parcel Refresh", "Nightly Backup [prod]", "Microsoft Sync Check", "Google Sheets Export",
    "One Drive Mirror", "OneDriveless Report",
]


@pytest.mark.parametrize("path", ["\\Microsoft\\", "\\Microsoft\\Windows\\X\\", "\\Microsoft"])
def test_microsoft_folder_and_subfolders_are_excluded(path):
    assert collect.should_include_task(_task(task_path=path), ON) is False


@pytest.mark.parametrize("path", ["\\MicrosoftSync\\", "\\MicrosoftSync\\Sub\\", "\\Microsofty\\"])
def test_a_folder_that_only_starts_with_microsoft_is_kept(path):
    assert collect.should_include_task(_task(task_path=path), ON) is True


def test_the_microsoft_filter_still_obeys_the_switch():
    assert collect.should_include_task(_task(task_path="\\Microsoft\\Windows\\X\\"),
                                       {"exclude_microsoft_tasks": False}) is True


def test_the_query_filters_microsoft_inside_powershell_and_pipes_the_task():
    script = collect.POWERSHELL_TASK_QUERY
    assert "__EXCLUDE_MICROSOFT__" in script
    assert "-like '\\Microsoft\\*'" in script
    assert "$task | Get-ScheduledTaskInfo" in script
    assert "Get-ScheduledTaskInfo -TaskName" not in script, "-TaskName can treat [ ] as wildcards"


def test_the_monthly_trigger_export_passes_the_name_not_the_task():
    # A piped task fails with "The parameter is incorrect" on a real Task Scheduler, so every
    # monthly task lost its months and days. Export-ScheduledTask -TaskName is literal: '[x]abc'
    # and 'abc*' do not match a task named xabc. The live seeded CI test checks both monthly tasks.
    script = collect.POWERSHELL_TASK_QUERY
    assert "Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath" in script
    assert "| Export-ScheduledTask" not in script


@pytest.mark.parametrize("exclude", [True, False])
def test_collect_tasks_sends_the_switch_to_powershell(monkeypatch, exclude):
    sent = []
    monkeypatch.setattr(collect, "run_powershell_json",
                        lambda script, timeout: sent.append(script) or [{"tasks": [_task()], "errors": []}])
    collect.collect_tasks(30, exclude_microsoft=exclude)
    assert "__EXCLUDE_MICROSOFT__" not in sent[0]
    assert f"$excludeMicrosoft = {'$true' if exclude else '$false'}" in sent[0]


def test_collect_tasks_excludes_by_default(monkeypatch):
    sent = []
    monkeypatch.setattr(collect, "run_powershell_json",
                        lambda script, timeout: sent.append(script) or [{"tasks": [], "errors": []}])
    collect.collect_tasks(30)
    assert "$excludeMicrosoft = $true" in sent[0]


def _fake_payload(monkeypatch, payload):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: [payload])
    monkeypatch.setattr(collect, "unreadable_tasks", [])


def test_a_listing_error_with_only_filtered_builtin_tasks_is_a_warning(monkeypatch):
    # One unreadable \Microsoft\ task plus nothing else: the filter emptied the list, the listing worked.
    _fake_payload(monkeypatch, {"tasks": [], "errors": [{"name": "\\Microsoft\\X", "message": "denied"}],
                                "excluded": 12})
    assert collect.collect_tasks(30, exclude_microsoft=True) == []


@pytest.mark.parametrize("payload", [
    {"tasks": [], "errors": [{"name": "root", "message": "denied"}], "excluded": 0},
    {"tasks": [], "errors": [{"name": "root", "message": "denied"}]},
])
def test_a_failed_listing_with_nothing_excluded_still_fails(monkeypatch, payload):
    _fake_payload(monkeypatch, payload)
    with pytest.raises(SystemExit, match="Cannot list scheduled tasks"):
        collect.collect_tasks(30, exclude_microsoft=True)


def test_preflight_passes_on_a_host_with_only_builtin_tasks_and_a_listing_error(monkeypatch):
    import taskdash_preflight as pf
    from test_phase3_preflight import make_ctx

    def fake(script, timeout):
        if "$excludeMicrosoft = $true" in script:
            return [{"tasks": [], "errors": [{"name": "\\Microsoft\\X", "message": "denied"}], "excluded": 3}]
        return [{"tasks": [_task(task_path="\\Microsoft\\Windows\\X\\")],
                 "errors": [{"name": "\\Microsoft\\X", "message": "denied"}]}]

    monkeypatch.setattr(collect, "run_powershell_json", fake)
    monkeypatch.setattr(collect, "unreadable_tasks", [])
    status, message = pf.check_tasks(make_ctx())
    assert status == pf.OK and "no task outside" in message


def test_the_query_counts_the_tasks_it_excludes():
    script = collect.POWERSHELL_TASK_QUERY
    assert "$excludedCount += 1" in script
    assert "excluded = [int]$excludedCount" in script


def test_a_task_name_with_square_brackets_passes_the_filters():
    row = _task(task_name="Nightly [prod] Backup", task_full_name="\\Nightly [prod] Backup")
    assert collect.should_include_task(row, {**ON, "exclude_task_name_regex": r"\[test\]"}) is True


def _example_regex() -> str:
    cfg = json.loads((data_dir() / "TaskDash.config.example.json").read_text(encoding="utf-8-sig"))
    return cfg["scheduler"]["exclude_task_name_regex"]


def test_the_example_regex_excludes_only_the_updater_tasks():
    cfg = {"exclude_task_name_regex": _example_regex()}
    assert _example_regex()
    for name in UPDATER_NAMES:
        assert collect.should_include_task(_task(task_name=name), cfg) is False, name
    for name in KEPT_NAMES:
        assert collect.should_include_task(_task(task_name=name), cfg) is True, name


def test_the_built_in_default_is_unchanged_for_the_reference_install():
    assert collect.should_include_task(_task(task_name=UPDATER_NAMES[0]), {}) is True

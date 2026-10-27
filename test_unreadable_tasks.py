"""H1-4: one corrupt or ACL-denied task definition must not stop the whole inventory.

The collector's PowerShell lists tasks with SilentlyContinue and returns the unreadable ones in an
errors list. The readable tasks are kept, the unreadable ones become a warning, the run succeeds.
No network: the PowerShell, AGOL and e-mail are all faked.
"""

from __future__ import annotations

import pytest

import taskdash_collect as collect
import taskdash_publish as publish
from test_taskdash import _task

BAD = {"name": "Synthetic Broken Task",
       "message": "The task image is corrupt or has been tampered with. (0x80041321)"}


def _payload(errors):
    return [{"tasks": [_task()], "errors": errors}]


def test_the_script_lists_tasks_without_stopping_on_one_bad_definition():
    script = collect.POWERSHELL_TASK_QUERY
    assert "Get-ScheduledTask -ErrorAction SilentlyContinue -ErrorVariable" in script
    assert "Add-ReadProblem" in script, "Info/Export failures are recorded, not swallowed"
    assert script.count('$ErrorActionPreference = "Stop"') == 1, "Stop stays for everything else"


def test_a_payload_with_errors_keeps_readable_tasks_and_warns_with_the_names(
        monkeypatch, capsys):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: _payload([BAD]))
    collect.unreadable_tasks.clear()
    tasks = collect.collect_tasks(30)
    assert [t["task_name"] for t in tasks] == ["Parcel Refresh"]
    assert len(collect.unreadable_tasks) == 1
    assert "Synthetic Broken Task" in collect.unreadable_tasks[0]
    out = capsys.readouterr().out
    assert "WARNING" in out and "Synthetic Broken Task" in out and "0x80041321" in out


def test_an_empty_errors_list_gives_no_warning(monkeypatch, capsys):
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: _payload([]))
    collect.unreadable_tasks.clear()
    assert len(collect.collect_tasks(30)) == 1
    assert collect.unreadable_tasks == []
    assert "WARNING" not in capsys.readouterr().out


def test_a_task_whose_info_cannot_be_read_is_skipped_not_listed_as_never_run():
    script = collect.POWERSHELL_TASK_QUERY
    catch = script.split("$task | Get-ScheduledTaskInfo", 1)[1].split("$actionCount = 0", 1)[0]
    assert "Add-ReadProblem" in catch and "continue" in catch


def test_a_failed_listing_with_no_tasks_fails_the_run_with_the_messages(monkeypatch):
    unnamed = {"name": "(unnamed task)", "message": "The Task Scheduler service is not running."}
    monkeypatch.setattr(collect, "run_powershell_json",
                        lambda script, timeout: [{"tasks": [], "errors": [unnamed]}])
    collect.unreadable_tasks.clear()
    with pytest.raises(SystemExit) as exc:
        collect.collect_tasks(30)
    message = str(exc.value)
    assert "Task Scheduler service is not running" in message and "Task Scheduler" in message
    assert "WARNING" not in message


@pytest.fixture
def fake_world(monkeypatch):
    seen = {"rows": []}
    item = type("I", (), {"title": "x", "id": "svc", "access": "private"})()
    monkeypatch.setattr(collect, "run_powershell_json", lambda script, timeout: _payload([BAD]))
    monkeypatch.setattr(collect, "collect_run_events", lambda *a, **k: [])
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "publish", lambda *a, **k: {})
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: False)
    monkeypatch.setattr(publish, "_write_collector_run_row",
                        lambda i, cfg, collected, **kw: seen["rows"].append(kw))
    return seen


def test_unreadable_tasks_are_recorded_on_a_successful_run(fake_world, tmp_path):
    args = publish.parse_args(["--apply"])
    args.target = "prod"
    code = publish._run_publish(args, {}, tmp_path, publish.utc_now(),
                                {"armed": False, "recorded": False})
    assert code == 0
    (row,) = fake_world["rows"]
    assert row["exit_status"] == "success"
    assert "Synthetic Broken Task" in row["error_text"]

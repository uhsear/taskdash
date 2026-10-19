"""H1-2: a wheel install runs taskdash.exe "collect --apply" from venv\\Scripts, not from the
runtime folder, so the own-task checks must also recognise that action."""

from __future__ import annotations

import pytest

import taskdash_metrics as metrics
import taskdash_publish as publish

WHEEL_TARGET = r"C:\Synthetic\venv\Scripts\taskdash.exe"


def _row(path, arguments):
    return {
        "command_target_path": path,
        "command_target_name": path.rsplit("\\", 1)[-1],
        "command_target_arguments": arguments,
    }


@pytest.mark.parametrize("path", [WHEEL_TARGET, WHEEL_TARGET.upper()])
def test_the_wheel_collect_action_is_the_own_task(path):
    row = _row(path, "collect --apply")
    assert publish.is_own_task(row)
    assert metrics.is_taskdash_command(row["command_target_name"], row["command_target_arguments"])


@pytest.mark.parametrize("path,arguments", [
    (WHEEL_TARGET, "preflight"),
    (WHEEL_TARGET, ""),
    (r"C:\Synthetic\venv\Scripts\othertool.exe", "collect --apply"),
])
def test_other_actions_are_not_the_own_task(path, arguments):
    row = _row(path, arguments)
    assert not publish.is_own_task(row)
    assert not metrics.is_taskdash_command(row["command_target_name"], row["command_target_arguments"])


def test_the_folder_layout_rules_still_hold():
    assert metrics.is_taskdash_command("run_taskdash.cmd")
    assert metrics.is_taskdash_command("taskdash_collect.py")
    assert not metrics.is_taskdash_command("taskdash.exe")

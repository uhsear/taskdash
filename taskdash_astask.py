"""Run the preflight checks as the task account, without runas.

Driver: taskdash preflight --as-task [--task-user NAME] [--apply]
Source: taskdash_task.xml (the template of the real task), the config, one hidden password prompt
Target: with --apply, one temporary task in Task Scheduler and one output file in the log folder.
        TaskDash tries to remove both on every path. A removal that fails is named with the fix, and
        the command exits 1. The collector task is only read.

    taskdash preflight --as-task            # print the plan, change nothing
    taskdash preflight --as-task --apply    # register, run once, print the output, remove

The task has the account and logon type of the real collector task, so it fails the way the real task
would. The password goes to Task Scheduler through its COM interface. It is never an argument of any
process and never printed.
"""

from __future__ import annotations

import argparse
import getpass
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple
from xml.sax.saxutils import escape

import taskdash_collect as collect
from taskdash_common import DEFAULT_CONFIG_FILE, data_dir, emit, load_config, normalize_text, runtime_dir, system32_exe
from taskdash_dashboard import DEFAULT_COLLECTOR_TASK_NAME
from taskdash_publish import resolve_log_dir

TEMP_TASK_PREFIX = "TaskDash preflight check "
TEMP_FILE_PREFIX = "preflight_as_task_"
# Preflight with the network checks takes about a minute; five minutes is a stuck run.
RUN_TIMEOUT_SECONDS = 300
POLL_SECONDS = 2
# A task that never started keeps this result, so it settles only after this wait (as install_task.ps1 does).
NOT_RUN_GUARD_SECONDS = 30
NOT_RUN_YET = 0x00041303

# Task Scheduler COM constants (taskschd.h): TASK_CREATE, TASK_LOGON_PASSWORD, TASK_LOGON_INTERACTIVE_TOKEN.
TASK_CREATE, LOGON_PASSWORD, LOGON_INTERACTIVE = 2, 1, 3
STATE_QUEUED, STATE_RUNNING = 2, 4
LOGON_NAMES = {LOGON_PASSWORD: "Password", LOGON_INTERACTIVE: "Interactive"}
XML_LOGON_TYPE = {"Password": "Password", "Interactive": "InteractiveToken"}
HRESULT_NOT_FOUND = -2147024894

COM_HINTS = {
    -2147024891: "Open PowerShell as administrator, then run the command again.",
    -2147023570: "Windows rejected the account's password. Run the command again and type the right password.",
}
RESULT_TEXT = {
    1: "preflight found a problem. Its output is above; each [!!] line ends with the fix.",
    NOT_RUN_YET: ("the task never started. The task account probably lacks the right to log on as a batch job, "
                  "or, for an Interactive task, is not signed in. Confirm it is in the local Administrators "
                  "group, then run the command again."),
    0x8007052E: "Windows rejected the task account's password. Run the command again and type the right password.",
    0x80070569: ("the task account may not log on as a batch job. Add it to the local Administrators group, "
                 "then run the command again."),
}


def connect_scheduler() -> Any:
    try:
        import win32com.client
    except ImportError:
        raise SystemExit("pywin32 is not installed, so TaskDash cannot talk to Task Scheduler. "
                         "Install it in the TaskDash environment with: python -m pip install pywin32") from None
    service = win32com.client.Dispatch("Schedule.Service")
    service.Connect()
    return service


def read_collector_identity(folder: Any, name: str) -> Optional[Tuple[str, int]]:
    """(account, COM logon type) of the real collector task, or None when it is not registered.
    This only reads the definition; nothing in this module changes or runs that task."""
    try:
        principal = folder.GetTask(name).Definition.Principal
    except Exception as exc:  # noqa: BLE001 - com_error has no importable base on a machine without pywin32
        if getattr(exc, "hresult", None) == HRESULT_NOT_FOUND:
            return None
        raise
    return normalize_text(principal.UserId), int(principal.LogonType)


def choose_account(folder: Any, collector: str, task_user: str) -> Optional[Tuple[str, str]]:
    """(account, 'Password' or 'Interactive'), or None after saying what to do."""
    identity = read_collector_identity(folder, collector)
    if identity is None and not task_user:
        emit(f"There is no collector task named '{collector}' to read the account from. Pass --task-user "
             "DOMAIN\\name to name the account, or register the collector task first.")
        return None
    user, logon = (task_user or identity[0]), (identity[1] if identity else LOGON_PASSWORD)
    if logon not in LOGON_NAMES or not user:
        emit(f"The collector task '{collector}' uses a logon type or account that --as-task cannot copy "
             "(it supports Password and Interactive, as install_task.ps1 does). Pass --task-user DOMAIN\\name, "
             "or run preflight from a window of the task account.")
        return None
    return user, LOGON_NAMES[logon]


def render_task_xml(user: str, logon: str, command: str, arguments: str, working_dir: str) -> str:
    """The collector's template, filled the way install_task.ps1 fills it. The trigger is removed, so the
    task runs only when started and a crash cannot leave an hourly task behind."""
    text = (data_dir() / "taskdash_task.xml").read_text(encoding="utf-8")
    text = re.sub(r"\s*<Triggers>.*?</Triggers>", "", text, flags=re.S)
    text = re.sub(r"<Description>.*?</Description>",
                  "<Description>Temporary TaskDash preflight check. TaskDash removes it when the check ends.</Description>",
                  text, flags=re.S)
    values = {"AUTHOR": user, "USER_ID": user, "LOGON_TYPE": XML_LOGON_TYPE[logon], "COMMAND": command,
              "ARGUMENTS": arguments, "WORKING_DIRECTORY": working_dir}
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", escape(value))
    return text


def preflight_command(out_file: Path, config: str, offline: bool) -> Tuple[str, str]:
    """cmd.exe runs the redirect; -X utf8 makes the file UTF-8 whatever the account's code page.
    --consume-handoff is what lets this run, and no other preflight, store the hand-off file."""
    extra = (f' --config "{config}"' if config else "") + (" --offline" if offline else "")
    inner = f'"{sys.executable}" -X utf8 -m taskdash_cli preflight --consume-handoff{extra} > "{out_file}" 2>&1'
    return system32_exe("cmd.exe"), f'/d /c "{inner}"'


def restrict_file(path: Path, account: str) -> None:
    """Create the output file and limit it to the task account, you, administrators and SYSTEM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=False)
    me = getpass.getuser()
    done = subprocess.run(
        [system32_exe("icacls.exe"), str(path), "/inheritance:r", "/grant:r", f"{account}:(M)",
         "*S-1-5-32-544:(F)", "*S-1-5-18:(F)", f"{me}:(F)"],
        capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"icacls could not restrict {path.name} (exit {done.returncode}). "
                           "Fix the permissions of the log folder, then run the command again")


def wait_for_run(task: Any) -> Optional[int]:
    """LastTaskResult once the run ends, or None at the deadline (the task is stopped by the caller)."""
    began = time.monotonic()
    while time.monotonic() - began < RUN_TIMEOUT_SECONDS:
        time.sleep(POLL_SECONDS)
        if task.State in (STATE_QUEUED, STATE_RUNNING):
            continue
        result = int(task.LastTaskResult) & 0xFFFFFFFF
        if result != NOT_RUN_YET or time.monotonic() - began >= NOT_RUN_GUARD_SECONDS:
            return result
    return None


def describe_result(result: int) -> str:
    return RESULT_TEXT.get(result) or collect.describe_task_result(result)


def read_output(path: Path, secret: Optional[str]) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text.replace(secret, "***") if secret else text


def remove_task(folder: Any, task: Any, name: str) -> bool:
    try:
        if task.State in (STATE_QUEUED, STATE_RUNNING):
            task.Stop(0)
        folder.DeleteTask(name, 0)
        return True
    except Exception as exc:  # noqa: BLE001 - say how to remove it by hand; cleanup must not hide the result
        emit(f"Could not delete the temporary task '{name}' ({type(exc).__name__}). Delete it from an "
             f'administrator window with: schtasks /delete /tn "{name}" /f')
        return False


def _run_registered(task: Any, user: str, password: Optional[str], out_file: Path) -> int:
    task.Run(None)
    result = wait_for_run(task)
    emit(read_output(out_file, password) or "(the run wrote no output)")
    if result is None:
        emit(f"Preflight as {user} did not finish in {RUN_TIMEOUT_SECONDS} seconds, so TaskDash stopped it. "
             "Run it by hand as that account to see where it waits.")
        return 1
    if result == 0:
        emit(f"Preflight as {user} passed.")
        return 0
    emit(f"Preflight as {user} failed with 0x{result:08X}: {describe_result(result)}")
    return 1


def remove_file(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        # A task stopped after a timeout can still hold the file. Raising here would hide the run's result.
        emit(f"Could not delete the output file '{path}' ({type(exc).__name__}). Delete it from an "
             "administrator window when the run has ended.")
        return False


def run_check(folder: Any, name: str, xml: str, user: str, logon: str, password: Optional[str], out_file: Path) -> int:
    """Register, run, print. The task and the file are removed on every path, including a crash.
    When either removal fails, the message says how to remove it by hand and the command exits 1."""
    task, code, removed = None, 1, True
    try:
        restrict_file(out_file, user)
        task = folder.RegisterTask(name, xml, TASK_CREATE, user, password,
                                   LOGON_PASSWORD if logon == "Password" else LOGON_INTERACTIVE)
        emit(f"Registered '{name}'. Running it once, waiting up to {RUN_TIMEOUT_SECONDS} seconds...")
        code = _run_registered(task, user, password, out_file)
    except Exception as exc:  # noqa: BLE001 - every failure ends in the cleanup below
        text = f"{type(exc).__name__}: {exc}"
        emit(f"Preflight as {user} could not run: {(text.replace(password, '***') if password else text)[:300]}. "
             + COM_HINTS.get(getattr(exc, "hresult", None), "Check the account and the log folder, then run the command again."))
    finally:
        if task is not None:
            removed = remove_task(folder, task, name)
        file_removed = remove_file(out_file)
    if task is not None and removed and file_removed:
        emit(f"Removed the temporary task '{name}' and its output file.")
    return code if removed and file_removed else 1


def run_as_task(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else runtime_dir() / DEFAULT_CONFIG_FILE
    config = load_config(path)
    collector = normalize_text((config.get("scheduler") or {}).get("collector_task_name")) or DEFAULT_COLLECTOR_TASK_NAME
    folder = connect_scheduler().GetFolder("\\")
    chosen = choose_account(folder, collector, normalize_text(args.task_user))
    if chosen is None:
        return 1
    user, logon = chosen
    suffix = uuid.uuid4().hex[:8]
    name = f"{TEMP_TASK_PREFIX}{suffix}"
    out_file = resolve_log_dir(config) / f"{TEMP_FILE_PREFIX}{suffix}.txt"
    command, arguments = preflight_command(out_file, str(path.resolve()) if args.config else "", args.offline)
    emit(f"Task name: {name}")
    emit(f"Runs as:   {user} ({logon} logon, the same as the collector task '{collector}')")
    emit(f"Command:   {command} {arguments}")
    emit(f"Output:    {out_file} (readable by that account, you and administrators only)")
    emit(f"It runs once, waits up to {RUN_TIMEOUT_SECONDS} seconds, prints the output, then deletes "
         "the task and the file.")
    if not args.apply:
        emit("Nothing registered. Re-run with --apply to run it"
             + (f"; it asks for the password of {user}." if logon == "Password" else "."))
        return 0
    password = None
    if logon == "Password":
        password = getpass.getpass(f"Password for {user} (not shown): ")
        if not password:
            emit(f"The password is empty, so nothing was registered. Run the command again and type "
                 f"the password of {user}.")
            return 1
    xml = render_task_xml(user, logon, command, arguments, str(runtime_dir()))
    return run_check(folder, name, xml, user, logon, password, out_file)

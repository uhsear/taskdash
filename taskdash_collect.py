"""Collect Windows Scheduled Tasks and run events, and shape them into TaskDash rows.

Everything here is host-local: Get-ScheduledTask and the Operational event log only see the
machine the script runs on. Fixture capture (--capture-fixture on taskdash_publish) exists so
this half can be tested off-host.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import shlex
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from taskdash_common import (
    emit,
    normalize_text,
    parse_iso_datetime,
    round_hours,
    run_powershell_json,
    runtime_dir,
    see_troubleshooting,
    short_duration,
    safe_float,
    safe_int,
    utc_now,
)

OPERATIONAL_LOG_NAME = "Microsoft-Windows-TaskScheduler/Operational"

# 100/102/200/201 bracket a normal run (start/end/action-start/action-result). The rest were
# added 2026-09-25 (collect-07) after the old reader left launch failures invisible and killed
# runs stuck "Running" forever -- verified against this machine's provider manifest
# (wevtutil gp Microsoft-Windows-TaskScheduler /ge /gm:true) and real Operational-log events:
# 101 launch failure, 103 action start failed, 111 terminated, 153 missed start rejected,
# 202/203 action failed/failed to start, 322 already running (skip), 329 killed on time limit,
# 330 stopped by user request, 332 user not logged on.
HISTORY_EVENT_IDS = (100, 101, 102, 103, 111, 153, 200, 201, 202, 203, 322, 329, 330, 332)

DEFAULT_OVERDUE_GRACE_MINUTES = 15.0

# Substring, not regex -- the Windows path fragment that marks "this task runs ArcGIS Pro's
# python". With a Single Use licence that interpreter is licensed only in the Interactive logon
# session. Configurable via scheduler.interactive_required_pattern; false or "" there turns the
# check off for a Named User or Concurrent Use install (taskdash_publish.interactive_pattern).
DEFAULT_INTERACTIVE_REQUIRED_PATTERN = "\\ArcGIS\\Pro\\bin\\Python"

# (run_status, is_error, result_text) for events that settle a run's outcome outright --
# these outrank whatever the 201 result code says because several of them (101/153/322/332)
# never get a 201 at all: the action never started. is_error is False for a benign skip
# (already running, missed-and-rejected) or a deliberate stop (user request); True for a
# real failure (launch failure, not logged on, killed on time limit) or an unexplained kill.
#
# A benign row is is_error=0. Missed and Skipped-AlreadyRunning are not runs at all
# (NON_RUN_STATUSES), so summarize_runs leaves them out of the 7-day counts and the success rate;
# a user-requested Stop was a real run and counts as one that did not fail.
OUTCOME_EVENT_STATUS: Dict[int, Tuple[str, bool, str]] = {
    101: ("StartFailed", True, "Task Scheduler could not start the task (launch failure)."),
    103: ("StartFailed", True, "An action failed to start."),
    202: ("StartFailed", True, "An action failed."),
    203: ("StartFailed", True, "An action failed to start."),
    153: ("Missed", False, "The scheduled start was missed and rejected."),
    322: ("Skipped-AlreadyRunning", False,
          "A new instance was not started because the task was already running."),
    329: ("KilledTimeLimit", True, "The task was stopped because it exceeded its execution time limit."),
    330: ("Stopped", False, "The task was stopped by user request."),
    332: ("NotLoggedOn", True, "The task did not start because the required user was not logged on."),
    111: ("Terminated", True, "The task process was terminated without a normal completion event."),
}

# Rows for starts that never happened. They are kept for the record, but they are not runs, so
# the 7-day run counts and the success rate must not count them as successful executions.
NON_RUN_STATUSES = frozenset({"Missed", "Skipped-AlreadyRunning"})

# Most-specific-reason-first so a paired 329/330 (why it stopped) beats the generic 111
# (that it stopped) when both land on the same run.
OUTCOME_EVENT_PRIORITY: Tuple[int, ...] = (332, 329, 330, 322, 153, 101, 103, 202, 203, 111)

# --------------------------------------------------------------------------- result codes

HRESULT_TEXT: Dict[int, str] = {
    0: "The operation completed successfully.",
    1: "Incorrect function.",
    2: "File not found.",
    0x00041300: "Task is ready to run at its next scheduled time.",
    0x00041301: "Task is currently running.",
    0x00041302: "Task is disabled.",
    0x00041303: "Task has not yet run.",
    0x00041304: "No more runs are scheduled for this task.",
    0x00041305: "Task is not scheduled; required schedule properties are missing.",
    0x00041306: "The last run of the task was terminated by the user.",
    0x00041307: "Task has no valid triggers.",
    0x00041308: "Event-triggered task does not have a scheduled next run time.",
    0x0004131B: "Task is registered, but some triggers failed.",
    0x0004131C: "Task may fail to start because batch logon privilege is missing.",
    0x00041325: "Task has been queued to run.",
    2147942401: "Incorrect function.",
    2147942402: "File not found.",
    2147942667: "The directory name is invalid.",
    2147943726: "The service has not been started.",
    2147942522: "Access is denied.",
    2147943645: "The network path was not found.",
    2147946720: "The operator or administrator has refused the request.",
    2147806724: "Unknown OneDrive update error.",
    # Observed in run history on the task server but previously unmapped.
    2147943691: "The service did not respond to the start or control request in a timely fashion.",
    # NTSTATUS STATUS_CONTROL_C_EXIT -- a process ends this way on Ctrl+C, a console close,
    # or Task Scheduler stopping it; not a Win32/HRESULT code, so ctypes.FormatError can't help.
    0xC000013A: "The run was stopped before it finished (it was ended, or its console closed).",
}

NON_ERROR_RESULT_CODES = {
    0, 0x00041300, 0x00041301, 0x00041302, 0x00041303,
    0x00041304, 0x00041308, 0x00041325,
}

WARNING_RESULT_CODES = {
    0x00041305, 0x00041306, 0x00041307, 0x0004131B, 0x0004131C,
}

PYTHON_EXECUTABLES = {"python.exe", "pythonw.exe", "py.exe", "python", "pythonw", "py"}
POWERSHELL_EXECUTABLES = {"powershell.exe", "pwsh.exe", "powershell_ise.exe", "powershell", "pwsh"}
CMD_EXECUTABLES = {"cmd.exe", "cmd"}
SCRIPT_HOST_EXECUTABLES = {"cscript.exe", "wscript.exe", "cscript", "wscript"}

# python3.exe / python3.11.exe / pythonw3.exe (and extensionless python3, pythonw3): Windows
# resolves an interpreter without ".exe" via PATHEXT, and a versioned install (py.org's
# "python3.11") is common enough that classify_command_target must not fall through to the
# generic "Executable" bucket -- that bucket is what exclude_command_target_types filters out.
PYTHON_VERSIONED_NAME_RE = re.compile(r"^py(?:thon)?w?\d+(?:\.\d+)*(?:\.exe)?$")
SCRIPT_EXTENSIONS = {".bat", ".cmd", ".exe", ".jar", ".js", ".ps1", ".py", ".pyw", ".vbs"}

SCRIPT_TYPE_BY_EXT = {
    ".bat": "BatchScript", ".cmd": "BatchScript", ".jar": "Jar", ".js": "JavaScript",
    ".ps1": "PowerShellScript", ".py": "PythonScript", ".pyw": "PythonScript", ".vbs": "VBScript",
}


# --------------------------------------------------------------------------- PowerShell payloads

POWERSHELL_TASK_QUERY = r"""
$ErrorActionPreference = "Stop"
if ($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage') { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 }

function Convert-ToIsoUtcOrNull {
    param($Value)
    if ($null -eq $Value) { return $null }
    try {
        if ($Value -is [datetime] -and $Value -ge [datetime]'2000-01-01') {
            return $Value.ToUniversalTime().ToString('o')
        }
    } catch { return $null }
    return $null
}

function Get-TriggerTypeName {
    param($Trigger)

    $name = ""
    try { $name = [string]$Trigger.CimClass.CimClassName } catch { $name = "" }
    if (-not $name) { $name = [string]$Trigger }

    # Strip the CIM prefix/suffix, but never return empty: the base class MSFT_TaskTrigger
    # reduces to "" under a naive strip, which used to bucket real triggers as "No Triggers".
    $stripped = $name -replace '^MSFT_Task', ''
    $stripped = $stripped -replace 'Trigger$', ''
    if (-not $stripped) { $stripped = 'Scheduled' }
    return $stripped
}

# Select-Xml, not an XPath method on the node: a method call on an XmlNode fails in ConstrainedLanguage.
function Select-FirstNode {
    param($Node, [string]$XPath)
    if (-not $Node) { return $null }
    $hit = Select-Xml -Xml $Node -XPath $XPath | Select-Object -First 1
    if ($hit) { return $hit.Node }
    return $null
}

function Get-XmlList {
    param($Parent, [string]$Name)
    $node = Select-FirstNode $Parent "*[local-name()='$Name']"
    if (-not $node) { return "" }
    # <Day>15</Day> and <Week>Last</Week> carry text; <January/> and <Monday/> are named.
    return (@($node.ChildNodes | Where-Object { $_.NodeType -eq 'Element' } | ForEach-Object {
        if ($_.InnerText) { $_.InnerText } else { $_.LocalName } }) -join ',')
}

$problems = @()
function Add-ReadProblem {
    param([string]$Name, [string]$Message)
    $script:problems += [ordered]@{ name = $Name; message = $Message }
}

# One corrupt or ACL-denied definition (e.g. 0x80041321, "task image is corrupt") used to make
# Get-ScheduledTask throw under Stop and end the whole inventory. Its per-task errors are
# collected here and reported; every readable task still comes back.
$tasks = Get-ScheduledTask -ErrorAction SilentlyContinue -ErrorVariable taskListErrors
foreach ($err in @($taskListErrors)) {
    $target = if ($err.TargetObject) { [string]$err.TargetObject } else { "(unnamed task)" }
    Add-ReadProblem $target ([string]$err.Exception.Message)
}
$excludeMicrosoft = __EXCLUDE_MICROSOFT__
$excludedCount = 0
$rows = foreach ($task in $tasks) {
    # The whole folder name: \Microsoft\ and its subfolders, not \MicrosoftSync\. Dropped here so
    # the built-in tasks are never read, converted to JSON or sent to Python. They are counted so
    # Python can tell "only built-in tasks" from "the listing failed".
    if ($excludeMicrosoft -and $task.TaskPath -like '\Microsoft\*') { $excludedCount += 1; continue }
    $info = $null
    # Piped, not -TaskName: a name is then never read as a pattern, so [ and ] in a name are safe.
    try { $info = $task | Get-ScheduledTaskInfo } catch {
        Add-ReadProblem "$($task.TaskPath)$($task.TaskName)" "Get-ScheduledTaskInfo failed, so the task is left out: $($_.Exception.Message)"
        # Skipped, not listed with blank run fields: a failing task would read as never run.
        continue
    }

    $actionCount = 0
    $primaryExecute = ""
    $primaryArguments = ""
    $primaryWorkingDirectory = ""
    $actions = ""
    $actionDetails = @()
    try {
        $actionList = @($task.Actions)
        $actionCount = $actionList.Count
        if ($actionCount -gt 0) {
            $primaryExecute = [string]$actionList[0].Execute
            $primaryArguments = [string]$actionList[0].Arguments
            $primaryWorkingDirectory = [string]$actionList[0].WorkingDirectory
        }
        $actionIndex = 0
        $actionDetails = @($actionList | ForEach-Object {
            $parts = @()
            if ($_.Execute) { $parts += $_.Execute }
            if ($_.Arguments) { $parts += $_.Arguments }
            if ($_.WorkingDirectory) { $parts += "(wd=$($_.WorkingDirectory))" }
            $detail = [ordered]@{
                index = [int]$actionIndex
                execute = [string]$_.Execute
                arguments = [string]$_.Arguments
                working_directory = [string]$_.WorkingDirectory
                display = [string]($parts -join " ")
            }
            $actionIndex += 1
            $detail
        })
        $actions = ($actionDetails | ForEach-Object { $_.display } | Where-Object { $_ }) -join " | "
    } catch {
        $actions = ""
        $actionDetails = @()
    }

    $triggerDetails = @()
    $triggerTypes = ""
    $triggerCount = 0
    try {
        $triggerList = @($task.Triggers)
        $triggerCount = $triggerList.Count
        $triggerTypes = ($triggerList | ForEach-Object { Get-TriggerTypeName $_ } | Where-Object { $_ } | Select-Object -Unique) -join " | "
        # A CIM trigger's own .ToString() returns only its class name (e.g. MSFT_TaskDailyTrigger),
        # never the schedule -- verified on this host. Every schedule property is read out here
        # instead: definition_hash and the change summary are built from them, so a schedule-only
        # edit (time, interval, days, repetition, delay) is seen. A property a trigger class does
        # not have (DaysOfWeek on a daily trigger) reads as $null, so one shape fits every class.
        # Monthly and monthly day-of-week triggers come back as the base class MSFT_TaskTrigger,
        # which has no day or month properties; only the task XML carries them (ScheduleByMonth,
        # ScheduleByMonthDayOfWeek). Its <Triggers> children are in the same order as .Triggers.
        # Exported only when needed: most tasks have no base-class trigger.
        $xmlTriggers = @()
        if (@($triggerList | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTrigger' }).Count) {
            try {
                # By name, not piped: a piped task fails with "The parameter is incorrect", and
                # -TaskName here is literal (a [ ] or * name matches only itself; checked on Windows 11).
                $xml = [xml](Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath)
                $xmlTriggers = @((Select-FirstNode $xml.DocumentElement "*[local-name()='Triggers']").ChildNodes |
                    Where-Object { $_.NodeType -eq 'Element' })
            } catch {
                $xmlTriggers = @()
                Add-ReadProblem "$($task.TaskPath)$($task.TaskName)" "Export-ScheduledTask failed, so monthly trigger details are blank: $($_.Exception.Message)"
            }
        }
        $triggerIndex = 0
        $triggerDetails = @($triggerList | ForEach-Object {
            $rep = $_.Repetition
            $calendar = $null
            if ($_.CimClass.CimClassName -eq 'MSFT_TaskTrigger' -and $triggerIndex -lt $xmlTriggers.Count) {
                $calendar = Select-FirstNode $xmlTriggers[$triggerIndex] `
                    "*[local-name()='ScheduleByMonth' or local-name()='ScheduleByMonthDayOfWeek']"
            }
            $triggerIndex += 1
            [ordered]@{
                type = [string](Get-TriggerTypeName $_)
                start = [string]$_.StartBoundary
                end = [string]$_.EndBoundary
                enabled = [bool]$_.Enabled
                days_interval = [string]$_.DaysInterval
                days_of_week = [string]$_.DaysOfWeek
                weeks_interval = [string]$_.WeeksInterval
                repetition_interval = if ($rep) { [string]$rep.Interval } else { "" }
                repetition_duration = if ($rep) { [string]$rep.Duration } else { "" }
                random_delay = [string]$_.RandomDelay
                delay = [string]$_.Delay
                months = Get-XmlList $calendar 'Months'
                days_of_month = Get-XmlList $calendar 'DaysOfMonth'
                weeks_of_month = Get-XmlList $calendar 'Weeks'
                month_days_of_week = Get-XmlList $calendar 'DaysOfWeek'
            }
        })
    } catch { $triggerDetails = @() }

    [ordered]@{
        task_name = [string]$task.TaskName
        task_path = [string]$task.TaskPath
        task_full_name = [string]("{0}{1}" -f $task.TaskPath, $task.TaskName)
        state = [string]$task.State
        enabled = [bool]$task.Settings.Enabled
        hidden = [bool]$task.Settings.Hidden
        author = [string]$task.Author
        run_as_user = [string]$task.Principal.UserId
        description = [string]$task.Description
        action_count = [int]$actionCount
        actions = [string]$actions
        action_details = @($actionDetails)
        primary_executable = [string]$primaryExecute
        primary_arguments = [string]$primaryArguments
        primary_working_directory = [string]$primaryWorkingDirectory
        trigger_count = [int]$triggerCount
        trigger_details = @($triggerDetails)
        trigger_types = [string]$triggerTypes
        logon_type = [string]$task.Principal.LogonType
        run_level = [string]$task.Principal.RunLevel
        execution_time_limit = [string]$task.Settings.ExecutionTimeLimit
        multiple_instances_policy = [string]$task.Settings.MultipleInstances
        start_when_available = [bool]$task.Settings.StartWhenAvailable
        last_run_time = if ($info) { Convert-ToIsoUtcOrNull $info.LastRunTime } else { $null }
        next_run_time = if ($info) { Convert-ToIsoUtcOrNull $info.NextRunTime } else { $null }
        last_task_result = if ($info) { [int64]$info.LastTaskResult } else { $null }
        number_of_missed_runs = if ($info) { [int64]$info.NumberOfMissedRuns } else { $null }
    }
}

[ordered]@{ tasks = @($rows); errors = @($problems); excluded = [int]$excludedCount } | ConvertTo-Json -Depth 6 -Compress
"""


POWERSHELL_LOG_HEALTH_QUERY_TEMPLATE = r"""
$ErrorActionPreference = "Stop"
if ($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage') { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 }
$LogName = "__LOG_NAME__"
try {
    $log = Get-WinEvent -ListLog $LogName -ErrorAction Stop
    [ordered]@{
        exists = $true
        is_enabled = [bool]$log.IsEnabled
        error_message = ""
    } | ConvertTo-Json -Compress
} catch {
    [ordered]@{
        exists = $false
        is_enabled = $false
        error_message = [string]$_.Exception.Message
    } | ConvertTo-Json -Compress
}
"""


POWERSHELL_HISTORY_QUERY_TEMPLATE = r"""
$ErrorActionPreference = "Stop"
if ($ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage') { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 }

$LogName = "Microsoft-Windows-TaskScheduler/Operational"
$DaysBack = __DAYS_BACK__
$MaxEvents = __MAX_EVENTS__
$MinRecordId = __MIN_RECORD_ID__

function Convert-ToIsoUtcOrNull {
    param($Value)
    if ($null -eq $Value) { return $null }
    try {
        if ($Value -is [datetime] -and $Value -ge [datetime]'2000-01-01') {
            return $Value.ToUniversalTime().ToString('o')
        }
    } catch { return $null }
    return $null
}

function Get-XmlDataMap {
    param([xml]$XmlEvent)
    $map = @{}
    if ($null -ne $XmlEvent -and $null -ne $XmlEvent.Event -and
        $null -ne $XmlEvent.Event.EventData -and $null -ne $XmlEvent.Event.EventData.Data) {
        foreach ($node in @($XmlEvent.Event.EventData.Data)) {
            $name = [string]$node.Name
            if ($name) { $map[$name] = [string]$node.'#text' }
        }
    }
    return $map
}

# ConstrainedLanguage refuses $event.ToXml() (a method on a type outside the allowed list), so
# there the fields come from $event.Properties by position. Each row lists the field names of that
# event's template in order, as printed by ToXml() on real events; an event with no row (or more
# values than names) keeps only the fields it has names for. 330 has no sample: TaskName only.
$FullLanguage = $ExecutionContext.SessionState.LanguageMode -eq 'FullLanguage'
$PropertyNames = @{
    100 = @('TaskName', 'UserContext', 'InstanceId')
    101 = @('TaskName', 'UserContext', 'ResultCode')
    102 = @('TaskName', 'UserContext', 'InstanceId')
    103 = @('TaskName', 'InstanceId', 'UserContext', 'ResultCode')
    111 = @('TaskName', 'InstanceId')
    153 = @('TaskName')
    200 = @('TaskName', 'ActionName', 'TaskInstanceId', 'EnginePID')
    201 = @('TaskName', 'TaskInstanceId', 'ActionName', 'ResultCode', 'EnginePID')
    202 = @('TaskName', 'TaskInstanceId', 'ActionName', 'ResultCode', 'EnginePID')
    203 = @('TaskName', 'TaskInstanceId', 'ActionName', 'ResultCode')
    322 = @('TaskName', 'TaskInstanceId')
    329 = @('TaskName', 'TaskInstanceId')
    330 = @('TaskName')
    332 = @('TaskName', 'UserName')
}

function Get-PropertyDataMap {
    param($Record)
    $map = @{}
    $names = $PropertyNames[[int]$Record.Id]
    if (-not $names) { return $map }
    $values = @($Record.Properties)
    for ($i = 0; $i -lt $names.Count -and $i -lt $values.Count; $i++) {
        $value = $values[$i].Value
        # The event XML prints a GUID in braces; a bare GUID would not match the Full-mode rows.
        $text = [string]$value
        if ($value -is [guid]) { $text = "{" + $text + "}" }
        $map[[string]$names[$i]] = $text
    }
    return $map
}

# Measured on the task server: the Operational log carries ~920 events/day, and only ~25% belong to
# monitored tasks -- the rest is the Edge updater and Windows Error Reporting. A flat 5000-event
# time window therefore reaches back barely 5 days, not the 30 the config asks for. Once a
# watermark exists, fetch only records newer than it so the budget is never spent on re-reading.
# -Oldest matters: Get-WinEvent is newest-first, so a backlog larger than MaxEvents (an outage of
# a few days) would return only its newest slice, and the watermark would then jump past the
# older runs for good. Oldest-first lets successive passes walk the backlog forward.
#
# -ErrorAction Stop + catch here (collect-05): SilentlyContinue used to turn ANY failure --
# access denied, a cleared log, a bad filter -- into a silent empty result indistinguishable
# from "nothing happened". Only the well-known "no matching events" outcome (identified by its
# stable FullyQualifiedErrorId, not the locale-dependent message text) is a legitimate empty
# result; everything else must surface so the caller's SystemExit actually fires.
try {
    if ($MinRecordId -gt 0) {
        $xpath = "*[System[(EventRecordID > $MinRecordId) and (__EVENT_ID_XPATH__)]]"
        $events = Get-WinEvent -LogName $LogName -FilterXPath $xpath -MaxEvents $MaxEvents -Oldest -ErrorAction Stop
    } else {
        $start = (Get-Date).AddDays(-1 * $DaysBack)
        $events = Get-WinEvent -FilterHashtable @{
            LogName = $LogName
            Id = @(__EVENT_ID_CSV__)
            StartTime = $start
        } -MaxEvents $MaxEvents -ErrorAction Stop
    }
} catch {
    if ($_.FullyQualifiedErrorId -eq 'NoMatchingEventsFound,Microsoft.PowerShell.Commands.GetWinEventCommand') {
        $events = @()
    } else {
        throw
    }
}

$rows = foreach ($event in $events) {
    $xmlEvent = $null
    if ($FullLanguage) {
        try { [xml]$xmlEvent = $event.ToXml() } catch { $xmlEvent = $null }
        $dataMap = Get-XmlDataMap $xmlEvent
    } else {
        $dataMap = Get-PropertyDataMap $event
    }
    $instanceId = [string]$dataMap['InstanceId']
    if (-not $instanceId) { $instanceId = [string]$dataMap['TaskInstanceId'] }

    # System/TimeCreated/@SystemTime is already UTC ISO-8601 straight from the event XML --
    # unlike $event.TimeCreated.ToUniversalTime(), it carries no local-timezone/DST conversion
    # to get wrong. Fall back to the old conversion only if ToXml() itself failed.
    $systemTimeRaw = $null
    if ($null -ne $xmlEvent -and $null -ne $xmlEvent.Event -and $null -ne $xmlEvent.Event.System -and
        $null -ne $xmlEvent.Event.System.TimeCreated) {
        $systemTimeRaw = [string]$xmlEvent.Event.System.TimeCreated.SystemTime
    }
    $eventTimeUtc = if ($systemTimeRaw) { $systemTimeRaw } else { Convert-ToIsoUtcOrNull $event.TimeCreated }

    [ordered]@{
        event_id = [int]$event.Id
        event_record_id = [int64]$event.RecordId
        event_time_utc = $eventTimeUtc
        task_full_name = [string]$dataMap['TaskName']
        instance_id = [string]$instanceId
        user_context = [string]$dataMap['UserContext']
        action_name = [string]$dataMap['ActionName']
        result_code = if ($dataMap.ContainsKey('ResultCode') -and $dataMap['ResultCode'] -ne '') { [int64]$dataMap['ResultCode'] } else { $null }
        computer_name = [string]$event.MachineName
    }
}

if ($MinRecordId -gt 0) {
    # The oldest surviving record rides in the same call as one extra row (no second process), so
    # the caller can tell a wrapped or cleared log from a quiet one. See event_gap_warning.
    # A failure here only loses the gap check, so it must not fail the read that already succeeded.
    $oldest = $null
    try { $oldest = Get-WinEvent -LogName $LogName -MaxEvents 1 -Oldest -ErrorAction Stop } catch { $oldest = $null }
    $marker = [ordered]@{ oldest_record_id = if ($oldest) { [int64]$oldest.RecordId } else { $null } }
    @($rows | Where-Object { $null -ne $_ }) + $marker | ConvertTo-Json -Depth 6 -Compress
} else {
    $rows | ConvertTo-Json -Depth 6 -Compress
}
"""


# "name: message" for each task the last collect_tasks call could not fully read. Module state, so
# collect_tasks keeps returning a plain task list for its callers; collect_everything clears it first.
unreadable_tasks: List[str] = []


def collect_tasks(timeout_seconds: int = 180, exclude_microsoft: bool = True) -> List[Dict[str, Any]]:
    script = POWERSHELL_TASK_QUERY.replace("__EXCLUDE_MICROSOFT__", "$true" if exclude_microsoft else "$false")
    rows = run_powershell_json(script, timeout_seconds)
    payload = rows[0] if rows else {}
    errors = [e for e in payload.get("errors") or [] if isinstance(e, dict)]
    tasks = [t for t in payload.get("tasks") or [] if isinstance(t, dict)]
    unreadable_tasks.extend(
        f"{normalize_text(e.get('name'))}: {normalize_text(e.get('message'))}" for e in errors)
    excluded = payload.get("excluded")
    if errors and not tasks and not (isinstance(excluded, int) and excluded > 0):
        # SilentlyContinue turns a failed listing (service stopped, no access to the root) into
        # errors and zero tasks. Carrying on would publish an empty inventory and mail a false
        # "recovered", so this stays the loud failure it was before per-task errors were tolerated.
        # Built-in tasks the filter dropped prove the listing worked, so they are not "zero tasks".
        raise SystemExit(
            f"Cannot list scheduled tasks: {'; '.join(unreadable_tasks[-len(errors):])}. Check that "
            f"the Task Scheduler service is running and that the task account can read the task "
            f"folders, then run again.\n" + see_troubleshooting("TaskDash cannot list any scheduled task"))
    if errors:
        emit(f"WARNING: {len(errors)} scheduled task problem(s); the readable tasks are still "
             f"collected: {'; '.join(unreadable_tasks[-len(errors):])}. Repair or re-create the "
             f"task, or give the task account read access to it.")
    return tasks


class EventLogDisabled(SystemExit):
    """A SystemExit the publisher can tell apart, so a disabled log defers its exit to the end
    of the run instead of discarding the task inventory and the alerts."""


def check_event_log_health(
    log_name: str = OPERATIONAL_LOG_NAME, timeout_seconds: int = 180
) -> Dict[str, Any]:
    """Fail fast with the fix, instead of letting the history query's error handling turn a
    disabled or unreadable channel into an indistinguishable "no runs found" result.
    """
    script = POWERSHELL_LOG_HEALTH_QUERY_TEMPLATE.replace("__LOG_NAME__", log_name)
    rows = run_powershell_json(script, timeout_seconds)
    info = rows[0] if rows else {}
    if not info.get("exists", False):
        raise SystemExit(
            f"Cannot read event log '{log_name}': {info.get('error_message') or 'not found'}. "
            f"Run this collector elevated, or confirm the channel exists on this host.\n"
            + see_troubleshooting("The Task Scheduler log cannot be read")
        )
    if not info.get("is_enabled", False):
        raise EventLogDisabled(
            f"Event log '{log_name}' is disabled, so run history cannot be collected. Enable "
            f"it with: wevtutil sl \"{log_name}\" /e:true (run elevated), then retry."
        )
    return info


def _event_id_xpath_clause(event_ids: Tuple[int, ...]) -> str:
    return " or ".join(f"EventID={eid}" for eid in event_ids)


def _event_id_csv(event_ids: Tuple[int, ...]) -> str:
    return ",".join(str(eid) for eid in event_ids)


# Oldest record id the last collect_run_events call saw (incremental reads only); module state so
# the function keeps returning a plain event list, like unreadable_tasks.
oldest_record_id: Optional[int] = None


def collect_run_events(days_back: int, max_events: int, timeout_seconds: int = 180,
                       min_record_id: int = 0) -> List[Dict[str, Any]]:
    """Fetch run events. With min_record_id > 0 only newer records are read (incremental)."""
    check_event_log_health(timeout_seconds=timeout_seconds)
    script = (
        POWERSHELL_HISTORY_QUERY_TEMPLATE
        .replace("__DAYS_BACK__", str(int(days_back)))
        .replace("__MAX_EVENTS__", str(int(max_events)))
        .replace("__MIN_RECORD_ID__", str(int(min_record_id)))
        .replace("__EVENT_ID_XPATH__", _event_id_xpath_clause(HISTORY_EVENT_IDS))
        .replace("__EVENT_ID_CSV__", _event_id_csv(HISTORY_EVENT_IDS))
    )
    global oldest_record_id
    oldest_record_id = None
    rows = run_powershell_json(script, timeout_seconds)
    for row in rows:
        if "oldest_record_id" in row:
            oldest_record_id = safe_int(row["oldest_record_id"])
    return [row for row in rows if "oldest_record_id" not in row]


def event_gap_warning(min_record_id: int, oldest: Optional[int]) -> str:
    """Warning text when records between the newest one a run read and the oldest surviving one
    are gone. min_record_id is that read mark, not the run watermark (see EVENT_READ_MARK_FILE)."""
    if not min_record_id or oldest is None or oldest <= min_record_id + 1:
        return ""
    skipped = oldest - min_record_id - 1
    return (f"{skipped} event record id(s) were skipped: the oldest record left in the "
            f"Operational log is {oldest} but the last one read was {min_record_id}. The log "
            f"wrapped or was cleared since TaskDash last read it. Raise the log size "
            f"with the preflight event-log fix.")


POWERSHELL_NEWEST_RECORD_QUERY = r"""
$ErrorActionPreference = "Stop"
$newest = Get-WinEvent -LogName "Microsoft-Windows-TaskScheduler/Operational" -MaxEvents 1 -ErrorAction SilentlyContinue
[ordered]@{ record_id = if ($newest) { [int64]$newest.RecordId } else { $null } } | ConvertTo-Json -Compress
"""


def newest_record_id(timeout_seconds: int = 180) -> Optional[int]:
    """Newest EventRecordID in the Operational log, or None when the log is empty/unreadable."""
    rows = run_powershell_json(POWERSHELL_NEWEST_RECORD_QUERY, timeout_seconds)
    return safe_int(rows[0].get("record_id")) if rows else None


# --------------------------------------------------------------------------- result-code helpers

def normalize_hresult_code(code: Optional[int]) -> Optional[int]:
    if code is None:
        return None
    code = int(code)
    candidates = [code]
    if code < 0:
        candidates.append(code + (1 << 32))
    elif code > 0x7FFFFFFF:
        candidates.append(code - (1 << 32))
    for candidate in candidates:
        if candidate in HRESULT_TEXT:
            return candidate
    return candidates[-1]


def _win32_format_error(win32_code: int) -> Optional[str]:
    """ctypes.FormatError text for a plain Win32 error code, or None if Windows has no
    message for it. Never raises: an out-of-range or unrecognized code must fall through to
    the generic "Unmapped" text, not blow up result-text formatting for every other row.
    """
    try:
        text = ctypes.FormatError(win32_code)
    except (OSError, ValueError, OverflowError, ArithmeticError):
        return None
    text = (text or "").strip()
    if not text or text == "<no description>":
        return None
    return text


def describe_hresult(code: Optional[int]) -> str:
    if code is None:
        return "No task result available."
    normalized = normalize_hresult_code(code)
    text = HRESULT_TEXT.get(normalized)
    if text:
        return text
    if normalized is not None:
        unsigned = normalized if normalized >= 0 else normalized + (1 << 32)
        # FACILITY_WIN32 (0x8007xxxx) wraps a plain Win32 error in its low 16 bits. A bare
        # small code is NOT the same thing -- for a completed action it is the process's own
        # exit code, not a Win32 error, so running it through FormatError produces a plausible
        # but wrong message (e.g. exit 5 reading as "Access is denied."). Only unwrap the
        # facility-tagged form.
        if (unsigned & 0xFFFF0000) == 0x80070000:
            formatted = _win32_format_error(unsigned & 0xFFFF)
            if formatted:
                return formatted
    unsigned = code if code >= 0 else (code + (1 << 32))
    return f"Unmapped task result code ({code}, 0x{unsigned:08X})."


# The launch events carry a real HRESULT; event 201 (action completed) carries the exit code.
LAUNCH_FAILURE_EVENT_IDS = frozenset({101, 103, 202, 203})


def describe_task_result(code: Optional[int], action_completed: bool = False) -> str:
    """describe_hresult, except where the code is the script's own exit code.

    Measured on the task server (2026-09-25 capture): an action that exits 1 leaves LastTaskResult 1,
    but its event 201 reports 0x80070001 -- the exit code's low 16 bits wrapped as a Win32
    HRESULT (exit 1056 -> 0x80070420, exit 0x10000000 -> 0x80070000). Read as Win32, exit 1 is
    "Incorrect function." and exit 2 "File not found.", which send the reader after the wrong
    fault. So a small LastTaskResult is always an exit code, and the 0x8007xxxx form is one only
    when it came from event 201 (action_completed). Launch failures keep their HRESULT text.
    """
    if code is None:
        return describe_hresult(code)
    unsigned = int(code) + (1 << 32) if int(code) < 0 else int(code)
    if 0 < unsigned <= 0xFFFF:
        exit_code = unsigned
    elif action_completed and (unsigned & 0xFFFF0000) == 0x80070000:
        exit_code = unsigned & 0xFFFF
    else:
        return describe_hresult(code)
    # Low 16 bits of 0 on an error code: the real exit code was larger and 201 truncated it.
    return f"Script exited with code {exit_code}." if exit_code else "Script exited with a non-zero code."


# --------------------------------------------------------------------------- command classification

def strip_wrapping_quotes(value: Any) -> str:
    text = normalize_text(value)
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].strip()
    return text


def split_command_arguments(text: Any) -> List[str]:
    clean = normalize_text(text)
    if not clean:
        return []
    try:
        return [part.strip() for part in shlex.split(clean, posix=False) if part.strip()]
    except ValueError:
        return [clean]


def get_file_extension(path_value: Any) -> str:
    return os.path.splitext(strip_wrapping_quotes(path_value))[1].lower()


def classify_command_target(executable: Any, arguments: Any) -> Tuple[str, str]:
    """Return (target_path, target_type) -- what the task actually runs, not its interpreter."""
    exec_clean = strip_wrapping_quotes(executable)
    arg_tokens = [strip_wrapping_quotes(token) for token in split_command_arguments(arguments)]
    exec_name = ntpath.basename(exec_clean).lower()
    exec_ext = get_file_extension(exec_clean)

    if exec_name in PYTHON_EXECUTABLES or PYTHON_VERSIONED_NAME_RE.match(exec_name):
        for token in arg_tokens:
            if token and not token.startswith("-") and get_file_extension(token) in {".py", ".pyw"}:
                return token, "PythonScript"
        return exec_clean, "Python"

    if exec_name in POWERSHELL_EXECUTABLES:
        for idx, token in enumerate(arg_tokens):
            if token.lower() == "-file" and idx + 1 < len(arg_tokens):
                return strip_wrapping_quotes(arg_tokens[idx + 1]), "PowerShellScript"
        for token in arg_tokens:
            if token and not token.startswith("-") and get_file_extension(token) == ".ps1":
                return token, "PowerShellScript"
        return exec_clean, "PowerShell"

    if exec_name in CMD_EXECUTABLES:
        for token in arg_tokens:
            if token and not token.startswith(("/", "-")) and get_file_extension(token) in {".bat", ".cmd"}:
                return token, "BatchScript"
        return exec_clean, "CommandShell"

    if exec_name in SCRIPT_HOST_EXECUTABLES:
        for token in arg_tokens:
            ext = get_file_extension(token)
            if token and not token.startswith(("/", "-")) and ext in {".vbs", ".js"}:
                return token, "VBScript" if ext == ".vbs" else "JavaScript"
        return exec_clean, "ScriptHost"

    if exec_ext in SCRIPT_EXTENSIONS:
        return exec_clean, SCRIPT_TYPE_BY_EXT.get(exec_ext, "Executable")

    return exec_clean, "Executable" if exec_clean else ""


def script_arguments(executable: Any, arguments: Any, target_path: str) -> str:
    """The arguments the task passes to its script, e.g. "--apply" -- the Script line of Task
    detail showed only the path, so run options could not be seen (task_detail-script-hides-
    arguments). Tokens after the script; all tokens when the executable is the script."""
    tokens = split_command_arguments(arguments)
    target = strip_wrapping_quotes(target_path).lower()
    if target and target != strip_wrapping_quotes(executable).lower():
        stripped = [strip_wrapping_quotes(t).lower() for t in tokens]
        if target not in stripped:
            return ""
        tokens = tokens[stripped.index(target) + 1:]
    # Not cut to the field length here: publish.prepare_rows scrubs the whole value first, then
    # cuts. A cut first could split a password from the flag that lets the scrubber find it.
    return " ".join(tokens)


# --------------------------------------------------------------------------- schedule buckets

TRIGGER_TOKEN_MAP = {
    "boot": "Startup",
    "daily": "Daily",
    "event": "Event",
    "idle": "Idle",
    "logon": "Logon",
    "monthly": "Monthly",
    "monthlydow": "Monthly",
    "registration": "Registration",
    "scheduled": "Scheduled",
    "sessionstatechange": "Session State Change",
    "time": "One Time",
    "weekly": "Weekly",
}

NO_NEXT_RUN_BUCKETS = {
    "", "No Triggers", "One Time", "Logon", "Startup",
    "Event", "Idle", "Registration", "Session State Change",
}


def parse_trigger_tokens(trigger_types: Any) -> List[str]:
    text = normalize_text(trigger_types)
    if not text:
        return []
    return [part.strip() for part in text.split("|") if part.strip()]


def _repetition_bucket(seconds: Optional[float]) -> str:
    if not seconds or seconds >= 86400:
        return ""
    if seconds < 3600:
        return f"Every {round(seconds / 60)} min"
    return "Hourly" if seconds == 3600 else f"Every {seconds / 3600:g} h"


def get_schedule_bucket(trigger_types: Any, trigger_details: Any = None) -> str:
    """The dashboard Schedule selector groups on this value, so the set stays small and stable:
    "Every N min" / "Hourly" / "Every N h" when an enabled trigger repeats within a day, else
    the calendar kind ("Daily", "Weekly", "Monthly", "One Time", "Startup", "Logon", ...), two
    kinds as "A + B", "Multiple / Other" for three or more, and "No Triggers".

    trigger_details wins over trigger_types when present: the CIM layer reports a monthly
    trigger as the base class ("Scheduled"), and only the XML days read into trigger_details
    show it is monthly. A capture without trigger_details falls back to trigger_types.

    A single Weekly trigger is further split by how many days it actually fires
    (interactions-weekly-bucket-hides-frequency): 7 day-of-week bits is "Daily" in practice (see
    Task Dash, and the critic-04 collision it causes), 2-6 bits is "N days a week" (wording-13 /
    interactions-schedule-option-labels: "Weekly (N days)" read like a once-every-N-days
    interval), and 1 bit stays "Weekly". "N days a week" drops the substring "Weekly", so a
    consumer that tested for it -- check_silent_skip_settings -- now calls
    is_calendar_schedule_bucket instead; the Daily+Weekly pairing below sorts on the token set,
    computed before this rewrite, so it is unaffected. Only done for one bare trigger -- with
    trigger_types alone (no trigger_details) there is no days_of_week to read.
    """
    # ponytail: repetition wins even when bounded to a short window each day; add the
    # repetition_duration to the bucket if such a task ever needs its own group.
    repeat = _repetition_bucket(shortest_repetition_seconds(trigger_details))
    if repeat:
        return repeat
    triggers = _trigger_list(trigger_details)
    raw_tokens = ([("Monthly" if _month_recurrence(t) else normalize_text(t.get("type"))) for t in triggers]
                  if triggers else parse_trigger_tokens(trigger_types))
    tokens = {TRIGGER_TOKEN_MAP.get(token.lower(), token) for token in raw_tokens if token}
    if not tokens:
        return "No Triggers"
    if len(tokens) == 1:
        token = next(iter(tokens))
        # Every-2-weeks triggers keep plain "Weekly": their day count does not say how often.
        if token == "Weekly" and len(triggers) == 1 and (safe_int(triggers[0].get("weeks_interval")) or 1) == 1:
            bit_count = bin((safe_int(triggers[0].get("days_of_week")) or 0) & 0x7F).count("1")
            if bit_count == 7:
                return "Daily"
            if 2 <= bit_count <= 6:
                return f"{bit_count} days a week"
        return token
    # Only name the pair explicitly; three or more triggers is not usefully summarized.
    if len(tokens) == 2:
        if tokens == {"Daily", "Weekly"}:
            return "Daily + Weekly"
        if tokens == {"Daily", "Logon"}:
            return "Daily + Logon"
        return " + ".join(sorted(tokens))
    return "Multiple / Other"


def expects_next_run(schedule_bucket: str) -> bool:
    return normalize_text(schedule_bucket) not in NO_NEXT_RUN_BUCKETS


_DAYS_A_WEEK_RE = re.compile(r"^\d+ days a week$")


def is_calendar_schedule_bucket(schedule_bucket: str) -> bool:
    """True for a bucket built from a Daily/Weekly/Monthly trigger (a "+"-joined pair counts if
    either side does), so a reboot spanning the trigger time skips the run instead of catching
    up. False for a sub-day repeat ("Hourly", "Every N min/h") and for "No Triggers" and the
    instant kinds (Logon, Startup, ...). Explicit, not a "Weekly" substring test
    (interactions-schedule-option-labels): get_schedule_bucket's "N days a week" label for a
    2-6-day weekly trigger dropped that substring on purpose.
    """
    parts = normalize_text(schedule_bucket).split(" + ")
    return any(p in ("Daily", "Weekly", "Monthly") or _DAYS_A_WEEK_RE.match(p) for p in parts)


# --------------------------------------------------------------------------- health

def get_health_status(
    enabled: Any,
    state: Any,
    last_task_result: Optional[int],
    number_of_missed_runs: Optional[int],
    next_run_time: Any,
    audit_dt: Optional[datetime],
    schedule_bucket: str = "",
    overdue_grace_minutes: float = DEFAULT_OVERDUE_GRACE_MINUTES,
) -> str:
    if not bool(enabled):
        return "Disabled"

    state_text = normalize_text(state).lower()
    normalized_code = normalize_hresult_code(last_task_result)
    missed_runs = number_of_missed_runs or 0
    next_run_dt = parse_iso_datetime(next_run_time)
    grace = timedelta(minutes=overdue_grace_minutes)
    # Without a grace window, a run that starts a few seconds after its scheduled time (the
    # PowerShell event query alone can take that long) reads as overdue before it ever runs.
    is_overdue = bool(audit_dt and next_run_dt and next_run_dt < audit_dt - grace and state_text != "running")

    if state_text == "running" or normalized_code == 0x00041301:
        return "Running"
    if normalized_code in WARNING_RESULT_CODES:
        return "Warning"
    if missed_runs > 0 or is_overdue:
        return "Warning"
    if expects_next_run(schedule_bucket) and next_run_dt is None and state_text != "running":
        return "Warning"
    if normalized_code in (None, 0x00041303):
        return "NotYetRun"
    if normalized_code == 0x00041325:
        return "Running"
    if normalized_code in NON_ERROR_RESULT_CODES:
        return "Success"
    return "Error"


def build_attention_reason(
    health_status: str,
    result_text: str,
    number_of_missed_runs: Optional[int],
    next_run_in_hours: Optional[float],
    missing_next_run_expected: bool,
    overdue_grace_hours: float = 0.0,
) -> str:
    """Why the task needs a look, or "" when it does not. publish prepends a run failure or a
    STUCK/BLOCKED warning to this text, so it must never hold a success or "running" sentence:
    that produced "...failed to start; The operation completed successfully." Hours use the
    common short_duration ("overdue by 3.2 h") so one value reads the same everywhere.
    """
    reasons: List[str] = []
    missed_runs = number_of_missed_runs or 0

    if missed_runs > 0:
        # wording-13: "1 missed run(s)" read oddly for the (usual) case of exactly one.
        reasons.append(f"{missed_runs} missed run" + ("" if missed_runs == 1 else "s"))
    if next_run_in_hours is not None and next_run_in_hours < -overdue_grace_hours:
        reasons.append(f"Next run is overdue by {short_duration(abs(next_run_in_hours) * 3600)}")
    if missing_next_run_expected:
        reasons.append("Task has no next scheduled run")

    if health_status == "Error" and result_text:
        reasons.append(result_text)
    elif health_status == "Disabled":
        reasons.append("Task is disabled")
    elif health_status == "NotYetRun":
        reasons.append("Task has not yet run")
    elif health_status == "Warning" and not reasons and result_text:
        reasons.append(result_text)  # the warning comes from the result code itself (0x4130x)
    return "; ".join(reasons)


# --------------------------------------------------------------------------- filtering

def should_include_task(row: Dict[str, Any], scheduler_config: Dict[str, Any]) -> bool:
    task_name = normalize_text(row.get("task_name"))
    task_path = normalize_text(row.get("task_path"))

    # Whole folder name, as the PowerShell query does: \MicrosoftSync\ is not a Microsoft folder.
    folder = task_path.lower()
    if scheduler_config.get("exclude_microsoft_tasks", True) and (
            folder == "\\microsoft" or folder.startswith("\\microsoft\\")):
        return False
    if not scheduler_config.get("include_disabled", True) and not row.get("enabled"):
        return False

    for pattern_key, value in (
        ("task_name_regex", task_name),
        ("task_path_regex", task_path),
        ("action_regex", normalize_text(row.get("actions"))),
    ):
        pattern = normalize_text(scheduler_config.get(pattern_key))
        if pattern and not re.search(pattern, value):
            return False

    exclude_pattern = normalize_text(scheduler_config.get("exclude_task_name_regex"))
    if exclude_pattern and re.search(exclude_pattern, task_name):
        return False

    excluded_types = {
        normalize_text(t).lower()
        for t in scheduler_config.get("exclude_command_target_types", []) or []
    }
    if excluded_types:
        _, target_type = classify_command_target(
            row.get("primary_executable"), row.get("primary_arguments")
        )
        if normalize_text(target_type).lower() in excluded_types:
            return False

    return True


# --------------------------------------------------------------------------- config checks
#
# 3.3: task-definition problems that Get-ScheduledTaskInfo never flags as an error, but that
# quietly break a run -- a moved script, a task that survived a rename with the wrong LogonType,
# or settings that let one hung run swallow every run after it. Always Warning, never Error:
# these describe risk in the definition, not an observed failed run. Reported on the row as
# config_warnings ("; "-joined, capped) and logon_type, but left out of health_status/
# needs_attention so an already-passing task's health does not flip on a definition nit.

def check_broken_action_targets(
    row: Dict[str, Any], exists: Callable[[str], bool] = os.path.exists, hide_paths: bool = False
) -> List[str]:
    """The interpreter/executable (when it is a path, not a bare PATH-resolved name), the
    script it runs, and its working directory must exist on this host. Only meaningful when
    running on the task's own host, which the collector always does -- a pure function over
    (row, exists) so tests can inject a fake filesystem instead of hitting the real one.

    Paths are expanded (%SystemRoot%, %USERPROFILE%, etc.) before the exists() check -- Task
    Scheduler actions and working directories commonly use them, and the literal "%SystemRoot%"
    string is never a real path, which made every such action a false "not found" warning.

    hide_paths (privacy.hide_paths): the note says "(path hidden)" instead of the path, which can
    name a user profile or a share.
    """
    def shown(path: str) -> str:
        return "(path hidden)" if hide_paths else path

    warnings: List[str] = []
    executable = os.path.expandvars(strip_wrapping_quotes(row.get("primary_executable")))
    if executable and ntpath.dirname(executable) and not exists(executable):
        warnings.append(f"interpreter/executable not found: {shown(executable)}")

    script_path, _ = classify_command_target(row.get("primary_executable"), row.get("primary_arguments"))
    script_path = os.path.expandvars(script_path) if script_path else script_path
    if script_path and script_path != executable and ntpath.dirname(script_path) and not exists(script_path):
        warnings.append(f"script not found: {shown(script_path)}")

    working_dir = os.path.expandvars(strip_wrapping_quotes(row.get("primary_working_directory")))
    if working_dir and not exists(working_dir):
        warnings.append(f"working directory not found: {shown(working_dir)}")

    return warnings


ARCPY_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+arcpy\b", re.MULTILINE)


def script_imports_arcpy(script_path: Any,
                         read: Callable[[str], str] = lambda p: Path(p).read_text(
                             encoding="utf-8", errors="replace")) -> Optional[bool]:
    """True/False when the script can be read, None when it cannot (then assume it might)."""
    path = os.path.expandvars(strip_wrapping_quotes(script_path))
    if not path.lower().endswith((".py", ".pyw")):
        return None
    try:
        return bool(ARCPY_IMPORT_RE.search(read(path)))
    except OSError:
        return None


def check_logon_type_drift(
    primary_executable: Any,
    logon_type: Any,
    interactive_required_pattern: str = DEFAULT_INTERACTIVE_REQUIRED_PATTERN,
    imports_arcpy: Optional[bool] = None,
) -> List[str]:
    """A task that launches ArcGIS Pro's python needs LogonType Interactive -- that python is
    licensed only in the interactive session, so anything else dies on `import arcpy` with
    "The Product License has not been initialized" (see the single-use-license notes).
    Only arcpy needs the licence: a pyodbc-only script under Pro's python runs fine with a
    Password logon (Replica Lag Monitor on the task server does), so a script known NOT to
    import arcpy is not flagged. An unreadable script is still flagged.
    """
    executable = strip_wrapping_quotes(primary_executable)
    pattern = normalize_text(interactive_required_pattern)
    if not pattern or pattern.lower() not in executable.lower():
        return []
    current = normalize_text(logon_type)
    if current.lower() == "interactive" or imports_arcpy is False:
        return []
    return [
        f"Runs ArcGIS Pro Python with LogonType '{current or 'unknown'}'. With a Single Use licence, "
        "arcpy is licensed only in the Interactive session. Fix: Export-ScheduledTask, set LogonType "
        "to InteractiveToken, then Register-ScheduledTask -Xml (never Set-ScheduledTask -User -Password)."
    ]


def parse_iso8601_duration_seconds(text: Any) -> Optional[float]:
    """Minimal parser for the PT#H#M#S / P#D task-settings durations (e.g. "PT0S", "PT72H").
    None means unparseable/empty -- the caller must treat that as unknown, not zero, since a
    missing ExecutionTimeLimit defaults to PT72H, not "never times out".
    """
    value = normalize_text(text)
    if not value:
        return None
    match = re.match(
        r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
        r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
        value,
    )
    if not match or not any(match.groups()):
        return None
    parts = match.groupdict()
    return (
        float(parts["days"] or 0) * 86400
        + float(parts["hours"] or 0) * 3600
        + float(parts["minutes"] or 0) * 60
        + float(parts["seconds"] or 0)
    )


def plain_duration(text: Any) -> str:
    """"PT3H30M" -> "3 h 30 min", "P1D" -> "1 day", "P3D" -> "3 days"; "" for empty, zero or
    unparseable. Days are spelled out, not abbreviated to "d" (wording-13): this feeds
    describe_trigger/describe_triggers, which compute_definition_hash covers, so the change rode
    along with the DEFINITION_HASH_PREFIX v2 -> v3 bump (collector_tab-04 / wording-06) instead
    of needing one of its own.
    """
    seconds = int(parse_iso8601_duration_seconds(text) or 0)
    parts = []
    days, seconds = divmod(seconds, 86400)
    if days:
        parts.append(f"{days} day" + ("" if days == 1 else "s"))
    for unit, size in (("h", 3600), ("min", 60), ("s", 1)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count} {unit}")
    return " ".join(parts)


# Task Scheduler's DaysOfWeek bitmask (MSFT_TaskWeeklyTrigger), Sunday = 1.
WEEKDAY_BITS = (("Sun", 1), ("Mon", 2), ("Tue", 4), ("Wed", 8), ("Thu", 16), ("Fri", 32), ("Sat", 64))


def _trigger_recurrence(kind: str, trigger: Dict[str, Any]) -> str:
    """"every day"/"every week" repeats the trigger kind word describe_trigger already put in
    front, so it is dropped when the interval is 1 (collector_tab-04 / wording-06): a Daily
    trigger becomes "Daily at 23:50 ...", not "Daily every day at 23:50 ...". A Weekly trigger
    with every day-of-week bit set and a multi-week interval still reads "Weekly ... on every
    day ..." here (mask == 0x7F below): describe_trigger only converts the plain
    every-week case (weeks_interval in (0, 1)) to kind "Daily" before calling this, because
    skipping whole weeks is not the same as running daily.
    """
    if kind == "Daily":
        days = safe_int(trigger.get("days_interval")) or 1
        return "" if days == 1 else f"every {days} days"
    if kind == "Weekly":
        weeks = safe_int(trigger.get("weeks_interval")) or 1
        mask = safe_int(trigger.get("days_of_week")) or 0
        prefix = "" if weeks == 1 else f"every {weeks} weeks "
        if mask == 0x7F:
            return f"{prefix}on every day".strip()
        names = [name for name, bit in WEEKDAY_BITS if mask & bit]
        return (prefix + (f"on {', '.join(names)}" if names else "")).strip()
    return ""


def _month_recurrence(trigger: Dict[str, Any]) -> str:
    """A monthly trigger's days and months, e.g. "on day 1, 15, Last in Jan, Jul" or "on Mon, Fri
    of week 1, Last", from the XML lists the task query reads; "" for any other trigger."""
    def names(key: str) -> List[str]:
        return [v if v == "Last" or v.isdigit() else v[:3]
                for v in normalize_text(trigger.get(key)).split(",") if v]

    days, weekdays, weeks, months = (names(k) for k in (
        "days_of_month", "month_days_of_week", "weeks_of_month", "months"))
    if days:
        text = f"on day {', '.join(days)}"
    elif weekdays:
        text = f"on {', '.join(weekdays)}" + (f" of week {', '.join(weeks)}" if weeks else "")
    else:
        return ""
    return text + (f" in {', '.join(months)}" if 0 < len(months) < 12 else "")


def _plain_time(iso: str) -> str:
    """"06:00" -> "6:00 AM" (no leading zero) -- 24-hour reads as military time next to every
    12-hour time elsewhere on the dashboard (task_detail-mixed-time-formats / wording-10)."""
    return datetime.strptime(iso[11:16], "%H:%M").strftime("%I:%M %p").lstrip("0")


_FIXED_UTC_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


def _fixed_utc_offset(iso: str) -> str:
    """"2026-09-18T07:05:00-04:00" -> "-04:00"; "" when the boundary has no offset (a plain
    local wall-clock time, Task Scheduler's normal storage). A trigger with an offset was
    registered with "Synchronize across time zones", so its wall-clock start drifts 1 h across
    a daylight-saving change instead of following the local time zone rule
    (operations-dst-synchronized-triggers)."""
    match = _FIXED_UTC_OFFSET_RE.search(iso) if len(iso) >= 19 else None
    return match.group(1) if match else ""


def _offset_display(offset: str) -> str:
    """"-04:00" -> "UTC-04:00"; "Z" -> "UTC" (wording-utc-offset-note-vague)."""
    return "UTC" if offset == "Z" else f"UTC{offset}"


_OFFSET_HHMM_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def _fixed_offset_direction(offset: str) -> str:
    """Compare a trigger's fixed UTC offset against the server's own local time zone
    (time.timezone / time.altzone, the latter only meaningful when time.daylight is set) to say
    which way the run drifts across a daylight-saving change (wording-utc-offset-note-vague).

    "early": the offset matches local DST, so a trigger held at that literal offset fires 1 h
    early once the clock falls back to standard time. "late": the offset matches local standard
    time, so the trigger fires 1 h late once the clock springs forward. "moves": the offset is
    "Z" (UTC never observes DST, so it can never match either local offset) or matches neither
    local offset -- the direction depends on the reader's own zone, not the server's.
    """
    match = _OFFSET_HHMM_RE.match(offset)
    if not match:
        return "moves"
    sign = -1 if match.group(1) == "-" else 1
    offset_seconds = sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)
    if offset_seconds == -time.timezone:
        return "late"
    if time.daylight and offset_seconds == -time.altzone:
        return "early"
    return "moves"


def _plain_date(iso: str) -> str:
    """"2024-07-27" -> "Jul 27, 2024" -- ISO reads as raw data next to every "MMM D, YYYY" date
    elsewhere (task_detail-mixed-time-formats / wording-10)."""
    d = datetime.strptime(iso[:10], "%Y-%m-%d")
    return f"{d.strftime('%b')} {d.day}, {d.year}"


def describe_trigger(trigger: Dict[str, Any]) -> str:
    """One trigger as plain text, e.g. "Weekly on Sat at 8:00 PM (since Jan 5, 2024), repeats
    every 1 h for 1 day" -- "every day"/"every week" is dropped when the interval is 1
    (_trigger_recurrence). Deterministic from the trigger's fields, so the same text is both
    what the change summary shows and what definition_hash covers: a hashed field the reader
    cannot see would log a DEFINITION CHANGED row whose old and new text look identical.
    Boundaries are shown as Task Scheduler stores them (local wall-clock time).

    A Weekly trigger with every day-of-week bit set and no multi-week interval fires daily in
    practice (Task Dash's real trigger); it reads as "Daily", not "Weekly on every day"
    (task_detail-r2regress-taskdash-schedule-wording / collector_tab-08 /
    r2regress-wording-06-taskdash) -- get_schedule_bucket already called this "Daily", so the two
    now agree. A multi-week interval (weeks_interval > 1) keeps the "Weekly ... on every day"
    wording in _trigger_recurrence: skipping whole weeks is not the same as running daily.

    Not covered: an event trigger's subscription query and the state a non-calendar base-class
    trigger watches (WNF state change and similar). Only their type, boundaries, repetition,
    delays and enabled flag are read, so an edit to the query alone is not seen.

    A start boundary with a fixed UTC offset ("Synchronize across time zones") is shown with its
    local wall-clock time, same as any other trigger, plus ", fixed to UTC-04:00" (or ", fixed to
    UTC" for Z): dropping the offset silently would hide the fact that this time drifts 1 h
    across a daylight-saving change (operations-dst-synchronized-triggers). That text feeds
    definition_hash (DEFINITION_HASH_PREFIX v7), so only a task with such a trigger moves the
    hash.
    """
    raw_type = normalize_text(trigger.get("type"))
    kind = TRIGGER_TOKEN_MAP.get(raw_type.lower(), raw_type or "Scheduled")
    start = normalize_text(trigger.get("start"))
    fixed_offset = _fixed_utc_offset(start)
    offset_note = f", fixed to {_offset_display(fixed_offset)}" if fixed_offset else ""
    month = _month_recurrence(trigger)
    if month:
        kind = "Monthly"  # the CIM layer reports it as the base class, i.e. "Scheduled"
    elif (kind == "Weekly" and (safe_int(trigger.get("weeks_interval")) or 0) in (0, 1)
          and (safe_int(trigger.get("days_of_week")) or 0) & 0x7F == 0x7F):
        kind = "Daily"
    words = [kind, month or _trigger_recurrence(kind, trigger)]
    if start and kind in ("Daily", "Weekly", "Monthly"):
        words.append(f"at {_plain_time(start)}{offset_note} (since {_plain_date(start)})")
    elif start:
        words.append(f"at {_plain_date(start)} {_plain_time(start)}{offset_note}")
    extras = [" ".join(w for w in words if w)]

    interval = plain_duration(trigger.get("repetition_interval"))
    if interval:
        duration = plain_duration(trigger.get("repetition_duration"))
        extras.append(f"repeats every {interval} " + (f"for {duration}" if duration else "indefinitely"))
    for key, label in (("random_delay", "random delay"), ("delay", "delay")):
        if plain_duration(trigger.get(key)):
            extras.append(f"{label} {plain_duration(trigger.get(key))}")
    end = normalize_text(trigger.get("end"))
    if end:
        extras.append(f"until {_plain_date(end)} {_plain_time(end)}" if len(end) >= 16 else f"until {end}")
    if trigger.get("enabled") is False:
        extras.append("trigger disabled")
    return ", ".join(extras)


def _trigger_list(trigger_details: Any) -> List[Dict[str, Any]]:
    # ConvertTo-Json can unwrap a one-element array; a fixture captured before trigger_details
    # existed has none at all.
    if isinstance(trigger_details, dict):
        return [trigger_details]
    return [t for t in trigger_details or [] if isinstance(t, dict)]


def describe_triggers(trigger_details: Any) -> str:
    return " | ".join(describe_trigger(t) for t in _trigger_list(trigger_details))


def shortest_repetition_seconds(trigger_details: Any) -> Optional[float]:
    """The shortest Repetition.Interval over the task's enabled triggers, or None."""
    intervals = [parse_iso8601_duration_seconds(t.get("repetition_interval"))
                 for t in _trigger_list(trigger_details) if t.get("enabled") is not False]
    positive = [s for s in intervals if s]
    return min(positive) if positive else None


# The Settings-tab words for MultipleInstancesPolicy ("If the task is already running").
MULTIPLE_INSTANCES_WORDS = {
    "IgnoreNew": "Do not start a new instance", "Parallel": "Run a new instance in parallel",
    "Queue": "Queue a new instance", "StopExisting": "Stop the existing instance",
}


def describe_task_settings(raw: Dict[str, Any]) -> str:
    """Plain text for the three Settings-tab values check_silent_skip_settings reads
    (MultipleInstancesPolicy, ExecutionTimeLimit, StartWhenAvailable), e.g. "If already running:
    Stop the existing instance | Time limit: 3 days | Catch-up: on".

    A real edit to one of these used to be silent unless it also changed an existing
    config-warning note (collector_tab-03): they were not in compute_definition_hash, and
    _definition_parts had nothing to show even when they were. Like describe_triggers, this text
    is both what the change summary shows and what the hash covers.
    """
    raw_policy = normalize_text(raw.get("multiple_instances_policy"))
    policy = MULTIPLE_INSTANCES_WORDS.get(raw_policy, raw_policy or "unknown")
    limit = plain_duration(raw.get("execution_time_limit")) or "none"
    catchup = "on" if raw.get("start_when_available") else "off"
    return f"If already running: {policy} | Time limit: {limit} | Catch-up: {catchup}"


def check_silent_skip_settings(
    multiple_instances_policy: Any,
    execution_time_limit: Any,
    start_when_available: Any,
    schedule_bucket: str,
    repetition_seconds: Optional[float] = None,
) -> List[str]:
    """Settings that make a task skip work without ever showing an error.

    IgnoreNew + no time limit means one hung run silently blocks every later scheduled run --
    Task Scheduler just declines to start the new instance, with nothing in the event log.
    IgnoreNew + a time limit longer than the trigger's repetition interval does the same for
    every repetition until the limit ends the hung run (PT72H on an hourly task is 72 skipped
    starts). Only Repetition.Interval counts, not the Daily/Weekly recurrence itself: PT72H is
    Windows' default limit, so counting a daily recurrence would flag nearly every daily task.
    StartWhenAvailable=false on a Daily/Weekly/Monthly task means a reboot spanning the trigger
    time skips that run (a whole month, for a monthly task) instead of catching up once the
    machine is back. A task that repeats within the day is bucketed by its repetition
    ("Hourly", ...) and not flagged: the next repeat replaces a skipped one.

    Wording: the plain effect first, then "To fix:" naming the Settings-tab option that must
    change, with its XML name in parentheses -- the setting named is the one to act on, not
    whichever one happens to be on already (health_table-setting-note-wrong-option /
    task_detail-07 / collector_tab-05 / wording-05 found "Stop the task if it runs longer than"
    tagged with IgnoreNew, the *other* setting's XML name). The XML names stay because
    acknowledge_config_warnings fragments match them.
    """
    warnings: List[str] = []
    policy = normalize_text(multiple_instances_policy)
    limit_seconds = parse_iso8601_duration_seconds(execution_time_limit)
    if policy.lower() == "ignorenew" and limit_seconds == 0:
        warnings.append(
            "No time limit and \"Do not start a new instance\": one hung run blocks every later "
            "run. To fix: set \"Stop the task if it runs longer than\" (ExecutionTimeLimit, now "
            "PT0S) to a real limit. Or set \"If the task is already running\" "
            "(MultipleInstancesPolicy=IgnoreNew) to \"Stop the existing instance\"."
        )
    elif (policy.lower() == "ignorenew" and limit_seconds and repetition_seconds
          and limit_seconds > repetition_seconds):
        warnings.append(
            f"Time limit ({plain_duration(execution_time_limit)}) is longer than the repetition "
            f"interval ({plain_duration(f'PT{int(repetition_seconds)}S')}), so one hung run blocks "
            f"repeats. To fix: set \"Stop the task if it runs longer than\" (ExecutionTimeLimit) "
            f"below the repetition interval. Or set \"If the task is already running\" "
            f"(MultipleInstancesPolicy=IgnoreNew) to \"Stop the existing instance\"."
        )

    if start_when_available is False and is_calendar_schedule_bucket(schedule_bucket):
        warnings.append(
            "Catch-up is off: if the server is down at start time, the run is skipped until the "
            "next schedule. To fix: turn on \"Run task as soon as possible after a scheduled "
            "start is missed\" (StartWhenAvailable)."
        )
    return warnings


def check_dst_offset_trigger(trigger_details: Any) -> List[str]:
    """One note per distinct fixed UTC offset among a task's triggers (operations-dst-
    synchronized-triggers). "Synchronize across time zones" pins the StartBoundary to a literal
    UTC offset instead of the local time zone rule, so after a daylight-saving change the trigger
    fires 1 h off local wall-clock time. The other, unaffected triggers use a plain local
    wall-clock start with no offset at all.

    The note names the direction (early or late) and the season it applies in, by comparing the
    offset against the server's own local zone (_fixed_offset_direction) -- a bare "1 hour off"
    forced the reader to work out early-vs-late and from-when themselves
    (wording-utc-offset-note-vague).
    """
    warnings: List[str] = []
    seen: List[str] = []
    if not time.daylight:
        return warnings  # a host with no daylight-saving time: a fixed offset never drifts
    for trigger in _trigger_list(trigger_details):
        offset = _fixed_utc_offset(normalize_text(trigger.get("start")))
        if offset and offset not in seen:
            seen.append(offset)
            direction = _fixed_offset_direction(offset)
            if direction == "early":
                when = "in standard time it runs 1 hour early"
            elif direction == "late":
                when = "in daylight-saving time it runs 1 hour late"
            else:
                when = "its local start time moves by 1 hour at each daylight-saving change"
            warnings.append(
                f"Start time is fixed to {_offset_display(offset)}, so {when}. To fix: clear "
                "\"Synchronize across time zones\" on the trigger."
            )
    return warnings


def build_config_warnings(
    raw: Dict[str, Any],
    schedule_bucket: str,
    *,
    interactive_required_pattern: str = DEFAULT_INTERACTIVE_REQUIRED_PATTERN,
    exists: Callable[[str], bool] = os.path.exists,
    acknowledged: Sequence[str] = (),
    hide_paths: bool = False,
) -> str:
    """acknowledged: workflow_descriptions.json "acknowledge_config_warnings" text fragments. A
    warning containing one (case-insensitive) is dropped -- for a setting the owner keeps on
    purpose, e.g. StartWhenAvailable off on Server Restart Window, where a catch-up run would reboot
    servers in business hours. Without it the note can never be cleared."""
    warnings = [
        *check_broken_action_targets(raw, exists, hide_paths),
        *check_logon_type_drift(
            raw.get("primary_executable"), raw.get("logon_type"), interactive_required_pattern,
            script_imports_arcpy(classify_command_target(
                raw.get("primary_executable"), raw.get("primary_arguments"))[0])),
        *check_silent_skip_settings(
            raw.get("multiple_instances_policy"), raw.get("execution_time_limit"),
            raw.get("start_when_available"), schedule_bucket,
            shortest_repetition_seconds(raw.get("trigger_details")),
        ),
        *check_dst_offset_trigger(raw.get("trigger_details")),
    ]
    fragments = [f.lower() for f in (normalize_text(a) for a in acknowledged) if f]
    kept = [w for w in warnings if not any(f in w.lower() for f in fragments)]
    return "; ".join(w.rstrip(".") for w in kept)[:1000]  # no ".;" between two notes


# --------------------------------------------------------------------------- inventory rows

def build_inventory_rows(
    raw_tasks: List[Dict[str, Any]],
    *,
    host_name: str,
    descriptions: Dict[str, Any],
    audit_dt: Optional[datetime] = None,
    overdue_grace_minutes: float = DEFAULT_OVERDUE_GRACE_MINUTES,
    interactive_required_pattern: str = DEFAULT_INTERACTIVE_REQUIRED_PATTERN,
    exists: Callable[[str], bool] = os.path.exists,
    hide_paths: bool = False,
) -> List[Dict[str, Any]]:
    """hide_paths: the caller's privacy.hide_paths (taskdash_publish.privacy_hides decides it)."""
    audit_dt = audit_dt or utc_now()
    rows: List[Dict[str, Any]] = []

    for raw in raw_tasks:
        task_name = normalize_text(raw.get("task_name"))
        target_path, target_type = classify_command_target(
            raw.get("primary_executable"), raw.get("primary_arguments")
        )
        target_name = ntpath.basename(target_path) if target_path else ""
        schedule_bucket = get_schedule_bucket(raw.get("trigger_types"), raw.get("trigger_details"))

        # A per-task overdue_grace_minutes override (workflow_descriptions.json) beats the
        # global scheduler.overdue_grace_minutes default for this one task's health/overdue.
        description_entry = lookup_description_entry(descriptions, task_name, target_name)
        row_grace_minutes = description_entry["overdue_grace_minutes"]
        if row_grace_minutes is None:
            row_grace_minutes = overdue_grace_minutes
        grace_hours = row_grace_minutes / 60.0

        enabled = 1 if raw.get("enabled") else 0
        state = normalize_text(raw.get("state"))
        result_code = safe_int(raw.get("last_task_result"))
        missed_runs = safe_int(raw.get("number_of_missed_runs")) or 0

        last_run_dt = parse_iso_datetime(raw.get("last_run_time"))
        next_run_dt = parse_iso_datetime(raw.get("next_run_time"))
        hours_since_last_run = (
            round_hours((audit_dt - last_run_dt).total_seconds() / 3600.0) if last_run_dt else None
        )
        next_run_in_hours = (
            round_hours((next_run_dt - audit_dt).total_seconds() / 3600.0) if next_run_dt else None
        )

        health = get_health_status(
            enabled, state, result_code, missed_runs,
            raw.get("next_run_time"), audit_dt, schedule_bucket,
            overdue_grace_minutes=row_grace_minutes,
        )
        result_text = describe_task_result(result_code)
        missing_next_run = bool(
            expects_next_run(schedule_bucket) and next_run_dt is None and normalize_text(state).lower() != "running"
        )
        attention_reason = build_attention_reason(
            health, result_text, missed_runs, next_run_in_hours, missing_next_run, grace_hours
        )
        # A disabled task has no obligation to run, so a stale next_run_time is not overdue.
        # The grace window absorbs collection latency (event query + PowerShell startup) so a
        # run that starts moments after its scheduled time is not flagged overdue.
        is_overdue = 1 if (enabled and next_run_in_hours is not None and next_run_in_hours < -grace_hours
                           and normalize_text(state).lower() != "running") else 0
        last_run_failed = 1 if health == "Error" else 0
        needs_attention = 1 if health in {"Error", "Warning"} else 0

        # 3.3 config checks: definition-level risk, always Warning-severity and reported
        # separately from health_status/needs_attention -- see the block comment above.
        config_warnings = build_config_warnings(
            raw, schedule_bucket, interactive_required_pattern=interactive_required_pattern,
            exists=exists, acknowledged=description_entry["acknowledge_config_warnings"],
            hide_paths=hide_paths,
        )

        rows.append({
            "host_name": host_name,
            "task_name": task_name,
            "task_path": normalize_text(raw.get("task_path")),
            "task_full_name": normalize_text(raw.get("task_full_name")),
            # The owner-written text wins; the Windows task Description fills the gap.
            "description": description_entry["description"] or normalize_text(raw.get("description")),
            "enabled": enabled,
            "state": state,
            "schedule_bucket": schedule_bucket,
            "trigger_types": normalize_text(raw.get("trigger_types")),
            "schedule_detail": describe_triggers(raw.get("trigger_details"))[:1000],
            "settings_detail": (
                describe_task_settings(raw)
                + (" | Accepted: " + description_entry["acknowledge_reason"]
                   if description_entry["acknowledge_reason"] else "")
            )[:255],
            "next_run_time": raw.get("next_run_time"),
            "next_run_in_hours": next_run_in_hours,
            "run_as_user": normalize_text(raw.get("run_as_user")),
            "logon_type": normalize_text(raw.get("logon_type")),
            "run_level": normalize_text(raw.get("run_level")),
            "command_target_path": target_path,
            "command_target_name": target_name,
            "command_target_type": target_type,
            "command_target_working_directory": normalize_text(raw.get("primary_working_directory")),
            "command_target_arguments": script_arguments(
                raw.get("primary_executable"), raw.get("primary_arguments"), target_path),
            # Uncut, like command_target_arguments: prepare_rows scrubs, then cuts to the field length.
            "actions": normalize_text(raw.get("actions")),
            "last_run_time": raw.get("last_run_time"),
            "hours_since_last_run": hours_since_last_run,
            "last_result_code": result_code,
            "last_result_text": result_text,
            "last_run_failed": last_run_failed,
            "number_of_missed_runs": missed_runs,
            "is_overdue": is_overdue,
            "health_status": health,
            "needs_attention": needs_attention,
            "attention_reason": attention_reason[:1000],
            "config_warnings": config_warnings,
        })

    rows.sort(key=lambda r: r["task_name"].lower())
    return rows


def parse_description_entry(value: Any) -> Dict[str, Any]:
    """A workflow_descriptions.json task value is either a plain string (the original format,
    kept working) or {"description": str, "overdue_grace_minutes": N} to also override this
    one task's overdue grace window, "acknowledge_config_warnings": [text, ...] to drop
    config notes the owner accepts (see build_config_warnings), and "acknowledge_reason" to say
    why -- shown on the card next to the acknowledged setting (build_inventory_rows), not just
    kept for the file's own reader (overview-catchup-off-acknowledgement-not-shown).
    """
    if isinstance(value, dict):
        acknowledged = value.get("acknowledge_config_warnings") or []
        if isinstance(acknowledged, str):
            acknowledged = [acknowledged]
        return {
            "description": normalize_text(value.get("description")),
            "overdue_grace_minutes": safe_float(value.get("overdue_grace_minutes")),
            "acknowledge_config_warnings": [normalize_text(a) for a in acknowledged if normalize_text(a)],
            "acknowledge_reason": normalize_text(value.get("acknowledge_reason")),
        }
    return {"description": normalize_text(value), "overdue_grace_minutes": None,
            "acknowledge_config_warnings": [], "acknowledge_reason": ""}


def lookup_description_entry(
    descriptions: Dict[str, Any], task_name: str, target_name: str
) -> Dict[str, Any]:
    # Normalizing at lookup time (not just in load_descriptions) means a caller that still
    # hands build_inventory_rows the old flat {name: "text"} dict directly -- test fixtures do
    # -- keeps working: parse_description_entry accepts a plain string or an already-parsed
    # entry dict interchangeably.
    for candidate in (task_name, target_name):
        clean = normalize_text(candidate).lower()
        if clean and clean in descriptions:
            return parse_description_entry(descriptions[clean])
    return parse_description_entry("")


def lookup_description(descriptions: Dict[str, Any], task_name: str, target_name: str) -> str:
    return lookup_description_entry(descriptions, task_name, target_name)["description"]


def load_descriptions(path_value: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    import json

    source = path_value or (runtime_dir() / "workflow_descriptions.json")
    if not source.exists():
        return {}
    try:
        # utf-8-sig: Notepad and PowerShell 5 save UTF-8 with a BOM, which plain utf-8 rejects.
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        emit(f"WARNING: could not read descriptions from {source}: {exc}")
        return {}

    raw_map = payload.get("tasks", {}) if isinstance(payload, dict) else {}
    result: Dict[str, Dict[str, Any]] = {}
    for key, value in raw_map.items():
        clean_key = normalize_text(key).lower()
        if not clean_key or str(key).startswith("_"):
            continue
        entry = parse_description_entry(value)
        if (entry["description"] or entry["overdue_grace_minutes"] is not None
                or entry["acknowledge_config_warnings"] or entry["acknowledge_reason"]):
            result[clean_key] = entry
    return result


# --------------------------------------------------------------------------- run rows

def get_run_status(end_time_utc: Any, result_code: Optional[int]) -> str:
    if not normalize_text(end_time_utc):
        return "Running"
    normalized = normalize_hresult_code(result_code)
    if normalized is None:
        return "Unknown"
    if normalized in NON_ERROR_RESULT_CODES:
        return "Success"
    if normalized in WARNING_RESULT_CODES:
        return "Warning"
    return "Error"


def resolve_result_code(entries: List[Tuple[Optional[float], int]]) -> Optional[int]:
    """The 201 result code for a run with multiple actions.

    Old behavior kept whichever 201 was processed last, which was the OLDEST one for a
    time-window (newest-first) fetch -- a later action's failure was silently dropped. This
    takes the LAST action's code (highest record id), unless an earlier action already failed:
    a later action succeeding must never bury an earlier failure.

    That guarantee holds only WITHIN one collection pass and its full set of events for a run.
    upsert_by_key (publish side) replaces the whole stored TaskRuns row, and keep_stored_start
    restores only the start fields -- so if a multi-action run's failing 201 lands in pass N and
    its succeeding final 201 lands in pass N+1 (each pass sees only its own event window), the
    stored Error gets overwritten with Success. taskdash_publish.carry_stored_failure closes that
    gap on the publish side; this function cannot see across passes.
    """
    if not entries:
        return None
    ordered = sorted(entries, key=lambda pair: pair[0] if pair[0] is not None else -1.0)
    for _, code in ordered:
        if normalize_hresult_code(code) not in NON_ERROR_RESULT_CODES:
            return code  # first action that did not report success
    return ordered[-1][1]  # every action succeeded -- last action's code


def resolve_run_outcome(
    end_time_utc: Any, result_code: Optional[int], outcome_event_ids: Set[int]
) -> Tuple[str, bool, str]:
    """Pick the run's final (status, is_error, fallback_text).

    An outcome event (launch failure, killed on time limit, skipped, ...) is definitive: several
    of them (101/153/322/332) never get a 201 at all, so falling back to the result code would
    leave the run looking like nothing happened, or stuck "Running" forever. A more specific
    reason (OUTCOME_EVENT_PRIORITY) always wins over the generic 111 "terminated" event.
    """
    for event_id in OUTCOME_EVENT_PRIORITY:
        if event_id in outcome_event_ids:
            status, is_error, description = OUTCOME_EVENT_STATUS[event_id]
            return status, is_error, description
    run_status = get_run_status(end_time_utc, result_code)
    return run_status, run_status == "Error", ""


LAUNCH_FAILURE_PAIR_SECONDS = 10


def drop_paired_launch_failures(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop a 101 when the same task logged a 103/203 within a few seconds.

    Measured 2026-09-25: every 101 on the task server (424/424) and on the workstation (164/164) has a
    203 for the same task within 5 s. The 203 carries the instance id, so it already marks that
    launch as failed; the 101 has no instance id and would add a second failure row for it.
    A 101 with no partner is kept -- it is then the only record of the failed launch.
    """
    partners: Dict[str, List[datetime]] = {}
    for raw in events:
        if safe_int(raw.get("event_id")) in (103, 203):
            when = parse_iso_datetime(raw.get("event_time_utc"))
            if when:
                partners.setdefault(normalize_text(raw.get("task_full_name")), []).append(when)
    window = timedelta(seconds=LAUNCH_FAILURE_PAIR_SECONDS)

    def paired(raw: Dict[str, Any]) -> bool:
        when = parse_iso_datetime(raw.get("event_time_utc"))
        times = partners.get(normalize_text(raw.get("task_full_name")), [])
        return bool(when and any(abs(when - other) <= window for other in times))

    return [r for r in events if not (safe_int(r.get("event_id")) == 101 and paired(r))]


def build_run_rows(
    events: List[Dict[str, Any]],
    inventory_rows: List[Dict[str, Any]],
    *,
    host_name: str,
) -> List[Dict[str, Any]]:
    """Group Operational-log events into one row per run instance.

    Events 100/102 bracket the run; 201 carries the result code. A run whose 100 event fell
    outside the fetch window used to emit a NULL start_time (57 such rows in the old table);
    here the earliest observed event time is used instead and flagged start_time_estimated.
    OUTCOME_EVENT_STATUS events (launch failure, killed on time limit, skipped, ...) settle the
    run's status outright and also close it out (end_time_utc), so a killed or never-started
    run does not sit "Running" forever.
    """
    by_full_name = {r["task_full_name"]: r for r in inventory_rows if r.get("task_full_name")}
    grouped: Dict[str, Dict[str, Any]] = {}

    for raw in drop_paired_launch_failures(events):
        instance_id = normalize_text(raw.get("instance_id"))
        task_full_name = normalize_text(raw.get("task_full_name"))
        event_id = safe_int(raw.get("event_id"))
        if not task_full_name:
            continue
        snapshot = by_full_name.get(task_full_name)
        if snapshot is None:
            continue

        record_id = safe_float(raw.get("event_record_id"))
        event_time = normalize_text(raw.get("event_time_utc"))
        event_dt = parse_iso_datetime(event_time)
        # 322's InstanceId is the instance that is ALREADY running, not the rejected launch
        # (manifest: 'Task Scheduler did not launch task "%1" because instance "%2" of the
        # same task is already running', verified 2026-09-25 against wevtutil gp
        # Microsoft-Windows-TaskScheduler /ge /gm:true). Keying it there would let this benign
        # skip attach to -- and override the outcome of -- the real running instance's own row.
        if instance_id and event_id != 322:
            run_key = f"{host_name}|{instance_id}"
        elif record_id is not None:
            # Launch-failure/skip/not-logged-on events (101/153/332 on the task server) carry no
            # InstanceId or TaskInstanceId at all -- there was never a running instance to key
            # on. Each becomes its own row keyed by record id, rather than silently merging
            # into whatever run happens to share this (blank instance_id, task) pair.
            # The event time is in the key because record ids restart after a log clear, and a
            # bare rec<id> would then collide with a different event still held in retention.
            stamp = event_dt.strftime("%Y%m%dT%H%M%S") if event_dt else ""
            run_key = f"{host_name}|{task_full_name}|rec{int(record_id)}|{stamp}"
        else:
            continue  # nothing to key this event by

        group = grouped.setdefault(run_key, {
            "run_key": run_key,
            "instance_id": instance_id,
            "task_full_name": task_full_name,
            "task_name": snapshot.get("task_name"),
            "task_path": snapshot.get("task_path"),
            "user_context": "",
            "computer_name": normalize_text(raw.get("computer_name")) or host_name,
            "start_time_utc": None,
            "end_time_utc": None,
            "first_event_time_utc": None,
            "result_code_events": [],
            "outcome_event_ids": set(),
            "max_event_record_id": None,
            "min_event_record_id": None,
            "event_count": 0,
        })

        group["event_count"] += 1

        if user_context := normalize_text(raw.get("user_context")):
            group["user_context"] = group["user_context"] or user_context
        if record_id is not None:
            prior = group["max_event_record_id"]
            group["max_event_record_id"] = record_id if prior is None else max(prior, record_id)
            low = group["min_event_record_id"]
            group["min_event_record_id"] = record_id if low is None else min(low, record_id)
        if event_dt is not None:
            first = parse_iso_datetime(group["first_event_time_utc"])
            if first is None or event_dt < first:
                group["first_event_time_utc"] = event_time

        if event_id == 100:
            existing = parse_iso_datetime(group["start_time_utc"])
            if existing is None or (event_dt and event_dt < existing):
                group["start_time_utc"] = event_time
        elif event_id == 102:
            existing = parse_iso_datetime(group["end_time_utc"])
            if existing is None or (event_dt and event_dt > existing):
                group["end_time_utc"] = event_time
        elif event_id == 201:
            code = safe_int(raw.get("result_code"))
            if code is not None:
                group["result_code_events"].append((record_id, code))
        elif event_id in OUTCOME_EVENT_STATUS:
            group["outcome_event_ids"].add(event_id)
            existing = parse_iso_datetime(group["end_time_utc"])
            if existing is None or (event_dt and event_dt > existing):
                group["end_time_utc"] = event_time
            # 101/103/202/203 carry ResultCode too (e.g. access denied, logon failure) -- without
            # this the row gets result_code=None and the generic OUTCOME_EVENT_STATUS text
            # instead of the real reason resolve_result_code/describe_hresult would surface.
            code = safe_int(raw.get("result_code"))
            if code is not None:
                group["result_code_events"].append((record_id, code))

    rows: List[Dict[str, Any]] = []
    for group in grouped.values():
        start_estimated = 0
        start_time = group["start_time_utc"]
        if not start_time:
            start_time = group["first_event_time_utc"]
            start_estimated = 1 if start_time else 0
        if not start_time:
            continue  # nothing datable at all -- unusable row

        start_dt = parse_iso_datetime(start_time)
        end_dt = parse_iso_datetime(group["end_time_utc"])
        # An estimated start is the first event SEEN, usually the 201 a moment before the end, so
        # a duration from it is ~0 s and would drag every average down. Unknown beats wrong.
        duration_seconds = (
            round((end_dt - start_dt).total_seconds(), 2)
            if start_dt and end_dt and end_dt >= start_dt and not start_estimated else None
        )
        result_code = safe_int(resolve_result_code(group["result_code_events"]))
        run_status, run_is_error, outcome_text = resolve_run_outcome(
            group["end_time_utc"], result_code, group["outcome_event_ids"]
        )
        result_text = (
            describe_task_result(result_code, action_completed=not (
                group["outcome_event_ids"] & LAUNCH_FAILURE_EVENT_IDS)) if result_code is not None
            else outcome_text
            or ("Task is still running." if run_status == "Running" else "No result code available.")
        )

        rows.append({
            "host_name": host_name,
            "task_name": normalize_text(group["task_name"]),
            "task_path": normalize_text(group["task_path"]),
            "task_full_name": normalize_text(group["task_full_name"]),
            "run_key": group["run_key"],
            "instance_id": group["instance_id"],
            "start_time_utc": start_time,
            "start_time_estimated": start_estimated,
            "end_time_utc": group["end_time_utc"],
            "duration_seconds": duration_seconds,
            "duration_minutes": round(duration_seconds / 60.0, 2) if duration_seconds is not None else None,
            "result_code": result_code,
            "result_text": result_text[:500],
            "run_status": run_status,
            "is_completed": 1 if group["end_time_utc"] else 0,
            "is_success": 1 if run_status == "Success" else 0,
            "is_error": 1 if run_is_error else 0,
            "user_context": normalize_text(group["user_context"]),
            "event_count": safe_int(group["event_count"]),
            "max_event_record_id": group["max_event_record_id"],
            # Not a column (prepare_rows drops it): lets the publish-side merge tell a re-read of
            # stored events from new ones, so event_count is not double counted.
            "_min_event_record_id": group["min_event_record_id"],
        })

    rows.sort(key=lambda r: (r["task_name"].lower(), r["start_time_utc"] or ""))
    return rows

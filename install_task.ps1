<#
Registers the hourly TaskDash collector task from taskdash_task.xml.

Without -Apply it only renders and validates the XML and prints the task settings.
With -Apply it registers the task, then reads it back and checks the logon type.

    .\install_task.ps1 -InstallDir C:\TaskDash
    .\install_task.ps1 -InstallDir C:\TaskDash -Apply
    .\install_task.ps1 -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe -Apply

-InstallDir holds run_taskdash.cmd, and is also the working directory the task starts in.
-Python names the interpreter for run_taskdash.cmd (it becomes TASKDASH_PYTHON), or a
taskdash.exe, which the task then runs as "taskdash collect --apply".

The logon type is never changed after registration. Editing a registered task's principal with
the Set- cmdlet and -User -Password silently flips Password to another logon type, so a wrong
task is deleted and registered again instead.
#>
[CmdletBinding()]
param(
    # The example config's scheduler.collector_task_name; preflight and the late-collector check look it up.
    [string]$TaskName = "TaskDash Collector",
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [ValidateSet("Password", "Interactive", "S4U")][string]$LogonType = "Password",
    [string]$Python = "",
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

# [Console]::Error keeps the message on one line; a thrown error is wrapped at the console width.
function Fail([string]$Message) {
    [Console]::Error.WriteLine("ERROR: $Message")
    exit 1
}

if ($LogonType -eq "S4U") {
    Fail "S4U cannot read stored credentials: Windows gives S4U logons no access to encrypted files. Use Password."
}
if (-not (Test-Path -LiteralPath $InstallDir -PathType Container)) {
    Fail "InstallDir '$InstallDir' does not exist. Pass the folder that holds run_taskdash.cmd."
}
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
if ($Credential) { $User = $Credential.UserName }

if ($Python -match '(?i)taskdash\.exe$') {
    $command = $Python
    $arguments = "collect --apply"
} else {
    $wrapper = Join-Path $InstallDir "run_taskdash.cmd"
    if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
        Fail "run_taskdash.cmd is not in '$InstallDir'. Point -InstallDir at the folder that holds it, or pass -Python <path to taskdash.exe>."
    }
    if ($Python) {
        # Task Scheduler cannot set an environment variable for an action, so cmd sets it first.
        $systemRoot = if ($env:SystemRoot) { $env:SystemRoot } else { "C:\Windows" }
        $command = Join-Path $systemRoot "System32\cmd.exe"
        $arguments = "/d /c `"set `"TASKDASH_PYTHON=$Python`"&& `"$wrapper`" --apply`""
    } else {
        $command = $wrapper
        $arguments = "--apply"
    }
}

$xmlLogonType = @{ Password = "Password"; Interactive = "InteractiveToken" }[$LogonType]
$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1).ToString("yyyy-MM-ddTHH:mm:ss")
$values = @{
    AUTHOR            = $User
    USER_ID           = $User
    LOGON_TYPE        = $xmlLogonType
    COMMAND           = $command
    ARGUMENTS         = $arguments
    WORKING_DIRECTORY = $InstallDir
    START_BOUNDARY    = $start
}

$templatePath = Join-Path $PSScriptRoot "taskdash_task.xml"
if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
    Fail "taskdash_task.xml is not next to install_task.ps1. Restore it from the repository."
}
$xmlText = Get-Content -LiteralPath $templatePath -Raw -Encoding UTF8
foreach ($key in $values.Keys) {
    $escaped = [System.Security.SecurityElement]::Escape($values[$key])
    $xmlText = $xmlText.Replace("{{$key}}", $escaped)
}
if ($xmlText -match '\{\{[A-Z_]+\}\}') {
    Fail "taskdash_task.xml still has an unfilled placeholder ($($Matches[0])). Restore the template from the repository."
}
try {
    $doc = New-Object System.Xml.XmlDocument
    $doc.LoadXml($xmlText)
} catch {
    Fail "The rendered task XML does not parse: $($_.Exception.Message) Check -User, -InstallDir and -Python for unusual characters."
}

Write-Host "Task name:         $TaskName"
Write-Host "Runs as:           $User ($LogonType)"
Write-Host "Command:           $command $arguments"
Write-Host "Working directory: $InstallDir"
Write-Host "Trigger:           every 1 hour from $start, no end"
Write-Host "Limits:            2 hour run limit, no second instance, catch up a missed run"
# Measured in a clean Windows: a standard account saw 170 of 229 tasks (not those other accounts
# registered), and without 'Log on as a batch job' its every start failed at logon (event 104,
# 0x80070569). An administrator, even at this LeastPrivilege run level, saw all of them.
Write-Host "Needs:             $User in the local Administrators group; a standard account misses other accounts' tasks"

if (-not $Apply) {
    Write-Host "CHECK MODE - nothing registered. Re-run with -Apply."
    exit 0
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Fail "A task named '$TaskName' already exists. Delete it with Unregister-ScheduledTask -TaskName '$TaskName', or pass a different -TaskName."
}

$register = @{ TaskName = $TaskName; Xml = $xmlText; User = $User }
$plain = $null
if ($LogonType -eq "Password") {
    if (-not $Credential) {
        $secure = Read-Host -AsSecureString "Password for $User"
        $Credential = New-Object System.Management.Automation.PSCredential($User, $secure)
    }
    $plain = $Credential.GetNetworkCredential().Password
    $register.Password = $plain
}
try {
    Register-ScheduledTask @register | Out-Null
} finally {
    $plain = $null
    $register.Remove("Password")
}

$actual = (Get-ScheduledTask -TaskName $TaskName).Principal.LogonType
Write-Host "Registered '$TaskName'. Principal.LogonType = $actual"
if ("$actual" -ne $LogonType) {
    Fail "Task '$TaskName' registered with logon type '$actual', not '$LogonType'. Delete it with Unregister-ScheduledTask -TaskName '$TaskName' and register it again."
}

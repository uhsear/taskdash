<#
Registers the hourly TaskDash collector task from taskdash_task.xml.

Without -Apply it only renders and validates the XML and prints the task settings.
With -Apply it registers the task, then reads it back and checks the logon type.
With -Replace, check mode also reads the existing task and runs every -Replace refusal below.

    .\install_task.ps1 -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe
    .\install_task.ps1 -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe -Apply
    .\install_task.ps1 -InstallDir C:\TaskDash -Apply

-InstallDir is the working directory the task starts in. A folder install also holds run_taskdash.cmd,
which TaskDash keeps as the wrapper of that install; the wheel does not ship it.
-Python names a taskdash.exe, which the task then runs as "taskdash collect --apply", or the interpreter
for run_taskdash.cmd (it becomes TASKDASH_PYTHON). Without -Python, the task runs run_taskdash.cmd when
the folder holds it, else <InstallDir>\venv\Scripts\taskdash.exe, which a wheel install makes.

-Replace rotates the task account's password. It registers the same XML over the existing task
(-Force), for the same account and the same logon type, and asks for the new password. It keeps the
existing task's command, arguments, working directory, trigger and settings, so a hand-added argument
or a changed schedule survives; -Python does not change them. It refuses when no task has that name,
when the task runs as another account or logon type, or when it has no single program action. The
account must match as DOMAIN\name, ignoring case; the name alone counts only when Task Scheduler
reports no domain. A different domain or computer name in front of the same account name is accepted,
so a renamed computer's local account (OLD\svc, now NEW\svc) keeps its task; the task is registered
again as the account that -User names. Without -Replace, an existing task is an error.

    .\install_task.ps1 -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe -User DOMAIN\svc-taskdash -Replace -Apply

-Apply refuses early when the window is not elevated or the task account is not in the local
Administrators group. Check mode reports both and still exits 0.
-EnableEventLog runs "taskdash preflight --enable-event-log --apply" through the venv's taskdash.exe
before it registers the task. -StartNow starts the task once after -Apply registers it, waits up to
10 minutes, and decodes LastTaskResult. Both switches only print their plan without -Apply.

    .\install_task.ps1 -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe -EnableEventLog -StartNow -Apply

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
    [switch]$Replace,
    [switch]$EnableEventLog,
    [switch]$StartNow,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

# [Console]::Error keeps the message on one line; a thrown error is wrapped at the console width.
function Fail([string]$Message) {
    [Console]::Error.WriteLine("ERROR: $Message")
    exit 1
}

# 'direct' when the account is a user in the group, 'group' when it may belong through one of the
# group's member groups, 'none' otherwise. The short name counts only for a bare account name:
# CORP\svc must not pass on a different PC01\svc (the rule of preflight's check_admin_account).
function Get-AdminMembership([string]$Account, $Members) {
    $bare = -not $Account.Contains('\')
    $short = ($Account -split '\\')[-1]
    foreach ($member in $Members) {
        if ("$($member.ObjectClass)" -ne 'User') { continue }
        $name = "$($member.Name)"
        if ($name -ieq $Account -or ($bare -and ($name -split '\\')[-1] -ieq $short)) { return 'direct' }
    }
    if (@($Members | Where-Object { "$($_.ObjectClass)" -eq 'Group' }).Count -gt 0) { return 'group' }
    return 'none'
}

# Cmdlets return LastTaskResult as unsigned and other tools as signed, so mask to 32 bits first.
# The numbers are decimal on purpose: PowerShell reads a hex literal above 0x7FFFFFFF as negative.
function Get-TaskResultText([int64]$Code) {
    $code = $Code -band 4294967295
    $hex = '0x{0:X8}' -f $code
    switch ($code) {
        0 { return "$hex - the run succeeded." }
        1 { return "$hex - TaskDash ran and stopped with an error. Run 'taskdash status', then read the last line of the newest file in the Logs folder of the install folder." }
        267009 { return "$hex - the task is still running." }
        267011 { return "$hex - the task never started. The task account probably lacks the right to log on as a batch job. Confirm it is in the local Administrators group, then read the task's History tab in Task Scheduler (event 104)." }
        2147943726 { return "$hex - Windows rejected the task account's password. Run this script again with -Replace -Apply and type the right password." }
        2147943785 { return "$hex - the task account may not log on as a batch job. Add it to the local Administrators group, then run this script again with -Replace -Apply." }
        default { return "$hex - not a code TaskDash knows. Read the task's History tab in Task Scheduler for the reason." }
    }
}

# Returns LastTaskResult of the run this call started, or $null when the run is still going at the deadline.
function Start-TaskAndWait([string]$Name, [int]$TimeoutSeconds) {
    $before = (Get-ScheduledTaskInfo -TaskName $Name).LastRunTime
    $began = Get-Date
    Start-ScheduledTask -TaskName $Name
    while (((Get-Date) - $began).TotalSeconds -lt $TimeoutSeconds) {
        Start-Sleep -Seconds 5
        $info = Get-ScheduledTaskInfo -TaskName $Name
        $state = "$((Get-ScheduledTask -TaskName $Name).State)"
        # LastTaskResult keeps the previous run's value until this run ends. A run that fails at logon
        # may not move LastRunTime, so after 30 seconds a task that is not Running has settled.
        $settled = $info.LastRunTime -gt $before -or ((Get-Date) - $began).TotalSeconds -ge 30
        if ($state -ne 'Running' -and $settled) { return [int64]$info.LastTaskResult }
    }
    return $null
}

if ($LogonType -eq "S4U") {
    Fail "S4U cannot read stored credentials: Windows gives S4U logons no access to encrypted files. Use Password."
}
if (-not (Test-Path -LiteralPath $InstallDir -PathType Container)) {
    Fail "InstallDir '$InstallDir' does not exist. Pass the folder that holds run_taskdash.cmd."
}
$InstallDir = (Resolve-Path -LiteralPath $InstallDir).Path
if ($Credential) { $User = $Credential.UserName }

# -Apply stops at the first problem; check mode lists them and exits 0, so it also works in a normal window.
$problems = @()
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $problems += "This window is not elevated. Registering a task needs an administrator. Open PowerShell with Run as administrator, then run the command again."
}
# The group is found by its well-known SID, because its name changes with the Windows language.
try {
    $adminState = Get-AdminMembership $User (Get-LocalGroupMember -SID S-1-5-32-544 -ErrorAction Stop)
} catch {
    $adminState = 'unknown'
    Write-Host "WARNING: could not list the local Administrators group ($($_.Exception.GetType().Name)). Confirm '$User' is a member with: net localgroup Administrators"
}
if ($adminState -eq 'none') {
    $problems += "'$User' is not in the local Administrators group. TaskDash needs an administrator task account: a standard account misses other accounts' tasks and cannot log on as a batch job, so the collector never starts. From an elevated window run: net localgroup Administrators $User /add"
} elseif ($adminState -eq 'group') {
    Write-Host "WARNING: '$User' is not a direct member of the local Administrators group. Confirm it belongs through one of that group's member groups."
}
foreach ($problem in $problems) {
    if ($Apply) { Fail $problem }
    Write-Host "WARNING: -Apply would refuse. $problem"
}

# The wheel does not ship run_taskdash.cmd, so a wheel install has only the venv's taskdash.exe to run.
$venvExe = Join-Path $InstallDir "venv\Scripts\taskdash.exe"
if (-not $Python -and -not (Test-Path -LiteralPath (Join-Path $InstallDir "run_taskdash.cmd") -PathType Leaf) -and
        (Test-Path -LiteralPath $venvExe -PathType Leaf)) {
    $Python = $venvExe
}

if ($Python -match '(?i)taskdash\.exe$') {
    $command = $Python
    $arguments = "collect --apply"
} else {
    $wrapper = Join-Path $InstallDir "run_taskdash.cmd"
    if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
        Fail "Neither run_taskdash.cmd nor venv\Scripts\taskdash.exe is in '$InstallDir'. Point -InstallDir at the install folder, or pass -Python <path to taskdash.exe>."
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

# The event log fix must run through the same venv as the task, so find that taskdash.exe before anything is written.
$taskdashExe = if ($Python -match '(?i)taskdash\.exe$') { $Python } else { Join-Path $InstallDir "venv\Scripts\taskdash.exe" }
if ($EnableEventLog -and -not (Test-Path -LiteralPath $taskdashExe -PathType Leaf)) {
    Fail "-EnableEventLog needs the venv's taskdash.exe, and '$taskdashExe' does not exist. Pass -Python <path to taskdash.exe>."
}

$xmlLogonType = @{ Password = "Password"; Interactive = "InteractiveToken" }[$LogonType]
# 's' with the invariant culture: a custom format string follows the user's calendar and time
# separator (a Thai or Arabic culture writes a year such as 2569), and Task Scheduler rejects that.
$now = Get-Date
$start = $now.Date.AddHours($now.Hour + 1).ToString('s', [System.Globalization.CultureInfo]::InvariantCulture)
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
$will = if ($Apply) { "will" } else { "would" }
if ($EnableEventLog) { Write-Host "Event log:         $will run '$taskdashExe preflight --enable-event-log --apply' before registering" }
if ($StartNow) { Write-Host "Start now:         $will start the task once after registering and wait up to 10 minutes for LastTaskResult" }
if ($Replace) { Write-Host "Replace:           yes - the existing task is registered again with the new password and keeps its command, trigger and settings" }
Write-Host "Trigger:           every 1 hour from $start, no end"
Write-Host "Limits:            2 hour run limit, no second instance, catch up a missed run"
# Measured in a clean Windows: a standard account saw 170 of 229 tasks (not those other accounts
# registered), and without 'Log on as a batch job' its every start failed at logon (event 104,
# 0x80070569). An administrator, even at this LeastPrivilege run level, saw all of them.
Write-Host "Needs:             $User in the local Administrators group; a standard account misses other accounts' tasks"

# Reading the task is safe in check mode, so -Replace runs every refusal below before -Apply.
$existing = if ($Replace -or $Apply) { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
$verb = if ($Apply) { "Keeping" } else { "Would keep" }
if ($Replace) {
    if (-not $existing) {
        Fail "-Replace found no task named '$TaskName'. Run without -Replace to register it, or pass the right -TaskName."
    }
    # A new password must go to the same account and logon type. Anything else is a different task.
    # With a prefix on both sides the account name must match. The prefix may differ only after a
    # computer rename: the old prefix is a name this computer no longer has, and -User names this computer.
    # CORP\svc and PC\svc are different accounts, so any other prefix change is refused.
    # A bare name on only one side still has to equal the other side's account name.
    $existingUser = "$($existing.Principal.UserId)"
    $sameAccount = if ($existingUser.Contains('\') -and $User.Contains('\')) {
                       $oldPrefix, $newPrefix = ($existingUser -split '\\')[0], ($User -split '\\')[0]
                       ($existingUser -split '\\')[-1] -ieq ($User -split '\\')[-1] -and
                       ($oldPrefix -ieq $newPrefix -or ($newPrefix -ieq $env:COMPUTERNAME -and
                           $oldPrefix -ine $env:COMPUTERNAME -and $oldPrefix -ine $env:USERDOMAIN))
                   }
                   else { $existingUser -notmatch '\\' -and ($existingUser -ieq ($User -split '\\')[-1]) }
    if (-not $sameAccount) {
        Fail "Task '$TaskName' runs as '$existingUser', not '$User'. Pass -User $existingUser to rotate its password. A different computer or domain name is accepted only after a computer rename, when -User starts with this computer's name."
    }
    if ($existingUser -ine $User -and $existingUser.Contains('\') -and $User.Contains('\')) {
        Write-Host "NOTE: the task runs as '$existingUser'. It will be registered again as '$User', the same account name after a computer rename."
    }
    if ("$($existing.Principal.LogonType)" -ne $LogonType) {
        Fail "Task '$TaskName' logs on as '$($existing.Principal.LogonType)', not '$LogonType'. Pass -LogonType $($existing.Principal.LogonType), or delete the task with Unregister-ScheduledTask -TaskName '$TaskName' and register it again."
    }
    # The rendered action is the install default. A hand-added argument such as --allow-host-mismatch,
    # or a wrapper that sets TASKDASH_PYTHON, would be lost, and every run would then refuse to publish.
    $actions = @($existing.Actions)
    if ($actions.Count -ne 1 -or -not $actions[0].Execute) {
        Fail "Task '$TaskName' has $($actions.Count) action(s), or one that does not start a program. -Replace keeps one program action. Delete the task with Unregister-ScheduledTask -TaskName '$TaskName' and register it again."
    }
    $keep = $actions[0]
    $namespaces = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
    $namespaces.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    $exec = $doc.SelectSingleNode("//t:Actions/t:Exec", $namespaces)
    $exec.SelectSingleNode("t:Command", $namespaces).InnerText = "$($keep.Execute)"
    $exec.SelectSingleNode("t:Arguments", $namespaces).InnerText = "$($keep.Arguments)"
    $exec.SelectSingleNode("t:WorkingDirectory", $namespaces).InnerText = "$($keep.WorkingDirectory)"
    # The template's hourly trigger and settings would also reset a schedule or run limit changed by
    # hand, for example every 30 minutes, and `taskdash status` would then report the task stale.
    $oldDoc = New-Object System.Xml.XmlDocument
    $oldDoc.LoadXml([string](Export-ScheduledTask -TaskName $TaskName))
    $oldNamespaces = New-Object System.Xml.XmlNamespaceManager($oldDoc.NameTable)
    $oldNamespaces.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    foreach ($section in "Triggers", "Settings") {
        $oldNode = $oldDoc.SelectSingleNode("//t:$section", $oldNamespaces)
        if (-not $oldNode) {
            Fail "Task '$TaskName' has no $section section to keep. Delete the task with Unregister-ScheduledTask -TaskName '$TaskName' and register it again."
        }
        $newNode = $doc.SelectSingleNode("//t:$section", $namespaces)
        [void]$doc.DocumentElement.ReplaceChild($doc.ImportNode($oldNode, $true), $newNode)
    }
    $xmlText = $doc.OuterXml
    Write-Host "$verb the task's existing action: $($keep.Execute) $($keep.Arguments)"
    Write-Host "$verb the task's existing trigger and settings (schedule, run limit); the Trigger and Limits lines above do not apply."
}

if (-not $Apply) {
    if ($Replace) {
        Write-Host "Replace checks passed: '$TaskName' exists, runs as '$existingUser' ($($existing.Principal.LogonType)) and has one program action. -Apply would ask for the password of $User and register the task again over it."
    }
    Write-Host "CHECK MODE - nothing registered. Re-run with -Apply."
    exit 0
}
if (-not $Replace -and $existing) {
    Fail "A task named '$TaskName' already exists. To give it a new password, add -Replace. To start over, delete it with Unregister-ScheduledTask -TaskName '$TaskName', or pass a different -TaskName."
}

if ($EnableEventLog) {
    # Nothing is registered yet, so a failure here leaves the machine as it was apart from the log setting.
    & $taskdashExe preflight --enable-event-log --apply
    if ($LASTEXITCODE -ne 0) {
        Fail "'taskdash preflight --enable-event-log --apply' failed with exit code $LASTEXITCODE, so nothing was registered. Read its output above, fix the cause, then run this script again."
    }
}

$register = @{ TaskName = $TaskName; Xml = $xmlText; User = $User }
# -Force overwrites the task. The XML still sets the logon type and run level.
if ($Replace) { $register.Force = $true }
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
} catch {
    # A wrong password is the usual cause. The raw error is a CIM exception that does not say so.
    Fail "Windows refused to register '$TaskName': $($_.Exception.Message) Check that the password is the one for $User and that this window is elevated, then run this script again. Nothing was registered or changed."
} finally {
    $plain = $null
    $register.Remove("Password")
}

$actual = (Get-ScheduledTask -TaskName $TaskName).Principal.LogonType
Write-Host "Registered '$TaskName'. Principal.LogonType = $actual"
if ("$actual" -ne $LogonType) {
    Fail "Task '$TaskName' registered with logon type '$actual', not '$LogonType'. Delete it with Unregister-ScheduledTask -TaskName '$TaskName' and register it again."
}

if ($StartNow) {
    Write-Host "Starting '$TaskName' once and waiting up to 10 minutes..."
    $result = Start-TaskAndWait -Name $TaskName -TimeoutSeconds 600
    if ($null -eq $result) {
        Fail "'$TaskName' was still running after 10 minutes. It stays registered. Check it later with Get-ScheduledTaskInfo -TaskName '$TaskName', and read the newest file in the Logs folder of '$InstallDir'."
    }
    $text = Get-TaskResultText $result
    if ($result -ne 0) { Fail "First run of '$TaskName': LastTaskResult $text" }
    Write-Host "First run of '$TaskName': LastTaskResult $text Run '$taskdashExe status' to see the result in ArcGIS Online."
}

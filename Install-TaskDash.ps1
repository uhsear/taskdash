<#
Installs TaskDash in one elevated PowerShell window, by chaining the steps of docs/INSTALL.md.

Without -Apply it only checks and prints what each step would do. With -Apply it acts, and it
stops at the first step that fails, printing the list of what it created so far.

    .\Install-TaskDash.ps1 -InstallDir C:\TaskDash -Wheel C:\Users\me\Downloads\taskdash-1.3.0-py3-none-any.whl -TaskAccount DOMAIN\svc-taskdash
    .\Install-TaskDash.ps1 -InstallDir C:\TaskDash -Wheel C:\Users\me\Downloads\taskdash-1.3.0-py3-none-any.whl -TaskAccount DOMAIN\svc-taskdash -Apply

The steps, in order:
  1. Check that the window is elevated.
  2. Create InstallDir and lock its permissions, create the venv, and install the wheel. When a file
     named like the wheel plus .sha256 sits beside it, its SHA-256 must match first.
  3. Copy TaskDash.config.example.json to TaskDash.config.json when the config is missing.
  4. taskdash signin: store the ArcGIS sign-in for this account, then hand one to the task account.
  5. taskdash preflight (after the event-log fix, which preflight needs to pass).
  6. taskdash setup (the check), then taskdash setup --apply.
  7. install_task.ps1 with -EnableEventLog and -StartNow.
  8. taskdash status.

A step that is already done prints "nothing to do", so the script is safe to run again. It does not
change an installed version: UPGRADE.md covers that. It never asks for, reads or prints a password:
taskdash signin and install_task.ps1 prompt for them in the console.

-Wheel is needed only while TaskDash is not installed in InstallDir.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [string]$Wheel = "",
    [Parameter(Mandatory = $true)][string]$TaskAccount,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$script:created = @()

# A copy of install_task.ps1's Get-AdminMembership: the installer must refuse the account before it
# creates anything, and install_task.ps1 runs its own checks when loaded. A test keeps the two equal.
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

# Prints what exists so far, so a failed run can be undone by hand or finished by running again.
function Stop-Install([string]$Message) {
    if ($script:created.Count -gt 0) {
        Write-Host "Created so far:"
        foreach ($item in $script:created) { Write-Host "    $item" }
    } else {
        Write-Host "Created so far: nothing"
    }
    # [Console]::Error keeps the message on one line; a thrown error is wrapped at the console width.
    [Console]::Error.WriteLine("ERROR: $Message")
    exit 1
}

function Report([string]$Step, [string]$Text) {
    Write-Host ("{0,-10} {1}" -f $Step, $Text)
}

function Get-WheelVersion([string]$Path) {
    # pip rejects a wheel whose name is not in this form, so a renamed file stops here instead.
    if ((Split-Path -Leaf $Path) -match '^taskdash-([^-]+)-py3-none-any\.whl$') { return $Matches[1] }
    return $null
}

# The first 64 hex characters of the file, so both "<hash>" and "<hash>  <file name>" work.
function Get-ExpectedHash([string]$SumFile) {
    $line = Get-Content -LiteralPath $SumFile -TotalCount 1 -ErrorAction SilentlyContinue
    if ("$line" -match '^\s*([0-9A-Fa-f]{64})(\s|$)') { return $Matches[1].ToLower() }
    return $null
}

# The hash that a SHA256SUMS.txt line "<hash>  <file name>" gives for one file name, or $null.
# This is the file release.yml writes, so the release page needs no per-wheel .sha256 file.
function Get-ListedHash([string]$SumsFile, [string]$Name) {
    foreach ($line in Get-Content -LiteralPath $SumsFile -ErrorAction SilentlyContinue) {
        if ("$line" -match '^\s*([0-9A-Fa-f]{64})\s+\*?(.+?)\s*$' -and $Matches[2] -eq $Name) { return $Matches[1].ToLower() }
    }
    return $null
}

# The sums are read from beside $WheelPath; the bytes hashed are those of $HashPath (a locked copy).
function Confirm-WheelHash([string]$WheelPath, [string]$HashPath = $WheelPath) {
    $wheelName = Split-Path -Leaf $WheelPath
    $sumFile = "$WheelPath.sha256"
    $sumsFile = Join-Path (Split-Path -Parent $WheelPath) "SHA256SUMS.txt"
    if (Test-Path -LiteralPath $sumFile -PathType Leaf) {
        $sumName = Split-Path -Leaf $sumFile
        $expected = Get-ExpectedHash $sumFile
        if (-not $expected) {
            Stop-Install "'$sumFile' does not start with a 64-character SHA-256 value. Download the .sha256 file again, or delete it to skip the check."
        }
    } elseif (Test-Path -LiteralPath $sumsFile -PathType Leaf) {
        $sumFile = $sumsFile
        $sumName = "SHA256SUMS.txt"
        $expected = Get-ListedHash $sumsFile $wheelName
        if (-not $expected) {
            Stop-Install "'$sumsFile' does not list '$wheelName'. It may belong to another release. Download SHA256SUMS.txt from the same release as the wheel, or delete it to skip the check."
        }
    } else {
        Report "wheel" "no $wheelName.sha256 or SHA256SUMS.txt beside the wheel, so its SHA-256 was not checked. Download SHA256SUMS.txt from the release into the same folder to check it."
        return
    }
    $actual = (Get-FileHash -LiteralPath $HashPath -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Stop-Install "The SHA-256 of '$WheelPath' does not match '$sumFile'. Do not install this file. Download the wheel and $sumName again."
    }
    Report "wheel" "SHA-256 matches $sumName."
}

# Returns the installed version, or $null when the venv has no taskdash.exe.
function Get-InstalledVersion {
    if (-not (Test-Path -LiteralPath $taskdashExe -PathType Leaf)) { return $null }
    $output = & $taskdashExe --version
    if ($LASTEXITCODE -eq 0 -and "$output" -match '^TaskDash (\S+)') { return $Matches[1] }
    Stop-Install "'$taskdashExe --version' failed (exit code $LASTEXITCODE). The venv is broken. Move '$venvDir' away, then run this script again."
}

function Invoke-Taskdash([string[]]$Arguments) {
    & $taskdashExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "'taskdash $($Arguments -join ' ')' failed with exit code $LASTEXITCODE. Read its output above, fix the cause, then run this script again. Finished steps are skipped."
    }
}

# The accounts that may own or change the install folder and its parents: SYSTEM, Administrators,
# TrustedInstaller (the owner of C:\Program Files) and this administrator.
function Get-TrustedSids {
    return @("S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
}

# True when the rules grant a write right to any account that is not trusted. Domain Users,
# INTERACTIVE or one named user can be as dangerous as Users, so only trusted accounts are listed.
# Compared by SID, so it holds on any Windows language. A deny rule grants nothing, so it is skipped.
# CREATOR OWNER passes its rights to whoever creates a child, and that right to create is checked.
# The task account is trusted once it resolves; an account that does not resolve counts as loose.
# GENERIC_WRITE (0x40000000) and GENERIC_ALL (0x10000000) are not FileSystemRights names, but an
# inherit-only (GW) or (GA) entry is stored with them, and files made later inherit a write right.
# preflight's WRITE_RIGHTS counts them too.
function Test-LooseRules($Rules,
        [System.Security.AccessControl.FileSystemRights]$Write = "WriteData, AppendData, WriteExtendedAttributes, WriteAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership",
        [int]$Generic = 0x50000000) {
    $trusted = @(Get-TrustedSids) + "S-1-3-0"
    if ($TaskAccount) {
        try {
            $trusted += (New-Object System.Security.Principal.NTAccount $TaskAccount).Translate([System.Security.Principal.SecurityIdentifier]).Value
        } catch {
            Write-Host "WARNING: '$TaskAccount' did not resolve to a SID, so its rights count as an ordinary user's."
        }
    }
    foreach ($rule in $Rules) {
        if ($rule.AccessControlType -ne "Allow") { continue }
        if ([int]($rule.FileSystemRights -band $Write) -eq 0 -and ([int]$rule.FileSystemRights -band $Generic) -eq 0) { continue }
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { return $true }
        if (-not ($trusted -contains $sid)) { return $true }
    }
    return $false
}

# True when ordinary users can write to the folder, for example one that inherits from C:\.
function Test-LooseAcl([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $true }
    return (Test-LooseRules (Get-Acl -LiteralPath $Path).Access)
}

# The owner of a folder can always change its permissions, so a folder that an ordinary user created
# stays theirs after icacls /grant:r. Only SYSTEM, Administrators, TrustedInstaller (the owner of
# C:\Program Files) or this administrator may own it.
function Test-UntrustedOwner([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    return -not ((Get-TrustedSids) -contains $owner)
}

# True when the path is a junction or a symbolic link, even one whose target is gone. Get-Acl and
# Test-Path follow a link, so the other checks would judge its target, and the user who made the link
# can point it at their own folder later.
function Test-ReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

# A user who can rename a parent folder can later move the locked folder away and put their own in
# its place. The drive root normally lets every user add folders, which is harmless; a root that also
# lets them delete or rename what is in it, or change its permissions, is not.
function Test-LooseRoot([string]$Path) {
    $rights = [System.Security.AccessControl.FileSystemRights]"DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership"
    return (Test-LooseRules (Get-Acl -LiteralPath $Path).Access $rights 0x10000000) -or (Test-UntrustedOwner $Path)
}

function Assert-TrustedParents {
    $parent = Split-Path -Parent $InstallDir
    while ($parent) {
        $isRoot = -not (Split-Path -Parent $parent)
        $loose = if ($isRoot) { Test-LooseRoot $parent } else { (Test-LooseAcl $parent) -or (Test-UntrustedOwner $parent) }
        if ((Test-ReparsePoint $parent) -or $loose) {
            Stop-Install "'$parent', a parent folder of -InstallDir, is missing or a link, or an ordinary user owns it or can write to it. That user could replace the install folder later. Use a folder directly under the drive, such as C:\TaskDash, or create and lock '$parent' first."
        }
        $parent = Split-Path -Parent $parent
    }
}

# Any user can create a folder under C:\, put files in it, and keep the right to change them, because
# a folder's owner can always reset its permissions. This window runs programs from the folder as an
# administrator, and taskdash signin sends the ArcGIS password to the URL in the folder's config. So
# the script never adopts such a folder, whatever it holds. Checked before anything in it runs, in
# check mode too.
function Assert-TrustedInstallDir {
    if (-not (Test-ReparsePoint $InstallDir) -and -not (Test-Path -LiteralPath $InstallDir)) { return }
    if ((Test-ReparsePoint $InstallDir) -or (Test-LooseAcl $InstallDir) -or (Test-UntrustedOwner $InstallDir)) {
        Stop-Install "'$InstallDir' already exists, and an ordinary user owns it or can write to it. A file in it may not be yours, and this script would run it as an administrator. Move the folder away, then run this script again. The script creates a new folder and locks it. Copy back only a config file that you have read."
    }
}

# The owner and permissions are part of the create call, so no other user can put a file in the
# folder before it is locked. The grants are those of step 1 of the guide.
function New-LockedFolder([string]$Path) {
    $sid = { param($Value) New-Object System.Security.Principal.SecurityIdentifier $Value }
    try {
        $task = (New-Object System.Security.Principal.NTAccount $TaskAccount).Translate([System.Security.Principal.SecurityIdentifier])
    } catch {
        Stop-Install "'$TaskAccount' is not an account that this computer knows. Check the -TaskAccount name, then run this script again."
    }
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner((& $sid "S-1-5-32-544"))
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($grant in @(@((& $sid "S-1-5-18"), "FullControl"), @((& $sid "S-1-5-32-544"), "FullControl"), @($me, "FullControl"), @($task, "Modify"))) {
        $security.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($grant[0], $grant[1], "ContainerInherit,ObjectInherit", "None", "Allow")))
    }
    # Windows PowerShell has the CreateDirectory overload; PowerShell 7 has the extension method instead.
    if ([System.IO.Directory].GetMethod("CreateDirectory", [type[]]@([string], [System.Security.AccessControl.DirectorySecurity]))) {
        [System.IO.Directory]::CreateDirectory($Path, $security) | Out-Null
    } else {
        [System.IO.FileSystemAclExtensions]::Create((New-Object System.IO.DirectoryInfo $Path), $security)
    }
    # A folder that another user made in the meantime is returned unchanged, so check the result.
    if ((Test-ReparsePoint $Path) -or (Test-LooseAcl $Path) -or (Test-UntrustedOwner $Path) -or @(Get-ChildItem -LiteralPath $Path -Force).Count -gt 0) {
        Stop-Install "'$Path' changed while this script created it, so another user may have made it first. Move the folder away, then run this script again."
    }
}

function Install-Venv([string]$WheelPath, [string]$WheelVersion) {
    # The task account cannot run a Python installed in a user profile, so only the all-users launcher counts.
    # Every Python call runs with -I, so a venv, pip or arcgis folder in the current folder is not imported.
    $launcher = Join-Path $env:SystemRoot "py.exe"
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        Stop-Install "py.exe is not in '$env:SystemRoot'. Install 64-bit Python 3.11 for all users (see Requirements in docs/INSTALL.md), then run this script again."
    }
    if (-not (Test-Path -LiteralPath $InstallDir)) {
        New-LockedFolder $InstallDir
        $script:created += "folder $InstallDir"
        Report "venv" "created $InstallDir, owned by Administrators and open only to SYSTEM, Administrators, you and $TaskAccount."
    }
    # Another user may have created the folder after Step-Venv checked it.
    Assert-TrustedInstallDir
    # pip reads the wheel again after the hash check, and the download folder is not locked. So copy
    # the wheel into the locked folder, check the copy, and install the copy.
    $wheelCopy = Join-Path $InstallDir (Split-Path -Leaf $WheelPath)
    Copy-Item -LiteralPath $WheelPath -Destination $wheelCopy -Force
    $script:created += $wheelCopy
    Confirm-WheelHash $WheelPath $wheelCopy
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        & $launcher -3.11 -I -m venv $venvDir
        if ($LASTEXITCODE -ne 0) {
            Stop-Install "'py -3.11 -m venv' failed with exit code $LASTEXITCODE. Run 'py -0p' to see the installed Pythons. Install 64-bit Python 3.11 for all users, then run this script again."
        }
        $script:created += "venv $venvDir"
    }
    & $venvPython -I -m pip install "${wheelCopy}[smtp]"
    if ($LASTEXITCODE -ne 0) {
        Stop-Install "pip could not install '$wheelCopy' (exit code $LASTEXITCODE). Read the pip output above. Check the connection to pypi.org, then run this script again."
    }
    if (-not (Get-InstalledVersion)) {
        Stop-Install "pip finished, but '$taskdashExe' does not exist. Move '$venvDir' away, then run this script again."
    }
    $script:created += "TaskDash $WheelVersion in $venvDir"
}

function Step-Venv {
    $wheelPath = ""
    $wheelVersion = $null
    if ($Wheel) {
        if (-not (Test-Path -LiteralPath $Wheel -PathType Leaf)) {
            Stop-Install "-Wheel '$Wheel' does not exist. Pass the path of the downloaded taskdash-<version>-py3-none-any.whl."
        }
        $wheelPath = (Resolve-Path -LiteralPath $Wheel).Path
        $wheelVersion = Get-WheelVersion $wheelPath
        if (-not $wheelVersion) {
            Stop-Install "'$(Split-Path -Leaf $wheelPath)' is not named taskdash-<version>-py3-none-any.whl. pip rejects a renamed wheel: download the file again and keep its name."
        }
        Confirm-WheelHash $wheelPath
    }
    Assert-TrustedParents
    Assert-TrustedInstallDir
    $installed = Get-InstalledVersion
    if ($installed) {
        if ($wheelVersion -and $wheelVersion -ne $installed) {
            Stop-Install "'$InstallDir' already has TaskDash $installed and -Wheel is $wheelVersion. This script does not change an installed version: follow docs/UPGRADE.md."
        }
        Report "venv" "TaskDash $installed is already installed in $venvDir; nothing to do."
        return
    }
    if (-not $wheelPath) {
        Stop-Install "TaskDash is not installed in '$venvDir', so -Wheel is required. Pass -Wheel <path of the downloaded wheel>."
    }
    if (-not $Apply) {
        Report "venv" "would create $InstallDir, lock its permissions, create the venv and install TaskDash $wheelVersion from $wheelPath."
        return
    }
    Install-Venv $wheelPath $wheelVersion
}

function Step-Config {
    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        Report "config" "$configPath exists; nothing to do."
        return
    }
    if (-not $Apply) {
        Report "config" "would copy TaskDash.config.example.json to $configPath."
        return
    }
    $example = Join-Path $shareDir "TaskDash.config.example.json"
    if (-not (Test-Path -LiteralPath $example -PathType Leaf)) {
        Stop-Install "The example config is not at '$example'. The wheel did not install completely: move '$venvDir' away, then run this script again with -Wheel."
    }
    Copy-Item -LiteralPath $example -Destination $configPath
    $script:created += $configPath
    Report "config" "copied the example config to $configPath."
}

# One value of the config, or "" when the config, the section or the key is missing.
function Get-ConfigValue([string]$Section, [string]$Key) {
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { return "" }
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Stop-Install "'$configPath' is not valid JSON ($($_.Exception.Message)). Fix the syntax; a missing comma is the usual cause."
    }
    $value = $config.$Section.$Key
    if ($null -eq $value) { return "" }
    return "$value"
}

function Test-SigninProfile([string]$ProfileName) {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { return $false }
    # A warning on stderr must not become an error here: only the exit code matters.
    $ErrorActionPreference = "Continue"
    & $venvPython -I -c "import sys; from arcgis.gis import ProfileManager; sys.exit(0 if sys.argv[1] in ProfileManager().list() else 1)" $ProfileName *> $null
    return ($LASTEXITCODE -eq 0)
}

function Test-CollectorTask {
    return [bool](Get-ScheduledTask -TaskName $collectorTask -ErrorAction SilentlyContinue)
}

function Step-Signin {
    $profileName = Get-ConfigValue "arcgis" "profile"
    if (-not $profileName) { $profileName = "taskdash" }
    $haveProfile = Test-SigninProfile $profileName
    # The task account cannot be read from here. Its sign-in is done once the task exists, or while
    # the hand-off file waits for the task's first run.
    $haveHandoff = (Test-CollectorTask) -or (Test-Path -LiteralPath (Join-Path $InstallDir "taskdash_handoff.bin") -PathType Leaf)
    if ($haveProfile) { Report "signin" "profile '$profileName' is already stored for this account; nothing to do." }
    if ($haveHandoff) { Report "signin" "the collector task or a hand-off file exists for $TaskAccount; nothing to do." }
    if ($haveProfile -and $haveHandoff) { return }
    if (-not $Apply) {
        Report "signin" "would run taskdash signin for this account and for $TaskAccount. It asks for the ArcGIS user name and password; the password is not shown."
        return
    }
    if (-not $haveProfile) {
        Invoke-Taskdash @("signin", "--apply")
        $script:created += "ArcGIS sign-in profile '$profileName' for $env:USERNAME"
    }
    if (-not $haveHandoff) {
        Invoke-Taskdash @("signin", "--for-task-account", "--task-user", $TaskAccount, "--apply")
        $script:created += "sign-in hand-off file for $TaskAccount (the task's first run stores it and deletes it)"
    }
}

# True when the Task Scheduler log is on and holds at least the 64 MB that preflight asks for.
function Test-EventLogReady {
    $ErrorActionPreference = "Continue"
    $text = (& (Join-Path $env:SystemRoot "System32\wevtutil.exe") gl "Microsoft-Windows-TaskScheduler/Operational" 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0) { return $false }
    return ($text -match '(?m)^enabled:\s*true' -and $text -match '(?m)^\s*maxSize:\s*(\d+)' -and [int64]$Matches[1] -ge 64MB)
}

function Step-Preflight {
    $logReady = Test-EventLogReady
    if (-not $Apply) {
        if ($logReady) { Report "preflight" "the event log is already on; nothing to do for it. Would run taskdash preflight." }
        else { Report "preflight" "would turn on the Task Scheduler event log, then run taskdash preflight." }
        return
    }
    # Preflight fails on an event log that is off or too small, and this step must pass before the task
    # is registered. A log that is already on is left alone, so a second run reports nothing to do for it.
    if ($logReady) { Report "preflight" "the event log is already on; nothing to do for it." }
    else { Invoke-Taskdash @("preflight", "--enable-event-log", "--apply") }
    Invoke-Taskdash @("preflight")
}

function Step-Setup {
    if (Get-ConfigValue "arcgis" "item_id") {
        Report "setup" "arcgis.item_id is already set in the config, so the ArcGIS items exist; nothing to do."
        return
    }
    if (-not $Apply) {
        Report "setup" "would run taskdash setup (the check), then taskdash setup --apply, to create the ArcGIS items."
        return
    }
    Invoke-Taskdash @("setup")
    Invoke-Taskdash @("setup", "--apply")
    $script:created += "the ArcGIS items; taskdash_setup_manifest.json lists them and taskdash remove undoes them"
}

# $Write false runs install_task.ps1 in its own check mode, which registers nothing.
function Invoke-InstallTask([bool]$Write) {
    $global:LASTEXITCODE = 0
    try {
        if ($Write) {
            & $installTask -InstallDir $InstallDir -Python $taskdashExe -User $TaskAccount -EnableEventLog -StartNow -Apply
        } else {
            & $installTask -InstallDir $InstallDir -Python $taskdashExe -User $TaskAccount -EnableEventLog -StartNow
        }
    } catch {
        $failure = $_.Exception.Message
        $global:LASTEXITCODE = 1
    }
    if ($LASTEXITCODE -eq 0) { return }
    if ($Write -and (Test-CollectorTask)) { $script:created += "scheduled task '$collectorTask'" }
    Stop-Install "install_task.ps1 failed. $failure Read its output above. If the task was registered, it stays: fix the cause, then run this script again."
}

function Step-Task {
    if (Test-CollectorTask) {
        Report "task" "'$collectorTask' is already registered; nothing to do."
        return
    }
    $canCheck = (Test-Path -LiteralPath $taskdashExe -PathType Leaf) -and (Test-Path -LiteralPath $installTask -PathType Leaf)
    if (-not $Apply) {
        if ($canCheck) { Invoke-InstallTask $false }
        else { Report "task" "would run install_task.ps1 to register '$collectorTask' for $TaskAccount, enable the event log and start it once." }
        return
    }
    Invoke-InstallTask $true
    $script:created += "scheduled task '$collectorTask', started once"
}

function Step-Status {
    if (-not $Apply) {
        Report "status" "would run taskdash status to read the first run back from ArcGIS Online."
        return
    }
    Invoke-Taskdash @("status")
}

# --- main: every step reads real state first, so a second run skips what is done.
if (-not [System.IO.Path]::IsPathRooted($InstallDir)) {
    Stop-Install "-InstallDir '$InstallDir' is not a full path. Pass one such as C:\TaskDash."
}
$venvDir = Join-Path $InstallDir "venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$taskdashExe = Join-Path $venvDir "Scripts\taskdash.exe"
$shareDir = Join-Path $venvDir "share\taskdash"
$installTask = Join-Path $shareDir "install_task.ps1"
$configPath = Join-Path $InstallDir "TaskDash.config.json"
# The default of install_task.ps1 -TaskName and of scheduler.collector_task_name in the example config.
$collectorTask = "TaskDash Collector"

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Report "elevation" "this window is elevated."
} else {
    $notElevated = "This window is not elevated. The install creates a folder under C:\, a scheduled task and an event-log setting, and each needs an administrator. Open PowerShell with Run as administrator, then run the command again."
    if ($Apply) { Stop-Install $notElevated }
    Write-Host "WARNING: -Apply would refuse. $notElevated"
}

# install_task.ps1 refuses a non-admin task account, but only after setup has created the ArcGIS
# items. Check it here first. The group is found by its well-known SID, because its name changes
# with the Windows language.
# The task account gets Modify on the install folder, and this window later runs taskdash.exe from
# it as an administrator. A non-admin task account could replace that file, so an account whose
# membership cannot be read is refused too. Membership through a group is the usual domain setup.
try {
    $adminState = Get-AdminMembership $TaskAccount (Get-LocalGroupMember -SID S-1-5-32-544 -ErrorAction Stop)
} catch {
    $adminState = 'unknown'
    $unknownAdmin = "could not list the local Administrators group ($($_.Exception.GetType().Name)), so '$TaskAccount' cannot be confirmed as an administrator. Run 'net localgroup Administrators'. If the list shows an account that no longer exists, remove it, then run this script again."
    if ($Apply) { Stop-Install $unknownAdmin }
    Write-Host "WARNING: -Apply would refuse. $unknownAdmin"
}
if ($adminState -eq 'none') {
    $notAdmin = "'$TaskAccount' is not in the local Administrators group. TaskDash needs an administrator task account. From an elevated window run: net localgroup Administrators $TaskAccount /add"
    if ($Apply) { Stop-Install $notAdmin }
    Write-Host "WARNING: -Apply would refuse. $notAdmin"
} elseif ($adminState -eq 'group') {
    Write-Host "WARNING: '$TaskAccount' is not a direct member of the local Administrators group. Confirm it belongs through one of that group's member groups."
}

# taskdash finds its config, Logs and state through TASKDASH_HOME. Only this process sees the value.
$env:TASKDASH_HOME = $InstallDir

Step-Venv
Step-Config
Step-Signin
Step-Preflight
Step-Setup
Step-Task
Step-Status

if (-not $Apply) {
    Write-Host "CHECK MODE - nothing changed. Re-run with -Apply."
    exit 0
}
if ($script:created.Count -eq 0) {
    Write-Host "Nothing to do: every step was already done."
} else {
    Write-Host "Created by this run:"
    foreach ($item in $script:created) { Write-Host "    $item" }
}
Write-Host "Done. Run '$taskdashExe status' at any time to check the install."

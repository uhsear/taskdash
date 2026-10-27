<#
.SYNOPSIS
Registers or removes the odd scheduled tasks that the CI live collect must read.

.DESCRIPTION
Used only by .github/workflows/matrix.yml on a throw-away runner. Do not run it on a real server.
Register creates eight harmless tasks that run cmd.exe as SYSTEM and writes their names to a JSON
manifest. Remove deletes them and the two folders. Names are built from [char] codes, because
Windows PowerShell 5.1 reads a script without a byte-order mark in the ANSI code page and would
garble a literal non-ASCII name.

.EXAMPLE
.\tests\ci\seed_tasks.ps1 -Action Register -ManifestPath $env:RUNNER_TEMP\seeded.json
#>
param(
    [Parameter(Mandatory)][ValidateSet("Register", "Remove")][string]$Action,
    [Parameter(Mandatory)][string]$ManifestPath
)

$ErrorActionPreference = "Stop"
$ciFolder = "\TaskDashCI\"
$syncFolder = "\MicrosoftSync\"
$accent = [string][char]0xE9
$kanji = (@(0x65E5, 0x672C) | ForEach-Object { [string][char]$_ }) -join ""

# Each case is a known collection trap: wildcard characters, repeated spaces, an apostrophe (SQL
# quoting), a folder whose name only starts with Microsoft, and a name outside ASCII.
$cases = @(
    @{ Path = $ciFolder; Name = "Plain probe" },
    @{ Path = $ciFolder; Name = "Brackets [1] and [a-z] probe" },
    @{ Path = $ciFolder; Name = "Spaces   inside   probe" },
    @{ Path = $ciFolder; Name = "Quote's probe" },
    @{ Path = $ciFolder; Name = "Caf$accent $kanji probe" },
    @{ Path = $syncFolder; Name = "Folder prefix probe" },
    # A monthly trigger reads back as the base trigger class, so the collector exports the task XML
    # for it. The bracket name read as a pattern also matches its look-alike below: if the export
    # took the wrong task, the days would not match.
    @{ Path = $ciFolder; Name = "Monthly [1] probe"; Days = 15 },
    @{ Path = $ciFolder; Name = "Monthly 1 probe"; Days = 3 }
)

function Get-MonthlyTaskXml {
    param([int]$Day)
    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T03:00:00</StartBoundary>
      <ScheduleByMonth>
        <DaysOfMonth><Day>$Day</Day></DaysOfMonth>
        <Months><January/><April/><July/><October/></Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal>
  </Principals>
  <Actions Context="Author"><Exec><Command>cmd.exe</Command><Arguments>/c exit 0</Arguments></Exec></Actions>
</Task>
"@
}

function Register-Cases {
    $act = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c exit 0"
    $trigger = New-ScheduledTaskTrigger -Daily -At "03:00"
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    foreach ($case in $cases) {
        if ($case.Days) {
            Register-ScheduledTask -TaskName $case.Name -TaskPath $case.Path -Xml (Get-MonthlyTaskXml $case.Days) `
                -Force | Out-Null
            continue
        }
        Register-ScheduledTask -TaskName $case.Name -TaskPath $case.Path -Action $act -Trigger $trigger `
            -Principal $principal -Force | Out-Null
    }
    $manifest = $cases | ForEach-Object { @{ path = $_.Path; name = $_.Name; days = $_.Days } }
    $json = ConvertTo-Json -InputObject @($manifest) -Compress
    [System.IO.File]::WriteAllText($ManifestPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Registered $($cases.Count) tasks; manifest at $ManifestPath"
}

function Remove-Cases {
    # Cleanup runs after a failed step, so a task that is already gone is not an error. With Stop,
    # Windows PowerShell 5.1 turns the stderr line of a native command into a terminating error.
    $ErrorActionPreference = "Continue"
    # schtasks.exe takes the name literally. Unregister-ScheduledTask reads [ ] as a wildcard set
    # and would miss the bracket task.
    foreach ($case in $cases) {
        & schtasks.exe /Delete /TN ($case.Path + $case.Name) /F 2>&1 | Out-Null
    }
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    foreach ($folder in @("TaskDashCI", "MicrosoftSync")) {
        try { $service.GetFolder("\").DeleteFolder($folder, 0) }
        catch { Write-Warning "Could not delete task folder ${folder}: $($_.Exception.Message)" }
    }
    Write-Host "Removed the seeded tasks."
}

if ($Action -eq "Register") { Register-Cases } else { Remove-Cases }

@echo off
REM TaskDash scheduled-task wrapper.
REM
REM PYTHONNOUSERSITE=1 is not optional: a per-user site-packages copy of `arcgis` will shadow
REM the Pro one and fail with version-skew errors that look nothing like the real cause.
REM
REM Output is redirected to a per-run Logs\bootstrap_<stamp>_<id>.log so a crash that happens
REM BEFORE setup_logging() runs -- bad config, missing module, wrong interpreter -- still leaves
REM a diagnosable record. Per run, not one shared file: cmd opens a redirect target exclusively,
REM so a second wrapper instance (a manual run during the hourly one) failed on the shared
REM bootstrap.log before python's run lock could even report the overlap.
REM
REM Exit code is propagated so Task Scheduler's Last Run Result reflects the actual outcome,
REM which is what the dashboard reads back.

setlocal
set PYTHONNOUSERSITE=1
set "PY=C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
REM TASKDASH_PYTHON, set in the task's environment, replaces the ArcGIS Pro interpreter above.
if defined TASKDASH_PYTHON set "PY=%TASKDASH_PYTHON%"

cd /d "%~dp0"
if not exist "Logs" mkdir "Logs"

REM The suffix is a GUID fragment, not %RANDOM%: cmd seeds %RANDOM% from the clock in whole
REM seconds, so two wrappers started in the same second drew the same number and collided.
for /f %%i in ('powershell -NoProfile -Command "(Get-Date -Format yyyyMMdd_HHmmss) + '_' + [guid]::NewGuid().ToString('N').Substring(0,8)"') do set "RUNID=%%i"
set "BOOT=Logs\bootstrap_%RUNID%.log"

"%PY%" taskdash_publish.py %* > "%BOOT%" 2>&1
set "RC=%ERRORLEVEL%"

REM Success: this run's output becomes Logs\bootstrap.log, the latest good run, as before.
REM Failure: keep it as bootstrap_FAIL_<stamp>_<id>.log -- the ONLY record of a crash outside the
REM timestamped log. Three failures on 2026-08-11 became undiagnosable when a shared bootstrap.log
REM was overwritten by the next hourly run. cleanup_old_logs ages out every bootstrap*.log.
if "%RC%"=="0" (
    move /y "%BOOT%" "Logs\bootstrap.log" >nul
) else (
    move /y "%BOOT%" "Logs\bootstrap_FAIL_%RUNID%.log" >nul
)

exit /b %RC%

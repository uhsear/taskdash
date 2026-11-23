# Upgrade and roll back TaskDash

This guide moves an installed TaskDash to a new release, and back to the previous one if the new
release fails. It also moves TaskDash to another computer, as "Move TaskDash to another computer"
describes. It assumes the install in [INSTALL.md](INSTALL.md) works. Plan for about 30 minutes.

Run every command on the computer that runs the collector. Use the two lines from step 1 of
[INSTALL.md](INSTALL.md) in each new PowerShell window. They set the folder and `$td`. Commands that
change the scheduled task, write to `C:\TaskDash` or install into its environment need a PowerShell
window **as administrator**. Your own account has Read & Execute only on that folder, as step 1 of
[INSTALL.md](INSTALL.md) explains, so such a command fails with access denied in a normal window.
Read-only commands, such as `status`, run in your normal window.

An upgrade does not change any password. To change one, follow [ROTATE.md](ROTATE.md).

The commands below use `C:\TaskDash\venv`. In a cloned ArcGIS Pro environment, replace `C:\TaskDash\venv\Scripts\python.exe` with
`C:\TaskDash\proenv\python.exe`. Then replace every other `venv` in a path with `proenv`, as "Use a
cloned ArcGIS Pro environment instead" in [INSTALL.md](INSTALL.md) describes.

## Upgrade

### Upgrade with the installer

If you installed TaskDash with `Install-TaskDash.ps1`, the same script can upgrade it. It does
steps 3 to 11 below for you, and step 12 only if you ask. Read the release notes first, as step 1
says. Download the new wheel, `requirements.lock` and `SHA256SUMS.txt` into one folder. Then, in an
administrator window, run the check first. It changes nothing:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-TaskDash.ps1" -InstallDir C:\TaskDash -Wheel "$env:USERPROFILE\Downloads\taskdash-<version>-py3-none-any.whl" -TaskAccount DOMAIN\svc-taskdash -Upgrade
```

When the check passes, run the same command with `-Apply` added. The script then works in this
order, and it stops at the first step that fails:

1. It disables the task and waits for a run in progress to end.
2. It copies `C:\TaskDash`, without `venv`, to a locked `C:\TaskDash_backup_<time>` folder.
3. It checks the wheel and the lock against `SHA256SUMS.txt`, then installs them.
4. It runs `taskdash collect --migrate --apply`. The command adds only what is missing, so a run
   with nothing to migrate changes nothing.
5. It runs `taskdash preflight`, enables the task and runs it once, then runs `taskdash status`.

A failure leaves the task disabled and prints how to roll back. "Roll back" below describes the
steps. Add `-RebuildDashboard` to rebuild the dashboard after the first run, as step 12 describes.
Without it, the script leaves the dashboard alone, because a rebuild replaces hand edits made in the
dashboard editor. If several computers publish to one service, upgrade each of them first. Then use
`-RebuildDashboard` on the last one.

The script refuses to start when the environment runs another Python than `-PythonVersion`, which is
3.13 unless you set it. Make a new environment first, as step 6 describes. The numbered steps below
do the same work by hand.

### Upgrade by hand

1. Read the release notes of every release after your installed version, up to the new one. A
   skipped release can change the schema or a default that the newest notes do not repeat. The notes
   are in [CHANGELOG.md](../CHANGELOG.md), and each release page shows its section. Each section opens with an
   **Upgrade notes** block. Note whether any of them changes the service schema, and which commands
   they ask for. A release that adds a table field says so. If none says anything about the schema, skip step 8.
2. Note the installed version, and keep its wheel file. You need it for a rollback. Check the
   Python version of the environment too. Release 1.2.0 needs Python 3.11 or later. Python 3.13 is
   the supported version, and the release's `requirements.lock` is built for it.

   ```powershell
   & $td --version
   C:\TaskDash\venv\Scripts\python.exe --version
   ```

   If the second command prints a version before 3.11, make a new environment in step 6. The pip
   command there fails on an older one. Do the same for 3.11 or 3.12, because the lock in step 6 may
   not install on them.

3. In the administrator window, disable the task. The next hourly run then cannot start while you
   work.

   ```powershell
   Disable-ScheduledTask -TaskName "TaskDash Collector"
   (Get-ScheduledTask -TaskName "TaskDash Collector").State
   ```

   The state must be `Disabled`. If a run is in progress, the task still shows `Running`. Wait until
   it ends before you go on.

4. In the administrator window, back up the config and the TaskDash folder. The copy leaves out the
   Python environment, which you can rebuild from the wheel. Replace `<date>` with today's date, for
   example `20261101`. The new folder under `C:\` lets every signed-in user read and change its
   files. So create the folder first and lock it, then copy into it. Only SYSTEM and Administrators
   can open it. If `New-Item` reports that the folder already exists, stop. Another user may own it.
   Choose a different name and start this step again.

   ```powershell
   New-Item -ItemType Directory C:\TaskDash_backup_<date> | Out-Null
   icacls C:\TaskDash_backup_<date> /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F"
   robocopy C:\TaskDash C:\TaskDash_backup_<date> /E /XD venv proenv
   ```

   The `icacls` line prints `Successfully processed 1 files; Failed processing 0 files`. `robocopy`
   exit codes 0 to 7 mean success. A code of 8 or more means a copy failed.

5. Download the new wheel and `requirements.lock` into `Downloads` from
   <https://github.com/uhsear/taskdash/releases/latest>. Check the SHA-256 value of each
   against `SHA256SUMS.txt` on the release page. Do not install a file that does not match.

   ```powershell
   Get-FileHash $env:USERPROFILE\Downloads\taskdash-<version>-py3-none-any.whl
   Get-FileHash $env:USERPROFILE\Downloads\requirements.lock
   ```

   You can also check who built the file with Sigstore. The steps are in the
   [requirements of INSTALL.md](INSTALL.md#requirements), under "The TaskDash wheel".

6. In the administrator window, install the wheel by its explicit path. Replace the path with the
   real one. Keep the original file name, because pip rejects a renamed wheel.

   ```powershell
   $whl = "C:\Users\<you>\Downloads\taskdash-<version>-py3-none-any.whl"
   $lock = "C:\Users\<you>\Downloads\requirements.lock"
   C:\TaskDash\venv\Scripts\python.exe -m pip install --require-hashes -r $lock
   C:\TaskDash\venv\Scripts\python.exe -m pip install --no-deps $whl
   & $td --version
   ```

   The first command installs the dependencies, and pip stops if a package differs from its hash. The
   second installs only the wheel. The one-window installer installs the same way.

   - If step 2 showed a Python other than 3.13, make a new environment first. Install 64-bit Python 3.13
     for all users, as "Requirements" in [INSTALL.md](INSTALL.md) describes. Then rename the old
     environment, so a rollback can use it, and create the new one. The new environment holds no
     packages, so the first pip command installs them all. Run both pip commands after these two lines.

     ```powershell
     Rename-Item C:\TaskDash\venv venv_old
     py -3.13 -m venv C:\TaskDash\venv
     ```

   - For a cloned ArcGIS Pro environment, skip the `requirements.lock` command. Run only
     `C:\TaskDash\proenv\python.exe -m pip install --no-deps $whl`. The lock and a missing
     `--no-deps` both replace packages that conda manages, and the clone can break.

   The last command must print the new version. Your config, logs and state stay in `C:\TaskDash`,
   and the install does not change them.

7. Check the config against the new version. The command changes nothing. It lists the keys that
   the new version does not know, the keys that a release renamed, and the keys that you did not set.

   ```powershell
   & $td preflight --config-diff
   ```

   Move the value of a renamed key to its new name. Correct or remove an unknown key. An absent key
   needs no action: TaskDash uses the default that the line shows.

8. If the release notes say the schema changed, check what the migration would add. Then apply it
   in the administrator window. The first command changes nothing. The second adds the new fields and stamps the service.

   ```powershell
   & $td collect --migrate
   & $td collect --migrate --apply
   ```

   A release can add an optional table. Release 1.2.0 adds `MetricChange`, which holds the latest and
   the usual value of each metric. Without the table, the collector logs one `WARNING` per run,
   publishes as before, and the dashboard keeps reading all of `TaskMetrics`. Nothing fails.

9. Run the preflight check. Fix every `[!!]` line before you go on.

   ```powershell
   & $td preflight
   ```

10. In the administrator window, enable the task. Run it once, and wait until the state is `Ready`.

    ```powershell
    Enable-ScheduledTask -TaskName "TaskDash Collector"
    Start-ScheduledTask -TaskName "TaskDash Collector"
    (Get-ScheduledTask -TaskName "TaskDash Collector").State
    (Get-ScheduledTaskInfo -TaskName "TaskDash Collector").LastTaskResult
    ```

11. In your normal window, check the result. `LastTaskResult` must be `0`. The exit code of
    `status` must be `0`. [USE.md](USE.md) lists the other codes.

    ```powershell
    & $td status
    ```

12. Rebuild the dashboard in the administrator window. Do this after the first run, because the `MetricChange` table stays
    empty until the collector runs once. A dashboard rebuilt earlier shows no Metric Change rows
    until the next hourly run. The first command changes nothing. The second saves the current
    definition as `dashboard_backup_<item id>_<time>.json` in the log folder, then replaces the
    dashboard. It also replaces any hand edits made in the Dashboards editor.

    ```powershell
    & $td dashboard --target prod
    & $td dashboard --target prod --apply
    ```

    The first command prints which source Metric Change reads. If several computers publish to
    this service, each computer writes only its own `MetricChange` rows. Upgrade every computer and
    let each run the collector once before you run `dashboard --target prod --apply`. A computer that still runs
    an older release adds no rows, so its metrics would be missing from Metric Change.

    If the output says the item has no `MetricChange` table, the dashboard reads a view that was made
    before the table existed. A view never gains a table. The dashboard keeps working with the old
    source. To use the table, run `taskdash collect --create-view --replace --apply`, as "Recreate the
    view" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md#rebuild-or-move-the-arcgis-items) describes.
    That command ends with its own dashboard rebuild. It keeps the old view until you delete it.

## Roll back

Roll back when the new version fails and the fix in [TROUBLESHOOTING.md](TROUBLESHOOTING.md) does not
help. You need the wheel of the previous version. Download it again from the release page if you
did not keep it. Check its SHA-256 value as in step 5.

1. In the administrator window, disable the task, as in step 3 of the upgrade.
2. If you made a new environment in step 6 of the upgrade, put the old one back. It still holds the
   previous version. Then skip the rest of this step, and run `& $td --version` to check it.

   ```powershell
   Rename-Item C:\TaskDash\venv venv_new
   Rename-Item C:\TaskDash\venv_old venv
   ```

   Otherwise, in the administrator window, install the previous wheel by its explicit path. Use `--no-deps` for a cloned ArcGIS
   Pro environment, as in step 6.

   ```powershell
   $whl = "C:\Users\<you>\Downloads\taskdash-<previous version>-py3-none-any.whl"
   C:\TaskDash\venv\Scripts\python.exe -m pip install "$whl[smtp]"
   & $td --version
   ```

   The last command must print the previous version. If pip says the requirement is already
   satisfied, add `--force-reinstall` to the install command.

3. Leave the service schema as it is. The schema does not roll back. A newer release stamped the
   service with its schema number, and the old version never lowers that stamp. The old version
   refuses `collect --migrate --apply` on a newer stamp. Do not lower the stamp by hand.

   The warning below appears only if the newer release changed the schema, and the version that you
   return to is 1.2 or later. Versions before 1.2 publish without the warning. In that case every
   `collect` run logs one warning, such as `WARNING: the service <id> has schema 2, newer than this
   TaskDash (<version>, schema 1); publishing continues but new fields stay empty.` `preflight`
   shows the same text as a warning, not as a failure. The old version still publishes every field
   it knows. The fields that only the newer release writes stay empty.
4. Compare the config with the backup from step 4 of the upgrade. Lines that start with `<=` are only
   in the current config, and lines that start with `=>` are only in the backup. Replace `<date>`
   as in that step.

   ```powershell
   Compare-Object (Get-Content C:\TaskDash\TaskDash.config.json) (Get-Content C:\TaskDash_backup_<date>\TaskDash.config.json)
   ```

   The older version ignores a key that it does not know. Two newer features still change what it
   does. To return to the config from before the upgrade, copy the backup file over it. Then redo
   the edits that you still want.

   - A per-task mute, an `alerting.maintenance_windows` entry with `"end": null`, does not mute
     under the older version. The older version ignores the entry with a warning, so the task
     alerts again.
   - `alert_state.json` holds the alerts already sent. The newer release saves its keys with the
     task's folder, and the older version does not read them. One repeat alert can go out for each
     failure that is still open.
5. In the administrator window, rebuild the dashboard with the previous version, if the dashboard no longer works with its
   data. This is the supported way to restore it. The first command changes nothing.

   ```powershell
   & $td dashboard --target prod
   & $td dashboard --target prod --apply
   ```

   The second command saves the current definition in a new `dashboard_backup_*.json` file in the
   log folder. No TaskDash command reads these files back. The file is a copy of the definition that
   you can keep for reference. The upgrade saved the older definition, from before the new
   release, in the same way. Pick the file by the time in its name.

   If the newer release added the `MetricChange` table, do this rebuild. The older version does not
   update that table, so a dashboard that still reads it shows old Metric Change values. The rebuild
   returns Metric Change to the whole `TaskMetrics` table.
6. In the administrator window, enable the task and run it once, as in step 10 of the upgrade.
   Then run `& $td status` in your normal window. The exit code must be `0` after a good run, even
   while the schema warning shows.

To go forward again, install the new wheel as in step 6 of the upgrade. The warning stops when the
schema number of the installed version is no longer lower than the service's.

## Move TaskDash to another computer

Use this when the computer that runs the collector is replaced. A quick install on the new computer
without the old config makes a second dashboard: `taskdash setup` creates new ArcGIS items. This
guide keeps the existing items. The new computer then publishes to the same service.

The new computer reports its own name in every row, and it watches its own scheduled tasks. If the
tasks that TaskDash watched move to the new computer too, the old name's inventory rows must move
with them. Step 4 handles that.

Run the steps in order. Use an administrator PowerShell window on each computer.

1. On the old computer, disable the task. Note the time of its last run. The old computer then
   publishes nothing more.

   ```powershell
   Disable-ScheduledTask -TaskName "TaskDash Collector"
   (Get-ScheduledTask -TaskName "TaskDash Collector").State
   ```

   The state must be `Disabled`. If a run is in progress, wait until it ends.

2. On the old computer, copy these files from `C:\TaskDash` to a safe place. Copy no other file.
   Do not copy the `venv` folder, the logs or `taskdash_handoff.bin`.

   - `TaskDash.config.json`, the config. It holds no password, but it names your accounts and
     addresses, so keep the copy private.
   - `taskdash_setup_manifest.json`. `taskdash remove` reads it, and without it the new computer
     cannot remove the items.
   - The `rules` folder, if you made one, as "Add your own rules" in [CONFIGURE.md](CONFIGURE.md)
     describes.

3. On the new computer, meet the Requirements in [INSTALL.md](INSTALL.md) and download the release
   files, as the quick install describes. Create the install folder, and lock it before you copy
   anything into it. Only SYSTEM, Administrators, you and the task account can open it. Run the
   lines in an administrator window. You get Read & Execute only, and Administrators own the folder.
   Replace `DOMAIN\svc-taskdash` with the task account. If `New-Item` reports that the folder exists, stop.
   Another user may own it.

   ```powershell
   New-Item -ItemType Directory C:\TaskDash | Out-Null
   icacls C:\TaskDash /setowner "*S-1-5-32-544"
   icacls C:\TaskDash /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "${env:USERDOMAIN}\${env:USERNAME}:(OI)(CI)RX" "DOMAIN\svc-taskdash:(OI)(CI)M"
   ```

   Copy the three items from step 2 into `C:\TaskDash`. Read the config before you use it. Change
   every path that exists only on the old computer, such as `metrics.log_roots`.

4. Choose how the new computer is named in the rows. Edit the copied config before the installer
   runs, because the installer starts the task once.

   - **New name, old inventory rows follow.** Leave `host_name_override` empty. Add the old name
     to `host_previous_names`, as in the example below. The run that follows adopts the old rows
     after 2 x `scheduler.collector_interval_minutes` have passed since the last run of the old
     computer. Until then it logs a `WARNING` and moves nothing. History keeps the old name.
     "Rename the computer" in [CONFIGURE.md](CONFIGURE.md) describes the key and its limits.

     ```json
     "host_previous_names": ["OLD-COMPUTER"]
     ```

   - **Keep the old name.** Set `host_name_override` to the old name. Every row then keeps one
     name. The task must pass `--allow-host-mismatch`, which the installer does not add. The
     installer's first run therefore stops with a host-mismatch refusal, and the installer reports
     a failed first run. The task stays registered. Add `--allow-host-mismatch` to the task's
     arguments, as the last paragraph of "Rename the computer" says. Then start the task, and check
     `LastTaskResult` in step 6.
   - **The tasks did not move.** Leave both keys alone. The old computer's tasks stay in the
     dashboard as stale rows until you retire the old name in step 7.

5. On the new computer, run the installer in check mode, then with `-Apply`, as the quick install in
   [INSTALL.md](INSTALL.md) describes. It keeps the config, so it skips `taskdash setup` and creates
   no ArcGIS item. It stores the ArcGIS sign-in for you and for the task account. It also asks for the
   mail password, if `smtp.user` is set. It registers the task and runs it once.
6. Check the result. `LastTaskResult` and the exit code of `status` must be `0`. If you chose a new
   name, keep `host_previous_names` until a run logs `adopted N row(s)` and no warning. Then remove
   the key.

   ```powershell
   (Get-ScheduledTaskInfo -TaskName "TaskDash Collector").LastTaskResult
   & $td status
   ```

7. If the old computer's tasks did not move, remove its inventory rows from the new computer. Use the
   name in the **Host** column, as "Retire a computer" in [CONFIGURE.md](CONFIGURE.md) describes.
8. On the old computer, remove the saved credentials, then the task. Do not run `taskdash remove`:
   it deletes the service that the new computer now uses. The second command asks for the task
   account's password. Run the commands in this order, because they read the task account from the
   task.

   ```powershell
   & $td signin --forget --apply
   & $td signin --forget --for-task-account --apply
   Unregister-ScheduledTask -TaskName "TaskDash Collector" -Confirm:$false
   ```

   Then uninstall the package, as step 4 of "Undo the install" in [INSTALL.md](INSTALL.md) describes.
   Delete the `C:\TaskDash` folder of the old computer last.

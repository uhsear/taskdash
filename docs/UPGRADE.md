# Upgrade and roll back TaskDash

This guide moves an installed TaskDash to a new release, and back to the previous one if the new
release fails. It assumes the install in [INSTALL.md](INSTALL.md) works. Plan for about 30 minutes.

Run every command on the computer that runs the collector. Use the two lines from step 1 of
[INSTALL.md](INSTALL.md) in each new PowerShell window. They set the folder and `$td`. Commands that
change the scheduled task need a PowerShell window **as administrator**. The other commands run in
your normal window.

An upgrade does not change any password. To change one, follow [ROTATE.md](ROTATE.md).

The commands below use `C:\TaskDash\venv`. If you installed into a cloned ArcGIS Pro environment,
use `C:\TaskDash\proenv` in its place, as "Use a cloned ArcGIS Pro environment instead" in
[INSTALL.md](INSTALL.md) describes.

## Upgrade

1. Read the release notes of the new version. They are in [CHANGELOG.md](../CHANGELOG.md) and on the
   release page. Find out whether the release changes the service schema. A release that adds a table
   field says so. If it says nothing about the schema, skip step 7.
2. Note the installed version, and keep its wheel file. You need it for a rollback.

   ```powershell
   & $td --version
   ```

3. In the administrator window, disable the task. The next hourly run then cannot start while you
   work.

   ```powershell
   Disable-ScheduledTask -TaskName "TaskDash Collector"
   (Get-ScheduledTask -TaskName "TaskDash Collector").State
   ```

   The state must be `Disabled`. If a run is in progress, the task still shows `Running`. Wait until
   it ends before you go on.

4. Back up the config and the TaskDash folder. The copy leaves out the Python environment, which you
   can rebuild from the wheel. Replace `<date>` with today's date, for example `20261101`. Keep the
   backup as private as `C:\TaskDash`.

   ```powershell
   robocopy C:\TaskDash C:\TaskDash_backup_<date> /E /XD venv proenv
   ```

   `robocopy` exit codes 0 to 7 mean success. A code of 8 or more means a copy failed.

5. Download the new wheel into `Downloads` from
   <https://github.com/uhsear/taskdash-releases/releases/latest>. Check its SHA-256 value against
   `SHA256SUMS.txt` on the release page. Do not install a file that does not match.

   ```powershell
   Get-FileHash $env:USERPROFILE\Downloads\taskdash-<version>-py3-none-any.whl
   ```

   You can also check who built the file with Sigstore. The steps are in the
   [requirements of INSTALL.md](INSTALL.md#requirements), under "The TaskDash wheel".

6. Install the wheel by its explicit path. Replace the path with the real one. Keep the original
   file name, because pip rejects a renamed wheel.

   ```powershell
   $whl = "C:\Users\<you>\Downloads\taskdash-<version>-py3-none-any.whl"
   C:\TaskDash\venv\Scripts\python.exe -m pip install "$whl[smtp]"
   & $td --version
   ```

   - For a cloned ArcGIS Pro environment, add `--no-deps`:
     `C:\TaskDash\proenv\python.exe -m pip install --no-deps $whl`. Without it, pip replaces packages
     that conda manages, and the clone can break.
   - If you used the locked install, repeat it with the new `requirements.lock`. See "Optional:
     locked install" in [INSTALL.md](INSTALL.md).

   The last command must print the new version. Your config, logs and state stay in `C:\TaskDash`,
   and the install does not change them.

7. If the release notes say the schema changed, check what the migration would add. Then apply it.
   The first command changes nothing. The second adds the new fields and stamps the service.

   ```powershell
   & $td collect --migrate
   & $td collect --migrate --apply
   ```

   A release can add an optional table. Release 1.4 adds `MetricChange`, which holds the latest and
   the usual value of each metric. Without the table, the collector logs one `WARNING` per run,
   publishes as before, and the dashboard keeps reading all of `TaskMetrics`. Nothing fails.

8. Rebuild the dashboard. The first command changes nothing. The second saves the current
   definition as `dashboard_backup_<item id>_<time>.json` in the log folder, then replaces the
   dashboard. It also replaces any hand edits made in the Dashboards editor.

   ```powershell
   & $td dashboard --target prod
   & $td dashboard --target prod --apply
   ```

   The first command prints which source Metric Change reads. If step 7 created the `MetricChange`
   table, do this step after step 10. The table stays empty until the collector runs once. A
   dashboard rebuilt earlier shows no Metric Change rows until the next hourly run.

   If several computers publish to this service, each computer writes only its own `MetricChange`
   rows. Upgrade every computer and let each run the collector once before you run `dashboard
   --apply`. A computer that still runs an older release adds no rows, so its metrics would be
   missing from Metric Change.

   If the output says the item has no `MetricChange` table, the dashboard reads a view that was made
   before the table existed. A view never gains a table. The dashboard keeps working with the old
   source. To use the table, recreate the view, as "Rebuild or move the ArcGIS items" in
   [TROUBLESHOOTING.md](TROUBLESHOOTING.md) describes.

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

## Roll back

Roll back when the new version fails and the fix in [TROUBLESHOOTING.md](TROUBLESHOOTING.md) does not
help. You need the wheel of the previous version. Download it again from the release page if you
did not keep it. Check its SHA-256 value as in step 5.

1. In the administrator window, disable the task, as in step 3 of the upgrade.
2. Install the previous wheel by its explicit path. Use `--no-deps` for a cloned ArcGIS Pro
   environment, as in step 6.

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
4. Rebuild the dashboard with the previous version, if the dashboard no longer works with its
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
5. In the administrator window, enable the task and run it once, as in step 10 of the upgrade.
   Then run `& $td status` in your normal window. The exit code must be `0` after a good run, even
   while the schema warning shows.

To go forward again, install the new wheel as in step 6 of the upgrade. The warning stops when the
schema number of the installed version is no longer lower than the service's.

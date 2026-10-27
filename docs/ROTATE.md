# Rotate the TaskDash passwords

TaskDash holds three passwords. This guide changes each one without a new install. Plan for about 15
minutes per password.

| Password | Where TaskDash keeps it | Section |
|---|---|---|
| The ArcGIS password | A saved sign-in (a profile) in Windows Credential Manager | [Rotate the ArcGIS password](#rotate-the-arcgis-password) |
| The mail password | The Windows keyring, under the service `taskdash-smtp` | [Rotate the mail password](#rotate-the-mail-password) |
| The task account password | The scheduled task | [Rotate the task account password](#rotate-the-task-account-password) |

Run every command on the computer that runs the collector. Use the two lines from step 1 of
[INSTALL.md](INSTALL.md) in each new PowerShell window. They set the folder and `$td`. Commands that
change the scheduled task need a PowerShell window **as administrator**. The other commands run in
your normal window.

The commands below use `C:\TaskDash\venv`. If you installed into a cloned ArcGIS Pro environment,
use `C:\TaskDash\proenv` in its place, as "Use a cloned ArcGIS Pro environment instead" in
[INSTALL.md](INSTALL.md) describes.

## A reset is not a change

An ArcGIS password and a mail password change at the service. The task account password changes in
Windows, and there are two ways:

- **A change.** The account owner, or a person who knows the old password, sets a new password.
  Windows re-protects the account's saved secrets with the new password. The saved ArcGIS sign-in
  and the mail password keep working. Only the scheduled task needs the new password.
- **A reset.** An administrator sets a new password without the old one. Windows can no longer unlock
  the saved secrets that the old password protected. This is how Windows protects them. The saved
  ArcGIS sign-in and the mail password of the task account can be lost. A domain account may recover
  them through a domain controller. Do not rely on that.

After a reset, store the ArcGIS sign-in and the mail password of the task account again. The
section [Rotate the task account password](#rotate-the-task-account-password) lists the steps. If you
do not know which one happened, treat it as a reset.

## Rotate the ArcGIS password

Do this when the password of the ArcGIS account changes. Store the new password twice: once for
yourself, and once for the task account. Each Windows account has its own Credential Manager.

1. Change the password of the ArcGIS account in ArcGIS Online first.
2. In your normal window, run the create command. Replace `ARCGIS_URL` with your `arcgis.url` and
   `ARCGIS_USER` with the ArcGIS account name. Type the new password at the `Password:` prompt. The
   command replaces the old profile.

   ```powershell
   C:\TaskDash\venv\Scripts\python.exe -c "import getpass; from arcgis.gis import ProfileManager; ProfileManager().create('taskdash', 'ARCGIS_URL', 'ARCGIS_USER', getpass.getpass())"
   ```

3. Run the check command. It must print the ArcGIS account name.

   ```powershell
   C:\TaskDash\venv\Scripts\python.exe -c "from arcgis.gis import GIS; print(GIS(profile='taskdash').users.me.username)"
   ```

4. Open a window as the task account. Windows asks for the task account's password.

   ```powershell
   runas /user:DOMAIN\svc-taskdash powershell.exe
   ```

   In the new window, run the create command and the check command again. If `runas` fails, see
   step 3 of [INSTALL.md](INSTALL.md). Close the window when you are done.
5. In the administrator window, run the task once, and wait until the state is `Ready`.

   ```powershell
   Start-ScheduledTask -TaskName "TaskDash Collector"
   (Get-ScheduledTask -TaskName "TaskDash Collector").State
   (Get-ScheduledTaskInfo -TaskName "TaskDash Collector").LastTaskResult
   ```

6. In your normal window, check the result. `LastTaskResult` must be `0`, and the exit code of
   `status` must be `0`. [USE.md](USE.md) lists the other codes.

   ```powershell
   & $td status
   ```

If the run fails, see "The stored sign-in does not work" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Rotate the mail password

Do this when the password of the mail account changes. It applies only if `smtp.user` is set in the
config. Store the new password twice: once for yourself, and once for the task account.

1. Change the password of the mail account at the mail service first.
2. In your normal window, run the keyring command. Replace `<smtp.user>` with the value of
   `smtp.user`. Type the new password at the prompt. The command replaces the old entry.

   ```powershell
   C:\TaskDash\venv\Scripts\python.exe -m keyring set taskdash-smtp <smtp.user>
   ```

3. Open a window as the task account, as in step 4 of the ArcGIS section. In that window, run the
   same keyring command.
4. In the same window, run the two lines from step 1 of [INSTALL.md](INSTALL.md). Then send a test
   message. The first command changes nothing. The second sends one message to every address in
   `alerting.to`. It exits 0 when the server accepts it.

   ```powershell
   & $td test-alert
   & $td test-alert --apply
   ```

5. Close the `runas` window. The next hourly run uses the new password.

If the test fails, see "The alert e-mail failed" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). The
mail steps are in [CONFIGURE.md](CONFIGURE.md).

## Rotate the task account password

Do this when the Windows password of the task account changes, or an administrator resets it. The
task stores the password. Until you register the task again, every run fails with `LastTaskResult`
`2147943726`, and the dashboard goes stale.

1. Set the new password of the task account in Windows. Note whether it was a change or a reset, as
   [A reset is not a change](#a-reset-is-not-a-change) describes.
2. In the administrator window, run the installer in check mode. Add `-Replace`. Use the same
   `-InstallDir`, `-Python` and `-User` as in step 7 of [INSTALL.md](INSTALL.md). It registers
   nothing.

   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File C:\TaskDash\venv\share\taskdash\install_task.ps1 `
       -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe -User DOMAIN\svc-taskdash -Replace
   ```

   The output has a line `Replace: yes`. Check that `Runs as` names the task account. The check
   mode reads the existing task and runs the same refusals as `-Apply`. It stops with a message if no
   task has the name, the account or logon type differs, or the task has no single program action.
   The account must match as `DOMAIN\name`, ignoring case. On success it prints `Would keep` lines for
   the action, trigger and settings, and a line that starts with `Replace checks passed`.
3. Run the same command with ` -Apply` added at the end. The script asks for the new password of the
   task account. Type it at the prompt; it does not echo. The script then registers the same task
   again, over the existing one, and prints `Registered 'TaskDash Collector'. Principal.LogonType =
   Password`.

   The script keeps the logon type of the task. It also keeps the task's command, arguments,
   working directory, trigger and settings as they are now. A hand-added argument, such as
   `--allow-host-mismatch`, and a schedule you changed, such as every 30 minutes, survive the
   rotation. `-Python` does not change the command. The script prints the kept command on a line that
   starts with `Keeping the task's existing action`. It renders only the account, the logon type and
   the run level from the install template again.

   The script refuses with a message when no task has the name, when the task runs as another
   account, or when its logon type differs from `-LogonType`. Pass the same `-TaskName` and
   `-LogonType` that you used at the install. Without `-Replace`, the script refuses to touch an
   existing task.
4. If an administrator reset the password, store the ArcGIS sign-in and the mail password of the task
   account again. Open a `runas` window as the task account, and type the new password. Run the
   create command and the check command of the ArcGIS section there. If you use mail, run the keyring
   command of the mail section too. After a plain change, skip this step.
5. Run the task once and check it, as in steps 5 and 6 of the ArcGIS section. `LastTaskResult` must
   be `0`.

If the task still fails, read "The scheduled task fails after a password change" in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

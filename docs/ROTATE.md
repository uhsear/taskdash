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
change the scheduled task, write to `C:\TaskDash` or install into its environment need a PowerShell
window **as administrator**. Your own account has Read & Execute only on that folder, as step 1 of
[INSTALL.md](INSTALL.md) explains, so such a command fails with access denied in a normal window.
Read-only commands, such as `status`, run in your normal window.

The commands below use `C:\TaskDash\venv`. In a cloned ArcGIS Pro environment, replace `C:\TaskDash\venv\Scripts\python.exe` with
`C:\TaskDash\proenv\python.exe`. Then replace every other `venv` in a path with `proenv`, as "Use a
cloned ArcGIS Pro environment instead" in [INSTALL.md](INSTALL.md) describes.

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
`taskdash signin` does both without `runas`, and it checks the account's privileges first, as the
install does.

1. Change the password of the ArcGIS account in ArcGIS Online first.
2. In your normal window, run the check. It asks for the ArcGIS user name and the new password at
   hidden prompts, signs in, and checks the account and its privileges. It saves nothing. Each line
   that starts with `[!!]` ends with the fix.

   ```powershell
   & $td signin
   ```

   When every line starts with `[ok]`, store the sign-in. The command saves the profile under the
   name in `arcgis.profile`, replaces the old one, and signs in with it to prove that it works.

   ```powershell
   & $td signin --apply
   ```

3. Open PowerShell as administrator. Run the two lines from step 1 of [INSTALL.md](INSTALL.md).
   Then seal the sign-in for the task account. The first command changes nothing. The second asks
   for the ArcGIS password again, and for the mail password if `smtp.user` is set. Press Enter to
   skip the mail password. It writes `taskdash_handoff.bin` in the TaskDash folder, so a normal
   window fails with access denied. Only the task account, SYSTEM and Administrators can read that
   file.

   ```powershell
   & $td signin --for-task-account
   & $td signin --for-task-account --apply
   ```

4. In the same administrator window, run the check as the task account. It asks for the password
   of the task account. Its output shows a line that starts `Stored the sign-in` before the `credentials` result. That line means the task
   account saved the new sign-in and deleted the hand-off file.

   ```powershell
   & $td preflight --as-task --apply
   ```

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

If the hand-off fails, see "The hand-off sign-in was kept" in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). If a hardened task account lets you use `runas`, you can
store the sign-in in that account's window instead. Open the window, then run `$td = "C:\TaskDash\venv\Scripts\taskdash.exe"`,
`& $td signin` and `& $td signin --apply` there. Windows asks for the account's password. If `runas`
fails, see step 3 of [INSTALL.md](INSTALL.md). Close the window when you are done.

```powershell
runas /user:DOMAIN\svc-taskdash powershell.exe
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

3. Open PowerShell as administrator. Run the two lines from step 1 of [INSTALL.md](INSTALL.md).
   Then seal the mail password for the task account. The command writes `taskdash_handoff.bin` in
   the TaskDash folder. There is no hand-off for the mail password alone.
   The command signs in to ArcGIS first, so type the current ArcGIS password at the first prompt.
   Type the new mail password at the second prompt. The command stores the same ArcGIS sign-in
   again, which changes nothing. The first command changes nothing.

   ```powershell
   & $td signin --for-task-account
   & $td signin --for-task-account --apply
   ```

4. In the same administrator window, run the check as the task account. It asks for the password
   of the task account. The output shows the `Stored the sign-in` line, and the `smtp` line must not start with `[!!]`. That proves that
   the task account can read a mail password. It does not prove that the mail server accepts it.

   ```powershell
   & $td preflight --as-task --apply
   ```

5. In your normal window, send a test message. It proves that the server accepts the new password.
   It reads your own saved password, so it does not test the task account's copy. The first command
   changes nothing. The second sends one message to every address in `alerting.to`. It exits 0 when
   the server accepts it.

   ```powershell
   & $td test-alert
   & $td test-alert --apply
   ```

The next hourly run uses the new password. If you cannot use the hand-off, open a `runas` window as
the task account, as in the ArcGIS section, and run the keyring command of step 2 there.

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
   The account name must match, ignoring case. The computer or domain name may differ only after a
   computer rename. On success it prints `Would keep` lines for
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
   account again. Do steps 2 to 4 of the ArcGIS section. The hand-off in step 3 asks for the mail
   password too, if `smtp.user` is set. The check in step 4 asks for the new task account
   password. After a plain change, skip this step.
5. Run the task once and check it, as in steps 5 and 6 of the ArcGIS section. `LastTaskResult` must
   be `0`. Then run `& $td preflight --as-task --apply` in the administrator window. It runs every
   check as the task account, and it asks for the new password.

If the task still fails, read "The scheduled task fails after a password change" in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

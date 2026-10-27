# TaskDash approval sheet

This sheet is for the IT and security review of a TaskDash install. Each statement was checked
against the code. For the steps themselves, see [INSTALL.md](INSTALL.md).

## What TaskDash does

TaskDash monitors the scheduled tasks on one Windows computer. Every hour it reads the Task Scheduler,
the Task Scheduler event log and the log files of each task. It publishes health, run history and
log metrics to hosted tables in your ArcGIS Online organization. A dashboard shows them. It never
changes a scheduled task. Alert e-mail and an outside health check are optional.

## Endpoints it contacts

TaskDash contacts nothing else. It has no update check and no telemetry. Every connection starts
at the computer. Nothing connects in.

| Endpoint | Use | When |
|---|---|---|
| `arcgis.url` (default `https://www.arcgis.com`) and the hosted service on `*.arcgis.com` | Sign-in, items, groups, tables. The `arcgis` package calls the portal's `/sharing/rest` API and the service's `FeatureServer` REST API. | Every run |
| The same table addresses, without a token | A check that anonymous users cannot read the tables | `taskdash preflight`, `taskdash collect --check-anonymous` |
| `smtp.host` on `smtp.port` | Alert e-mail with STARTTLS or SSL. TaskDash checks the server certificate. | Only if alerts are on |
| `alerting.healthcheck_url` | A `GET` to the address, then with `/start` and `/fail` added. No data is sent. | Only if set |
| `pypi.org`, `files.pythonhosted.org` | `pip` downloads packages | Install and upgrade only |

## Accounts

- **Task account:** a Windows account in the local Administrators group, with a password. A standard
  account sees only some tasks and lacks the batch logon right, so the task never starts.
- **ArcGIS account:** a built-in account without multifactor authentication. It needs these
  privileges, which a Creator has: `portal:user:createItem`, `portal:user:createGroup`,
  `portal:user:shareToGroup` and `features:user:edit`. It creates the items, the private group and
  the rows. TaskDash shares only with that group, never with everyone.
- **Mail account:** optional. Set `smtp.user` to use one. The password lives in the Windows keyring
  under the service `taskdash-smtp`.

No password is in the config by default. A plaintext `arcgis.password` works only when
`arcgis.allow_plaintext_password` is set to `true`. The ArcGIS password lives in a saved sign-in profile of each Windows
account that signs in.

## Privileges and task settings

The task runs `taskdash.exe collect --apply` in the install folder. Its Task Scheduler settings are:

- Logon type Password, run level `LeastPrivilege`.
- Trigger: every hour (`PT1H`), no end. Run limit two hours (`PT2H`).
- One instance only (`IgnoreNew`). A missed run starts when possible (`StartWhenAvailable`).

An administrator window is needed once, to register the task and to enlarge the Task Scheduler
event log. The collector only reads tasks. `taskdash preflight --as-task --apply` registers one
temporary test task and removes it.

## Files it reads and writes

- **Reads:** `TaskDash.config.json`, the shipped rules, the task definitions, and the event log
  `Microsoft-Windows-TaskScheduler/Operational`. It reads the end of each task's `.log` files that changed in the last
  `metrics.days_back` days. If `metrics.log_roots` is set, it reads only inside those folders and skips links.
- **Install folder:** `TaskDash.config.json.bak_setup_*` backups, `taskdash_setup_manifest.json`,
  and `taskdash_handoff.bin`. The last one is temporary. It holds the sign-in passwords sealed with
  DPAPI at machine scope, so its permissions are the control: only the task account (read and
  delete), SYSTEM and Administrators can open it, and no entry is inherited. The first run as the
  task account stores the sign-in and deletes the file. When AppLocker or WDAC limits PowerShell to
  ConstrainedLanguage mode, each PowerShell result passes through a temporary `.taskdash_ps_*.json`
  file. It holds task names and run data. TaskDash reads it and deletes it in the same call.
- **Log folder** (`logging.log_dir`, default `Logs`): `taskdash_*.log`, `taskdash.lock`, `alert_state.json`,
  `first_run_budget*.json`, `event_read_mark*.json`, `metrics_state*.json`, `collector_runs_pending_*.json`,
  `alert_preview_*.txt`, `dashboard_backup_*.json` and `bootstrap_FAIL_*.log`. The
  `preflight --as-task --apply` command also writes `preflight_as_task_*.txt` there, readable by
  the task account, you and administrators, and deletes it when the check ends.
- **Task Scheduler event log:** enabled and enlarged only by `taskdash preflight --enable-event-log --apply`.
- **Task XML:** `install_task.ps1` fills the shipped template in memory and registers it. It writes no file.

## Data that leaves the computer

- **To ArcGIS:** task names and folders, state, last result, schedule, run times and results,
  metric names and values, change summaries, and the host name. Errors and change text are scrubbed of secrets.
- **Hidden by default:** command lines, account names, paths and sample log lines. The four
  `privacy` switches (`privacy.hide_command_lines`, `privacy.hide_accounts`, `privacy.hide_paths`
  and `privacy.hide_sample_lines`) control them. Secret scrubbing and the refusal to share publicly have no switch.
- **To the mail server:** the host name, the task names and a link to the dashboard. The last 15 lines
  of each failed task's log are added, scrubbed. `alerting.include_log_tail` set to `false` leaves them out.
- **To the health check:** nothing but the request itself.

## How to remove it

Follow "Undo the install" in [INSTALL.md](INSTALL.md#undo-the-install). `taskdash remove` shows what
it would delete from ArcGIS, and `taskdash remove --apply` deletes it. The undo leaves the larger
event log, the task account's group membership and Python in place.
[UPGRADE.md](UPGRADE.md) covers moving to another release.

## Release integrity

Each release lists the wheel, `SHA256SUMS.txt`, a hash-pinned `requirements.lock`, a CycloneDX SBOM and
Sigstore bundles for the wheel and for `SHA256SUMS.txt`. Compare the hash and check the signature
as INSTALL.md describes in its Requirements, and use the lock file as in "Optional: locked
install". To report a vulnerability, follow [SECURITY.md](../SECURITY.md).

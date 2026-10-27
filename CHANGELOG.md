# Changelog

All notable changes to TaskDash are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The version numbers follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - not yet released

### Added

- `decimal_separator` in a rules file (`"."` default, or `","`) reads metric numbers such as `1,5` and
  `1.234,5` from logs that write decimals with a comma. The default changes nothing.
- `TaskDash.config.example.json` ships a default `scheduler.exclude_task_name_regex` that skips the
  Microsoft Edge update, Google update and OneDrive tasks. It affects new installs only; the
  built-in default stays empty. No table field changed.

- ConstrainedLanguage support for the PowerShell commands (AppLocker or WDAC; H4-4). The commands
  that read the task list and the event log now run in that mode. The event reader takes fields from
  `Properties` by position where `ToXml()` is refused. In that mode each result comes back through a
  UTF-8 temp file named `.taskdash_ps_*.json` in the TaskDash folder, which TaskDash reads and
  removes, because the console encoding cannot be set there. A failure in that mode now names the
  mode and the fix (an allow rule for the TaskDash folder, or an exempt account). `taskdash
  preflight` has a read-only `language mode` line that warns in ConstrainedLanguage. Output in
  FullLanguage is unchanged, and so is the reference install: it reads the same JSON from stdout. A
  failed PowerShell call starts one more PowerShell, only to read the mode. No table field changed.
- A CI matrix, `.github/workflows/matrix.yml` (H4-10). It runs on a version tag, weekly and on demand:
  the full suite on `windows-2022` and `windows-2025` with Python 3.11, 3.12 and 3.13 (`windows-2019`
  is retired); legs for ConstrainedLanguage, the de-DE and fi-FI cultures and two daylight-saving
  time zones, each asserting that it runs in what it names; a live dry-run collect that reads eight
  seeded odd tasks (brackets, repeated spaces, an apostrophe, a non-ASCII name, a `\MicrosoftSync\`
  folder and two monthly tasks, one with brackets in its name) registered by `tests/ci/seed_tasks.ps1` and removed afterwards; and a
  500-task synthetic host (`taskdash_synthetic.build_scale_host`) that must finish its offline passes
  in 15 minutes. The scale test runs only when `TASKDASH_SCALE_TASKS` is set, so the default suite
  stays fast. The generator is the only shipped code that changed, and `taskdash demo` does not use
  it. No config key, table field or default changed.
- `docs/APPROVAL.md`, a one-page sheet for an IT and security review. It lists the endpoints,
  accounts, task settings, files, outgoing data, removal steps and release checks. A test checks its
  commands, flags, config keys, file names and privileges against the code, and that the network
  code stays in the three places it names. README, INSTALL and INTRODUCTION link it. No code or
  table field changed.
- `taskdash signin` infers the organization URL. When `arcgis.url` is empty it offers
  `https://www.arcgis.com`, and after the sign-in it prints the organization's own URL
  (`urlKey` plus `customBaseUrl`) with the line to put in the config. With `--apply` it writes only
  `arcgis.url`, after a `.bak_setup_` backup of the config. No table field changed.
- `Install-TaskDash.ps1`, a one-window installer. It chains the venv and wheel install (checking the
  wheel against a `<wheel>.sha256` file when one is beside it), the config copy, `taskdash signin`
  for this account and for the task account, `taskdash preflight` (after the event-log fix),
  `taskdash setup`, `install_task.ps1 -EnableEventLog -StartNow`, and `taskdash status`. It takes
  `-InstallDir`, `-Wheel`, `-TaskAccount` and `-Apply`, checks only without `-Apply`, stops at the
  first failure and lists what it created, and reports `nothing to do` for a finished step, so a
  second run is safe. It never reads or prints a password. It ships in the wheel next to
  `install_task.ps1`. `docs/INSTALL.md` opens with a two-command "Quick install". No table field
  changed.
- `taskdash signin --for-task-account [--task-user NAME] [--apply]`: seals the ArcGIS sign-in, and the
  mail password when `smtp.user` is set, into `taskdash_handoff.bin` in the TaskDash folder, so the
  install no longer needs `runas`. The bytes use DPAPI machine scope. The file's ACL allows only the
  task account, SYSTEM and Administrators, with no inheritance. The next `collect --apply` run, or
  `preflight` check, by the task account saves the ArcGIS profile and the keyring entry, checks that
  the profile signs in, and then deletes the file. A failure keeps the file and fails the run like a
  sign-in failure. The file never holds plain text, and nothing prints or logs a secret. `runas`
  stays as the documented fallback. New module `taskdash_handoff`. No table field changed.
- `taskdash preflight --as-task [--task-user NAME] [--apply]`: runs the preflight checks as the task
  account without `runas`. Without `--apply` it prints the plan (temporary task name, account,
  command, output file) and changes nothing. With `--apply` it asks for the password (hidden prompt;
  it goes only to Task Scheduler's COM interface, never to a command line), registers a one-time task
  from `taskdash_task.xml` with the collector task's account and logon type and no trigger, runs it,
  waits up to 300 seconds, prints the output from a restricted file in the log folder, and always
  deletes the task and the file. It only reads the collector task. New module `taskdash_astask`.
  No table field changed.
- Two read-only checks in `taskdash preflight`. `base python` warns when the Python that made the
  virtual environment is under `C:\Users`, which the task account may not be able to run. `install
  folder` fails when Users, Authenticated Users or Everyone hold a write right on the install
  folder, because the task runs as an administrator and loads code and config from there. It reads
  the permissions with `icacls` by full path and prints the `icacls` fix. No table field changed.
- A wheel install finds its config from any folder: when `TASKDASH_HOME` is not set and the current
  folder has no config, `home_dir()` uses the folder that contains the running virtual environment
  if that folder holds `TaskDash.config.json`. The order is otherwise unchanged.
- `taskdash signin [--apply]`: asks for the ArcGIS user name (default `arcgis.username`) and the
  password (hidden prompt, never an argument, never printed), signs in, checks that `users.me` is not
  empty and that the account has the privileges preflight checks, and with `--apply` saves the
  profile the way the install guide's one-liner did, then proves it signs in. Without `--apply` it
  saves nothing. Step 3 of `docs/INSTALL.md` uses it instead of the Python one-liners. No table field
  changed.
- A `host name` check in `taskdash preflight`: it warns when `host_name_override` is set and differs
  from this computer's name, and names the fix. It uses the same test as the `collect --apply`
  refusal. The example config now leaves `host_name_override`, `arcgis.url`,
  `arcgis.service_description` and `alerting.dashboard_url` empty, so a copy never points at another
  organization. The code defaults are unchanged. No table field changed.
- An upgrade and rollback guide, `docs/UPGRADE.md`, linked from the README and `docs/INSTALL.md`. A
  `collect --apply` run now logs one `WARNING` when the service schema is newer than the installed
  TaskDash, as after a rollback. It names both schema numbers and keeps publishing; the fields that
  only the newer release writes stay empty. The refusal in `collect --migrate --apply` is unchanged.
  No table field changed.
- `host_previous_names` (top-level config key, default empty): after a computer rename, each
  `collect --apply` moves the TaskInventory rows stored under the listed old names to the current
  name and logs the count, so no stale duplicates remain. It moves only the tasks that run
  publishes, so the inventory shrink guard stays meaningful. It skips, with a `WARNING`, an old name
  whose collector ran within twice `scheduler.collector_interval_minutes`, so a replacement computer
  that took the name keeps its rows. History tables keep the old name. The
  key must not hold an empty name or the current name. "Rename the computer" in docs/CONFIGURE.md
  gives the steps, including the wait after the old name's last run. No table field changed.
- `taskdash status` now returns an exit code: 0 for a fresh success, 1 when the newest run failed,
  2 for no run or a success older than twice the collector interval, and 3 when it cannot sign in
  or read. It shows the age of the last success, and `--host NAME` (default: this install's host,
  `--host all` for every host) filters the runs.
- `taskdash status --prune [--apply] [--keep N]` lists, and with `--apply` removes, old dashboard
  and config backups, empty pending files and bootstrap logs older than 7 days in the `Logs` folder
  beside the config, whatever `logging.log_dir` says. It keeps the newest `--keep` bootstrap logs and
  `bootstrap_FAIL_` crash records. It stops with a message when it cannot find the config, and never
  reads a `Logs` folder under the directory the command runs in. With `--apply` it skips the pending
  files while a collector run holds the run lock.
- A per-task mute: an `alerting.maintenance_windows` entry with `"end": null`
  mutes the matching tasks until you remove it, and `start` may be left out too. A muted task's
  `Why` text ends with `Muted (alerts off)`, a bounded window's with `In maintenance until <end>`,
  and each publishing run logs one `WARNING` with the number of muted tasks. The note comes after a
  failing task's own reason, and a healthy muted task shows it alone. No table field changed.
- `taskdash preflight --config-diff` lists the config keys TaskDash does not know (likely typos), the
  renamed keys with their new names, and the known keys that are absent with the default that
  TaskDash uses. It prints no value from the config, and exits 0 unless the config cannot be read.
- `taskdash setup --apply` turns on delete protection for the service, the view and the dashboard it
  creates, and marks them in its printed list. `taskdash preflight` has two new read-only checks:
  `dashboard` (it exists, is not public and reads the view) and `delete protection` (service and
  view). Both warn and never change an item. "Rebuild or move the ArcGIS items" in
  docs/TROUBLESHOOTING.md explains how to rebuild the dashboard, recreate the view and change owner.

- `taskdash remove [--apply]` undoes `taskdash setup`. `taskdash setup --apply` now saves
  `taskdash_setup_manifest.json` beside the config (item IDs, types, titles and creation order, no
  secrets). Remove lists what it would delete and changes nothing without `--apply`. With `--apply`
  it turns off delete protection and deletes, newest first, only the manifest items that still exist,
  that you own and that still have the manifest's type and title. It turns protection back on when a
  delete fails, keeps the service when its view was skipped, and reports any item it skips. It prints the commands that remove the Windows
  task and the package. "Undo the install" in docs/INSTALL.md now covers the whole removal.
- `taskdash collect --retire-host NAME [--apply]` retires a computer that published to a shared
  service. It counts that computer's rows in every table and changes nothing without `--apply`. With
  `--apply` it deletes only the TaskInventory rows; the history tables age out by retention. It
  refuses the computer that runs it, an empty name and a wildcard. "Retire a computer" in
  docs/CONFIGURE.md gives the steps. No table field changed.
- `install_task.ps1 -Replace` rotates the task account's password. With `-Apply` it registers the
  same XML over the existing task (`-Force`), keeps the account and the logon type, and asks for the
  new password. It keeps the task's existing command, arguments, working directory, trigger and
  settings, so a hand-added `--allow-host-mismatch` or a changed schedule survives. It refuses when no task has that name, or when the
  task runs as another account or logon type. The account must match as `DOMAIN\name`, ignoring
  case. Check mode with `-Replace` runs the same read-only refusals and prints what `-Apply` would
  do. Without `-Replace`, an existing task is still an error, and the message now names
  `-Replace`. The new guide `docs/ROTATE.md` covers the ArcGIS, mail and task account passwords,
  including what an administrator reset of the task account password does to the saved secrets.
- `install_task.ps1 -Apply` refuses before it registers anything when the window is not elevated or
  the task account is not a direct or nested member of the local Administrators group (nested
  membership only warns). Check mode prints the same problems as warnings and exits 0. Two new
  switches, `-EnableEventLog` and `-StartNow`, run `taskdash preflight --enable-event-log --apply`
  before the registration and start the task once after it, waiting up to 10 minutes and decoding
  `LastTaskResult`. Both only print a plan without `-Apply`. No table field changed.

### Changed

- The dashboard's Metric Change reads a small new table, `MetricChange`, instead of every
  `TaskMetrics` row. After each `collect --apply` run writes and purges metrics, it reads its host's
  stored `TaskMetrics` rows and stores one row per host, task and metric: the newest value, the usual
  value (the median of up to 30 earlier values, none under 3), the lowest and highest earlier value,
  and the earlier count. The flag rules stay in the dashboard, which gives the same rows as before.
  The table has a unique index on `series_key` and follows `retention.metrics_days`. It is optional.
  A service without it publishes exactly as before, `collect` logs one `WARNING` with the fix, and
  `taskdash dashboard` keeps the old source until the table exists. The drift check, `preflight` and
  `--create-view` do not need it. To switch, run `taskdash collect --migrate --apply`, then one
  `collect --apply`, then `taskdash dashboard --apply`; `docs/UPGRADE.md` has the steps. A view made
  before this release does not gain the table, so a dashboard that reads such a view keeps the old
  source. `SCHEMA_VERSION` is now 2. Prod-visible: until `--migrate --apply` runs, each `collect`
  run logs the extra `WARNING`, and `CollectorRuns.schema_version` reads 2 from the first run of the
  new version. After the migration, a run that wrote or purged metric rows makes one more read of
  the host's `TaskMetrics` rows and sends only the series whose values moved (usually a few rows
  per hour, not one per series). A run that wrote none skips both, except to fill an empty table.
- The hourly publish sends only the metric rows of log files that changed since the last clean
  `--apply` run, and reads only the stored metric keys those files can hold (bounded by the
  changed files' names), not every stored key of the host.
  A per-target state file, `Logs\metrics_state.json` (`metrics_state_test.json` for `--target test`),
  keeps one digest of the rows of each log file. It is written only after a clean publish, never in
  a dry run. With no state file, or after a change to the `metrics` or `privacy` settings, the
  service or the version, or after `--refresh-metrics --apply`, one run sends every row as before.
  Prod-visible: the hourly run writes far fewer rows and `observed_at_utc`, `collected_at_utc` of
  an unchanged row stay at their last values. No table field changed.
- The hourly publish reads 30 days of stored runs to build the run summary, not the whole 90-day
  table. One grouped `MAX(start_time_utc)` query per host finds each task's newest real run, and a
  second query fetches only the runs of tasks whose newest run is older than 30 days, so those tasks
  keep their last-run facts. Every count, the duration baseline and the last-run columns are
  unchanged. The mean duration, which the anomaly check uses only when a task has fewer than 5
  baseline runs, is now a 30-day mean. The follow-up query stops at the window's start, so a stale
  task's Missed and Skipped rows inside the window are not counted twice. A stuck "Running" row older than 30 days no longer sets the
  running time of a task that has later runs. A task with only "Running" rows in the window gets
  one more grouped query for its newest finished run, so its last duration and result stay on the
  dashboard. No table field changed.
- The package now requires Python 3.11 or later (it said 3.9), matching CI, the lock file and the
  install guide. The install guide, `Install-TaskDash.ps1` and the upgrade guide install the wheel
  with the `smtp` extra on every path, so there is no choice to make. The extra stays installable
  alone. The setup refusal for an existing install now names the commands for a wheel install and a
  folder install.
- `taskdash preflight` prints `[--]`, not `[!!]`, for results that only mean "not set up yet": no
  `arcgis.item_id` (the `service` and `sharing` lines) and no registered collector task (the
  `collector logon type` line). The `dashboard`, `view` and `collector account` lines already did.
  A `[--]` line does not count as a problem and does not set exit 1, so a fresh install that has
  not run `setup` yet exits 0. The reference install, which has both, behaves as before.
- An `alerting.maintenance_windows` entry without an `end` key is still ignored with a warning, so
  an upgrade never starts muting tasks. Only `"end": null` mutes until you remove the entry. To stop
  a misspelled key from becoming a silent mute, the run also ignores an entry with no `start` or a
  null `end` that holds a key other than `task`, `start`, `end` and `reason` (a `_` key is a note),
  and the config load warns about it. The config never fails to load for these keys. An entry whose `task` is not non-empty text
  also only warns, and it matches no task.
- The dashboard heartbeat leaves out a computer that has made no collector run for
  `scheduler.heartbeat_ignore_after_days` days (default 7), so a retired or renamed computer no longer
  shows as down for ever. The dashboard reads only the collector runs inside that window. Rebuild the
  dashboard to get it. A single computer sees no change unless its collector has been silent that long.
- `taskdash preflight` shows a service schema that is newer than the installed TaskDash as a warning,
  not a failure, with the advice that `collect` logs. A rolled-back collector keeps publishing.
  Missing tables, fields and indexes still fail.
- `taskdash dashboard` without `--target` now uses `test` only when `arcgis.test_item_id` is set.
  Otherwise a check run uses `prod`, so a new install no longer fails. It prints one line naming the target.
  `--apply` without `--target` stops when there is no test service. Pass `--target prod --apply` to update prod.
  Before `--apply` updates an existing dashboard, it warns that the rebuild replaces hand edits.
- A refusal or exit message that `docs/TROUBLESHOOTING.md` documents now ends with the line
  `See TROUBLESHOOTING.md, section <heading>.`, so you find the fix without searching. It covers the
  config, sign-in, schema, sharing, host, retire-host, setup, remove, demo and status messages. The
  message texts themselves did not change. A test checks that every heading the code names exists.

### Fixed

- `install_task.ps1` built the trigger's `StartBoundary` with a custom format string, so a culture
  with another calendar (for example Thai, year 2569) wrote a date that Task Scheduler rejected. It
  now uses the sortable `s` format with the invariant culture.
- A run with `--target test` no longer shares `Logs\event_read_mark.json` and
  `Logs\first_run_budget.json` with the production run. It uses `event_read_mark_test.json` and
  `first_run_budget_test.json`. The production names are unchanged, so an existing install keeps its
  position.
- A later (non-first) event-log read that times out now halves its next size, down to 250 events, as
  a first-run read does. Before, it retried the same size on every run. A dry run never saves the size.
- When `taskdash_common` itself fails to import, a wheel install now saves the traceback in
  `Logs\bootstrap_FAIL_<time>.log` before it re-raises. Before, no file was written. The file goes
  to the same folder as every other log, including the folder that owns the running venv.
- `Install-TaskDash.ps1` now treats an inherit-only generic write or full-control entry (`(GW)`, `(GA)`)
  for Users, Authenticated Users or Everyone as a loose folder, as preflight does. Before, it skipped
  the lock and preflight failed afterwards.
- docs/TROUBLESHOOTING.md now says that a task whose XML export fails stays in the inventory with
  blank monthly trigger details. Only a task that cannot be read at all is left out.
- A collector run by an account that cannot read the restricted hand-off file (a window that is not
  elevated, or another account) now leaves the file alone and logs a warning. Before, it failed the
  run with "The hand-off file could not be opened".
- `taskdash preflight` no longer reports `[ok]` for the install folder on a Windows in another
  language. It matches the local names of Users, Authenticated Users and Everyone, and warns when it
  finds neither SYSTEM nor Administrators in the permissions.
- The `host name` preflight line and the `collect --apply` refusal now share one mismatch test.
- The hand-off store now installs the ArcPy import guard before its first ArcGIS import. The collector
  and `preflight --as-task` store the hand-off before they connect, so on a cloned Pro environment a
  Password-logon task died on the Pro licence and kept the file. `signin --for-task-account --apply`
  now creates the hand-off file with share mode 0 and restricts it through that handle, so no other
  handle can open the file while it still has the folder's permissions.
- `taskdash preflight --as-task` no longer stops with a traceback when it cannot delete its output
  file. It names the file, tells you to delete it, and exits 1.

- The push and release workflows now install PyYAML for the suite, and the workflow-parsing tests
  fail instead of skipping when `CI` is set and PyYAML is missing. They skipped on those runs before.
- A failed `MetricChange` write now makes the next run send the metric files again. Before, the
  run saved `metrics_state.json` anyway, so the next run skipped the recompute and the table kept
  stale values until a log file changed. Prod-visible only after such a failure.
- With `decimal_separator` set to `","`, a `.` groups thousands only in a number such as `1.234` or
  `12.345.678,9`. `1.5` and `0.512` stay decimals instead of reading as 15 and 512. `TASKDASH`
  metric lines are no longer rewritten, so they keep their documented `3.5` format.
- A sign-in or an ArcGIS call that fails on a certificate error now names SSLError and the fix (a TLS-inspecting proxy: trust its root certificate or set `REQUESTS_CA_BUNDLE`) instead of the generic "cannot reach" or "failed after several tries" text. Prod-visible only on that failure.
- The collector and `taskdash preflight` print one WARNING when the installed `arcgis` is outside `>=2.4,<3`. Preflight gains an "arcgis version" line. No change when the version is in range. The version comes from the package metadata, so the check never imports `arcgis` before the ArcPy import guard is installed.

- Stored keys fold case (H4-8). A task renamed only in case now updates its row in the hosted
  tables (`TaskInventory` included) instead of adding a duplicate that the case-insensitive unique
  index rejects. The change history (`TaskChanges`) treats it as the same task too: no
  `task_removed` and `task_added` pair for a case-only rename.
  `alert_state.json` is now keyed on the full task name (folder and name), so two tasks that share a
  short name in different folders no longer share one alert. An older `alert_state.json` is re-keyed
  on read, so the upgrade sends no repeat alert and drops none. The next write saves the new keys.
  The e-mail still names tasks by their short name. An old alert for a short name that two tasks
  share is not re-keyed, because the old key cannot say which task it was. The next alert e-mail
  then lists that problem once more. No table field changed.
- A corrupt `alert_state.json` (H4-7) is no longer reset silently. TaskDash logs a WARNING, keeps
  the bad file as `alert_state.json.corrupt-<stamp>` and rebuilds the state, so alerts already sent
  may repeat once. A file whose fields have the wrong type (a text where a list or an object belongs)
  gets the same treatment, because it would otherwise fail every hourly alert check.
  `--preview-alert` still changes no file. `read_last_lines` already capped a
  read at 8 MB from the end; a test now pins that default.
- The Microsoft task filter (H4-5) now matches the whole folder name. It skips `\Microsoft\` and
  its subfolders, and no longer skips a folder such as `\MicrosoftSync\`. If the reference install
  has such a folder, its tasks now appear. The PowerShell query drops the Microsoft tasks itself,
  so they are never sent to Python, and the "Collected N task row(s)" line counts fewer rows. A
  Microsoft task that `Get-ScheduledTaskInfo` cannot read no longer appears in the unreadable-task
  warning; an error from the task listing itself still does. The query now pipes each task to `Get-ScheduledTaskInfo` and, for a monthly trigger, to `Export-ScheduledTask`
  instead of passing its name, because a name with `[` and `]` was read as a pattern. A monthly task
  with brackets in its name now shows its months and days. `taskdash preflight`
  still passes the task check on a computer whose only tasks are under `\Microsoft\`, even when
  the listing reports an error for one of them, so the installer is not blocked. No table field changed.

### Security

- `Install-TaskDash.ps1` never uses an existing install folder that an ordinary user owns or can write
  to. It stops before it runs `taskdash.exe` or reads a config from such a folder, in check mode too.
  It creates a new folder with its owner and permissions in one step, so no other user can change the
  folder in between. It copies the wheel into that folder, checks the hash of the copy, and installs
  the copy. It also refuses a junction or link at the install path and a parent folder that an
  ordinary user can change, and it runs Python with `-I`, so files in the current folder are not
  imported. Any write right held by an account other than SYSTEM, Administrators, TrustedInstaller,
  the installing administrator or the task account now counts as loose, not only those of Users,
  Authenticated Users and Everyone. With `-Apply`, it stops when it cannot read the local
  Administrators group, because it would give an unconfirmed task account write access to a folder
  that it runs programs from as an administrator.
- `taskdash preflight` now warns when the install folder's owner is not SYSTEM, Administrators,
  TrustedInstaller or a direct member of the Administrators group. The owner can always change the
  folder's permissions, so a locked folder owned by an ordinary user is not locked against that user.
  The warning gives the `icacls /setowner` fix. It does not set exit code 1.

## [1.1.0] - 2026-10-27

### Added

- `taskdash preflight` has a `health check` line. It warns, without failing, when
  `alerting.healthcheck_url` is empty, because a collector that never starts is otherwise silent. It
  prints only "set" or "not set", never the address. INSTALL.md now recommends an outside health check.

- `taskdash test-alert` checks the alert e-mail path. Without `--apply`, it prints the recipients,
  the server and the subject, and sends nothing. With `--apply`, it sends one test message through
  the same send code as the hourly run, and exits 1 with the reason if that fails.

- `alerting.include_log_tail` (default `true`) controls the log tail in the alert e-mail. Set it to
  `false` to leave the tail out.
- `secret_words` in a rules file adds key words, such as `Passwort` or `senha`, to the secret
  scrubber. `rules/default.json` lists eight German, French, Spanish, Portuguese and Dutch words.

### Security

- Each release now lists `requirements.lock` (every dependency pinned with SHA-256 hashes, for
  Python 3.11 64-bit on Windows) and a CycloneDX SBOM, `taskdash-<version>.cdx.json`. Both are in
  `SHA256SUMS.txt`. INSTALL.md has an optional locked install that uses `--require-hashes`.
  The version ranges in `pyproject.toml` do not change.
- Task text that reaches dashboard list cards (descriptions, paths, command lines, result and log
  text) is now HTML-escaped in the Arcade expressions, so `<` `>` `&` show as text. That includes
  the task name and host name in list cards, which come through `{expression/f_*}`. The alert
  e-mail drops control characters other than tab and newline, and its subject stays on one line.
- Every `collect --apply` run now refuses to publish when the configured view or dashboard is
  shared with everyone. The run still sends the failed-task alert, writes nothing and exits 1.
  Organization and group sharing stay allowed. A view or dashboard that TaskDash cannot find only
  warns.
- `collect --create-view` now leaves the new view owner only. Before, it copied the organization and
  group sharing of the service onto the view. It prints how to share the view with the viewers
  group. `taskdash setup` is unchanged: it shares with its private group only.
- `metrics.log_roots` (default empty) confines log reads. When it lists folders, TaskDash mines
  and quotes in alert e-mails only logs that resolve inside them. It skips junctions and symbolic
  links, and warns once per run with the number of skipped folders. It checks the opened file
  again by the opened file's own path, so a file or folder swapped for a link after the check is
  not read.
- A log read now takes at most the last 8 MB of a file. Before, a huge log with no line breaks was
  read whole, and the alert e-mail tail could fill memory.
- The secret scrubber now also hides values after `--client-secret`, `-ClientSecret`,
  `--access-token`, `-AccessToken`, `--auth-token` and `--refresh-token`, `token=` in a web
  address, and GitHub, Slack, AWS and JSON Web tokens wherever they appear.
- TaskDash now scrubs every line and traceback it writes to its own log file. It also scrubs the
  copy it prints, which `run_taskdash.cmd` keeps in `Logs\bootstrap.log`. A crash traceback and a
  fatal error message on stderr, which `run_taskdash.cmd` keeps in `Logs\bootstrap_FAIL_<id>.log`,
  are scrubbed the same way.
- A failed health-check ping now logs only `scheme://host/<redacted>`. Before, it logged the whole
  address, and the path is the secret ping ID.
- The log tail in the alert e-mail now reads UTF-16 logs correctly. Before, a UTF-16 log came out
  with NUL bytes between the letters, so no secret pattern matched.
- TaskDash now starts `powershell.exe`, `wevtutil.exe` and `cmd.exe` by absolute path under
  `%SystemRoot%\System32` (`C:\Windows` if unset). A planted copy in the working folder or PATH can
  no longer run as the task account.
- `SECURITY.md` now states response targets: acknowledgement within 7 days, an assessment or fix
  plan within 30 days, and coordinated disclosure after a fix ships or 90 days after the report. The
  targets are goals, not a service-level agreement.
- The GitHub workflows are hardened. Every action is pinned to a full commit SHA on a Node 24
  release, checkouts no longer keep the token, and the top-level permission is read-only. The
  release workflow now runs the full test suite and builds with read access only. A separate publish
  job holds the only write permission and needs both to pass, so a failing tag cannot publish. CI
  pins `arcgis>=2.4,<3`, and the gitleaks download is checked against its published SHA-256.
- Releases now carry Sigstore signatures. A separate sign job signs the wheel and `SHA256SUMS.txt`
  with its GitHub identity and no stored key, and the release lists a `.sigstore.json` bundle for
  each. Only the sign job has `id-token: write`, so the unlocked build tools never run where they
  could request the token. The workflows also pin `build` to an exact version.
  `docs/INSTALL.md` shows the optional verify command.
  `SECURITY.md` notes that the public transparency log records the repository and workflow path.

### Deprecated

- `alerting.mail_config_path` is deprecated. TLS stays unverified for that file, so alerts keep
  working, but `taskdash preflight` now warns on the `smtp` line and the collector logs one WARNING
  per run with the steps to move to the `smtp` block. A separate warning appears when the file holds
  a login and turns TLS off. Neither warning prints a value from the file.

### Changed

- The default of `scheduler.collector_task_name` is now `TaskDash Collector`, the task name that
  `install_task.ps1` registers. Before, the default was `Task Dash`, so a new install had to set the
  key. An install that registered its task under another name must set the key to that name.

- Release files are public at `uhsear/taskdash-releases`. INSTALL.md and README.md link to that
  page and no longer say that you must ask for access.

### Fixed

- A failed alert e-mail is no longer silent. When an alert is due and cannot be sent, including
  when alerting is on but the mail settings are unusable, the run still publishes everything. It
  then saves `Alert e-mail failed: <reason>` in `error_text` of the `CollectorRuns` row, pings the
  health check with `/fail`, and exits 1 with a message that names the `smtp` settings and
  `taskdash test-alert`. The alert state stays unsaved, so the next run tries again. A run with
  nothing to send still exits 0. An empty `alerting.to` now fails the same way when an alert is due.

- A schema-drift refusal or a public source service no longer silences the failed-task alert.
  The run still writes nothing to the service and still exits 1, but it now sends the alert first.
  It saves the `CollectorRuns` row to the pending file, as it does when AGOL is down.
- A wheel install now keeps a config error or a module import error in
  `Logs\bootstrap_FAIL_<time>.log`. Before, the task ran `taskdash.exe` with no log to read.
  This includes a config saved as UTF-16 by Windows PowerShell 5.1 and a config the task account
  cannot read. Both now exit with a message that says how to fix the file.
- The help example in `install_task.ps1` passed the venv folder as `-InstallDir`. The task would
  start there and miss the config. The example now matches `docs/INSTALL.md`.
- A turned-off Task Scheduler log no longer discards the whole run. The run now publishes the task
  inventory and the overdue state, sends the alerts, and saves the `CollectorRuns` row with the
  message in `error_text`. It then exits 1. It reads no events, so the watermark does not move.
- A wheel install now shows the collector's own failures and its Latest log. TaskDash now also
  recognises the task that runs `taskdash.exe collect`, wherever the venv sits.
- A first `--apply` run on a busy event log no longer repeats the same timed-out read every hour.
  A timed-out PowerShell call now ends with a message that names `powershell_timeout_seconds`,
  not a traceback. The run still publishes the task inventory, sends the alerts and saves the
  `CollectorRuns` row, then exits 1. A timed-out first read also halves the next one, saved in
  `Logs\first_run_budget.json`. The file stays until a publish moves the watermark, so a run never
  goes back to a read that timed out, and `--preview-alert` never changes it.
- One corrupt or unreadable task definition no longer stops the whole inventory. The run collects
  every readable task and prints a warning that names each problem task. It saves the names in
  `error_text` of the `CollectorRuns` row, and the run still counts as a success. A task whose
  run info cannot be read is left out, not listed as never run. If Task Scheduler returns errors
  and no task at all, the run fails with `Cannot list scheduled tasks`.
- A Task Scheduler log that wrapped or was cleared between two runs is no longer silent.
  The event read now also returns the oldest record id left. When that id is more than one past
  the newest record the last published run read, the run warns how many record ids were skipped
  and saves the warning in `error_text`. The run still exits 0. TaskDash keeps the newest id read
  in `Logs\event_read_mark.json`, not the watermark, because the watermark moves only with
  monitored runs. A host with idle monitored tasks therefore gets no false warning.

## [1.0.2] - 2026-10-19

### Fixed

- Paged reads from the hosted tables are ordered by object ID. Without an order, a short last page
  made the arcgis package fetch pages on several threads. On the Python that ships with ArcGIS
  Pro 3.6 and 3.7, that turned off certificate checks for the rest of the run. The order also keeps
  offset paging stable.

## [1.0.1] - 2026-10-18

### Fixed

- `taskdash setup` creates the view in the setup folder, not the account's root folder. A view that
  fails after it exists is now in the "Created by this run" list, so the undo steps remove it.
- The `taskdash demo --apply` refusal says that the demo signs in only through a profile. It no
  longer tells an install that signs in with a username to run setup again.
- TaskDash treats its folder as a plain-folder install only when `rules/default.json` is beside the
  modules. An unrelated `rules` package in the same site-packages no longer changes where it reads
  its data.
- `SHA256SUMS.txt` in a release uses LF line endings, so `sha256sum -c` works on Linux.

## [1.0.0] - 2026-10-18

The first stable version.

### Added

- An install guide, `docs/INSTALL.md`, that takes a new organization from nothing to a working
  dashboard.
- An introduction, `docs/INTRODUCTION.md`, a `README.md` and a security policy, `SECURITY.md`.
- The Apache License 2.0 (`LICENSE`) and a `NOTICE` file. The package metadata names the license and
  the readme.
- A preflight check that the hourly task's account is a member of the local Administrators group.
  A standard account sees only some of the tasks and misses the rest without an error.
- A continuous-integration workflow that runs the tests in three time zones and scans the history
  for secrets. A release workflow builds the wheel and its SHA-256 checksum for each version tag.
- Tests that fail when the documentation omits a configuration key, a command-line flag or an exit
  message. Items on a pending list do not fail yet.
- A `taskdash demo` command that runs TaskDash on invented data in its own `demo` folder. With
  `--apply`, it publishes that data to separate `DEMO_` items in the `TaskDash_Demo` folder.
- A configuration guide, `docs/CONFIGURE.md`, a dashboard and e-mail guide, `docs/USE.md`, and a
  troubleshooting guide, `docs/TROUBLESHOOTING.md`, that lists every exit message.

### Changed

- The install guide tells you to lock the permissions of the install folder. Only SYSTEM,
  administrators, the installing user and the task account can open it.
- Every ArcGIS sign-in honors the `HTTPS_PROXY` environment variable.
- Sharing uses the current ArcGIS API property, `Item.sharing`, in place of the older `share()`
  method.
- The first-install error messages now name the fix. The preflight fixes name both the wheel and
  the folder install forms.
- The install guide says that the repository is private and how to check the downloaded wheel.

### Fixed

- The example configuration note for `scheduler.exclude_command_target_types` said file
  extensions, such as `.vbs`. The setting takes type names, such as `VBScript`.
- The install guide said that the dashboard shows times in the collector's time zone. It shows
  them in the time zone of the viewer's device.

## [0.9.0] - 2026-10-17

The version that the code reports before the first stable release. It has no tag. The date is the
date of its last commit.

### Added

- `taskdash setup`: creates the folder, the hosted service with five tables and a read-only view.
  It also creates a private viewers group and the dashboard, and writes the new item IDs into the
  config.
- `taskdash preflight`: checks the config, the ArcGIS account and its privileges, and the event log.
  It also checks the task definitions, the log folders, the mail server, the service and the view.
  Then it checks their sharing, the task's logon type and the tested scale. It changes nothing,
  except with `--enable-event-log --apply`.
- One `taskdash` command with the subcommands `collect`, `setup`, `preflight`, `dashboard` and
  `status`.
- A Python wheel, and an installer script, `install_task.ps1`, that registers the hourly task.
- A read-only, query-only view. The dashboard now reads the view, and the editable service stays
  owner only. Publishing stops when a view is configured and the service is shared with everyone.
- Organization rules in data files under `rules/`, so one code base serves many organizations.
  An organization's own rules file stays private and is not packaged.
- Secret scrubbing on the published free-text fields and on alert e-mails. The
  `collect --scrub-history` option cleans rows stored earlier.
- Privacy switches that hide command lines, accounts, paths and log samples. A missing switch hides.
- A golden replay test. It rebuilds the tables and the alert e-mail from a captured fixture and
  compares them with an approved result.
- A test suite that runs on synthetic task names.

### Changed

- The dashboard was rebuilt around what the operator does next. It shows a reason for each task that
  needs attention and an About panel with the version. Its numbers and labels are consistent.
- Publishing is safe to retry and to overlap. Writes to ArcGIS Online keep one row per key and
  trust only unique indexes.
- The collector and the log miner fail loudly instead of silently when a source is unreadable.
- TaskDash detects problems earlier. It sends an alert even when publishing fails.
- One incident e-mail names the likely shared cause when several tasks fail together.
- The alert state file is written again after a short wait when Windows holds a lock on it.
- Stuck and disabled tasks show the correct state. A refresh on an unmigrated service is refused.

### Fixed

- Run starts are no longer overwritten with estimates of about 0 seconds.
- The log level word no longer replaces a duration metric.
- The row of the TaskDash task no longer reads "Running now" between collections.
- Many numbers, labels and messages on the dashboard and in alerts now match what happens.

### Security

- Hosting the data behind a view means viewers read through a query-only item. The editable service
  is never shared by setup.
- Fixtures, comments and tests use synthetic names in place of real organization data.

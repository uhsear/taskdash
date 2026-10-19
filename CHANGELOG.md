# Changelog

All notable changes to TaskDash are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The version numbers follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `taskdash preflight` has a `health check` line. It warns, without failing, when
  `alerting.healthcheck_url` is empty, because a collector that never starts is otherwise silent. It
  prints only "set" or "not set", never the address. INSTALL.md now recommends an outside health check.

- `taskdash test-alert` checks the alert e-mail path. Without `--apply`, it prints the recipients,
  the server and the subject, and sends nothing. With `--apply`, it sends one test message through
  the same send code as the hourly run, and exits 1 with the reason if that fails.

### Changed

- The default of `scheduler.collector_task_name` is now `TaskDash Collector`, the task name that
  `install_task.ps1` registers. Before, the default was `Task Dash`, so a new install had to set the
  key. An install that registered its task under another name must set the key to that name.

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

# Troubleshoot TaskDash

TaskDash stops with a message when it cannot continue safely. This guide lists every such message,
what causes it, and how to fix it. Find your message by its first words. A `<name>` in a message
stands for a value that changes, such as a path or an item ID.

Most messages end with the line `See TROUBLESHOOTING.md, section <heading>.`. The heading is the name
of the entry in this guide that explains the message. A few messages do not name a section yet.

In the commands below, `taskdash` stands for the `taskdash.exe` that you installed. The install
guide calls it `$td`. Run the commands in the folder where TaskDash keeps its config, as step 1 of
[INSTALL.md](INSTALL.md) describes.

Entries are grouped by command:

- [Read the result of a run](#read-the-result-of-a-run)
- [Any command](#any-command)
- [Sign-in and config](#sign-in-and-config)
- [Collect and publish](#collect-and-publish)
- [Setup](#setup)
- [Remove](#remove)
- [Preflight](#preflight)
- [Rebuild or move the ArcGIS items](#rebuild-or-move-the-arcgis-items)
- [Dashboard](#dashboard)
- [Demo](#demo)
- [Status](#status)

## Read the result of a run

If the scheduled task failed, start with its last result and its newest log. Step 8 of
[INSTALL.md](INSTALL.md) explains both, and lists what each `LastTaskResult` value means.

1. In PowerShell as administrator, run
   `(Get-ScheduledTaskInfo -TaskName "TaskDash Collector").LastTaskResult`.
2. If the value is `1`, TaskDash started and stopped with an error. Open the newest
   `taskdash_*.log` file in the `Logs` folder of the install, such as `C:\TaskDash\Logs`.
3. Find the line that contains `TaskDash exiting:`. The text after it is the message. Look it up in
   this guide.

If the config sets `logging.log_dir`, the logs are in that folder instead.

If no log has the time of the failed run, or the newest log has no `TaskDash exiting:` line,
`collect` stopped before it opened its log. A config problem causes this, because `collect` reads
the config first. To see the message, run `taskdash preflight --offline`. The `config` line shows
it.

If the log has the line `TaskDash failed with an uncaught exception.`, TaskDash met an error that
this guide does not list. The lines after it show where. Remove identifying values from them, as the rule in
[Issues](../README.md#issues) says. Then send them to the TaskDash maintainer.

When you run a command in a window, it prints its message there. The `collect` command also
writes the message to its log. The `setup`, `remove`, `dashboard`, `demo` and `status` commands write
no log of their own. A message ends the command with exit code 1.

The `preflight` command does not stop at a message. It shows the message in a `[!!]` line, after
`SystemExit:`, and runs the other checks.

To see the last line of the newest log without opening it, run `taskdash status --offline`.

## Any command

### A command says it is not part of this install

> `<module>`.py is not part of this install, so this command is unavailable. Reinstall TaskDash from a complete wheel.

**Cause.** The `taskdash` command found the command's Python file missing. The files are
`taskdash_publish` (`collect`), `taskdash_setup`, `taskdash_remove` (`remove`), `taskdash_preflight`,
`taskdash_dashboard` and `taskdash_demo`. A damaged install or an incomplete wheel causes this.

**Fix.** Run the install again with the full wheel from the release. If pip says the package is
already installed, add `--force-reinstall --no-deps` to the `pip install` command from step 1 of
[INSTALL.md](INSTALL.md).

### A module failed to import

> `<module>` failed to import. Reinstall TaskDash; if it persists, send this traceback to the maintainer.

**Cause.** The command's Python file exists but raised an error when Python loaded it. A damaged
or mixed-version install causes this. A wheel install has no wrapper script, so TaskDash also
saves the message and the traceback in `Logs\bootstrap_FAIL_<time>.log` under the `TASKDASH_HOME`
folder, or the working folder if that is not set. A wheel install saves a config error there too,
before the run log exists. TaskDash hides secrets in the text and deletes the file with the other
old logs. The folder install keeps its own `bootstrap_FAIL_` file through `run_taskdash.cmd`.

If `taskdash_common` itself fails to import, the message starts with `taskdash_common failed to
import`. A wheel install saves the traceback in the same file. This one file is not scrubbed of
secrets, because the scrubber lives in the module that failed. It holds only an import traceback.

**Fix.** Read the traceback. Run the install again with the full wheel from the release, as in the
fix above.

### A command prints a usage line and an error

> taskdash: unknown command '`<name>`'

**Cause.** The command name or a flag is wrong, or two flags cannot be used together. The
command-line parser then prints a `usage:` line and an error, and ends with exit code 2. For an
unknown command name, `taskdash` prints the message above and the list of commands. It also ends
with exit code 2.

**Fix.** Read the error line. Run `taskdash <command> --help` to see the flags of that command.
The reference part of [CONFIGURE.md](CONFIGURE.md) lists the flags.

### A log line was withheld

> log line withheld: secret scrubbing failed

**Cause.** TaskDash removes secrets from every line before it writes it to its own log. The
scrubber raised an error on one line, so TaskDash wrote this text instead of the line. It never
writes a line that it could not scrub. The run continues.

**Fix.** If the text repeats, check the rules files for a recent change to `secret_words`, then
re-run. If it still repeats, send the time of the line to the maintainer. Do not send the task's
command line.

## Sign-in and config

These messages come from reading `TaskDash.config.json` and from signing in to ArcGIS Online. Every
command that reads the config can show them.

### The config file is missing

> Config not found at `<path>`: copy TaskDash.config.example.json to `<file name>` in `<folder>` and fill it in.

**Cause.** TaskDash looks for `TaskDash.config.json` in the folder it starts in. It did not find the
file there, or the file named by `--config` does not exist.

**Fix.** Do one of these:

1. Change to the install folder with `Set-Location C:\TaskDash`, then run the command again.
2. Pass the file with `--config <path>`.
3. Set the `TASKDASH_HOME` environment variable to the folder that holds the config.
4. If the file does not exist, create it as step 2 of [INSTALL.md](INSTALL.md) describes.

### The config is not valid JSON

> Config at `<path>` is not valid JSON (line `<number>`): `<detail>`

**Cause.** The file has a JSON syntax error at or just before the named line. A missing comma, an
extra comma after the last item, or a missing quote are the usual causes.

**Fix.** Open the file in an editor, go to the line, and correct the syntax. Run
`taskdash preflight --offline` to check it again.

### The config is not UTF-8 text

> Config at `<path>` is not UTF-8 text. Open it in an editor and save it as UTF-8, then run again.

**Cause.** The file uses another encoding. Windows PowerShell 5.1 writes UTF-16 with `Out-File`.

**Fix.** Open the file in an editor and save it as UTF-8. Run `taskdash preflight --offline` to
check it again.

### The config cannot be read

> Cannot read the config at `<path>`: `<detail>`. Give the account that runs TaskDash read access to the file, then run again.

**Cause.** The account that runs TaskDash has no read access to the file, or another program
locks it.

**Fix.** Give the task account read access to the file. Close any program that holds it open.

### The config is not a JSON object

> Config at `<path>` must be a JSON object, not `<type>`.

**Cause.** The file is valid JSON, but its top level is not an object. For example, it is a list.

**Fix.** Make the file start with `{` and end with `}`. Compare it with
`TaskDash.config.example.json`.

### The config has problems

> Config at `<path>` has `<number>` problem(s) -- fix and re-run:

One line follows for each problem.

**Cause.** A key has a value of the wrong type, or a key in the `privacy` section is unknown. A
line of the first kind reads `<key> must be <type>, got <value>`. A line of the second kind reads
`unknown key 'privacy.<key>' (valid privacy keys: ...)`. A mistyped privacy key stops the run on
purpose. Otherwise the field it was meant to hide would stay published. TaskDash shows a password
value as `<redacted>` in these lines.

Two more lines concern `host_previous_names`. One says it `must not hold an empty name`. The other
says it `must not list the current host name`. Remove that entry. Keep only earlier names of this
computer. The current name is `host_name_override`, or the computer name if that key is empty.

**Fix.** Correct each line, then run the command again. The `_notes` section of
`TaskDash.config.example.json` explains each key. [CONFIGURE.md](CONFIGURE.md) lists each key and
its type.

### The config has no rules profiles

```text
Config at <path> has no rules.profiles. Add "rules": {"profiles": []} to use only rules/default.json, or list your rules files, e.g. {"profiles": ["rules/<org>.json"]}.
```

**Cause.** The config has no `rules.profiles` key. TaskDash will not guess, because an install that
has its own rules file would lose those rules without a warning.

**Fix.** Add `"rules": {"profiles": []}` to the config to use only the shipped `rules/default.json`.
To use your own rules file, list its path in `profiles`. TaskDash reads that path relative to the
install folder.

### A rules file has a problem

> Rules problem in `<where>`: `<problem>`. Fix: `<fix>`, then re-run.

**Cause.** A rules file (`rules/default.json` or a file listed in `rules.profiles`), or the
`metrics.tasks` section of the config, has an entry that TaskDash cannot load. `<where>` names the
file and the entry. `<problem>` says what is wrong. These problems are the usual ones:

- A pattern is not a valid regular expression. In JSON, write one backslash as two.
- The file cannot be read, is not valid JSON, or does not hold a JSON object.
- A section name is unknown. Start the name of a note with `_`.
- An entry has no `order` number, or two entries use the same `order`.
- A `cause_text` entry is missing for a cause that a rule can produce.
- A `secret_words` entry is empty, or the section is not a list of words.
- `decimal_separator` is not `"."` or `","`.

**Fix.** Do what the `Fix:` part of the message says. Paths in `rules.profiles` are relative to the
install folder, the folder that TaskDash starts in.

### TaskDash cannot reach ArcGIS

```text
Cannot reach arcgis.url '<url>'. Check the URL in the config (ArcGIS Online is https://www.arcgis.com or https://<org>.maps.arcgis.com), the network and any proxy. Detail: <error>
```

**Cause.** The computer could not open an HTTPS connection to `arcgis.url`. The URL may be wrong,
the network may be down, or a proxy may block the connection.

**Fix.**

1. Check that `arcgis.url` is spelled correctly and uses the same URL as your stored sign-in.
2. If the computer uses a proxy, set the `HTTPS_PROXY` variable as the requirements in
   [INSTALL.md](INSTALL.md) describe. Open a new PowerShell window afterwards.
3. If only the scheduled task still fails, restart the computer. Task Scheduler then sees the new
   variable.

### The certificate was not trusted (SSLError)

> SSLError: the certificate of arcgis.url '`<url>`' was not trusted. A proxy that inspects TLS traffic is the usual cause. Add the proxy's root certificate to the Windows certificate store, or save it as a PEM file and set the REQUESTS_CA_BUNDLE variable to that file's path. Open a new PowerShell window afterwards. Detail: `<error>`

The same text follows this start when the failure comes after sign-in, in a query or a write:

> `<label>` failed after `<number>` attempts. SSLError: the certificate of the ArcGIS server was not trusted.

**Cause.** A proxy that inspects TLS traffic replaces the ArcGIS certificate with its own. Python
does not trust the proxy's root certificate.

**Fix.** Ask your network team for the proxy's root certificate.

1. Add it to the Windows certificate store, or save it as a PEM file.
2. If you saved a PEM file, set the `REQUESTS_CA_BUNDLE` variable to the file's path, as
   `HTTPS_PROXY` is set in [INSTALL.md](INSTALL.md).
3. Open a new PowerShell window. Restart the computer if only the scheduled task still fails.

### The arcgis package is outside the tested range

> WARNING: arcgis `<version>` is outside the tested range (arcgis>=2.4,<3).

**Cause.** TaskDash is tested with `arcgis` 2.4 up to, but not including, 3. The collector and
`taskdash preflight` warn once when the installed version is outside that range.

**Fix.** Install a version in the range: `pip install "arcgis>=2.4,<3"`. ArcGIS Pro installs its own
version, so install it in the virtual environment that runs TaskDash.

### The stored sign-in does not work

> arcgis.profile '`<name>`' did not sign in. Store it once with GIS(url, user, password, profile='`<name>`') as the account the scheduled task runs under.

**Cause.** The Windows account that runs the command has no saved sign-in under that profile name,
or the saved password is no longer correct. Each Windows account has its own Credential Manager.
The scheduled task runs as the task account, so that account needs its own profile.

**Fix.** Store the profile again for the Windows account that ran the command. Run
`taskdash signin --apply`, as step 3 of [INSTALL.md](INSTALL.md) describes. If the scheduled task
failed, run `taskdash signin --for-task-account --apply` in your own window, then start the
collector task. The task account stores the sign-in on that run. If the hand-off fails, see
"The hand-off sign-in was kept". As a fallback, run `taskdash signin --apply` in a `runas` window as
the task account. If the ArcGIS password changed, or an administrator reset the task account
password, follow [ROTATE.md](ROTATE.md).

### The scheduled task fails after a password change

> `LastTaskResult` is `2147943726` (0x8007052E), or `install_task.ps1` ends with `ERROR: A task named '<name>' already exists. To give it a new password, add -Replace.`

**Cause.** The task stores the task account's Windows password. After the password changes in
Windows, the stored copy is old, and Windows rejects every start. The installer refuses to touch an
existing task unless you add `-Replace`.

**Fix.** In a window as administrator, run the installer from step 7 of [INSTALL.md](INSTALL.md)
again with ` -Replace -Apply` added, and type the new password. The installer stops with one of these
messages when the task is not the one it expects:

- `-Replace found no task named '<name>'`: no task has that name. Pass the right `-TaskName`, or run
  without `-Replace` to register the task.
- `Task '<name>' runs as '<account>', not '<account>'`: pass `-User` with the account that the task
  runs as. A new account needs a new task.
- `Task '<name>' logs on as '<type>', not '<type>'`: pass `-LogonType` with the type that the task
  uses.
- `Task '<name>' has <n> action(s), or one that does not start a program`: `-Replace` keeps one
  program action. Delete the task with `Unregister-ScheduledTask`, then register it again.

`-Replace` keeps the task's command, arguments and working directory, so a hand-added argument
survives. Use `Unregister-ScheduledTask` and a new install if you want the install default back.

If an administrator reset the password, the saved ArcGIS sign-in and the mail password of the task
account may also be lost. [ROTATE.md](ROTATE.md) lists the steps for each case.

### The installer refuses with -Apply

> `ERROR: This window is not elevated.` or `ERROR: '<account>' is not in the local Administrators group.`

**Cause.** `install_task.ps1 -Apply` registers a task, which needs an administrator window. The task
account must also be an administrator, or the collector misses other accounts' tasks and never starts.
Without `-Apply`, the installer prints the same problem as `WARNING: -Apply would refuse.` and exits 0.

**Fix.** Open PowerShell with **Run as administrator** and run the command again. If the account
is missing from the group, run `net localgroup Administrators DOMAIN\svc-taskdash /add`, with your
account name. A `WARNING` that the account is not a direct member means it may belong through a
group. Confirm that, because the installer cannot read nested membership.

Two more messages come from the new switches:

- `-EnableEventLog needs the venv's taskdash.exe`: pass `-Python` with the full path of
  `taskdash.exe`, or install TaskDash into `<InstallDir>\venv`.
- `'taskdash preflight --enable-event-log --apply' failed with exit code <n>`: nothing was registered.
  Read the output above the message, fix the cause, and run the installer again.

### The one-window installer stops

> `ERROR: <message>` from `Install-TaskDash.ps1`, followed by `Created so far:`

**Cause.** `Install-TaskDash.ps1` stops at the first step that fails. The lines under
`Created so far:` list what it made before the stop. Check mode (no `-Apply`) stops at the same
problems, so run it first.

**Fix.** Fix the cause that the message names. Then run the same command again. A step that is
already done prints `nothing to do`, so the script continues where it stopped. These are the messages:

- `This window is not elevated`: open PowerShell with **Run as administrator**. Check mode prints
  this as `WARNING: -Apply would refuse` and continues.
- `could not list the local Administrators group`: Windows could not read the group, so the
  script cannot confirm that the task account is an administrator. The task account gets write
  access to the install folder, so the script stops. Run `net localgroup Administrators`. If the
  list shows an account that no longer exists, remove it, then run the command again.
- `'<account>' is not in the local Administrators group`: add the task account to the group with
  `net localgroup Administrators <account> /add` in an elevated window. Check mode prints this as
  `WARNING: -Apply would refuse`. The script checks it before it creates anything.
- `-InstallDir '<path>' is not a full path`: pass a path such as `C:\TaskDash`.
- `-Wheel '<path>' does not exist`: pass the path of the downloaded wheel.
- `'<file>' is not named taskdash-<version>-py3-none-any.whl`: download the wheel again and keep its
  name. pip rejects a renamed wheel.
- `'<file>.sha256' does not start with a 64-character SHA-256 value`: download the `.sha256` file
  again. Or move it away to skip the check.
- `'<folder>\SHA256SUMS.txt' does not list '<wheel>'`: the file belongs to another release. Download
  `SHA256SUMS.txt` from the same release as the wheel. Or move it away to skip the check.
- `The SHA-256 of '<wheel>' does not match`: do not install the file. Download the wheel and its
  `SHA256SUMS.txt` (or `.sha256`) file again.
- `'<folder>' already has TaskDash <a> and -Wheel is <b>`: the script does not change an installed
  version. Follow [UPGRADE.md](UPGRADE.md).
- `TaskDash is not installed in '<venv>', so -Wheel is required`: pass `-Wheel`.
- `py.exe is not in '<folder>'`: install 64-bit Python 3.11 for all users, as the Requirements in
  [INSTALL.md](INSTALL.md) describe.
- `'<folder>' already exists, and an ordinary user owns it or can write to it`: any user can create
  a folder under `C:\` and put files in it. The script would run them as an administrator, and
  `taskdash signin` would send your password to the URL in that folder's config. So the script
  never uses such a folder. Move the folder away, then run the command again. The script creates a
  new folder, owned by Administrators and open only to SYSTEM, Administrators, you and the task
  account. Copy back only a config file that you have read.
  An ordinary user here is any account except SYSTEM, Administrators, TrustedInstaller, you and the
  task account. A junction or a symbolic link at the install path stops the script the same way.
- `'<folder>', a parent folder of -InstallDir, is missing or a link, or an ordinary user owns it or
  can write to it`: a user who can rename a parent folder can later swap the install folder for
  their own. Use a folder directly under the drive, such as `C:\TaskDash`. Otherwise create the
  parent folder from an elevated window, and leave write rights only to SYSTEM, Administrators and
  you. At the drive root, every user may add folders; the script stops only when ordinary users
  may also delete folders there or change the root's permissions.
- `'<account>' is not an account that this computer knows`: check the `-TaskAccount` name.
- `'<folder>' changed while this script created it`: another user made the folder first. Move the
  folder away, then run the command again.
- `'py -3.11 -m venv' failed`, `pip could not install '<wheel>'`, or `pip finished, but '<exe>' does
  not exist`: read the output above the message. Check the connection to `pypi.org`. If the
  virtual environment is broken, move the `venv` folder away and run the command again.
- `'<exe> --version' failed ... The venv is broken`: move the `venv` folder away and run the command
  again.
- `The example config is not at '<path>'`: the wheel did not install completely. Move the `venv`
  folder away and run the command again with `-Wheel`.
- `'<config>' is not valid JSON`: fix the syntax in the config. A missing comma is the usual cause.
- `'taskdash <command>' failed with exit code <n>`: read the output above the message. It ends each
  `[!!]` line with the fix. See the entry for that command in this guide.
- `install_task.ps1 failed`: read the output above the message, and see "The installer refuses with
  -Apply". If the task was registered, it stays. Run the command again after the fix.

### The first run started by the installer fails

> `ERROR: First run of '<name>': LastTaskResult 0x...` or `ERROR: '<name>' was still running after 10 minutes.`

**Cause.** With `-StartNow -Apply`, the installer starts the task once and waits up to 10 minutes.
The code after `LastTaskResult` tells the reason. The task stays registered in every case.

**Fix.** Follow the text after the code. It matches the list in step 8 of [INSTALL.md](INSTALL.md).
If the run took more than 10 minutes, wait for it. Then run `taskdash status`.

### TaskDash finds no ArcGIS credentials

> No ArcGIS credentials resolved. Set arcgis.username/password in the config, or set arcgis.username_env/password_env to environment variable names.

**Cause.** `arcgis.profile` is empty. TaskDash then looks for a user name and a password in the
config or in environment variables, and found none.

**Fix.** Set `arcgis.profile` to `taskdash`, and store the sign-in as step 3 of
[INSTALL.md](INSTALL.md) describes. This is the safe option, because the password stays out of the
config.

### The config holds a plaintext password

> arcgis.password is stored in plaintext in the config. Store a profile once, as the account the scheduled task runs under: GIS(url, user, password, profile="name"), then set arcgis.profile to that name and delete arcgis.password. To accept the risk instead, set arcgis.allow_plaintext_password to true.

**Cause.** The config has an `arcgis.password` value, and no `arcgis.profile`. TaskDash refuses to
use a password that anyone with access to the file can read.

**Fix.**

1. Store a profile, as step 3 of [INSTALL.md](INSTALL.md) describes.
2. Set `arcgis.profile` to the profile name.
3. Delete `arcgis.password` from the config.

Only if you accept the risk, set `arcgis.allow_plaintext_password` to `true` instead.

## Collect and publish

These messages come from `taskdash collect`, which the hourly task runs as `collect --apply`. The
`setup` and `dashboard` commands can show some of them too.

If `collect --apply` cannot sign in or find the service, it still collects and checks alerts
before it exits with the error.

### The Task Scheduler log cannot be read

> Cannot read event log '`<log name>`': `<reason>`. Run this collector elevated, or confirm the channel exists on this host.

**Cause.** TaskDash reads run history from the Windows event log
`Microsoft-Windows-TaskScheduler/Operational`. The log does not exist, or the account that runs
TaskDash may not read it. `<reason>` is the error that Windows reported.

**Fix.**

1. Run the command as administrator. The task account must be in the local Administrators group.
2. If the reason says the log was not found, run
   `Get-WinEvent -ListLog Microsoft-Windows-TaskScheduler/Operational` in PowerShell. If that
   command also fails, the log is missing on this computer.

### The Task Scheduler log is turned off

> Event log '`<log name>`' is disabled, so run history cannot be collected. Enable it with: wevtutil sl "`<log name>`" /e:true (run elevated), then retry.

**Cause.** Windows often ships this log turned off. TaskDash cannot collect run history while it is
off. The run still publishes the task list and the overdue state, and it still sends alerts. It
records this message in `error_text` of the `CollectorRuns` row, then exits with code 1.

**Fix.** Open PowerShell as administrator and run `taskdash preflight --enable-event-log --apply`.
Step 5 of [INSTALL.md](INSTALL.md) lists the full steps. Run history starts when you turn the log on.

### A task could not be read

> WARNING: `<n>` scheduled task problem(s); the readable tasks are still collected: `<task>: <message>`

**Cause.** Task Scheduler refused to read one task definition. A common message is 0x80041321, "The
task image is corrupt or has been tampered with". Access denied gives the same warning. The run does
not fail. It lists every readable task. It leaves out a task it could not read at all. One
warning is different: if it says "monthly trigger details are blank", the task stays in the list.
Only the export of its XML failed, so its monthly schedule shows no day or month.

**Fix.** Open Task Scheduler and look at the named task. Re-create the task if it is corrupt. If the
message says access denied, give the task account read access to the task. The `CollectorRuns` row
holds the same names in `error_text`, and its exit status stays success.

### TaskDash cannot list any scheduled task

> Cannot list scheduled tasks: `<task>: <message>`. Check that the Task Scheduler service is running and that the task account can read the task folders, then run again.

**Cause.** Task Scheduler returned errors and no task at all. The service may be stopped, or the task
account may have no access to the task folders. The run fails because an empty list would publish an
empty inventory and send a false "recovered" e-mail.

**Fix.** Start the Task Scheduler service, or give the task account read access to the task folders.
Then run the collector again.

### Event records were skipped

> WARNING: `<n>` event record id(s) were skipped: the oldest record left in the Operational log is `<id>` but the last one read was `<id>`. The log wrapped or was cleared since TaskDash last read it. Raise the log size with the preflight event-log fix.

**Cause.** The Task Scheduler log is a ring. It overwrote old records between two runs.
The run history for those records is lost. This run is fine, and the exit code stays 0.
TaskDash compares the oldest record with the newest record the last published run read. It keeps
that id in `Logs\event_read_mark.json` (`event_read_mark_test.json` for `--target test`). It warns once for each gap. Without the file, which is the
case on the first run after an upgrade, it skips the check.

**Fix.** Raise the log size so it holds more than the longest gap between runs. Run preflight and
apply its event-log fix. The `CollectorRuns` row holds the same warning in `error_text`.

### Log folders were skipped

> WARNING: skipped `<n>` log folder(s) outside metrics.log_roots or reached through a link. To read them, add their parent folder to metrics.log_roots.

**Cause.** `metrics.log_roots` is set, and some task logs are outside every listed folder. A log that
is reached through a symbolic link or a junction is skipped too. The affected tasks get no log
metrics. The run itself is fine.

**Fix.** Add the parent folder of those logs to `metrics.log_roots`. If a link points to the logs,
list the link's target folder instead. See "Limit where TaskDash reads logs" in
[CONFIGURE.md](CONFIGURE.md).

### The alert state file was corrupt

> WARNING: `<path>\alert_state.json` was ignored: `<reason>`. TaskDash rebuilds it on this run, so alerts already sent may repeat once. The bad file was kept as `alert_state.json.corrupt-<stamp>`.

**Cause.** `alert_state.json` is not valid JSON, it does not hold a JSON object, or one of its
fields has the wrong type (`signature` and `digest_pending` must be lists of text, and
`problem_last_alert_utc` and `problem_first_seen_utc` must be objects). A disk full or
a crash while the file was written can cause this. TaskDash starts with an empty state and
carries on.

**Fix.** No fix is needed. The next run writes a new file. You may delete the `.corrupt-` copy
after you check it. If the warning returns every run, check the disk space and the folder
permissions of the Logs folder.

### PowerShell failed

> PowerShell collection failed (exit `<code>`): `<error text>`

**Cause.** The PowerShell command that reads the task list or the event log ended with a non-zero
exit code. `<error text>` is the first 500 characters of its error output.

**Fix.** Read the error text. If it says access is denied, run the command as administrator. TaskDash
starts PowerShell without a profile, so a profile script is not the cause.

### PowerShell runs in ConstrainedLanguage mode

> PowerShell is in ConstrainedLanguage mode (AppLocker or WDAC is on) and the command failed (exit `<code>`): `<error text>`
> Ask your administrator to add an allow rule for the TaskDash folder to the AppLocker or WDAC policy, or run the collector as an account that the policy exempts.

**Cause.** An AppLocker or WDAC policy limits PowerShell to ConstrainedLanguage mode. That mode
refuses .NET method calls on most types and `Add-Type`. TaskDash's own PowerShell commands avoid
both, so this message means a command still hit a limit, or the policy blocked something else.
`<error text>` names the refused step. TaskDash checks the mode only after a command fails, so a
healthy run starts no extra process.

**Fix.** Do one of these.

- Ask your administrator to add an allow rule for the TaskDash folder to the AppLocker or WDAC
  policy. PowerShell then runs the TaskDash commands in FullLanguage mode.
- Run the collector as an account that the policy exempts.

Then run `taskdash preflight`. The `language mode` line shows the mode the task account sees. See
[The language mode line warns](#the-language-mode-line-warns).

### PowerShell returned text that is not JSON

> PowerShell returned non-JSON output (`<detail>`): `<first 300 characters>`

**Cause.** PowerShell ran, but its output was not the JSON that TaskDash expects. The output after
the colon shows what it printed instead.

**Fix.** Read the printed text. TaskDash starts PowerShell without a profile, so a profile script
is not the cause. If you cannot find the cause, remove identifying values from the message, as the rule in
[Issues](../README.md#issues) says. Then send it to the TaskDash maintainer.

### An ArcGIS call failed after several tries

> `<label>` failed after `<number>` attempts: `<last error>`

**Cause.** TaskDash called ArcGIS and the call failed every time. It retries a network error, a
timeout, and the error codes 429, 498, 499, 500, 502, 503 and 504. It waits longer between tries.
`<label>` names the table and the action. `<last error>` is the error from the final try.

**Fix.** Read the last error.

- If it names a network or proxy problem, fix the connection. See "TaskDash cannot reach ArcGIS".
- If it names a sign-in or token problem, test the stored sign-in. Run `taskdash preflight`, and read
  its `credentials` line.
- If ArcGIS Online had an outage, wait. The next hourly run tries again.

### ArcGIS refused a write

> `<label>` `<adds, updates or deletes>` failed: `<ArcGIS error>` -- a row with this key already exists (unique index). The next run matches it and updates it instead; if this repeats, look for a key written twice.

or

> `<label>` `<adds, updates or deletes>` failed: `<ArcGIS error>` -- check field lengths and types against the table schema.

**Cause.** ArcGIS accepted the request but rejected a row. The first form means a row with the
same key is already stored. The second form covers every other row error. A value that does not
fit its field is one cause.

**Fix.**

1. In the first form, wait for the next run. It finds the stored row and updates it. If the message
   repeats, search the table in ArcGIS Online for the key in the error.
2. In the second form, run `taskdash collect --migrate`. It lists the fields that the service
   lacks. If it lists missing fields, run `taskdash collect --migrate --apply`.
3. Also look in the log for `WARNING: schema type mismatch`. That line names a field whose type in
   the service differs from the type that TaskDash writes. The migration never changes a field
   type, so correct that field by hand.

### The service is not in the config

> No arcgis.`<item_id or test_item_id>` in the config. A new install runs `taskdash setup --apply`, which creates the service and fills the id in. To add a service to an existing install, run --create-service, then paste the returned item id into the config.

**Cause.** The config key for the service is empty. `--target prod`, the default, reads
`arcgis.item_id`. `--target test` reads `arcgis.test_item_id`.

**Fix.** For a new install, run `taskdash setup --apply`, as step 6 of [INSTALL.md](INSTALL.md)
describes. To add a service to an existing install, run `taskdash collect --create-service --apply`
and paste the item ID it prints into the config.

### The service or view item cannot be found

> ArcGIS item `<id>` not found or not visible to this account.

> View item `<id>` not found or not visible to this account.

> Service item `<id>` not found or not visible to this account.

**Cause.** The item ID in the config does not match an item that the signed-in account may see. The
ID may be wrong, the item may be deleted, or the account may lack access.

- The first message is about the service in `arcgis.item_id` or `arcgis.test_item_id`.
- The second message is about the view in `arcgis.view_item_id` or `arcgis.test_view_item_id`.
- The third message comes from the `dashboard` command and from setup. It names the item that the
  dashboard reads. That item is the view, or the service if the config sets no view.

**Fix.** Open the item in ArcGIS Online and compare its ID with the config. Check that the account
in the stored sign-in owns the item or has access to it.

### A service table is missing

> Target service is missing table(s): `<table names>`. Run --create-service.

> Target service has no TaskRuns table. Run --migrate --apply first.

> Target service has no TaskMetrics or TaskInventory table. Run --migrate --apply first.

> Target service has no TaskInventory table. Run --migrate --apply first.

**Cause.** The hosted service does not hold all the tables that TaskDash writes.

**Fix.** Run `taskdash collect --migrate --apply`. It creates the missing tables and fields. The
`--migrate` flag alone shows what it would change.

### `--retire-host` refuses the name

> --retire-host needs the exact computer name, as it shows in the Host column. Leave out wildcards such as * and %. Run it again with one name.

> Cannot retire '`<name>`': it is this computer, and its next collector run would add the rows again. Stop the collector on that computer first, then run --retire-host from a computer that stays.

> Cannot retire '`<name>`': a collector under '`<name>`' ran within the last `<N>` min. Stop it first (step 1 of 'Retire a computer'), then run --retire-host again.

**Cause.** The first message means the name is empty or holds `*`, `%` or `?`. The second means the
name is the computer that runs the command. Its next collector run would publish its rows again. The
third means the other computer still publishes under that name. Without `--apply`, the same text
shows as a `WARNING` line.

**Fix.** For the first message, copy one name from the **Host** column of the dashboard. For the
second and third, stop and remove the collector task on that computer. Then run the command from a
computer that stays. For the third, wait two collector intervals after the last run. See "Retire a
computer" in the configuration guide.

If the output says `Nothing to retire`, the service holds no inventory rows under that name. Check
the spelling. The name must match the **Host** column, including the case.

### The service schema is out of date

> Schema drift -- table(s) are missing field(s) the current code writes: Run `--migrate --apply` to add them.

The real message lists each table and its missing fields between the colon and `Run`.

> Schema drift -- table(s) are missing the unique index a retried write relies on to avoid duplicating rows: Run `--migrate --apply` to add them.

The real message lists the tables and fields between the colon and `Run`.

**Cause.** An older TaskDash created the service. The new code writes a field or relies on an
index that the service does not have yet.

**Fix.** Run `taskdash collect --migrate --apply`, then run the failed command again.

While the schema is out of date, `collect --apply` writes nothing to the service. It still sends the
failed-task alert, and it exits 1. The log says `WARNING: refusing to write to AGOL`. The next
successful run writes the saved `CollectorRuns` row.

### A schema change failed

> `<table>`: add_to_definition failed creating the table: `<reply>`

> `<table>`: created but not found on re-query -- re-run --migrate --apply.

> `<table>`: add_to_definition failed adding field(s) `<fields>`: `<reply>`

> `<table>`: add_to_definition failed adding `<unique>` index on `<fields>`: `<reply>`

**Cause.** ArcGIS Online did not accept a change to the service while `--migrate --apply` ran. The
reply at the end of the message is what ArcGIS returned. For the second message, ArcGIS reported
success, but the new table did not appear in the next query.

**Fix.** Read the reply. Check that the stored sign-in belongs to the owner of the service. Then run
`taskdash collect --migrate --apply` again. The migration skips what already exists.

### A unique index cannot be added

> `<table>`: cannot add a unique index on `<fields>` -- duplicate key(s) exist, e.g. `<key>`. Resolve the duplicates first.

**Cause.** The table holds more than one row with the same key. A unique index cannot be built until the key
is unique.

**Fix.** In ArcGIS Online, open the table data, find the rows with that key, and delete the extra
rows. Then run `taskdash collect --migrate --apply` again.

### The service has a newer schema than TaskDash

> The service `<id>` has schema version `<number>`, newer than this TaskDash (`<version>`, schema `<number>`). Upgrade TaskDash.

**Cause.** A newer TaskDash migrated this service. The older program you ran does not know the
newer schema, so it refuses to change it.

**Fix.** Install the newer wheel, as step 1 of [INSTALL.md](INSTALL.md) describes. Do not lower the
version stamp by hand.

### The collector warns about a newer schema

> WARNING: the service `<id>` has schema `<number>`, newer than this TaskDash (`<version>`, schema `<number>`); publishing continues but new fields stay empty. Install the newer TaskDash to fill them.

**Cause.** A newer TaskDash migrated the service, and you then installed an older one, usually
after a rollback. Each `collect --apply` run logs this line once.

**Fix.** No fix is needed for the run. It publishes every field the older version knows. The fields
that only the newer release writes stay empty. To fill them, install the newer wheel again, as
[UPGRADE.md](UPGRADE.md) describes. Do not lower the version stamp by hand.

### The collector warns about the MetricChange table

> WARNING: the service has no MetricChange table, so the dashboard's Metric Change keeps reading all of TaskMetrics. Run `taskdash collect --migrate --apply`, then `taskdash dashboard` to switch it.

> WARNING: MetricChange write failed (`<error type>: <text>`); Metric Change may be out of date until the next run.

**Cause.** The first line means the service predates the `MetricChange` table. Each `collect --apply`
run logs it once, and publishes everything else as usual. The second line means the run could not
write the table. The table holds values worked out from `TaskMetrics`, so a failed write never fails
the run.

**Fix.** For the first line, run `taskdash collect --migrate --apply`. Then run `taskdash collect
--apply` once, so the collector fills the table. Then run `taskdash dashboard --target prod --apply`.
The dashboard command prints which source Metric Change reads. For the second line, read the error
text. The next run sends the metric files again and tries the write again. If the warning repeats, check the sign-in as "ArcGIS refused a write"
describes.

A view you made before this table existed does not gain the table, and the dashboard that reads it
keeps the old source. To change that, recreate the view, as "Rebuild or move the ArcGIS items"
describes.

### The version stamp could not be saved

> Could not stamp the version on service `<id>`. Re-run --migrate --apply.

**Cause.** The migration finished, but ArcGIS did not save the TaskDash version in the item.

**Fix.** Run `taskdash collect --migrate --apply` again. If it fails again, check that the stored
sign-in belongs to the owner of the service.

### A service with that name already exists

> A feature service named '`<name>`' already exists (`<id>`). Delete it or choose another name with --service-name.

**Cause.** `--create-service --apply` found a service with the same name. TaskDash never reuses an
existing service.

**Fix.** Delete the extra service in ArcGIS Online, or pass another name, such as
`--service-name <new name>`.

### Public sharing is refused

> TaskDash never shares an item with everyone (public). Share it with the organization or named groups instead; there is no override flag.

**Cause.** A sharing call asked for public access. No TaskDash command does this, and no flag turns
the check off.

**Fix.** If you see this message, remove identifying values from it, as the rule in
[Issues](../README.md#issues) says. Then send it to the TaskDash maintainer. Do not share the item
with everyone.

### An item is not private

> Item `<id>` should be private but its access is '`<access>`'. Set its sharing to Owner in ArcGIS Online, then re-run.

**Cause.** TaskDash expected a private item, and the item has a wider sharing level. The message
shows the level. `taskdash preflight` fails for the editable service at any level except Owner. A
`collect` run only warns at `org` or `shared`, and it refuses to continue at `public`.

**Fix.** In ArcGIS Online, open the item and set its sharing to **Owner**. Then run the command
again.

### An item was not shared with the group

> Item `<id>` was not shared with group(s) `<group ids>`. Share it with them by hand, or add the collector account to those groups and re-run.

**Cause.** ArcGIS did not add the item to the group. The signed-in account may not be a member of
the group.

**Fix.** Add the account to the group, then run the command again. Or share the item with the group
in ArcGIS Online.

### The service is shared with everyone

> The service `<id>` is shared with everyone. Set its sharing to Owner in ArcGIS Online, then re-run. Share only the read-only view.

**Cause.** The service is public. A view cannot hide data that the service itself exposes.

**Fix.** In ArcGIS Online, set the sharing of the service to **Owner**. Share only the read-only
view, with the organization or with named groups.

While the service is public, `collect --apply` writes nothing to it. It still sends the
failed-task alert, and it exits 1.

### The view or dashboard is shared with everyone

> The item `<id>` (arcgis.view_item_id) is shared with everyone. Set its sharing to your organization or to the viewers group in ArcGIS Online, then re-run.

**Cause.** The view or the dashboard in the config is public. The key in the message,
`arcgis.view_item_id`, `arcgis.test_view_item_id` or `arcgis.dashboard_id`, names the item. A public
view lets anyone read your task data.

**Fix.** In ArcGIS Online, set the sharing of the item to your organization or to the viewers group.
Sharing with the organization or with groups is allowed.

While the item is public, `collect --apply` writes nothing to it. It still sends the failed-task
alert, and it exits 1. If TaskDash cannot find the item, it warns and skips this check. Correct the
ID in the config, or share the item with the collector account.

### A view update failed

> View `<what>`: update_definition failed: `<reply>`

**Cause.** ArcGIS did not accept a change to the view, such as hiding fields. `<reply>` is what
ArcGIS returned.

**Fix.** Read the reply. Check that the stored sign-in belongs to the owner of the service. Then run
the command again.

### A view already exists

> arcgis.`<view_item_id or test_view_item_id>` is already `<id>`; the view exists. Remove the key only if you mean to create another view.

**Cause.** You ran `taskdash collect --create-view`, and the config already holds a view ID.

**Fix.** Do not create another view unless you need one. If you do, clear the key in the config and
run the command again.

### A view with that name exists, and it may be half built

> A feature service named '`<name>`' already exists (`<id>`). It may be half-configured by an earlier failed run, with no fields hidden. Delete it in ArcGIS Online and re-run; do not set arcgis.`<view_item_id or test_view_item_id>` to it.

**Cause.** An earlier `--create-view` or `setup` run may have stopped after it made the view and
before it hid the fields. That view could show data that the privacy switches hide.

**Fix.** Delete that view in ArcGIS Online. Run the command again. Do not put its ID in the config.

### The new view has no tables

> The new view `<id>` has no table(s) `<names>`. Run --migrate --apply on the service first, delete the view, and retry.

**Cause.** The service lacks tables, so the view built from it lacks them too. TaskDash reads the
new view several times over about two minutes before it stops.

**Fix.**

1. Run `taskdash collect --migrate --apply`.
2. Delete the new view in ArcGIS Online.
3. Run the command that creates the view again.

### Anonymous users may read the data

> Anonymous users may read TaskDash data, or the check could not prove they cannot:

One line follows for each table. After the lines, the message adds the fix.

**Cause.** TaskDash queries each table without signing in. It does this after
`--migrate --apply`, after `--create-view --apply` and after `setup --apply`. Each line in the
message names a table whose answer was not a clear refusal. The table answered with rows, or the
answer was inconclusive. TaskDash does not treat a bare HTTP 401 or 403 as proof, because a proxy
can send those.

**Fix.**

1. In ArcGIS Online, set the sharing of the service to **Owner**.
2. Set the sharing of the view to your organization or to named groups.
3. Run `taskdash collect --check-anonymous`. It exits with 0 when every table refuses.
4. If a line says the check was inconclusive, open that query URL in a private browser window. It
   must ask you to sign in.

### A collection from the wrong host is refused

> Refusing to publish: host_name_override is '`<name>`' but this machine is '`<computer name>`'. Collected data would describe the wrong host. Run this on '`<name>`', or pass --allow-host-mismatch if you are certain.

**Cause.** `host_name_override` in the config differs from the name of this computer. With
`--apply`, TaskDash would store this computer's tasks under the other name.

**Fix.** If you installed TaskDash by [INSTALL.md](INSTALL.md), set `host_name_override` to `""`, so
TaskDash uses the name of this computer. Or run the command on the computer that the config names.
Pass `--allow-host-mismatch` only if you are sure.

### A previous host name is still in use

> WARNING: host_previous_names lists '`<name>`', but a collector under that name ran within the last `<number>` min, so its rows were not moved. Another computer still publishes under that name. Stop its collector first, or remove '`<name>`' from host_previous_names.

**Cause.** `host_previous_names` holds a name that a live collector still uses. A replacement
computer often takes the old name. TaskDash found a `CollectorRuns` row for that name newer than
2 x `scheduler.collector_interval_minutes`. It left that computer's rows alone, so the two computers
do not take them from each other every hour.

**Fix.** If the old computer is gone, stop and remove its task, wait that long, then run again. If
another computer uses the name, remove it from `host_previous_names`.

After a rename, the same warning means the old name ran less than that long ago. Keep the key, wait
until 2 x `scheduler.collector_interval_minutes` have passed since the old name's last run, then run
`collect --apply` again. Remove the key only after a run logs `adopted N row(s)` with no warning.

### The inventory update would delete most stored tasks

> `<label>`: refusing to delete `<number>` of `<number>` stored task(s) for host '`<name>`' -- a drop of more than half usually means collection ran on the wrong host. Re-run on the right machine, or pass --force if the shrink is real.

**Cause.** More than half of the tasks stored for this host are missing from this collection.
That usually means the command ran on another computer, or ran as an account that sees only some
tasks.

**Fix.**

1. Check the computer name and the account that ran the command. The task account must be in the
   local Administrators group.
2. If the drop is real, for example after you removed many tasks, run
   `taskdash collect --apply --force`.

### Run collection is stalled

> Run collection is stalled: the oldest events past the watermark filled the whole budget and none belongs to a monitored task. Raise history.max_events_per_run, or run once with --full-history to jump to the current log window.

**Cause.** TaskDash reads run events in order from a saved position. The next block of events held
no run of a monitored task, so the position could not move. Without this stop, every run would read
the same events.

**Fix.** Do one of these:

1. Raise `history.max_events_per_run` in the config.
2. Run `taskdash collect --apply --full-history` once. It ignores the saved position and reads the
   event-log window, up to `history.first_run_max_events` events.

### PowerShell did not finish

> PowerShell did not finish within `<n>` s. Raise powershell_timeout_seconds in the config, or check that the computer is not overloaded.

> Reading up to `<n>` events from the Operational log did not finish in `<n>` s. Raise powershell_timeout_seconds, or lower history.first_run_max_events.

**Cause.** A PowerShell call took longer than `powershell_timeout_seconds` (default 180). The second
message is the event-log read. On a first run it asks for up to `history.first_run_max_events`
events (default 30000), which a busy log cannot always return in time. For a later run, the
message names `history.max_events_per_run` instead.

The run still publishes the task list and sends the alerts, then exits 1. It reads no events, so the
watermark does not move.

**Fix.** Do one of these:

1. Raise `powershell_timeout_seconds` in the config.
2. Lower `history.first_run_max_events`.
3. Wait. You need to do nothing else. A read that times out saves half of its size in
   `Logs\first_run_budget.json`. Each later run halves it again, down to 250 events, until a read
   gets through. From 30000, that takes at most 8 runs. A later run (not the first) halves
   `history.max_events_per_run` the same way. It loses no history, because it reads the oldest
   events first.

A first-run read that gets through keeps only the newest events in the window, so older runs are
not backfilled. TaskDash then deletes `first_run_budget.json`. To read the full size again
sooner, delete the file, or change the setting.

A run with `--target test` uses `first_run_budget_test.json` and `event_read_mark_test.json`
instead, so a test run never changes the files of the production run. The production names stay
as they are.

### The metric upload resends every row

After a successful `--apply` run, TaskDash saves `Logs\metrics_state.json` (`metrics_state_test.json`
for `--target test`). The file holds a short digest of the metric rows of each log file. The next
run sends only the rows of log files whose digest changed. It also reads only the stored metric
rows that those files can hold, so the read stays small as the table grows.

Without the file, the run sends every row, as it did before, and then saves it. These changes make
one run send every row again:

- You change the `metrics` or `privacy` settings, the service, or the TaskDash version.
- You run `--refresh-metrics --apply`.
- You delete the file.

If the log shows `WARNING: ignored metrics_state.json`, the file was damaged. TaskDash sends every
row and replaces the file. A dry run never writes the file.

If you delete metric rows from the table by hand, delete the file too. Otherwise TaskDash does not
send the rows of a log file that has not changed.

## Alert e-mail

### The alert e-mail failed

> Alert e-mail failed: `<reason>`. The data was published, but nobody was told about the problem. Check smtp.host, smtp.port, smtp.from and smtp.user in the config and the stored SMTP password, then run `taskdash test-alert --apply` to prove the mail path. The next run tries again.

**Cause.** An alert was due, and TaskDash could not send it. The mail server refused or did not
answer, the stored SMTP password is wrong or missing, or alerting is on but there are no usable mail
settings. The run still publishes everything first. It then saves `Alert e-mail failed: <reason>` in
`error_text` of the `CollectorRuns` row, sends the failure ping to the health check, and exits with
code 1. The row shows `alert_sent` as 0 and an exit status of failed.

**Fix.** Do these steps:

1. Run `taskdash test-alert`. It prints the recipients and the server, and it sends nothing.
2. Run `taskdash test-alert --apply`. It sends one test message and prints the reason if it fails.
3. Fix the `smtp` settings or the stored password named in the reason. The mail password steps are
   in [CONFIGURE.md](CONFIGURE.md). To store a new password, see [ROTATE.md](ROTATE.md).
4. Wait for the next hourly run. TaskDash keeps the alert state until a send works, so it sends the
   same alert again. Nothing is lost.

### The test alert has no recipient

> alerting.to is empty, so the test message has no recipient. Add at least one address to alerting.to in the config.

**Cause.** `taskdash test-alert` sends to the addresses in `alerting.to`, and the list is empty.

**Fix.** Add at least one address to `alerting.to`, then run the command again.

### The test alert has no mail settings

> There are no usable mail settings, so the test message cannot be sent. Fix the smtp block in the config (host, from, user and the stored password); the WARNING above says which part is missing.

**Cause.** The `smtp` object lacks `smtp.host` or `smtp.from`, or the password for `smtp.user` is
not in the keyring of this Windows account.

**Fix.** Complete the `smtp` object, and store the password as the account that runs the task. The
steps are in [CONFIGURE.md](CONFIGURE.md). Run the command again.

### The legacy mail file warning

> WARNING: alerting.mail_config_path is deprecated and TLS is not verified for it, so the mail server's certificate is never checked. Move to the smtp block (smtp.host, smtp.from, smtp.user; store the password with 'python -m keyring set taskdash-smtp <smtp.user>'), then clear alerting.mail_config_path.

> WARNING: the legacy mail file holds a login but turns TLS off, so the password crosses the network in plain text. Move to the smtp block with smtp.starttls set to true.

**Cause.** `alerting.mail_config_path` is set and the `smtp` object is empty. TaskDash still sends
alerts through the old file. It does not check the mail server's certificate for that file. The
collector logs the first warning once per run, and `taskdash preflight` shows it on the `smtp` line.
The second warning appears when the file also holds a login and turns TLS off.

**Fix.** Fill the `smtp` object, store the password in the keyring, and clear
`alerting.mail_config_path`. The steps are in [CONFIGURE.md](CONFIGURE.md). Run `taskdash preflight`
and `taskdash test-alert --apply` to prove the new path. Alerts keep working until you move.

## Setup

These messages come from `taskdash setup`. Setup checks each name before it writes anything, so
the first five messages leave ArcGIS Online unchanged. For the steps that follow a failure after
setup began to write, see step 6 of [INSTALL.md](INSTALL.md).

### The config already holds an install

> The config already sets `<keys>`, so an install exists. Setup is for a new install. To keep this install, maintain it with 'taskdash collect --migrate --apply' and 'taskdash dashboard --target prod' (folder install: python taskdash_publish.py --migrate --apply and python taskdash_dashboard.py). To start again, delete the ArcGIS items and empty those keys: see 'The config already holds an install' in docs/TROUBLESHOOTING.md.

**Cause.** One or more of `arcgis.item_id`, `arcgis.view_item_id` and `arcgis.dashboard_id` is
not empty. Setup does not change an install that already exists.

**Fix.** To keep the install, maintain it with `taskdash collect --migrate --apply` and
`taskdash dashboard --target prod`. To start again, do these steps:

1. Delete the ArcGIS items, as Undo step 2 of [INSTALL.md](INSTALL.md) describes.
2. Empty the three keys in the config.
3. Run `taskdash setup --apply` again.

### The folder name is empty

> --folder must be a folder name. Pass e.g. --folder TaskDash.

**Cause.** The `--folder` value was empty or held only slashes.

**Fix.** Pass a folder name, such as `taskdash setup --folder TaskDash`.

### The folder already exists

> The folder '`<name>`' already exists for this account. Choose another with --folder, or delete the empty folder in ArcGIS Online.

**Cause.** The signed-in account already has a folder with this name. Setup does not reuse it.

**Fix.** Pass another name with `--folder`. Or delete the empty folder in ArcGIS Online.

### An item with the planned title already exists

> `<item type>` '`<title>`' already exists (`<id>`). Setup never reuses an item: pass --title-prefix to pick other titles, or delete that item in ArcGIS Online.

**Cause.** The account already owns an item with one of the planned titles. The item type is
`Feature Service` or `Dashboard`.

**Fix.** Pass `--title-prefix <text>` to put text before every title. Or delete the item in
ArcGIS Online, if nothing uses it.

### Another account holds the service name

> Another account in this organization already has a hosted service named '`<title>`'. Pass --title-prefix to pick other names.

**Cause.** Hosted service names are unique across the whole organization. Another account owns a
service or view with one of the planned names. Setup checks this before it creates anything.

**Fix.** Pass `--title-prefix <text>` to put text before every title. If `taskdash demo --apply`
shows this message, another account already ran the demo. Ask that person to delete the `DEMO_`
items, or run the demo from that account.

### The viewers group already exists

> A group titled '`<title>`' already exists (`<id>`). Choose another with --group-title, or delete the group.

**Cause.** A group with the planned title already exists. Setup does not reuse it.

**Fix.** Pass another title with `--group-title`. Or delete the group in ArcGIS Online.

### The group could not be created

> ArcGIS did not create the group '`<title>`'. Check that the account may create groups, then re-run; delete anything listed above first.

**Cause.** ArcGIS returned no group. The account may lack the privilege to create groups.

**Fix.**

1. In ArcGIS Online, delete the items that setup listed above this message. Use the order of Undo
   step 2 in [INSTALL.md](INSTALL.md).
2. Copy the newest `TaskDash.config.json.bak_setup_*` file over `TaskDash.config.json`.
3. Ask your ArcGIS administrator to let the account create groups.
4. Run `taskdash setup --apply` again.

### The new dashboard is not visible

> The new dashboard `<id>` is not visible to this account. Find it in ArcGIS Online, move it into '`<folder>`' and share it with the group.

**Cause.** Setup created the dashboard but could not read it back. The ID is already in the config.

**Fix.**

1. Find the dashboard in ArcGIS Online.
2. Move it into the named folder, and share it with the viewers group that setup created.
3. Run `taskdash collect --check-anonymous`. Setup stopped before its own anonymous check.

Do not run setup again, because the config now holds the install.

### The dashboard could not be moved

> Could not move dashboard `<id>` into '`<folder>`': `<reply>`

**Cause.** ArcGIS refused to move the dashboard into the folder.

**Fix.**

1. In ArcGIS Online, move the dashboard into the folder by hand.
2. Share the dashboard with the viewers group that setup created.
3. Run `taskdash collect --check-anonymous`. Setup stopped before its own anonymous check.

The ID is already in the config, so do not run setup again.

### Delete protection could not be turned on

> Could not turn on delete protection for item `<id>`. Turn it on by hand (Item > Settings > Delete protection), or ask an administrator to.

**Cause.** Setup turns on delete protection for the service, the view and the dashboard as its last
step. ArcGIS refused the change for the named item. The items already exist and are shared correctly.

**Fix.**

1. In ArcGIS Online, open the named item.
2. Select **Settings**, and turn on **Delete protection** for it.
3. Repeat for any of the three items that the setup output does not mark `(delete protection on)`.

The config already holds the install, so do not run setup again.

### Setup ends with exit code 0

Exit code 0 is not an error. Setup gives 0 when it finished or when a dry run printed its plan. A
message from this guide gives exit code 1.

## Remove

These messages come from `taskdash remove`. Both stop the command before it signs in, so they leave
ArcGIS Online unchanged. The command ends with exit code 1 for a message, and also when `--apply`
skipped an item. Without `--apply` it ends with exit code 0.

### There is no setup manifest

> There is no setup manifest at `<path>`, so there is nothing to remove. Setup saves taskdash_setup_manifest.json next to the config. For an install made before version 1.2, delete the items by hand: see 'Undo the install' in docs/INSTALL.md.

**Cause.** The manifest is not beside the config. Setup before version 1.2 did not save one. You may
also have run the command with a `--config` path from another folder.

**Fix.**

1. If you moved the config, pass `--config` with the path of the config that sits beside the manifest.
2. Otherwise, delete the items by hand, as Undo step 2 of [INSTALL.md](INSTALL.md) describes.

### The setup manifest cannot be read

> The setup manifest `<path>` cannot be read: `<error type>`. Restore it from a backup, or delete the items by hand: see 'Undo the install' in docs/INSTALL.md.

**Cause.** The file is not valid JSON, or it lacks the `items` list that setup writes.

**Fix.** Restore `taskdash_setup_manifest.json` from a backup. If you have none, delete the items by
hand, as Undo step 2 of [INSTALL.md](INSTALL.md) describes. Do not edit the file to add items.

### Remove skipped an item

When `--apply` skips an item, the command prints `Skipped:` and the reason, and then exits with code
1. The other items are still deleted. The reason is one of these:

- `owned by <name>, not <name>`. Sign in as the owner, or ask that person or an administrator to
  delete the item.
- `could not turn off delete protection`. In ArcGIS Online, open the item, select **Settings** and
  turn off **Delete protection**. Or ask an administrator to.
- `it is now a <type> titled '<title>', not the <type> '<title>' that setup created`. The ID in the
  manifest now names another item, or someone renamed the item. Check it in ArcGIS Online. Delete it
  by hand if it is the item that setup created.
- `its view still exists`. The service stays while its view stays, so the view does not break. Fix
  the reason shown for the view, then run the command again.
- `it has a view that is not in the manifest: <id>`. ArcGIS deletes a hosted layer's views with it.
  A view made later, for example with `collect --create-view`, would go too. Delete that view by
  hand first, or keep the service.
- `its views cannot be listed`. TaskDash could not check for other views, so it kept the service.
  Run the command again later.
- `delete protection is on; turn it off in the group's settings`. Open the group in ArcGIS Online,
  turn off **Delete protection** in its settings, then run the command again.
- `delete protection is off`, after an error. The delete failed and TaskDash could not turn
  protection back on. Turn **Delete protection** on again in the item's **Settings**.
- `the folder is not empty`. The folder holds items that setup did not create, or items that were
  skipped above. Move or delete them, or leave the folder.
- `a different folder has that name`. The folder is not the one setup created. Someone made it
  again, or it belongs to the account that you are signed in as. Delete it by hand if you want it gone.
- An error name and text from ArcGIS, such as a failed delete. Fix what the text says.

After you fix the reason, run `taskdash remove --apply` again. It reports the deleted items as
`Already gone`.

## Sign in

These messages come from `taskdash signin`. It never prints the password. Without `--apply` it saves
nothing, so a failure leaves the stored sign-in as it was. A `[!!]` line gives exit code 1. The two
messages below stop the command before it asks for the password.

### arcgis.profile is empty

> arcgis.profile is empty, so there is no name to store the sign-in under. Set arcgis.profile to taskdash in the config, then run again.

**Cause.** The config has no `arcgis.profile` value, and the command needs a name for the profile.

**Fix.** Set `arcgis.profile` to `taskdash` in the config, then run `taskdash signin` again.

### There is no URL or user name

> There is no ArcGIS URL or no ArcGIS user name to sign in with. Type them at the prompts, or set arcgis.url and arcgis.username in the config.

**Cause.** You pressed Enter at a prompt, and the config has no value for it.

**Fix.** Run the command again and type the value. If `arcgis.url` is empty, press Enter at the URL
prompt to use `https://www.arcgis.com`. After the sign-in, the command prints your organization's own
URL, such as `https://<org>.maps.arcgis.com`, and the line to put in the config. Run it with
`--apply` to write only `arcgis.url`, with a backup of the config. TaskDash signs in with the URL in
the config.

### The sign-in command fails

The command prints one line for each check. Each `[!!]` line ends with the fix.

- `[!!] sign in`. ArcGIS refused the user name or the password, or the account uses single sign-on
  (SAML) or multifactor authentication. Check the user name and the password. TaskDash supports only a
  built-in ArcGIS account without multifactor authentication today. Ask your ArcGIS administrator
  for one, and run the command again.
- `[!!] account` with `users.me is empty`. ArcGIS accepted the sign-in but returned no account. The
  usual cause is a wrong user name or an account of another kind. Use a built-in account.
- `[!!] privileges`. The line names each missing privilege. Ask an organization administrator to add
  them to the account's role. A Creator user type has them.
- `[!!] profile` with `could not save the profile`. The Windows account could not write to Credential
  Manager. Run the command in a normal window of the account that will use the profile.
- `[!!] profile` with `the profile does not sign in`. The save worked, but the saved profile did not
  sign in. Run `taskdash signin --apply` again.

Run the same command again after you fix the cause. The profile belongs to the Windows account that
ran the command, so run `taskdash signin --apply` once as you and once as the task account. To store
it for the task account without `runas`, use `taskdash signin --for-task-account --apply`, as the
next section describes.

### The hand-off file is not written

With `--for-task-account --apply`, the command seals the passwords into `taskdash_handoff.bin` in
the TaskDash folder. Only the task account, SYSTEM and Administrators can open the file. The command
never prints a password.

- `[!!] hand-off` with `could not write taskdash_handoff.bin`. The command ends with the error name.
  `ModuleNotFoundError` means pywin32 is missing; install it with `python -m pip install pywin32`.
  An error from Windows about the account means the name is wrong. Pass `--task-user DOMAIN\name`,
  written as the task shows it. TaskDash removes a file it could not restrict, so none is left behind.
- The line `There is no collector task named '<name>' to read the account from` means the collector
  task is not registered yet. Pass `--task-user DOMAIN\name`.

If the hand-off keeps failing, store the sign-in with `runas`, as step 3 of [INSTALL.md](INSTALL.md)
describes under "If the hand-off fails".

### The hand-off file could not be opened

> The hand-off file could not be opened. It is kept. Make it again on this computer with taskdash signin --for-task-account --apply, or store the sign-in with runas as step 3 of docs/INSTALL.md describes. Detail: `<error name>`

**Cause.** The collector run found `taskdash_handoff.bin`, and Windows could not decrypt it. The
file was made on another computer, or it is damaged. The run fails like a sign-in failure.

**Fix.** On this computer, run `taskdash signin --for-task-account --apply` again. It replaces the
file. TaskDash keeps the old file until you do.

### The hand-off sign-in was kept

> The hand-off sign-in was kept, not deleted, because storing or checking it failed. Fix the cause, then run the collector again. If the ArcGIS password is wrong, run taskdash signin --for-task-account --apply again. Detail: `<text>`

**Cause.** The collector run, as the task account, opened the file but could not finish. The ArcGIS
password may be wrong, or the account may be blocked. Windows may have refused to save the profile or
the mail password. The `Detail` text names the step. When it names the keyring package, the `smtp`
extra is not installed. The run fails like a sign-in failure, and the file stays.

**Fix.** Read `Detail`, and fix that cause. If the password was wrong, run `taskdash signin
--for-task-account --apply` again with the right one. It replaces the file. Then start the collector
task. The next run stores the sign-in, logs `Stored the sign-in`, and deletes the file.

### The hand-off file stays after a run

Three log lines explain this. All start with `WARNING`.

- `taskdash_handoff.bin is for <account>, so this run left it alone`. An account other than the
  one in the file ran TaskDash, for example you ran `taskdash collect` by hand. Nothing is wrong. The
  task account stores the sign-in on its own next run.
- `taskdash_handoff.bin is for another account, which this account cannot read, so this run left it
  alone`. The file allows only the task account, SYSTEM and Administrators. A run in a window that is
  not elevated, or by another account, cannot read it. Nothing is wrong. If the task account itself
  logs this line, the file names a different account. Run `taskdash signin --for-task-account --apply`
  again with the right `--task-user`.
- `stored the sign-in, but could not delete taskdash_handoff.bin`. The sign-in works. Delete the file
  by hand. It holds the passwords sealed, not in plain text.

## Preflight

### Preflight ends with exit code 1

Preflight prints no error message for this exit code. The last line counts the problems and the
warnings, such as `Preflight: 3 problem(s), 0 warning(s).` The exit
code is:

- 0 when the count of problems is 0.
- 1 when the count of problems is 1 or more.

A warning line also starts with `[!!]`, but a warning alone does not change the exit code.

With `--enable-event-log`, the exit code is 1 in these cases:

- `wevtutil` could not start. The output starts with `Could not run wevtutil`.
- With `--apply` only: `wevtutil` failed. The output starts with `wevtutil sl failed`.
- With `--apply` only: the log is still not enabled, or its size is not the wanted size.

In every other case it is 0.

### A setting has no effect

If a config key seems ignored, run `taskdash preflight --config-diff`. Under "Unknown keys", look for
your key. A misspelled name is the usual cause. The command exits 0 unless it cannot read the config.
Then it prints `Cannot read the config` or `Config not found`. Fix the file, or pass `--config`.

Step 5 of [INSTALL.md](INSTALL.md) explains each preflight line.

### The dashboard line warns

The line starts with `WARNING: dashboard`. It is a warning, so preflight still exits 0. Preflight
reads the dashboard item that `arcgis.dashboard_id` names. It never changes the item.

- "not found or not visible": the ID is wrong, someone deleted the dashboard, or the signed-in
  account cannot read it. Fix `arcgis.dashboard_id`, or rebuild the dashboard as the runbook below
  describes.
- "shared with everyone (public)": TaskDash never shares an item with everyone. In ArcGIS Online,
  open the dashboard, select **Share**, and choose your viewers group or organization.
- "do not read the view": the dashboard still reads the service, or an old view. Run
  `taskdash dashboard --target prod --apply` to rebuild it from the view in `arcgis.view_item_id`.

The check skips the data-source test when the config names no view.

### The delete protection line warns

The line starts with `WARNING: delete protection is off`. It names the service, the view or both.
Preflight changes nothing. In ArcGIS Online, open the named item, select **Settings**, and turn on
**Delete protection**. A new install gets protection from `taskdash setup`.

### The host name line warns

The line starts with `WARNING: host_name_override is`. It is a warning, so preflight still exits 0.
`collect --apply` refuses to run in this state, because it would file this computer's tasks under
the wrong name. Preflight changes nothing.

- If you copied the config from another computer, clear `host_name_override`. TaskDash then uses
  this computer's name.
- If you renamed this computer, set `host_name_override` to the new name. Add the old name to
  `host_previous_names` for one run. See "Rename the computer" in [CONFIGURE.md](CONFIGURE.md).

### The base python line warns

The line starts with `WARNING: the base Python is installed for one user`. It is a warning, so
preflight still exits 0. The base Python is the one that made the virtual environment. When its folder
is under `C:\Users`, it belongs to one user, and the task account usually cannot run it. Preflight
changes nothing.

1. Install Python 3.11 for all users. See the Python requirement in [INSTALL.md](INSTALL.md).
2. Run `py -0p`. The path for 3.11 must be under `C:\Program Files`.
3. Build the virtual environment again with that Python, as step 1 of [INSTALL.md](INSTALL.md) shows.
4. Run `taskdash preflight`. The `base python` line must be `[ok]`.

### The language mode line warns

The line starts with `WARNING: PowerShell runs in ConstrainedLanguage mode`. It is a warning, so
preflight still exits 0. AppLocker or WDAC puts PowerShell in ConstrainedLanguage mode, and the
mode limits what a script may call. Preflight changes nothing.

TaskDash still collects in this mode. It reads event fields by position, because the event method
that returns XML is refused. It also writes each PowerShell result to a temporary file in the
TaskDash folder, reads it as UTF-8 and removes it, because the console encoding cannot be set.
The temporary file is named `.taskdash_ps_` followed by random characters. You may delete a leftover
one by hand.

1. If collection works, you do not need to act. The line only tells you which path TaskDash takes.
2. If collection fails, read [PowerShell runs in ConstrainedLanguage mode](#powershell-runs-in-constrainedlanguage-mode).
3. To restore FullLanguage mode, ask your administrator to add an allow rule for the TaskDash
   folder to the AppLocker or WDAC policy, or to exempt the task account.

A line that starts with `WARNING: could not read the PowerShell language mode` means the check got no
answer. In PowerShell as the task account, run `$ExecutionContext.SessionState.LanguageMode`.
FullLanguage is the expected answer.

### The install folder line fails

The line starts with `[!!] install folder:` and names the folder. Users, Authenticated Users or
Everyone can write to it. The collector task runs as an administrator and loads code and config from
this folder. Anyone who can write there can make it run their code as an administrator.

Preflight reads the permissions with `icacls` and changes nothing. If you run the command on the line
from an elevated prompt, it removes inherited permissions and gives full control to SYSTEM and the
Administrators group only:

```powershell
icacls "C:\TaskDash" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /remove:g "*S-1-5-32-545" "*S-1-5-11" "*S-1-1-0"
```

If your own account is not an administrator, add it with another `/grant:r` entry, as in step 1 of
[INSTALL.md](INSTALL.md). Then run `icacls` on the folder. It must not list `Users`,
`Authenticated Users` or `Everyone` with `(F)`, `(M)` or `(W)`.

The check matches the names of the three groups in English and in the language of this computer,
and it matches their SIDs. If it cannot find SYSTEM or Administrators in the list, it cannot trust
its own result. The line then starts with `WARNING: could not recognise SYSTEM or Administrators`.
Run `icacls` on the folder yourself, as the line says, and look for the local names of the three groups.
If `icacls` fails, the line starts with `WARNING: could not read the permissions`. Run `icacls`
yourself, as the line says.

A line that starts with `WARNING: <folder> is owned by` names the owner of the folder. The owner can
always change the folder's permissions, even after you lock them. So an owner who is not an
administrator could put code in the folder that the task then runs as an administrator. The check
accepts SYSTEM, Administrators, TrustedInstaller and direct members of the Administrators group. If
the owner is an administrator through a group, for example a domain admins group, the folder is
safe. Otherwise, run this from an elevated prompt:

```powershell
icacls "C:\TaskDash" /setowner *S-1-5-32-544
```

If the line starts with `WARNING: could not read the owner`, run `(Get-Acl 'C:\TaskDash').Owner` in
PowerShell. The answer must be Administrators or an administrator account.

### The as-task check does not run

`taskdash preflight --as-task --apply` runs the preflight checks as the task account. It registers a
temporary task, runs it once, prints the output, and removes the task. Find your message below.

> pywin32 is not installed, so TaskDash cannot talk to Task Scheduler. Install it in the TaskDash environment with: python -m pip install pywin32

**Cause.** The command talks to Task Scheduler through the `pywin32` package. A normal install gets
it with the `arcgis` package. A hand-built environment may lack it.

**Fix.** Run `python -m pip install pywin32` with the Python of the TaskDash environment. Run the
command again.

> There is no collector task named '<name>' to read the account from.

**Cause.** The command copies the account and the logon type from the collector task. That task is
not registered yet, or `scheduler.collector_task_name` names another task.

**Fix.** Register the collector task first. Or add `--task-user DOMAIN\name` to name the account.
Without the collector task, the check uses the Password logon type.

> The collector task '<name>' uses a logon type or account that --as-task cannot copy

**Cause.** The check supports the Password and Interactive logon types, as `install_task.ps1` does.
The collector task uses another type, such as S4U, or runs as SYSTEM.

**Fix.** Register the collector task again with `install_task.ps1`. Or run `taskdash preflight` in a
window opened with `runas` as the task account.

> Preflight as <account> could not run

**Cause.** Task Scheduler refused a step. The line ends with the reason. Two reasons are common. The
window is not elevated, because registering a task for another account needs an administrator. Or
Windows rejected the password.

**Fix.** Open PowerShell as administrator, and run the command again. Type the right password at the
prompt. TaskDash never prints the password and never passes it on a command line.

> Preflight as <account> failed with 0x<code>

**Cause.** The temporary task ran, and preflight or Task Scheduler stopped it with that code. Code
`0x00000001` means preflight found a problem. Read its output above, then fix each `[!!]` line.
Code `0x00041303` means the task never started. The account probably cannot log on as a batch job.
Add it to the local Administrators group.

> Could not delete the temporary task

**Cause.** TaskDash could not remove the temporary task after the run. The command exits 1.

**Fix.** From an administrator window, run the `schtasks /delete` command that the line prints. The
task has no trigger, so it never runs by itself. TaskDash still tries to remove the output file.

> Could not delete the output file

**Cause.** TaskDash could not remove the output file in the log folder. A stopped task can hold it
for a moment. The command exits 1.

**Fix.** Wait a minute. From an administrator window, delete the file that the line names. The file
holds only the preflight output, and only the task account, you and administrators can read it.

> Preflight as <account> did not finish in 300 seconds

**Cause.** The preflight run waited for something, such as a network check. TaskDash stopped the
task and removed it.

**Fix.** Run `taskdash preflight --as-task --apply --offline` to skip the network checks. Or open a
window as the task account with `runas`, and run `taskdash preflight` there to see where it waits.

### A line shows [--] on a new install

A `[--]` line for `service`, `sharing`, `collector logon type` or `collector account` means that the
service id or the collector task is not set up yet. It is not a problem. Preflight exits 0 for it.
Run `taskdash setup --apply` to create the service. Run `install_task.ps1` to register the task.
Then run `taskdash preflight` again. Each line must become `[ok]`.

If the config sets `arcgis.view_item_id` or `arcgis.dashboard_id` but `arcgis.item_id` is empty,
the `service` line fails instead. The install is damaged, not new. Put the service item id back in
`arcgis.item_id`.

The `collector logon type` and `collector account` lines stay `[--]` only while `arcgis.item_id` is
empty. After setup, a missing task prints `WARNING: collector task '<name>' is not registered, or
this window cannot see it`. A normal window cannot see a task that runs as another account. Run
`taskdash preflight` in an administrator window. If the warning stays, register the task with
`install_task.ps1`, or set `scheduler.collector_task_name` to the real task name.

## Rebuild or move the ArcGIS items

Turn off delete protection on an item before you delete it. In ArcGIS Online, open the item, select
**Settings**, and turn off **Delete protection**. Setup turns it on for the service, the view and
the dashboard.

**Rebuild the dashboard.** The dashboard holds no data, so rebuilding is safe.

1. Run `taskdash dashboard --target prod` to see the plan.
2. Run `taskdash dashboard --target prod --apply`. It updates the dashboard that
   `arcgis.dashboard_id` names. It replaces hand edits, and it saves the old definition first.
3. Run `taskdash preflight`. The `dashboard` line must be `[ok]`.

If someone deleted the dashboard, empty `arcgis.dashboard_id` first. Then run
`taskdash dashboard --target prod --apply` and put the new ID in the config. Share the new
dashboard with the viewers group, and turn on its delete protection by hand.

**Recreate the view.** Do this when the view is gone or damaged. The service keeps the data. Do it
also when you want a view that holds the `MetricChange` table. A view never gains a table that you
add to the service later.

1. Empty `arcgis.view_item_id` in the config.
2. Run `taskdash collect --create-view --apply`. It prints the new item ID.
3. Put that ID in `arcgis.view_item_id`.
4. In ArcGIS Online, share the new view with the viewers group, and turn on its delete protection.
5. Run `taskdash dashboard --target prod --apply`, so the dashboard reads the new view.
6. Run `taskdash preflight`.

**Move the items to another account.** The collector account must own the service, or be an
administrator.

1. In ArcGIS Online, an administrator opens each item and selects **Settings**. Under **Change
   owner**, choose the new account. Do this for the service, the view and the dashboard.
2. Change the sign-in in the config to the new account, as "The stored sign-in does not work"
   describes.
3. Check that the view and the dashboard are still shared with the viewers group. If not, add the
   new account to the group and share them again.
4. Run `taskdash preflight`. Every `[ok]` line must stay `[ok]`, and the `delete protection` line
   must not warn.

## Dashboard

These messages come from `taskdash dashboard`. The command builds the dashboard definition and
checks it before it posts anything. Without `--apply`, it writes the definition to a local file,
prints its plan, and changes nothing in ArcGIS Online.

### The collector interval is not above 0

> scheduler.collector_interval_minutes must be a number above 0, got `<value>`. Set it to the minutes between collector runs, or remove the key for 60.

**Cause.** The dashboard uses this value to decide when a collector run is late. The value is 0 or
less.

**Fix.** Set `scheduler.collector_interval_minutes` to the minutes between runs, such as `60`. Or
remove the key.

### The heartbeat ignore time is not above 0

> scheduler.heartbeat_ignore_after_days must be a number above 0, got `<value>`. Set it to the days of silence after which the heartbeat leaves a computer out, or remove the key for 7.

**Cause.** The dashboard heartbeat leaves out a computer with no collector run for this many days.
The value is 0 or less, or it is not a number.

**Fix.** Set `scheduler.heartbeat_ignore_after_days` to a number of days, such as `7`. Or remove the
key. Then rebuild the dashboard.

### The service has no tables

> The `<target>` service has no `<table names>` table(s). Create them first: python taskdash_publish.py --migrate --target `<target>` --apply

**Cause.** The item that the dashboard reads lacks tables. That item is the view, or the service if
the config sets no view.

**Fix.** Run `taskdash collect --migrate --target <target> --apply`, then run the dashboard command
again. If the message repeats for a view, see "The new view has no tables".

### The dashboard item is wrong

> arcgis.dashboard_id `<id>` is not a Dashboard visible to this account. Correct the id in the config; this script never creates a second prod dashboard.

**Cause.** With `--target prod`, TaskDash updates the dashboard in `arcgis.dashboard_id`. That item
is not a dashboard, or the account cannot see it.

**Fix.** Open the dashboard in ArcGIS Online. Copy its ID into `arcgis.dashboard_id`.

### The test title matches the production dashboard

> Refusing: the test title '`<title>`' resolves to the prod dashboard `<id>`. Rename one of them or pass a different --title.

**Cause.** The command builds the test dashboard. That is the default only when `arcgis.test_item_id`
is set, or when you pass `--target test`. The title of the test dashboard matches the production
dashboard. `--apply` would overwrite the production dashboard.

**Fix.** Pass a different title, such as `taskdash dashboard --title <new title>`.

### More than one dashboard has the title

> `<number>` dashboards are titled '`<title>`' (`<ids>`). Delete the extras so --apply cannot update the wrong one.

**Cause.** Several dashboards that you own have the same title. TaskDash will not guess which one to
update.

**Fix.** Delete the extra dashboards in ArcGIS Online. Or pass another title with `--title`.

### The backup could not be read

> Could not read dashboard `<id>` for a backup; not overwriting it.

**Cause.** Before `--apply` replaces a dashboard, TaskDash saves its live definition. The read
returned nothing, so TaskDash left the dashboard unchanged.

**Fix.** Open the dashboard in ArcGIS Online and check that it opens. Then run the command again.

### The dashboard could not be published

> Dashboard publish failed: `<reply>`

**Cause.** ArcGIS rejected the request to create or update the dashboard. `<reply>` is its answer.

**Fix.** Read the reply. Check that the account may create items and may edit the dashboard. Then
run the command again.

### Offline and apply cannot be combined

> --offline never signs in, so it cannot --apply. Drop one of the two flags.

**Cause.** `--offline` builds and checks the definition without signing in. `--apply` needs a
sign-in.

**Fix.** Remove `--offline` or remove `--apply`.

### The dashboard has no service to read

> No arcgis.`<item_id or test_item_id>` in the config: the `<target>` dashboard needs its service.

**Cause.** The config key for the service is empty. `--target test` reads `arcgis.test_item_id`.
`--target prod` reads `arcgis.item_id`. Without `--target`, the command uses `test` only when
`arcgis.test_item_id` is set. Otherwise a check run uses `prod`. A run with `--apply` stops instead.

**Fix.** If you meant the production dashboard, add `--target prod`. Setup fills only
`arcgis.item_id`, so a test dashboard needs `arcgis.test_item_id` set to a test service.

### The dashboard command will not apply without a target

> The config has no test service, so --apply without --target would update the production dashboard. To do that, run `taskdash dashboard --target prod --apply`.

**Cause.** `--apply` came without `--target`, and `arcgis.test_item_id` is empty. TaskDash will not
replace the production dashboard unless you name it.

**Fix.** If you meant the production dashboard, run `taskdash dashboard --target prod --apply`.

### The widget templates are missing

> Widget templates unavailable: dashboard_templates.json is missing and source dashboard `<id>` no longer exists. Reinstall the TaskDash wheel to restore dashboard_templates.json.

```text
No widget templates: dashboard_templates.json is missing. Reinstall the TaskDash wheel to restore it, or pass --source-dashboard <id> of any dashboard whose widget structures to borrow.
```

> dashboard_templates.json is missing required template widget(s): `<names>`

**Cause.** TaskDash builds the dashboard from `dashboard_templates.json`. The wheel installs this
file under `share\taskdash` in the Python environment. The file is missing, or it lacks a widget
that the build needs.

**Fix.** Reinstall TaskDash from the full wheel. That restores the file. Do not edit it.

### The definition check refuses the dashboard

The `dashboard` command and `setup` check the definition before they post it. Each check below
protects viewers from a widget that Dashboards would show wrongly. The command stops and posts
nothing. If setup stops here, it has already created the items that it lists. Step 6 of
[INSTALL.md](INSTALL.md) explains how to recover. The first message is a check on the definition
as a whole. The table lists the others.

> Refusing to publish: definition still contains a hardcoded host filter.

**Cause.** The definition holds the name of this computer or of `host_name_override` in quotes, or
it holds `"server_name"` with its quotes. A dashboard with a fixed computer name would show only that
computer.

**Fix.** If you edited `dashboard_templates.json`, remove the name. Otherwise remove identifying
values from the message, as the rule in [Issues](../README.md#issues) says, and send it to the
TaskDash maintainer.

The messages below come from a defect in the widget templates or the build code. Your ArcGIS setup
does not cause them.

**Fix for every message in this table.** If you edited `dashboard_templates.json`, reinstall
TaskDash from the full wheel. That restores the shipped copy. Otherwise remove identifying values
from the message, as the rule in [Issues](../README.md#issues) says, and send it to the TaskDash
maintainer.

| Message | What it checks |
|---|---|
| Dashboard references `<field>`, which is not a field on `<table>`. Add it to the matching *_fields() list in taskdash_publish.py, or correct the widget. | A widget names a field that the table does not have. |
| A list template names a field, but its Arcade has no single 'attributes: {' block to carry it -- add the field to the expression. | A list widget cannot carry the field it names. |
| Refusing to publish: '`<widget>`' is categoryType 'features' but its dataset still declares groupByFields/statisticDefinitions -- a features chart must read raw rows, not a grouped/aggregated query. | A chart that plots rows also asks for grouped data. |
| Refusing to publish: '`<widget>`' series is bound to x=`<x>`/y=`<y>` but its dataset returns x=`<x>`/y=`<names>`. | A chart series does not match its data. |
| Refusing to publish: '`<widget>`' filters `<field>` as `<type>` but the schema says `<type>`. Dashboards renders a wrong filter type as 'Cannot access data'. | A filter uses the wrong field type. |
| '`<widget>`': valueFields and columnSettings disagree. | A table widget lists different columns in two places. |
| Refusing to publish: '`<widget>`' shows `<fields>`, which its Arcade data source does not return. Add them to the script's fields. | A table shows a column that its script does not return. |
| Refusing to publish: '`<widget>`' column `<name>` is a raw layer field but is neither a groupByField nor a statistic alias of this grouped dataset -- Dashboards can only return group-by fields and aggregated statistics from a grouped query. | A grouped table shows a raw column. |
| '`<widget>`' computes `<names>` but shows no column for them. Dashboards renders an undisplayed statistic as an extra unlabelled column with an error marker on every row. | A statistic has no column. |
| '`<widget>`' column `<name>` has no number format; an unrounded average renders with 10+ decimals (defect 4.10). | An average column has no number format. |
| Refusing to publish: `<source>` -> `<target>` targets a widget that does not exist in this view. | A click action points to a missing widget. |
| Refusing to publish: `<source>` -> `<target>` names dataset `<name>`, which '`<widget>`' does not have. | A click action points to a missing dataset. |
| Refusing to publish: `<source>` -> `<target>` is not a 5.x whereClause target; a legacy flat action filters nothing and shows no error. | A click action uses an old format. |
| Refusing to publish: `<source>` -> `<target>` crosses from `<table>` to `<table>` without a fieldMap; keyed on OBJECTID it matches nothing (ce0221b). | A click action joins two tables without a key. |
| Refusing to publish: `<source>` -> `<target>` maps `<number>` fields. Dashboards applies only the first (probed 2026-09-28); join on one key field instead. | A click action maps more than one field. |
| Refusing to publish: `<source>` -> `<target>` maps `<name>`, which the Arcade data source of '`<widget>`' does not return. | A click action maps a field that the script does not return. |
| Refusing to publish: '`<widget>`' tells the reader to select a row, but selecting does nothing (defect 4.1). | A caption asks for a click that has no action. |
| Refusing to publish: '`<widget>`' requires a selection from `<names>`; one row click can never satisfy all of them. Keep requiresSelection true on the row source only. | A widget needs several selections at once. |
| Refusing to publish: '`<widget>`' caps its query at `<number>` rows without saying so in its title (no silent caps). | A query limits its rows without telling the reader. |
| Refusing to publish: '`<widget>`' has no help text. Add it to HELP. | A widget has no help text. |
| Refusing to publish: '`<widget>`' indicator Arcade must return a flat object with middleText; the list contract renders blank. | An indicator script returns the wrong shape. |
| Refusing to publish: '`<widget>`' reads Arcade data source `<id>` with no refreshInterval; the heartbeat would never age. | A script source never refreshes. |
| Refusing to publish: widget data sources name item(s) `<ids>`, not the configured `<id>`. | A widget reads an item other than the configured one. |
| Refusing to publish: Arcade source `<name>` does not read the configured item `<id>`. | A script reads an item other than the configured one. |

The last message depends on your config.

> Refusing to publish: a widget reads `<table>`.`<field>`, which the view hides. Turn the privacy switch off or remove the widget's read.

**Cause.** A `privacy` switch hides a field in the read-only view, and a widget needs that field.

**Fix.** If you edited `dashboard_templates.json`, remove the read of that field. Otherwise remove
identifying values from the message, as the rule in [Issues](../README.md#issues) says, and send it
to the TaskDash maintainer. Turn the privacy switch off only if you accept that viewers
see the field.

## Demo

These messages come from `taskdash demo`. The command builds synthetic data in a `demo` folder. That
folder is inside the install folder, or inside the source checkout if you run the demo from one.
With `--apply`, the demo also publishes that data to separate `DEMO_` items in the `TaskDash_Demo`
folder in ArcGIS Online.

### The demo needs a real sign-in

> --apply signs in only through the real config's arcgis.url and arcgis.profile, and one of them is empty. A username or password is never copied to the demo. Store a profile as INSTALL.md step 3 describes, set arcgis.profile, then re-run `taskdash demo --apply`.

**Cause.** The demo signs in with `arcgis.url` and `arcgis.profile` from the real config. One of them
is empty, or the real config does not exist. A config that signs in with `arcgis.username` and a
password also gets this message, because the demo never copies a password.

**Fix.** Set both keys as steps 2 and 3 of [INSTALL.md](INSTALL.md) describe. Then run
`taskdash demo --apply` again.

### The demo and the real install share an item

> The demo config uses item id(s) `<ids>` that the real config also uses, so publishing would overwrite real data. Delete the demo folder's TaskDash.config.json and run `taskdash demo --apply` again.

**Cause.** The config in the `demo` folder holds an item ID that the real config also holds. The
demo would write synthetic data into your real service.

**Fix.** Delete `TaskDash.config.json` in the `demo` folder. Run `taskdash demo --apply` again. The
demo then creates its own `DEMO_` items.

### The demo install is incomplete

> The demo config lacks arcgis.`<keys>`. A failed demo setup left a partial install. Delete the DEMO_ items in the TaskDash_Demo folder and `<path>`, then re-run `taskdash demo --apply`.

**Cause.** The config in the `demo` folder holds some of `arcgis.item_id`, `arcgis.view_item_id` and
`arcgis.dashboard_id`, but not all three. An earlier `taskdash demo --apply` stopped during setup.

**Fix.**

1. In ArcGIS Online, delete the `DEMO_` items, the `DEMO_TaskDash viewers` group and the
   `TaskDash_Demo` folder. Use the order of Undo step 2 in [INSTALL.md](INSTALL.md).
2. Delete the file that the message names. It is `TaskDash.config.json` in the `demo` folder.
3. Run `taskdash demo --apply` again.

### The demo collect run failed

> The demo collect run exited with code `<code>`. Read the newest log in `<folder>`, fix the cause, then re-run `taskdash demo`.

**Cause.** A collect run on the invented data did not finish. The demo stops so that it does not
report data it never published.

**Fix.** Open the newest log file in the `Logs` folder inside the `demo` folder. Look up its last
error in this guide, fix the cause, and run the same `taskdash demo` command again.

### Demo ends with exit code 0

Exit code 0 is not an error. The demo gives 0 when the check or the publish finished.

## Status

These messages come from `taskdash status`. Without `--offline`, the command signs in and reads the
newest row of the `CollectorRuns` table for this install's host.

The exit code shows which kind of problem it found:

| Code | Meaning |
|---|---|
| `0` | The newest run succeeded and is fresh. |
| `1` | The newest run failed. |
| `2` | There is no run for the host, or the newest success is older than twice the collector interval. |
| `3` | The command could not sign in or read the service. Every message in this section that is not listed as another code exits with `3`. |

### The newest run failed

> The newest run failed. Read its log in the log folder; see TROUBLESHOOTING.md.

**Cause.** The `Service` line shows `exit_status failed`. The exit code is `1`.

**Fix.** Run `taskdash status --offline` to see the last line of the newest log. Open that log and
search for the first `ERROR` line. The `error_text` column of the **CollectorRuns** table holds the same text.

### The newest success is too old

> The newest success is older than `<minutes>` min (2 x scheduler.collector_interval_minutes). Check that the collector task still runs.

**Cause.** The newest successful run is older than twice the collector interval, 120 minutes by
default. The collector task did not run, or every run since then failed. The exit code is `2`.

**Fix.** In Task Scheduler, check that the collector task is enabled and ran. If you changed the
schedule, set `scheduler.collector_interval_minutes` to the new interval in minutes.

### Status finds no run for the host

> Service: CollectorRuns has no rows for host '`<name>`'. Run `taskdash collect --apply`, or name the collector's host with --host (`--host all` reads every host).

**Cause.** The table has no row for the host. By default, `status` reads the host in
`host_name_override`, or the computer name. On another computer, that name is not the collector's
name. The exit code is `2`.

**Fix.** Run `taskdash status --host NAME` with the name of the collector's host. Run
`taskdash status --host all` to read every host. If the table is empty, run `taskdash collect --apply`.

### Status cannot read the service

> Cannot read the service: `<reason>`. Check the sign-in and the network, then run again.

**Cause.** The sign-in or the network failed, or the config could not be read. The exit code is `3`.

**Fix.** Read the reason. Run `taskdash preflight` to check the sign-in. Run `taskdash status --offline`
to read only the local log.

### Status has no service to read

> No arcgis.`<item_id or test_item_id>` in the config, so there is no `<target>` service to read. Set it, or use `taskdash status --offline`.

**Cause.** The config key for the service is empty. `--target prod`, the default, reads
`arcgis.item_id`. `--target test` reads `arcgis.test_item_id`.

**Fix.** Set the key, or run `taskdash setup --apply` for a new install. To read only the local log,
run `taskdash status --offline`.

### Status cannot find the service item

> Service item `<id>` was not found or is not visible to this account.

**Cause.** The item ID in the config does not match an item that the signed-in account may see.

**Fix.** Open the item in ArcGIS Online and compare its ID with the config. Check the stored
sign-in.

### The service has no CollectorRuns table

> The service has no CollectorRuns table. Run `taskdash collect --migrate --apply` to create it.

**Cause.** The service does not have the table that records collector runs.

**Fix.** Run `taskdash collect --migrate --apply`.

### Prune cannot find the config

> Config not found at `<path>`, so TaskDash cannot tell which folders to prune. Pass --config with the path of the install's config, or run the command from the TaskDash folder (the folder TASKDASH_HOME names).

**Cause.** `taskdash status --prune` takes the log folder and the `Logs` folder from the config. It
found no config at that path, and it will not guess a folder. It removed nothing. The exit code is `1`.

**Fix.** Run `taskdash status --prune --config <path of the config>`. Or set `TASKDASH_HOME` to the
install folder and run it again.

### Prune cannot remove a file

> Could not remove `<path>`: `<reason>`. Close any program that holds the file, then run again.

**Cause.** `taskdash status --prune --apply` could not delete a listed file. Another program holds it
open, or the account has no delete right on the folder. The exit code is `1`.

**Fix.** Close the program that holds the file, or give the account the right to delete in the
folder. Then run `taskdash status --prune --apply` again.

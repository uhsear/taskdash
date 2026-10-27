# Configure TaskDash

TaskDash works with its default settings. Use this guide to change them. The first part shows how
to do common jobs, such as choosing which tasks to watch or turning on alert e-mails. The second
part, "Reference", lists every config key and every command-line flag.

Install TaskDash first. See [INSTALL.md](INSTALL.md). For an error message, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Before you start

TaskDash reads these files from its own folder. This guide uses `C:\TaskDash`, the folder of the
install guide.

| File | Required | Purpose |
|---|---|---|
| `TaskDash.config.json` | Yes | The config. It holds every setting in this guide, except the two files below. |
| `workflow_descriptions.json` | No | Your task descriptions, per-task grace times and accepted settings notes. |
| `rules\<name>.json` | No | Your own rules for reading numbers from task logs. |

TaskDash does not install `workflow_descriptions.json` or a rules file. You create them.

In a wheel install, "its own folder" is the folder named by the `TASKDASH_HOME` environment
variable. If `TASKDASH_HOME` is not set, it is the current folder. If the current folder has no
config, TaskDash uses the folder that contains the virtual environment running it, when that folder
holds `TaskDash.config.json`. That folder also holds `Logs` and
the saved state. The scheduled task from the install guide does not set `TASKDASH_HOME`. It starts in
`C:\TaskDash`, so the task and your own commands use the same folder only when you run
`Set-Location C:\TaskDash` first, as the install guide says. If you set `TASKDASH_HOME` for your own
commands, set it to the folder the task starts in.

Follow these rules when you edit the config:

- The config is JSON. In a JSON text value, write each backslash twice. For the path
  `D:\Jobs\Logs`, write `"D:\\Jobs\\Logs"`.
- A key that starts with an underscore is a note. TaskDash ignores it. The `_notes` section of the
  example config explains every key in one line.
- Write the full structure. The key `smtp.host` is the key `host` inside the object `smtp`.
- TaskDash reads the config at the start of every run. A change takes effect at the next hourly run.
  You do not restart anything.

TaskDash checks the config on every run. A wrong type stops the run, and the message lists every
problem. An unknown key does not stop the run. It prints a `WARNING` line and TaskDash ignores the key.
An unknown key in the `privacy` object is an error instead, because a typo there could publish a
field you meant to hide. A config without `rules.profiles` is also an error.

### Check a change

1. Save the config.
2. In a normal PowerShell window, run the check below. The first line must be
   `[ok] config: config loaded, no warnings`.

   ```powershell
   & $td preflight --offline
   ```

3. Open PowerShell as administrator, then run a dry run. It writes nothing to ArcGIS Online.

   ```powershell
   & $td collect
   ```

The dry run prints `Tasks after filtering: <n>`, then a summary and the tasks that need attention.
Use an administrator window, because a normal window can miss tasks that other accounts own.

In each new window, run the two lines from step 1 of the install guide before you use `$td`.

## Choose which tasks to monitor

By default, TaskDash monitors every task except the built-in tasks under `\Microsoft\`. To narrow the
list, set keys in the `scheduler` object. TaskDash applies the filters in this order, and a task must
pass all of them:

1. `scheduler.exclude_microsoft_tasks`: when `true`, skip every task in the folder `\Microsoft\` and
   its subfolders. A folder such as `\MicrosoftSync\` is not skipped. PowerShell drops these tasks
   before it sends the list to TaskDash.
2. `scheduler.include_disabled`: when `false`, skip disabled tasks.
3. `scheduler.task_name_regex`, `scheduler.task_path_regex` and `scheduler.action_regex`: keep only
   tasks whose name, folder path or action text matches the regex. An empty value matches all tasks.
4. `scheduler.exclude_task_name_regex`: skip tasks whose name matches the regex.
5. `scheduler.exclude_command_target_types`: skip tasks that run a script of a listed type.

A regex is a Python regular expression. TaskDash searches the text, so the regex needs to match only
part of it. The match is case-sensitive. To ignore case, start the regex with `(?i)`.

The folder path of a task starts and ends with a backslash, such as `\Nightly\`. A task in no folder
has the path `\`. The action text holds the program, the arguments and the working directory of each
action of the task.

Examples:

```json
"scheduler": {
  "task_name_regex": "^(Parcel|Address) ",
  "exclude_task_name_regex": "(?i)test",
  "exclude_command_target_types": ["VBScript"]
}
```

This example keeps tasks whose name starts with `Parcel ` or `Address `. It then skips names that
contain `test`, in any case. It also skips tasks that run a VBScript file.

`TaskDash.config.example.json` ships a starting `scheduler.exclude_task_name_regex` for new
installs. It skips the Microsoft Edge update, Google update and OneDrive update, reporting and startup tasks.
If you copied the example config, remove or edit that value to monitor those tasks. A config
without the key, such as an existing install, skips nothing by name.

`scheduler.exclude_command_target_types` takes type names, not file extensions. The names are not
case-sensitive. These are the types:

| Type name | Task runs |
|---|---|
| `PythonScript`, `Python` | A `.py` or `.pyw` script, or Python with no script |
| `PowerShellScript`, `PowerShell` | A `.ps1` script, or PowerShell with no script |
| `BatchScript`, `CommandShell` | A `.bat` or `.cmd` file, or `cmd.exe` with no file |
| `VBScript`, `JavaScript`, `ScriptHost` | A `.vbs` or `.js` file, or `wscript` or `cscript` with no script file |
| `Jar`, `Executable` | A `.jar` file that the task starts directly, or any other program |

If a filter removes tasks that TaskDash already stored, the next `--apply` run deletes their rows
from the inventory table. If that removes more than half of the stored tasks, TaskDash refuses and
stops. This guard catches a run on the wrong computer. If the shrink is real, run this once:

```powershell
& $td collect --apply --force
```

## Set the overdue grace time

A task is overdue when its next run time is in the past by more than the grace time. An overdue task
is enabled and is not running. TaskDash lists an overdue task as a problem, and the alert e-mail names it.

- `scheduler.overdue_grace_minutes` sets the grace time for all tasks. The default is 15 minutes.
  The grace time absorbs the seconds that a task needs to start.
- To give one task another grace time, set `overdue_grace_minutes` for that task in
  `workflow_descriptions.json`. This value replaces the global value for that task.

Use a longer grace time for a task that starts late, such as one that waits for a network share.
The next section shows the file format.

If you change `scheduler.overdue_grace_minutes`, rebuild the dashboard so that its help text shows the new
value. See "Rebuild the dashboard".

## Describe tasks and accept settings notes

`workflow_descriptions.json` gives each task a description. It can also set a grace time for one
task, and it can hide settings notes that you accept. Create the file in `C:\TaskDash`:

```json
{
  "tasks": {
    "Parcel Update": {
      "description": "Loads the new parcels every night.",
      "overdue_grace_minutes": 90,
      "acknowledge_config_warnings": ["StartWhenAvailable"],
      "acknowledge_reason": "Catch-up is off on purpose. A late run would overlap the backup."
    },
    "nightly_backup.py": "Copies the data folder to the backup share."
  }
}
```

Each key under `tasks` names one task. It is either the task name, or the file name of the script or program
that the task runs, such as `nightly_backup.py`. The match is not case-sensitive. TaskDash tries the task
name first. TaskDash ignores a key that starts with an underscore.

A value is either a text string, or an object with these keys. All keys are optional.

| Key | Meaning |
|---|---|
| `description` | Text shown in **Task detail** on the dashboard. If you leave it out, TaskDash shows the Description from Windows. |
| `overdue_grace_minutes` | Grace time for this task, in minutes. It replaces `scheduler.overdue_grace_minutes`. |
| `acknowledge_config_warnings` | List of text fragments. A settings note that contains one is not published. |
| `acknowledge_reason` | Why you accept the note. TaskDash adds it to the task's settings text as `Accepted: <reason>`. |

TaskDash adds settings notes to a task when its definition carries a risk, such as a catch-up
setting that is off. A fragment matches a note when the note contains the fragment. The match is
not case-sensitive. Use a fragment that is specific to one note:

| Fragment | Note it hides |
|---|---|
| `interpreter/executable not found`, `script not found` or `working directory not found` | A program, script or folder of the task is missing on this computer. |
| `LogonType` | A task that runs ArcGIS Pro Python does not use the Interactive logon type. |
| `MultipleInstancesPolicy=IgnoreNew` or `ExecutionTimeLimit` | One hung run can block every later run. |
| `StartWhenAvailable` | Catch-up is off, so Windows skips a missed daily, weekly or monthly run. |
| `Synchronize across time zones` | The start time is fixed to a UTC offset and moves at a daylight-saving change. |

TaskDash reads the file at every run. If the file is not valid JSON, TaskDash writes a `WARNING` line
to the log and uses no descriptions at all for that run. After each edit, search the newest file in
`C:\TaskDash\Logs` for `could not read descriptions`.

## Mine numbers from task logs

TaskDash reads the log files of each task and stores the numbers it finds, such as records
processed, durations and error lines. The dashboard charts them over time.

### Write a metric line

The most reliable method is a metric line in the task's own log. Print one line that starts with
`TASKDASH`, followed by `key=value` pairs:

```text
2026-10-17 02:00:03 - INFO - TASKDASH rows_updated=1234 skipped=3
```

TaskDash accepts the line in these forms:

- after a timestamp, with or without a level, as in the example above;
- after a level only, such as `INFO:root:TASKDASH rows_updated=1234` or `[INFO] TASKDASH rows_updated=1234`;
- with nothing before `TASKDASH`.

Any other text before `TASKDASH` makes the line ordinary prose, not a metric line. Follow these rules for the pairs:

- A key starts with a letter. It holds letters, digits and underscores. TaskDash writes it in
  lower case.
- A value is a plain number, such as `-12`, `3.5` or `1,234`.
- TaskDash ignores a word that is not a `key=value` pair.
- If a key appears twice in one log file, the last line wins.

TaskDash does not apply the synonym, noise or meaningful-word rules to a metric line. The key you
write is the key it stores. A metric line also does not count as an error line, so `failed=0` is
safe.

In a Python script that uses `logging`, write the line like this:

```python
logging.info("TASKDASH rows_updated=%d skipped=%d", updated, skipped)
```

### Make sure TaskDash finds the logs

TaskDash looks for the logs of a task in these folders:

- the folder of the script that the task runs, and its `Logs`, `logs`, `Log` and `log` subfolders;
- the working directory of the task, and its subfolders with those names. TaskDash skips a working
  directory that contains the script folder.

TaskDash reads `.log` files in all of these folders. It reads `.txt` files only in a folder named
`Logs`, `logs`, `Log` or `log`. It reads a log file only if Windows changed it in the last
`metrics.days_back` days. From those, it takes the newest `metrics.max_log_files_per_task` files, and
reads the last `metrics.max_lines_per_file` lines of each. It treats each file as one run, and the
change time of the file is the time of the observation.

Several tasks can share one folder. Then TaskDash gives a log file to a task only if the file name
contains the script name without its extension. A word of three or more letters or digits from the
task name also works.

If TaskDash cannot find the logs of a task, name the folder in `metrics.log_directory_overrides`. The key is the full task name,
which is the folder path of the task followed by its name. The value is the log folder:

```json
"metrics": {
  "log_directory_overrides": {
    "\\Nightly\\Parcel Update": "D:\\Jobs\\parcel_update\\output"
  }
}
```

The folder must exist on the computer. TaskDash reads `.txt` files from an override folder too.
Run `& $td preflight` to check that every override folder exists and is readable.

TaskDash never reads its own logs. It reads logs only on the computer where it runs.

#### Limit where TaskDash reads logs

A task author sets the script path and the working directory of a task. Someone who can create tasks
can point one at any folder, or at a junction named `Logs`. TaskDash runs as an administrator, so it
would read that folder and quote its last lines in the alert e-mail.

To prevent this, list the folders that hold your logs in `metrics.log_roots`:

```json
"metrics": {
  "log_roots": ["D:\\Jobs", "E:\\Scripts"]
}
```

When the list is empty or missing, TaskDash reads any folder, as before. When it is set, TaskDash
reads a log folder or file only if it resolves to a place inside one of these folders. The check
compares whole folder names, so `D:\Jobs2` is not inside `D:\Jobs`. It ignores letter case.
TaskDash also skips every folder or file that is a junction or a symbolic link. This rule applies to
folders in `metrics.log_directory_overrides` too, and to the log tail in the alert e-mail.

TaskDash writes one warning per run with the number of skipped folders. To read a skipped folder,
move its logs inside a root, or add its parent folder to `metrics.log_roots`.

### Add your own rules

TaskDash also finds numbers in ordinary log text, such as `Duration: 0:00:07` or `Records updated: 40`.
The generic rules are in `rules\default.json`. TaskDash installs that file in
`C:\TaskDash\venv\share\taskdash\rules`. Do not edit it.

To teach TaskDash the phrases of your own scripts, write a rules file, then name it in
`rules.profiles`. Create the folder `C:\TaskDash\rules`, then create `your-org.json` in it:

```json
{
  "_notes": "Rules for the log phrases of our own scripts.",
  "prose_count_rules": [
    {"order": 1000, "pattern": "Loaded (\\d[\\d,]*) parcels", "key": "parcel_count"}
  ],
  "fixed_value_rules": [
    {"order": 1000, "pattern": "No new parcels to load", "keys": ["new_parcel_count"], "value": 0}
  ],
  "noise_metric_key_patterns": [
    {"order": 1000, "pattern": "^retry_count$"}
  ],
  "display_names": {"parcel_count": "Parcels loaded"}
}
```

Then set the list in the config. The path is relative to the TaskDash folder.

```json
"rules": { "profiles": ["rules/your-org.json"] }
```

TaskDash loads `rules\default.json` first, then each profile in list order. These sections are
allowed in a rules file:

| Section | Entry fields | Use |
|---|---|---|
| `prose_count_rules` | `order`, `pattern`, `key` | The first capture group of the regex is the number. TaskDash stores the metric under `key`. |
| `fixed_value_rules` | `order`, `pattern`, `keys`, `value` | A line that matches records the number `value` for each key in `keys`. Use it for "nothing to do" lines. |
| `canonical_metric_rules` | `order`, `pattern`, `key` | Merges several log labels into one metric key. The first match wins. |
| `noise_metric_key_patterns` | `order`, `pattern` | Drops a metric key that is a label, not a count. |
| `meaningful_metric_tokens` | `order`, `token` | A metric key is kept only if a word of it starts with a listed token, such as `count` or `row`. |
| `metric_change_lower_is_better` | `order`, `token` | A metric key that contains the token counts as better when it falls. |
| `cause_patterns` | `order`, `cause`, `pattern` | Groups failed tasks by cause for the shared-cause alert. |
| `cause_text` | object of `cause` to two strings | What the cause is, and what to check first. Each cause needs an entry. |
| `display_names` | object of `key` to text | The label of a metric key on the dashboard. |
| `display_acronyms` | list of words | Words that the dashboard writes in upper case. |
| `secret_words` | list of words | Key words whose value TaskDash hides in `key=value` and `key: value` text, such as `Passwort`. They add to the built-in English words. |
| `decimal_separator` | `"."` or `","` | How the logs write decimals. The default `"."` reads `1,234.5`. Set `","` if the logs write `1.234,5`. See the rule below. |
| `log_preamble`, `section_pattern` | see `rules\default.json` | How TaskDash strips the timestamp and level from a log line, and finds a section header. |

Follow these rules:

- If you set `decimal_separator` to `","`, a `,` between digits starts the decimals. A `.` groups
  thousands only when the number starts with one to three digits, not `0`, and every group after
  each `.` has exactly three digits. `1,5` reads as 1.5, `1.234` as 1234 and `1.234,5` as 1234.5.
  Any other dotted number, such as `1.5`, `0.512` or `1.2345`, stays a plain decimal. The setting
  applies to every log, but not to `TASKDASH` metric lines, which always use `3.5` and `1,234`.
  A later rules file overrides an earlier one. Any other value stops the run with a rules error.
- Every entry in a numbered section needs a whole-number `order`. TaskDash applies entries from the
  lowest `order` to the highest.
- No two entries in one section may share an `order`, across all loaded files. The default file uses
  the numbers 10 to 510. Start your numbers at 1000.
- A key in `prose_count_rules` must still pass the other filters. Name it with a word that starts
  with a listed token, such as `count`, `record` or `row`.
- A section or a field that TaskDash does not know is an error. Put notes under a key that starts
  with an underscore.
- A bad rule stops the run before any work, and the message names the file, the entry and the fix.
  In JSON, a regex backslash needs two characters, as in `\\d`.

### Change the rules of one task

To change the rules of one task only, use `metrics.tasks` in the config. The key is the full task
name, such as `\Nightly\Parcel Update`. In JSON, write it as `"\\Nightly\\Parcel Update"`. All settings are optional.

```json
"metrics": {
  "tasks": {
    "\\Nightly\\Parcel Update": {
      "enabled": true,
      "prose_count_rules": [{"pattern": "Loaded (\\d+) parcels", "key": "parcel_count"}],
      "noise_metric_key_patterns": ["^retry_count$"]
    }
  }
}
```

- `enabled`: set `false` to stop metric rows for the task. The task keeps its latest-log summary.
- `prose_count_rules`: extra rules for this task. They run after the shared rules. Each entry has
  exactly the fields `pattern` and `key`, and no `order`.
- `noise_metric_key_patterns`: a list of regexes. TaskDash drops a metric key that matches one, for
  this task only.

If a key matches no collected task, TaskDash writes a `WARNING` line, because a typo would otherwise
change nothing.

### Clean up after a rules change

A new rule affects only the log files that TaskDash reads from now on. Metric rows that already
exist under a retired key stay for `retention.metrics_days` days. To remove them, run the clean-up
on the computer that holds the logs. First run it without `--apply`. It reports what it would
change.

```powershell
& $td collect --refresh-metrics
& $td collect --refresh-metrics --apply
```

## Turn on alert e-mails

TaskDash sends an e-mail when a task fails or is overdue, and when the problems clear. It does
not send e-mail for warnings. It can also send one daily check e-mail. Alerts are off by default.

An alert e-mail names the host and the tasks, quotes the end of each failed task's log, and links to
the dashboard. TaskDash removes secrets from the quoted lines. The privacy switches do not change
the e-mail.

TaskDash sends alerts only from a run of `taskdash collect --apply --target prod`. A run with
`--target test` never sends e-mail.

To turn on alerts:

1. In the `smtp` object, set `smtp.host`, `smtp.port` and `smtp.from`. Use port 587 with
   `smtp.starttls` set to `true` (STARTTLS), or port 465 with `smtp.ssl` set to `true` (SSL).
   TaskDash checks the certificate of the mail server.
2. If your mail server needs a sign-in, set `smtp.user` to the mail user name. Each Windows account
   has its own keyring, so store the password twice, as you stored the ArcGIS sign-in in step 3 of
   the install guide. The hourly
   task needs the copy of the task account. Your own `preflight` run needs your copy. For the task
   account, open a window with `runas`. In each window, run this command and type the password at
   the prompt. This step needs the `keyring` package. The install guide's `pip install` line
   already added it. If a cloned ArcGIS Pro environment lacks it, run
   `C:\TaskDash\proenv\python.exe -m pip install keyring`.

   ```powershell
   C:\TaskDash\venv\Scripts\python.exe -m keyring set taskdash-smtp <smtp.user>
   ```

   Replace `<smtp.user>` with the value of `smtp.user`. If your mail server needs no sign-in, leave
   `smtp.user` empty and skip this step. TaskDash never keeps the mail password in the config.
3. In the `alerting` object, set `alerting.to` to the list of addresses, and set `alerting.enabled` to
   `true`.
4. Run `& $td preflight`. The `smtp` line must be `[ok]`. This check connects to the server and
   starts encryption. It does not sign in and it sends nothing. So it cannot find a wrong password.
   To find one, run `& $td test-alert`, then `& $td test-alert --apply`. The second command sends
   one test message to every address in `alerting.to`.
5. Run `& $td collect --preview-alert`. TaskDash collects the tasks and writes
   `alert_preview_<time>.txt` in the log folder. The file holds the decision and the exact e-mail that the next
   hourly run would send. The preview sends nothing and changes no state.

If the password is missing, or the keyring cannot be read, TaskDash writes a `WARNING` line to the
log. It turns off alerts for that run. The data still publishes. When an e-mail is sent, the log
holds the line `Alert e-mailed to <addresses>`.

If an alert is due and TaskDash cannot send it, the run fails. It publishes first, then exits with
code 1, and it pings the health check with `/fail`. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). The run with nothing to send still exits 0.

### When TaskDash sends an e-mail

TaskDash sends an e-mail in these cases, and no others:

- **Problem, changed or recovered:** the list of failed and overdue tasks differs from the list in
  the last e-mail.
- **Repeat:** a problem has stayed open longer than `alerting.realert_hours` since its last
  e-mail. The default is 24 hours. TaskDash keeps what it sent in `alert_state.json` in the log folder.
- **Daily check:** it is the hour `alerting.heartbeat_hour` on the computer's clock, and TaskDash has not sent a
  daily check in the last 12 hours. Without that key, TaskDash sends no daily check. The daily check
  tells you that the monitor is alive.

When `alerting.shared_cause_min_tasks` or more tasks fail at the same time, TaskDash sends one
e-mail about one incident. It names the likely cause when most failed logs point to one. While the incident is
open, TaskDash holds new failures that have the same cause. It sends them in one digest every
`alerting.incident_digest_hours` hours. A recovery, an overdue task or a failure of another cause
is sent at once. To turn off the incident grouping, set `alerting.shared_cause_min_tasks` to `0`.
To send every change at once, set `alerting.incident_digest_hours` to `0`.

### Watch for a silent collector

An alert e-mail cannot tell you that TaskDash itself stopped. For that, set
`alerting.healthcheck_url` to the ping address of an outside monitor, such as a Healthchecks.io check.
During each hourly production run, TaskDash pings `<address>/start` at the start. It pings `<address>`
when the run succeeds and `<address>/fail` when it fails. TaskDash logs a failed ping, and a failed ping never fails the run.

A failed ping logs only `scheme://host/<redacted>`, because the path holds the secret ping ID.

### Hide secrets in logs and e-mails

TaskDash hides secrets in its own log, in the e-mail and in published text. It replaces the value
with `<redacted>` and keeps the key. It hides these:

- Values after a key word such as `password`, `token` or `secret`, and after the words in `secret_words`.
- Values after a flag such as `-Password`, `--client-secret` or `-AccessToken`.
- A `token=` value in a web address.
- GitHub and Slack tokens, AWS access key IDs and JSON Web Tokens, wherever they appear.

TaskDash does not hide long hex strings, GUIDs or item IDs, because the dashboard needs them. To add
key words in another language, list them in `secret_words` in a rules file. See [Add your own rules](#add-your-own-rules).

The alert e-mail ends each failed task with the last lines of its log. To leave those lines out, set
`alerting.include_log_tail` to `false`. The default is `true`.

The dashboard link in the e-mail comes from `alerting.dashboard_url`. If that key is empty, TaskDash builds the
link from `arcgis.url` and `arcgis.dashboard_id`.

## Silence alerts during maintenance

To stop e-mails for tasks that you expect to fail, add a window to `alerting.maintenance_windows`.
Each window has a task regex, a start time and an end time:

```json
"alerting": {
  "maintenance_windows": [
    {
      "task": "^(Parcel|Address) ",
      "start": "2026-11-01T22:00:00-05:00",
      "end": "2026-11-02T06:00:00-05:00",
      "reason": "Server patching"
    }
  ]
}
```

| Key | Meaning |
|---|---|
| `task` | Python regex, searched in the task name only, not in the folder path. |
| `start`, `end` | ISO 8601 times. The window is active from `start` to `end`, both included. |
| `reason` | A note for you. TaskDash does not read it. |

Always write the offset, such as `-05:00`, or `Z` for UTC. A time without an offset is read as UTC.

While a window is active, TaskDash holds the alerts for the matching tasks. The e-mail lists them
under `In maintenance (not alerted)`, and the log names them. If a task still fails when the window
ends, the next e-mail includes it.

The task's `Why` text on the dashboard ends with `In maintenance until <end>`. A task that needs
attention shows its own reason first, so the table cell reads, for example, `Last run failed (+1 more)`.
The detail card lists every reason. A healthy task shows the note alone.

TaskDash logs a window that has ended, and ignores it. Remove old windows from the list. An entry
with a time that TaskDash cannot read, or a regex that is not valid, produces a `WARNING` line and
TaskDash skips it.

### Mute one task until you remove the entry

If you set `end` to `null`, the window never ends. The matching tasks send no failure, overdue or
recovery alerts until you delete the entry. Also leave out `start` to mute them from now on:

```json
"alerting": {
  "maintenance_windows": [
    { "task": "^Legacy Sync$", "end": null, "reason": "Retired in the next release" }
  ]
}
```

An entry with no `end` key does not mute anything. TaskDash logs a `WARNING` and skips it, as it did
before this feature existed. You must write `"end": null` on purpose. An entry with a real `end` but
no `start` is also skipped, so an old config does not start muting after the upgrade.

The `Why` text of a muted task ends with `Muted (alerts off)`. Muted tasks stay out of the shared-cause
group in the e-mail. Every run that publishes writes one `WARNING` line with the number of muted
tasks, such as `2 task(s) are muted`. If you see that line and no longer need the mute, delete the entry.

An entry may hold only `task`, `start`, `end` and `reason`, plus notes whose key starts with `_`.
It must have a non-empty `task`. The config still loads if it does not. TaskDash writes a `WARNING`
line that reads `alerting.maintenance_windows[0] has an unknown key` or `has no non-empty "task"`.

If an entry has a null `end` or no `start` and also has an unknown key, TaskDash ignores the whole entry.
The key may be a misspelled `end` or `start`, such as `End` or `Start`. The entry would then mute the
task for ever, or start at once. Fix the key, then run again. An unknown key in an entry that has
both a `start` and an `end` is ignored, and the entry still works.

A `task` value must be text. A number or `true` matches nothing.

## Choose what viewers see

### Privacy switches

TaskDash hides sensitive fields from viewers. Four switches in the `privacy` object control this.
Each is `true` by default, and a missing key counts as `true`. Only an explicit `false` shows the
field. The install guide asks you to keep all four on.

| Switch | Hidden fields | Also masked |
|---|---|---|
| `privacy.hide_command_lines` | Inventory: `actions`, `command_target_arguments` | The command line in change summaries |
| `privacy.hide_sample_lines` | Metrics: `sample_line` | |
| `privacy.hide_accounts` | Inventory: `run_as_user`. Runs: `user_context` | The account in change summaries |
| `privacy.hide_paths` | Inventory: `command_target_path`, `command_target_working_directory`, `latest_log_path` | Paths in settings notes and change summaries. A path inside a command line needs `privacy.hide_command_lines` too. |

TaskDash blanks a hidden field in the hosted table, and the read-only view does not show it.
The view also hides the field `definition_hash`, whatever the switches say.

Two protections have no switch. TaskDash always removes secrets, such as passwords and tokens,
from command lines, descriptions, settings notes, log samples, change summaries and error
text. TaskDash also refuses to share any item with everyone. The scrubbing is best effort, so keep
the switches on.

To hide more, for example after you set a switch to `true`:

1. Save the config.
2. Hide the fields on the view. Run the check first, then `--apply`:

   ```powershell
   & $td collect --migrate
   & $td collect --migrate --apply
   ```

3. Blank the values that earlier runs stored. Check first, then apply:

   ```powershell
   & $td collect --scrub-history
   & $td collect --scrub-history --apply
   ```

To show more, set the switch to `false`, then run step 2 above to restore the view fields. The next
runs publish the values. Then rebuild the dashboard, as in "Rebuild the dashboard". The rebuilt
**Task detail** shows the paths, the arguments and the account, and the **Latest metric values** table
shows the log line. No default widget reads `actions` or `user_context`. TaskDash refuses to build a
dashboard whose widget reads a hidden field.

### Sharing and the viewers group

`taskdash setup` shares only two items, and only with the private group `TaskDash viewers`. These
are the read-only view and the dashboard. The service that holds the data stays private.

- The view allows queries only. Nobody can edit data through it.
- To give a person access, add them to the group. In ArcGIS Online, select **Groups**, open the
  group, and select **Invite members**. To remove access, remove the member.
- To share with your whole organization instead, share the view and the dashboard with the
  organization in ArcGIS Online. Never share the service.
- TaskDash never shares an item with everyone. It refuses and stops. If the config names a view, every
  `collect` run also refuses to continue when the service is public.
- If the config names a view and the service is shared with the organization or a group, `collect`
  prints a `WARNING` line and continues. `taskdash preflight` fails for any sharing level except
  Owner. Set the service to **Owner** to clear both.

To check that nothing answers without a sign-in, run this command. It queries every table of the
service and the view without a token, and it reads only:

```powershell
& $td collect --check-anonymous
```

The last line must be `Anonymous check passed: every table refused a query without a token.` If a
table answers, the output names it. Set the service to Owner in ArcGIS Online, and the view to
your organization or groups.

## Keep more or less history

- `history.days_back` sets how many days of Task Scheduler events one read covers. The default is 30.
- `history.max_events_per_run` caps the events read in one hourly run. If the log shows
  `hit the 5000-event budget`, raise it.
- `history.first_run_max_events` caps the read on the first `--apply` run, and on an `--apply` run with
  `--full-history`.
- `retention.runs_days`, `retention.metrics_days` and `retention.changes_days` set how long TaskDash
  keeps rows. After each publish, it deletes older rows. `retention.runs_days` also applies to the
  collector run table.
- `logging.max_log_age_days` sets how long TaskDash keeps its own log files. A value of `0` keeps them all.

## Rebuild the dashboard

The dashboard help text carries values from the config. These keys change it:
`scheduler.collector_interval_minutes`, `scheduler.overdue_grace_minutes`,
`scheduler.collector_task_name`, `scheduler.heartbeat_ignore_after_days`, `alerting.heartbeat_hour`,
`retention.runs_days` and `retention.metrics_days`. The collector-late and collector-down thresholds also come from
`scheduler.collector_interval_minutes`. They are 1.5 and 3 times the interval.

If you change one of these keys, rebuild the dashboard. TaskDash saves the current definition to
`dashboard_backup_<id>_<time>.json` in the log folder before it replaces the dashboard. Run the
check first:

```powershell
& $td dashboard --target prod
& $td dashboard --target prod --apply
```

The key `scheduler.collector_interval_minutes` does not change the schedule of the hourly task. It
only tells TaskDash how often the task runs.

## Retire a computer

Several computers can publish to one service. Every row carries the name of its computer. If you
take one out of service, remove its inventory rows so the dashboard stops listing its tasks.

1. On the old computer, stop and remove the collector task. If you skip this, the next run adds
   the rows again. Run `Unregister-ScheduledTask -TaskName "TaskDash Collector" -Confirm:$false`
   in an elevated PowerShell window. Use the task name you chose if it differs.
2. On a computer that stays, count what would change. Use the name in the **Host** column:

   ```powershell
   & $td collect --retire-host OLD-COMPUTER
   ```

   The output counts the rows of that computer in each table. Only the inventory rows would go.
3. If the counts are right, delete the inventory rows:

   ```powershell
   & $td collect --retire-host OLD-COMPUTER --apply
   ```

   The history tables keep their rows for that computer. They age out under the `retention` keys.
4. Rebuild the dashboard, as in "Rebuild the dashboard". The heartbeat then reads the new filter.

`--retire-host` refuses the name of the computer that runs it. It also refuses an empty name or one
with `*`, `%` or `?`. The name must match the stored name exactly, including the case.

If you cannot delete the rows yet, the heartbeat leaves out the old computer after
`scheduler.heartbeat_ignore_after_days` days without a collector run. The default is 7.

## Rename the computer

A computer reports its name in every row. After you rename it, the next run publishes new inventory
rows under the new name. The old name's inventory rows stay and show as stale tasks. The key
`host_previous_names` makes the next run move those rows to the new name instead.

1. Rename the computer, then restart it. Windows keeps the old name until the restart. Until then,
   the new name in the config makes every hourly run stop. Run `Disable-ScheduledTask -TaskName
   "TaskDash Collector"` in an administrator window before you rename. Run `Enable-ScheduledTask
   -TaskName "TaskDash Collector"` after step 5. Note the time of the last run under the old name.
   TaskDash moves no rows until 2 x `scheduler.collector_interval_minutes` have passed since then
   (120 minutes at the default of 60). It reads a newer run as another computer that still uses the name.
2. Open the config. If `host_name_override` holds the old name, set it to `""` or to the new name.
   Otherwise `collect --apply` stops, because the override differs from the computer name.
3. Add the old name to `host_previous_names`. Use the name as it shows in the **Host** column:

   ```json
   "host_previous_names": ["OLD-COMPUTER"]
   ```

4. Run the check mode. It writes nothing and stops with a message if the key is wrong:

   ```powershell
   & $td collect
   ```

5. Wait until the time from step 1 has passed. Open PowerShell as administrator, then publish:

   ```powershell
   & $td collect --apply
   ```

   The log shows one line, such as `TaskInventory: adopted 12 row(s) from OLD-COMPUTER as host
   'NEW-COMPUTER'; dropped 0 duplicate(s).` The number is how many inventory rows moved.
   TaskDash moves only the tasks that this run publishes. A task that exists only under the old name
   stays there, so the sync never counts it as a stale task of the new name.
   If the log shows a `WARNING: host_previous_names lists` line instead, TaskDash moved nothing.
   Wait for the time in step 1, then run the command again. Keep the key until a run logs
   `adopted N row(s)` and no such warning.
6. After that run, remove the old name from `host_previous_names`, or set it to `[]`.
   Do not leave the key set after a new computer takes the old name. TaskDash skips an old name
   whose collector ran within 2 x `scheduler.collector_interval_minutes`, and warns, so a live
   computer keeps its rows. Remove the key anyway.

The step moves the inventory rows only. The history tables (`TaskRuns`, `TaskMetrics`,
`TaskChanges` and `CollectorRuns`) keep the old name, and their rows age out under the `retention`
keys. The dashboard then shows the old name in those tables until they age out.

The first `collect --apply` under the new name also finds no stored watermark for that name. It reads
the whole event-log window again and stores each run under the new name. `TaskRuns` then holds two
copies of every run in that window, one under each name, until the old rows age out. Run counts and
failure lists on the dashboard are higher for up to `history.days_back` days. If you need run counts
that stay correct, use `host_name_override` instead.

To keep the old name instead, set `host_name_override` to the old name and leave
`host_previous_names` empty. Every row then keeps one name. TaskDash warns on each run that the
override differs from the computer name. `collect --apply` also stops unless you pass
`--allow-host-mismatch`. The hourly task from the install guide does not pass that flag, so add it
to the task's arguments first.

---

# Reference

## Config keys

The tables list the default that TaskDash uses when the key is missing. The example config holds
placeholder values for some keys, such as `arcgis.url` and the `smtp` keys. The Meaning column names
some example values. A type of "number" means a finite JSON number. TaskDash
rejects a key of the wrong type.

### Top level

| Key | Type | Default | Meaning |
|---|---|---|---|
| `host_name_override` | string | empty | Name that this computer reports in every row. If empty, TaskDash uses the computer name. Set it to keep history after a rename. `collect --apply` refuses to run when the value differs from the computer name, unless you pass `--allow-host-mismatch`. `taskdash preflight` warns on the `host name` line when the value differs. To fix it, clear the key. After a rename, set it to the new name and add the old name to `host_previous_names`. The example config leaves it empty. |
| `host_previous_names` | list of strings | empty | Earlier names of this computer. Each `collect --apply` moves the inventory rows stored under these names to the current name, and logs how many. The history tables keep the old name. A name must not be empty or equal to the current name. Use it for one run after a rename, then empty it. See "Rename the computer". |
| `powershell_timeout_seconds` | number | `180` | Seconds to wait for each PowerShell call before TaskDash stops it. |

### `arcgis`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `arcgis.url` | string | `https://www.arcgis.com` | Address of your ArcGIS Online organization. The setup and the sign-in profile must use the same address. |
| `arcgis.profile` | string | empty | Name of the saved sign-in profile. TaskDash signs in with it and ignores the other sign-in keys. The example config sets `taskdash`. |
| `arcgis.username` | string | empty | Sign-in name. Used only when `arcgis.profile` is empty. |
| `arcgis.password` | string | empty | Password. TaskDash refuses it unless `arcgis.allow_plaintext_password` is `true`, and ignores it when `arcgis.profile` is set. Leave it empty. |
| `arcgis.username_env` | string | empty | Name of an environment variable that holds the sign-in name. |
| `arcgis.password_env` | string | empty | Name of an environment variable that holds the password. |
| `arcgis.allow_plaintext_password` | boolean | `false` | Set `true` only to accept a password stored in the config. |
| `arcgis.item_id` | string | empty | Item ID of the hosted service that the collector writes to. `taskdash setup --apply` fills it. |
| `arcgis.test_item_id` | string | empty | Item ID of a test copy of the service. Used by `--target test`. |
| `arcgis.view_item_id` | string | empty | Item ID of the read-only view that the dashboard reads. `taskdash setup --apply` fills it. |
| `arcgis.test_view_item_id` | string | empty | Item ID of the read-only view of the test service. |
| `arcgis.dashboard_id` | string | empty | Item ID of the dashboard. `taskdash setup --apply` fills it. `taskdash dashboard --target prod` updates this item. |
| `arcgis.service_description` | string | `TaskDash - scheduled task monitoring` | Description text for a service that TaskDash creates. It has no effect on an existing service. |
| `arcgis.service_tags` | string | `taskdash,monitoring,scheduled tasks` | Comma-separated tags for a service that TaskDash creates. |
| `arcgis.dashboard_tags` | string | `taskdash,monitoring` | Comma-separated tags for the dashboard, set when TaskDash writes it. |

### `alerting`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `alerting.enabled` | boolean | `false` | Turns alert e-mails on or off. |
| `alerting.to` | list of strings | empty | Addresses that receive alert e-mails. If the list is empty when an alert is due, TaskDash sends none, and the run fails with exit code 1. |
| `alerting.mail_config_path` | string | empty | Path of an older mail settings file with an `email settings` object. TaskDash reads it only when the `smtp` object is empty. New installs use `smtp`. Deprecated: TaskDash does not verify the mail server's certificate for this file. `taskdash preflight` and each collector run warn about it. To move, fill the `smtp` object and clear this key. |
| `alerting.healthcheck_url` | string | empty | Address of an outside monitor. TaskDash pings it around each hourly production run. |
| `alerting.include_log_tail` | boolean | `true` | Puts the last lines of a failed task's log in the alert e-mail. Set `false` to leave them out. |
| `alerting.dashboard_url` | string | empty | Dashboard link placed in alert e-mails. If empty, TaskDash builds it from `arcgis.url` and `arcgis.dashboard_id`. |
| `alerting.heartbeat_hour` | number | none | Hour of the day, 0 to 23 on the computer's clock, for the daily check e-mail. Without it, no daily check. The example config sets `7`. |
| `alerting.realert_hours` | number | `24` | Hours before TaskDash repeats an alert for a problem that has not cleared. |
| `alerting.shared_cause_min_tasks` | number | `3` | Number of failed tasks that makes one incident e-mail. `0` turns the grouping off. |
| `alerting.incident_digest_hours` | number | `6` | Hours between digests of new failures during an open incident. `0` turns the digest off. |
| `alerting.maintenance_windows` | list of objects | empty list | Windows that hold alerts. Each object has `task`, `start`, `end` and `reason`. With `"end": null`, the window mutes the tasks until you remove it. |

### `scheduler`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `scheduler.task_name_regex` | string | empty | Monitor only tasks whose name matches. Empty matches all. |
| `scheduler.task_path_regex` | string | empty | Monitor only tasks whose folder path matches. Empty matches all. |
| `scheduler.action_regex` | string | empty | Monitor only tasks whose action text matches. Empty matches all. |
| `scheduler.exclude_task_name_regex` | string | empty | Skip tasks whose name matches. |
| `scheduler.exclude_microsoft_tasks` | boolean | `true` | Skip the built-in tasks whose folder path starts with `\Microsoft`. |
| `scheduler.include_disabled` | boolean | `true` | Keep disabled tasks in the inventory. |
| `scheduler.exclude_command_target_types` | list of strings | empty list | Skip tasks that run a script of a listed type, such as `VBScript`. Use the type names in the table of "Choose which tasks to monitor", not file extensions. |
| `scheduler.interactive_required_pattern` | string or `false` | `\ArcGIS\Pro\bin\Python` | Text, not a regex, that marks a program path as needing a signed-in session, such as ArcGIS Pro Python under a Single Use licence. TaskDash adds a settings note to a matching task that does not use the Interactive logon type. A script that TaskDash can read and that does not import arcpy gets no note. An empty value keeps the default. `false` turns the check off. |
| `scheduler.collector_task_name` | string | `TaskDash Collector` | Full name of the task that runs the collector. It must match the `-TaskName` of `install_task.ps1`. The default is the installer's default task name. Set this key only if you registered the task under another name. |
| `scheduler.collector_interval_minutes` | number | `60` | Minutes between collector runs. The dashboard calls the collector late after 1.5 intervals and down after 3. It must be above 0. |
| `scheduler.overdue_grace_minutes` | number | `15` | Minutes a task may run late before it is overdue. |
| `scheduler.heartbeat_ignore_after_days` | number | `7` | Days. The dashboard heartbeat leaves out a computer that has made no collector run for this long, so a retired or renamed computer does not show as down. It must be above 0. Rebuild the dashboard after you change it. |

### `history`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `history.max_events_per_run` | number | `5000` | Most Task Scheduler events read in one run. |
| `history.days_back` | number | `30` | Days of Task Scheduler history that a read covers. |
| `history.first_run_max_events` | number | `30000` | Most events read when there is no stored position: the first `--apply` run, or an `--apply` run with `--full-history`. `--preview-alert` also uses it. |

### `metrics`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `metrics.days_back` | number | `30` | Read only log files that changed in the last so many days. |
| `metrics.max_log_files_per_task` | number | `40` | Most log files read per task. |
| `metrics.max_lines_per_file` | number | `5000` | Lines read from the end of each log file. A value of `0` or less reads the whole file. |
| `metrics.log_directory_overrides` | object | empty object | Maps a full task name to the folder that holds its logs. |
| `metrics.log_roots` | list of strings | empty list | When set, TaskDash reads logs only inside these folders. It skips links and junctions. |
| `metrics.tasks` | object | empty object | Maps a full task name to `enabled`, `prose_count_rules` and `noise_metric_key_patterns`. |

### `upload`, `retention` and `logging`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `upload.chunk_size` | number | `200` | Most rows sent to ArcGIS Online in one request. |
| `retention.runs_days` | number | `90` | Days to keep rows in the runs table and the collector run table. |
| `retention.metrics_days` | number | `90` | Days to keep rows in the metrics table and the MetricChange table. |
| `retention.changes_days` | number | `365` | Days to keep rows in the changes table. |
| `logging.log_dir` | string | empty | Folder for the run logs and `alert_state.json`. If empty, the `Logs` folder in the TaskDash folder. |
| `logging.max_log_age_days` | number | `30` | Days to keep TaskDash's own log files. `0` or less keeps all. |

### `smtp`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `smtp.host` | string | empty | Mail server name. Without `smtp.host` and `smtp.from`, TaskDash turns alerts off for the run. |
| `smtp.port` | number | `587` | Mail server port. Use 587 for STARTTLS, 465 for SSL. |
| `smtp.from` | string | empty | Sender address. |
| `smtp.user` | string | empty | Mail sign-in name. Its password comes from the Windows keyring (service `taskdash-smtp`), never from the config. If empty, TaskDash does not sign in. |
| `smtp.starttls` | boolean | `true` | Upgrade the connection with STARTTLS. |
| `smtp.ssl` | boolean | `true` when `smtp.port` is `465`, else `false` | Use SSL from the first byte. |
| `smtp.allow_plaintext_auth` | boolean | `false` | Set `true` only to send the mail password over an unencrypted connection. Without it, TaskDash refuses to sign in when both `smtp.starttls` and `smtp.ssl` are off. |

### `privacy` and `rules`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `privacy.hide_command_lines` | boolean | `true` | Publish no command lines. |
| `privacy.hide_sample_lines` | boolean | `true` | Publish no sample log lines. |
| `privacy.hide_accounts` | boolean | `true` | Publish no account names. |
| `privacy.hide_paths` | boolean | `true` | Blank the script, working-folder and log paths. Mask paths in settings notes and change summaries. |
| `rules.profiles` | list of strings | none (required) | Rules files added to `rules\default.json`, such as `rules/your-org.json`. The key must exist. An empty list uses only the default rules. |

## Command-line flags

Run `taskdash <command> --help` for the options of a command. Without `--apply`, a command writes
nothing to ArcGIS Online or to Windows settings. Several flags write a local file, as noted.

Every command accepts `--version`. It prints `TaskDash <version>` and exits. `taskdash --version`
does the same. The `status` command has no `--version` flag.

### `taskdash collect`

This command collects the tasks, runs and log metrics, and publishes them. The hourly task runs
`taskdash collect --apply`. A normal install needs only that, and the dry run in "Check a change".
The other flags are for setup, upgrades and repair. The table below marks them.

| Flag | Default | Meaning |
|---|---|---|
| `--apply` | off | Write to ArcGIS Online. Without it, `collect` is a dry run. |
| `--target` | `prod` | Which configured item IDs to use: `prod` or `test`. A `test` run never sends e-mail. |
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file. |
| `--force` | off | Allow the inventory sync to delete more than half of the stored tasks of this host. After a filter change, use it once. |
| `--allow-host-mismatch` | off | Allow `--apply` when `host_name_override` differs from the computer name. Without it, `collect --apply` stops. The hourly task does not pass this flag. |
| `--full-history` | off | Ignore the stored position in the event log once, and read the whole window. Safe. After a gap, or to repair history, use it. |
| `--preview-alert` | off | Collect, then write `Logs\alert_preview_<time>.txt` with the e-mail the next hourly run would send. It sends nothing. It cannot combine with `--apply`, `--migrate`, `--capture-fixture`, `--create-service` or `--target test`. |
| `--check-anonymous` | off | Query every table of the service and view without a sign-in. Exit code 0 means all refused. Reads only. It runs alone and cannot combine with `--apply`. |
| `--migrate` | off | Report, or with `--apply` add, tables, fields and indexes that the service lacks, and fix the fields of the view. It publishes nothing. After you upgrade TaskDash or change a privacy switch, run it. Preflight tells you when it is due. |
| `--scrub-history` | off | Count the stored rows that still hold a value the privacy switches hide or the secret scrubber redacts. With `--apply`, blank or redact them. Maintenance: run it after you turn on a privacy switch. It runs alone. |
| `--refresh-metrics` | off | Report stored metric rows under keys the log miner no longer makes, and outdated metric names. With `--apply`, delete and rename them, and make the next run send every metric row again. Maintenance: run it after a rules change or an upgrade, on the task computer. It runs alone. |
| `--retire-host` | none | Count the stored rows of the computer you name, then exit. With `--apply`, delete that computer's inventory rows only. The history tables keep theirs and age out by retention. Run it from another computer. It refuses the name of the computer that runs it, an empty name and a wildcard. It runs alone. See "Retire a computer". |
| `--refresh-result-text` | off | Report stored run rows whose result text is out of date. With `--apply`, update that text. Maintenance: needed only if an upgrade changes the wording. It runs alone. |
| `--create-service` | off | Print what the hosted service would hold. With `--apply`, create it and print its item ID. A new install does not need it, because `taskdash setup` does this. |
| `--service-name` | `TaskDash` | Name of the service that `--create-service` creates. |
| `--create-view` | off | Print the plan for a read-only view of the service. With `--apply`, create it and print its item ID. The view is owner only; share it with the viewers group yourself. A new install does not need it. It stops when `arcgis.view_item_id` is set. |
| `--capture-fixture` | none | Write the tasks and events of this computer to the JSON file you name, then exit. The file holds raw command lines and account names. Keep it private and never send it in a report (see [Issues](../README.md#issues)). For tests only. |
| `--fixture` | none | Read tasks and events from a captured JSON file instead of Task Scheduler. For tests only. |

### `taskdash setup`

This command creates the ArcGIS items of a new install. See step 6 of the install guide. It stops if
the config already holds `arcgis.item_id`, `arcgis.view_item_id` or `arcgis.dashboard_id`.

| Flag | Default | Meaning |
|---|---|---|
| `--apply` | off | Create the folder, service, view, group and dashboard, and write their IDs to the config. |
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file to read and update. |
| `--folder` | `TaskDash` | Name of the ArcGIS folder that holds the new items. It must not exist yet. |
| `--title-prefix` | empty | Text put before every item title. |
| `--group-title` | `<prefix>TaskDash viewers` | Title of the viewers group. |

With `--apply`, setup also saves `taskdash_setup_manifest.json` beside the config. It lists the ID,
type, title and creation order of every item that setup created, the group ID and the folder name. It
holds no secret. Setup saves it even when it stops partway, so `taskdash remove` can find the items.

### `taskdash remove`

This command undoes `taskdash setup`. It reads `taskdash_setup_manifest.json` beside the config,
signs in, and lists the items it would delete. It changes nothing without `--apply`.

| Flag | Default | Meaning |
|---|---|---|
| `--apply` | off | Delete the listed items that still exist and that the signed-in account owns. |
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file. The manifest must be in the same folder. |

The order is the reverse of creation: the dashboard, the group, the view, the service, and then the
folder if it is empty. The command turns off delete protection on each item just before it deletes
that item. If the delete fails, it turns protection back on. It never deletes an item that the
manifest does not list. It also checks that each item still has the type and title that the manifest
records, and it finds the folder by the manifest's folder ID.

The command skips an item, reports why, and goes on in these cases: another account owns the item,
the item's type or title changed, ArcGIS refuses to turn off delete protection or to delete, the
service's view was skipped, or the folder still holds other items. An
item that is already gone is reported and is not an error. The exit code is 1 when `--apply` skipped
an item, and 0 otherwise.

The command does not touch the Windows task or any file. It prints the `Unregister-ScheduledTask`
command with the collector task name (`scheduler.collector_task_name`) and the `pip uninstall`
command. Run them yourself, as the Undo section of [INSTALL.md](INSTALL.md) describes.

### `taskdash signin`

This command stores the ArcGIS sign-in that TaskDash reads, as step 3 of the install guide
describes. It reads `arcgis.url`, `arcgis.profile` and `arcgis.username` from the config. It asks for
the user name, and offers `arcgis.username` as the default. It asks for the URL when `arcgis.url` is
empty, and then tells you to set it in the config. It reads the password with a hidden prompt. The
password is never an argument, and TaskDash never prints or logs it.

The command signs in and prints one line for each check: the sign-in, the account (`users.me` must
not be empty), and the privileges that `taskdash preflight` also needs. Without `--apply` it saves
nothing and exits 0 when every check passes. It exits 1 when one fails.

| Flag | Default | Meaning |
|---|---|---|
| `--apply` | off | After the checks pass, save the profile under the name in `arcgis.profile`, and sign in with it to prove it works. It replaces a profile of the same name. With `--for-task-account`, write the hand-off file instead. |
| `--for-task-account` | off | After the checks pass, seal the sign-in into a hand-off file for the task account, instead of saving a profile for you. Without `--apply` it prints the plan and writes nothing. See "Hand the sign-in to the task account". |
| `--task-user` | the account of the collector task | With `--for-task-account`, the account that gets the sign-in, written `DOMAIN\name`. Needed only when the collector task is not registered yet. |
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file. |

The profile belongs to the Windows account that ran the command. Run the command once as you and
once as the task account, or use `--for-task-account` for the second run. Only a built-in ArcGIS
account without multifactor authentication works today. The command stops with a message when
`arcgis.profile` is empty.

#### Hand the sign-in to the task account

Each Windows account has its own Credential Manager, so you cannot save the task account's sign-in
from your own window. `--for-task-account --apply` avoids `runas`. After the checks pass, it asks
for the mail password too, when `smtp.user` is set. Then it writes `taskdash_handoff.bin` in the
TaskDash folder.

- The file holds the ArcGIS URL, user name and password, and the mail password. Windows seals them for
  this computer (DPAPI, machine scope), so the file never holds plain text.
- The file's permissions allow only the task account, SYSTEM and Administrators. They do not inherit
  from the folder. The task account may read and delete it.
- The next `taskdash collect --apply` run by the task account saves the ArcGIS profile and the mail
  password into that account's own stores. It checks that the profile signs in, and then deletes the
  file. A run by any other account leaves the file alone.
- If saving or checking fails, TaskDash keeps the file, logs the error, and the run fails like a
  sign-in failure. See "The hand-off sign-in was kept" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- A new `--for-task-account --apply` run replaces an older file.

The command needs pywin32, which the `arcgis` package installs. The `runas` method of step 3 in
[INSTALL.md](INSTALL.md) still works as a fallback.

### `taskdash preflight`

This command checks the config, the account, the computer and the ArcGIS items. It changes nothing,
except with `--enable-event-log --apply`.

| Flag | Default | Meaning |
|---|---|---|
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file. |
| `--offline` | off | Skip the checks that need the network. They print `[--]`. |
| `--enable-event-log` | off | Show the state of the Task Scheduler log and the command that enables it. It runs alone, without the other checks. |
| `--apply` | off | With `--enable-event-log`, run the command. It needs an administrator window. With `--as-task`, register the temporary task and run it. |
| `--max-size-mb` | the larger of the current size and 64 | With `--enable-event-log`, set the log size in megabytes. |
| `--config-diff` | off | Compare the config keys with the keys TaskDash knows. It runs alone and cannot combine with `--offline` or `--enable-event-log`. |
| `--as-task` | off | Run the preflight checks as the task account, in a temporary task, without `runas`. Without `--apply` it prints the plan and changes nothing. It runs alone. It accepts `--config` and `--offline`, and passes them to the run. See "Run preflight as the task account". |
| `--task-user` | the account of the collector task | With `--as-task`, the account to run as, written `DOMAIN\name`. Needed only when the collector task is not registered yet. |
| `--consume-handoff` | off | Store the sign-in from the hand-off file, and delete the file, before the `credentials` check. `--as-task` adds it to the run it starts. A plain `taskdash preflight` never touches the file. Do not use it by hand. |

`--config-diff` prints three lists and changes nothing:

- **Unknown keys.** TaskDash ignores these. A misspelled key is the usual cause.
- **Renamed keys.** Each line reads `old -> new`. Move the value to the new name.
- **Absent keys.** TaskDash uses the default for each. Each line reads `key (default: value)`. The
  value comes from the TaskDash code. The values in `TaskDash.config.example.json` are placeholders,
  so the list does not copy them. A key that holds a password shows its name only.

The command never prints a value from your config. It exits 0 even when it lists problems. It
exits 1 only when it cannot read the config. The message then says to fix the file and run again.

#### Run preflight as the task account

`--as-task` shows what preflight sees when the collector task runs it. It replaces the `runas`
window. Without `--apply` it prints the plan and changes nothing: the name of the temporary task, the
account, the command, and the output file. The task name is `TaskDash preflight check` and a random
suffix.

With `--apply`, run it in a PowerShell window opened as administrator. The command does these steps:

1. It reads the account and the logon type of the collector task (`scheduler.collector_task_name`).
   It only reads that task. It never changes, runs or deletes it.
2. It asks for the password of the account at a hidden prompt, for the Password logon type. The
   Interactive logon type needs no password. The password goes to Task Scheduler through its COM
   interface. It is never a command line argument and TaskDash never prints it.
3. It creates an empty output file in the log folder. Only the task account, you, the Administrators
   group and SYSTEM can read it.
4. It registers a temporary task from the same `taskdash_task.xml` template as the collector task,
   without the hourly trigger. It runs the task once and waits up to 300 seconds.
5. It prints the preflight output, and exits 0 when preflight passed.
6. It deletes the temporary task and the output file. This happens even when the run fails or times out.

If the collector task is not registered yet, pass `--task-user DOMAIN\name`. The check then uses the
Password logon type. Add `--offline` to skip the network checks. A failed run prints the reason, and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) explains each message.

### `taskdash dashboard`

This command builds the dashboard definition and updates the dashboard item. A new install does not
need it, because `taskdash setup` creates the dashboard. After a config change or an upgrade, use it
to rebuild the dashboard.

| Flag | Default | Meaning |
|---|---|---|
| `--apply` | off | Create or update the dashboard item. |
| `--target` | `test` if `arcgis.test_item_id` is set, otherwise `prod` | `prod` updates the dashboard in `arcgis.dashboard_id`. `test` builds `TaskDash_TEST`. The command prints one line naming the target it chose. With `--apply` and no test service, it stops. Pass `--target prod` to update production. |
| `--title` | the standard title for the target | Dashboard title. |
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file. |
| `--offline` | off | Build and check the definition with placeholder IDs. It never signs in and cannot combine with `--apply`. It writes `dashboard_taskdash.json`. For checks without ArcGIS. |
| `--source-dashboard` | empty | Existing dashboard to copy widget structure from. A fallback for when `dashboard_templates.json` is lost. A normal install does not need it. |

A rebuild replaces any hand edits you made in the Dashboards editor. Before `--apply` updates an
existing dashboard, the command prints a line saying so. It then saves the current definition as
`dashboard_backup_<item id>_<time>.json` in the log folder. To keep a hand edit, copy it from that file.

### `taskdash status`

This command is read-only, except with `--prune --apply`. It prints the last result in the newest
local log, the newest row of the collector run table, and the age of the newest successful run.

The exit code tells a script what to do. `0` means the newest run succeeded and is fresh. `1` means
the newest run failed. `2` means there is no run, or the newest success is older than twice
`scheduler.collector_interval_minutes` (120 minutes by default). `3` means the command could not
sign in or read the service. `--offline` returns `0` unless it cannot read the config.

| Flag | Default | Meaning |
|---|---|---|
| `--offline` | off | Print only the local line. It never signs in and works without a config. |
| `--target` | `prod` | Which service to read: `prod` or `test`. |
| `--host` | this install's host: `host_name_override`, else the computer name | Read the runs of this host only. `--host all` reads every host. |
| `--prune` | off | List old TaskDash backups and logs, instead of the status. It removes nothing without `--apply`. |
| `--apply` | off | With `--prune`: remove the files that `--prune` lists. |
| `--keep` | `10` | With `--prune`: how many to keep of each kind. This covers dashboard backups for each dashboard, config backups, bootstrap logs and crash records. |
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file. |

`--prune` looks only at files with TaskDash's own names, and it skips links. It lists these files:

- `dashboard_backup_<item id>_<time>.json` in the log folder, except the newest `--keep` of each dashboard.
- `<config name>.bak_setup_<time>`, which `taskdash setup --apply` writes beside the config, except the newest `--keep`.
- `collector_runs_pending_prod.json` and `collector_runs_pending_test.json` in the log folder. The file
  must hold no saved rows and be unchanged for 7 days. With `--apply`, the command first takes the
  collector's run lock. If a collector run holds the lock, or the lock cannot be checked, the command
  skips every pending file, says so, and you run it again later. Without `--apply`, it does not take the lock.
- Bootstrap logs older than 7 days: `bootstrap.log` and `bootstrap_<id>.log` in the `Logs` folder beside
  the config file, except the newest `--keep`. The task launcher writes a bootstrap log there on every
  run, whatever `logging.log_dir` says, so that folder is in use. A log younger than 7 days is never listed.
- `bootstrap_FAIL_<id>.log` in that same folder, except the newest `--keep`. The file must be
  older than 7 days. These files are the only record of a failure before logging starts.

The command needs the config. If it cannot find the config, it stops with a message and prunes nothing.
Pass `--config` with the path, or run the command from the TaskDash folder.

### `taskdash test-alert`

This command checks the alert e-mail path. Without `--apply`, it prints the recipients, the server
and the subject of the test message, and it sends nothing. With `--apply`, it sends one short test
message the way the hourly run sends alerts. It exits 0 when the server accepts the message, and 1
with the reason when it does not. It never prints the mail password.

| Flag | Default | Meaning |
|---|---|---|
| `--apply` | off | Send the test message to every address in `alerting.to`. |
| `--config` | `TaskDash.config.json` in the TaskDash folder | Path of the config file. |

### `taskdash demo`

This command shows TaskDash with invented data, apart from your install. It keeps its own config,
logs and data in the `demo` folder inside the TaskDash folder.

Without `--apply`, the command builds the data, runs a dry run of `collect` and builds the dashboard
definition offline.

| Flag | Default | Meaning |
|---|---|---|
| `--apply` | off | Create the `DEMO_` items in the ArcGIS folder `TaskDash_Demo` and publish the invented data to them. It signs in with `arcgis.url` and `arcgis.profile` of your own config. |

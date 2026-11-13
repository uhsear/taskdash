# Install TaskDash

TaskDash watches the scheduled tasks on one Windows computer. Every hour it publishes their health,
run history and log metrics to ArcGIS Online. A dashboard shows the result.

If your IT or security team must approve the install, give them [APPROVAL.md](APPROVAL.md) first.
It lists the endpoints, accounts, files and data.

Every release lists `taskdash-docs-<version>.zip`. It holds these guides, so you can read them
before you install anything. After the install, the same guides sit in
`C:\TaskDash\venv\share\taskdash\docs`.

This guide takes you from nothing to a working dashboard. Plan for about one hour. Run every step
on the computer whose tasks you want to watch, at its console or through Remote Desktop. A
PowerShell remoting session (`Enter-PSSession`) does not work: Windows gives it no access to the
saved sign-in, and TaskDash fails with "A specified logon session does not exist".

## Quick install

If you meet the Requirements below, two commands do steps 1 to 8 for you. From the release page
(<https://github.com/uhsear/taskdash-releases/releases/latest>), download these four files into your
`Downloads` folder: `Install-TaskDash.ps1`, the wheel, `requirements.lock`, and `SHA256SUMS.txt`. The
script checks the wheel and the lock against `SHA256SUMS.txt`. To check the script too, compare the
output of `Get-FileHash` on it with its line in `SHA256SUMS.txt`. Open PowerShell as administrator.
The account in that window owns the ArcGIS sign-in, and later you run `status` and `preflight` as
that account. So use your daily account if it has administrator rights. If a separate administrator
account elevates the window, `$env:USERPROFILE` names that account's folder. Then replace
`$env:USERPROFILE\Downloads` in the commands with the full path of the folder that holds the files.
Replace `<version>` with the wheel's version and `DOMAIN\svc-taskdash` with the task account. The
script makes the virtual environment with Python 3.13. Add `-PythonVersion 3.12` only to use another
installed version, which is not supported. Run the check first. It changes nothing:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-TaskDash.ps1" -InstallDir C:\TaskDash -Wheel "$env:USERPROFILE\Downloads\taskdash-<version>-py3-none-any.whl" -TaskAccount DOMAIN\svc-taskdash
```

It prints what each step would do. If a line stops with `ERROR`, fix it and run the check again.
When the check passes, run the same command with `-Apply` added at the end:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-TaskDash.ps1" -InstallDir C:\TaskDash -Wheel "$env:USERPROFILE\Downloads\taskdash-<version>-py3-none-any.whl" -TaskAccount DOMAIN\svc-taskdash -Apply
```

With `-Apply`, the script runs these steps in order and stops at the first one that fails. Then it
lists what it created so far.

1. It checks that the window is elevated.
2. It creates `C:\TaskDash`, locks its permissions and makes the virtual environment (step 1
   below). It installs the packages of `requirements.lock` with `pip install --require-hashes`, and
   then the wheel with `--no-deps`. `SHA256SUMS.txt` must be beside the wheel and list both files,
   and their SHA-256 values must match it first. A missing `SHA256SUMS.txt`, a missing
   `requirements.lock`, a file that the sums do not list or a value that differs stops the script.
   A file named like the wheel plus `.sha256` can replace the wheel's line, but not the lock's.
3. It copies the example config to `TaskDash.config.json` if there is none (step 2).
4. It runs `taskdash signin` twice, once for this account and once for the task account (step 3).
   It asks for the ArcGIS user name and password each time. The script never reads the password.
   It then prints `Sign-in stored for <account>; run status and preflight as this account`.
5. It runs `taskdash preflight`, after it turns on the Task Scheduler event log (step 5).
6. It runs `taskdash setup`, then `taskdash setup --apply` (step 6).
7. It runs `install_task.ps1` with `-EnableEventLog` and `-StartNow` (steps 7 and 8). It asks for the
   task account's password.
8. It runs `taskdash status`.
9. It prints the dashboard link and the next steps: invite viewers (step 9 below), set
   `alerting.healthcheck_url` ("Set up an outside health check" after step 8) and turn on alerts.

A matching SHA-256 value proves that a file is the one the release lists. It does not prove who
built the release: a person who can change the release page can change `SHA256SUMS.txt` too. To
check who built the files, verify the Sigstore bundles, as the Requirements describe.

If your organization has its own ArcGIS Online address, press Enter at the sign-in prompt for the
URL. `taskdash signin` then finds your organization's address and writes it into the config. Do not
make `C:\TaskDash` or its config by hand first: the installer refuses a folder that it did not lock.

Run the same `-Apply` command again after a failure, or on a working install. Each step that is
already done prints `nothing to do`. Without `-Upgrade`, the script does not change an installed
version: to move to another release, follow [UPGRADE.md](UPGRADE.md), which describes `-Upgrade`. If a message stops the script, see "The
one-window installer stops" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). The numbered steps below
describe the same work by hand.

## Requirements

- **Windows:** Windows 10, Windows 11 or Windows Server 2016 or later.
- **Your Windows account:** sign in with the account you use every day. Steps 1 to 8 run in a
  PowerShell window opened as administrator, so this account must be an administrator. Only an
  administrator can edit the config in `C:\TaskDash`: step 1 gives your account only Read &
  Execute there. You also need administrator rights to undo the install. If your administrator rights come from a
  separate admin account, use it for steps 1 to 8. It then owns the ArcGIS sign-in.
- **Python:** 64-bit Python 3.13 from python.org, the supported version, **installed for all users**.
  Python 3.11 and 3.12 pass the same tests in CI, but they are not supported, and the lock below may
  not install on them. The last 3.11 release with a Windows installer is 3.11.9, and it gets no
  security fixes. In the installer, select **Customize installation**, keep **py launcher**
  selected, and on the next page select **Install Python 3.13 for all users**. The task account runs
  this Python, and it cannot run a Python installed in your own user profile. To check, run
  `py -0p`. The path for 3.13 must be under `C:\Program Files`, not under `C:\Users`.
- **Internet access** from the computer over HTTPS:
  - to `pypi.org` and `files.pythonhosted.org`, for step 1;
  - to `www.arcgis.com`, your organization's `<org>.maps.arcgis.com`, and `*.arcgis.com`, in steps
    3 to 8 and every hour after that.

  If the computer reaches the internet through a proxy, run this once in an administrator window
  before step 1, with your proxy's address:

  ```powershell
  [Environment]::SetEnvironmentVariable("HTTPS_PROXY", "http://<proxy>:<port>", "Machine")
  ```

  Then close every PowerShell window and open new ones, because an open window does not see the
  new value. If the first run in step 8 still cannot reach ArcGIS Online, restart the computer, so
  that Task Scheduler sees the new value too.
- **ArcGIS Online:** a built-in ArcGIS account (a user name and a password) without multifactor
  authentication. It must be able to create items, publish hosted feature layers, create groups,
  share to groups and edit features. A Creator user type has these privileges, and step 5 checks
  them. An account that signs in only through your organization's single sign-on (SAML), or that
  requires multifactor authentication, cannot be used. Ask your ArcGIS administrator for a
  dedicated account, not a person's. Give it a custom role with only these five privileges:
  `portal:user:createItem`, `portal:publisher:publishFeatures`, `portal:user:createGroup`,
  `portal:user:shareToGroup` and `features:user:edit`. [ROTATE.md](ROTATE.md) shows how to change its password.
- **A Windows account for the task.** The collector runs as this account. It must be a member of
  the local Administrators group on this computer. It needs a password, because the task stores it.
  This guide calls it `DOMAIN\svc-taskdash`. Use your own account name everywhere you see it. For a
  local account, write `COMPUTERNAME\name`, for example `TASKHOST1\svc-taskdash`.
- **The TaskDash wheel:** from <https://github.com/uhsear/taskdash-releases/releases/latest>, download the
  file `taskdash-<version>-py3-none-any.whl` into your `Downloads` folder. Keep only one TaskDash
  wheel in that folder. If this computer cannot reach GitHub, download the file on another computer
  and copy it into `Downloads`. The release page is public, so you need no GitHub account.
  - The release also lists `SHA256SUMS.txt`. It has one line each for the wheel, `requirements.lock`,
    the `.cdx.json` SBOM, `Install-TaskDash.ps1` and the `taskdash-docs-<version>.zip` of the guides. To check the download, compare its value with the
    output of `Get-FileHash $env:USERPROFILE\Downloads\taskdash-*.whl`. The value proves that you
    have the file the release lists, not who built it. The Sigstore check below does that.
  - Optional: check who built the files. The release lists a `.sigstore.json` bundle for the wheel
    and for `SHA256SUMS.txt`. Download both bundles into `Downloads`. In a separate virtual
    environment, install the checker with `python -m pip install sigstore`. Then run this command
    for each file. Replace `<tag>` with the release tag, for example `v1.2.0`, and `<file>` with the
    file name:

    ```powershell
    python -m sigstore verify github --cert-identity https://github.com/uhsear/taskdash/.github/workflows/release.yml@refs/tags/<tag> --bundle <file>.sigstore.json <file>
    ```

    The command prints `OK: <file>` when the release workflow of the TaskDash repository signed
    the file. That is the private repository that builds the release. The maintainer copies the
    signed files to the public release page by hand, so the identity names the private repository.
    For example, release `v1.1.0` carries the identity
    `https://github.com/uhsear/taskdash/.github/workflows/release.yml@refs/tags/v1.1.0`. The command
    also checks that GitHub Actions issued the signing certificate.
    Any other output means you must not install the file. Ask the TaskDash maintainer.

TaskDash is tested on one computer with 29 tasks that run at most hourly. A weekly automated test
also runs the offline passes on 500 synthetic tasks and checks that they finish in 15 minutes.
Larger installs may work.

To move an installed TaskDash to a new release, or back to the previous one, follow
[UPGRADE.md](UPGRADE.md) instead of this guide.

## 1. Install TaskDash

Open PowerShell **as administrator** (right-click PowerShell, then **Run as administrator**). Keep
this window for steps 1 to 8. Run these commands. They create `C:\TaskDash`, make Administrators its
owner, lock its permissions, make a Python virtual environment inside it, and install the wheel from
your `Downloads` folder. In the `icacls` line, replace `DOMAIN\svc-taskdash` with the task account.

```powershell
New-Item -ItemType Directory -Force C:\TaskDash | Out-Null
icacls C:\TaskDash /setowner "*S-1-5-32-544"
icacls C:\TaskDash /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "${env:USERDOMAIN}\${env:USERNAME}:(OI)(CI)RX" "DOMAIN\svc-taskdash:(OI)(CI)M"
py -3.13 -m venv C:\TaskDash\venv
$whl = (Get-ChildItem "$env:USERPROFILE\Downloads\taskdash-*.whl" | Select-Object -First 1).FullName
$whl
C:\TaskDash\venv\Scripts\python.exe -m pip install "$whl[smtp]"
C:\TaskDash\venv\Scripts\taskdash.exe --version
```

Each `icacls` line prints `Successfully processed 1 files; Failed processing 0 files`. A new folder
under `C:\` lets every signed-in user change its files, and TaskDash runs from this folder every
hour as an administrator. The second line removes those inherited permissions. It allows only SYSTEM,
the Administrators group, the task account and you. You get Read & Execute, because an account that
can write to this folder controls the task account. To edit a file here, use an administrator
window. Step 4 checks the result, and `taskdash preflight` fails if a non-administrator can write.

The `$whl` line prints the full path of the wheel. If it prints nothing, the wheel is not in
`Downloads`. Then set the path yourself, for example `$whl = "D:\Files\taskdash-1.2.0-py3-none-any.whl"`.
Keep the original file name; pip rejects a renamed wheel, such as one with ` (1)` in its name.

The `pip install` line includes the `smtp` extra, which names the small `keyring` package. The
`arcgis` package installs `keyring` too, so the extra changes nothing today. It keeps `keyring`
installed if a later `arcgis` drops it. TaskDash reads `keyring` only when you set `smtp.user`.
Always use this line, whether or not you want mail alerts.

### Optional: locked install

The release also lists `requirements.lock`. It pins every package that TaskDash needs, with a
SHA-256 hash for each file, including `keyring` from the `smtp` extra. Use it if you want pip to
refuse any package that differs from the one the release was built with. The lock is for Python 3.13
64-bit on Windows. On any other Python, use the normal install above.

1. Download `requirements.lock` into `Downloads`.
2. Compare its SHA-256 value with the line in `SHA256SUMS.txt`, as you did for the wheel.
3. Check the wheel the same way. Do not install a file that does not match.
4. Run these commands in place of the `pip install "$whl[smtp]"` line:

```powershell
C:\TaskDash\venv\Scripts\python.exe -m pip install --require-hashes -r "$env:USERPROFILE\Downloads\requirements.lock"
C:\TaskDash\venv\Scripts\python.exe -m pip install --no-deps $whl
```

The first command stops with an error if a downloaded file does not match its hash. The second
command installs only the wheel, because the first command already installed its dependencies. The
one-window installer always installs this way. The hashes in the lock hold pip to the packages that
the release was built with, but they do not show who built the release.
The release also lists a CycloneDX software bill of materials, `taskdash-<version>.cdx.json`. It
lists the same packages. Keep it if your organization asks for one.

The install takes a few minutes. It ends with a line that starts `Successfully installed` and
names `taskdash-<version>`. Lines that start `[notice] A new release of pip is available` are
harmless; you do not need to upgrade pip. The last command prints `TaskDash <version>`, for example
`TaskDash 1.2.0`.

TaskDash keeps its config, logs and state in the folder it starts in. **Run these two lines in every
new PowerShell window before you use `$td`:**

```powershell
Set-Location C:\TaskDash
$td = "C:\TaskDash\venv\Scripts\taskdash.exe"
```

## 2. Create the config

Copy the example config into `C:\TaskDash` and open it:

```powershell
Copy-Item C:\TaskDash\venv\share\taskdash\TaskDash.config.example.json C:\TaskDash\TaskDash.config.json
notepad C:\TaskDash\TaskDash.config.json
```

The `_notes` section in the file explains every key. Change only these keys:

| Key | Set it to |
|---|---|
| `arcgis.url` | Your organization's URL, such as `https://myorg.maps.arcgis.com`. `https://www.arcgis.com` also works. Step 3 must use the same URL. |
| `arcgis.profile` | Keep `taskdash`. Step 3 stores a sign-in under this name. |
| `host_name_override` | `""`, so TaskDash uses this computer's name |
| `alerting.enabled` | Keep `false`. Set up e-mail alerts after the install works. |

Keep `arcgis.item_id`, `arcgis.view_item_id` and `arcgis.dashboard_id` empty (`""`). Step 6 fills them.
If this is a second computer for an existing dashboard, stop here and follow "Add another computer".
Keep the `privacy` switches on. They hide command lines, accounts, paths and log samples from viewers.

The other defaults are safe. For example, `scheduler.exclude_microsoft_tasks` is `true`, so the
built-in tasks under `\Microsoft\` are not monitored.

Save the file. Then check it:

```powershell
& $td preflight --offline
```

The first line must be `[ok] config: config loaded, no warnings`. If it is `[w] config: ...` or `[!!] config: ...`, the
line names each key to fix. A JSON syntax error names the line and column; a missing comma is the
usual cause. The other lines do not matter yet.

To find a mistyped key, run `& $td preflight --config-diff`. It lists the keys TaskDash ignores.

## 3. Store the ArcGIS sign-in

TaskDash never keeps your ArcGIS password in the config. It reads a saved sign-in, called a profile.
The password goes into Windows Credential Manager. Each Windows account has its own Credential
Manager, so you store the profile twice: once for yourself, and once for the task account. Use the
same ArcGIS account both times.

**For yourself,** in your administrator window, run:

```powershell
& $td signin --apply
```

The command asks for the ArcGIS user name and password. It signs in, and it checks the account and
its privileges. Type the password at the prompt; it does not echo. If a line starts with `[!!]`, the
command saves nothing. Fix the line and run the command again. Each line ends with the fix.

When every line starts with `[ok]`, the command saves the profile under the name in `arcgis.profile`.
Then it signs in with the saved profile to prove it works. If you run it again, it replaces the profile.

If `arcgis.url` is empty, press Enter at the URL prompt to accept `https://www.arcgis.com`. After the
sign-in, the command finds your organization's own URL. It writes only `arcgis.url`, after it saves a
backup of the config. It saves the profile with that same URL.

Without `--apply`, the command signs in, checks and saves nothing. You do not need that run first.

**For the task account,** stay in your own window. You do not need to sign in as that account. Run:

```powershell
& $td signin --for-task-account --task-user DOMAIN\svc-taskdash --apply
```

The command asks for the same ArcGIS password and checks the account before it writes anything.
If you set `smtp.user` in the config, it also asks for the mail password. Press Enter to skip it.

The command seals the passwords into `C:\TaskDash\taskdash_handoff.bin`. Windows seals them for this
computer, and the file's permissions allow only the task account, SYSTEM and Administrators. The next
run of TaskDash as the task account saves the sign-in into that account's own Credential Manager,
checks that it signs in, and deletes the file. The check in step 5 does this, and so does the first
run in step 8. The log line is `Stored the sign-in from taskdash_handoff.bin for DOMAIN\svc-taskdash
and removed the file.` The file stays until that run succeeds. [CONFIGURE.md](CONFIGURE.md) lists the details.

### If the hand-off fails

If `signin --for-task-account` fails, or the run that should store the sign-in fails, see "The
hand-off sign-in was kept" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md). To skip the hand-off, open a
PowerShell window as the task account. Windows asks for its password:

```powershell
runas /user:DOMAIN\svc-taskdash powershell.exe
```

In the new window, run `& $td signin` and `& $td signin --apply`, as you did for yourself. Run
`$td = "C:\TaskDash\venv\Scripts\taskdash.exe"` first, because the new window does not have `$td`.
If `runas` says the account
cannot log on here, the account lacks the "Allow log on locally" right on this computer, or a Group
Policy sets "Deny log on locally" for it. A "Deny" setting always wins over "Allow". Run
`gpresult /h C:\gp.html` and look under **User Rights Assignment** to see which applies. Ask your
domain administrator for a temporary exception for the task account while you run the commands.
Close the window when you are done. Then delete `C:\TaskDash\taskdash_handoff.bin` if it exists.

### Optional: evaluate with the demo

To see TaskDash on invented data before you set up the hourly task, follow these steps. The demo
needs only your own sign-in. It does not use the task account.

1. Do steps 1 to 3 of this guide. In step 3, do only the part "For yourself".
2. Run `& $td demo`. It prints the result as text and signs in to nothing.
3. Run `& $td demo --apply`. It creates `DEMO_` items in the ArcGIS folder `TaskDash_Demo`.
4. Open the link that follows `Open:` in the last line of the output.
5. To install TaskDash for your real tasks, continue at step 4 of this guide.
6. To delete the demo, run `& $td demo --remove --apply`.

The demo never changes your real items. See "Try the demo" in [USE.md](USE.md#try-the-demo).

## 4. Check the folder permissions

The task writes logs and state into `C:\TaskDash`, and runs the code in it. In your administrator
window, run:

```powershell
icacls C:\TaskDash
```

The output must list exactly four entries, each with `(OI)(CI)`: `NT AUTHORITY\SYSTEM` and
`BUILTIN\Administrators` with `(F)`, the task account with `(M)`, and your account with `(RX)`.
If it lists `Users` or `Authenticated Users`, or the task account is missing, run the `icacls` line
from step 1 again with the right task account name. If your account has `(F)` or `(M)`, run it again
too. Preflight fails while any non-administrator can write to the folder.

## 5. Check the computer

### Turn on the event log

TaskDash reads each task's run history from the Windows event log
`Microsoft-Windows-TaskScheduler/Operational`. Windows often ships this log turned off, or too small
to hold more than a few days. Do this first, because the preflight check below needs it:

1. In your administrator window, run the two lines from step 1 that set the folder and `$td`, if
   you opened a new window since then.
2. Run `& $td preflight --enable-event-log --apply`.
3. Check that the last line it prints says `enabled=true` and a max size of 67108864 bytes (64 MB) or more.

Run history starts at the moment the log is turned on. Runs before that do not appear. If the
`event log` line of the next check is `[!!]`, run these steps again.

### Run the preflight check

In your administrator window, run the preflight check. It changes nothing.

```powershell
& $td preflight
```

Each line starts with a mark:

- `[ok]` means the check passed.
- `[w]` means a warning. Read it. A warning never changes the exit code, and you can continue.
- `[!!]` means a problem. The line ends with the fix.
- `[--]` means the check does not apply to your config, such as `smtp` while alerting is off. It also
  means the thing to check is not set up yet, or that it needs a check that failed (`needs: credentials`).
  A `[--]` line never counts as a problem, and it never changes the exit code.

The last line counts the problems and the warnings, such as `Preflight: 3 problem(s), 0 warning(s).`
Fix every `[!!]` line before you continue. One cause shows as one `[!!]` line: a missing sign-in
fails `credentials`, and the checks that need it show `[--]`.

At this point these lines are expected as `[--]`, because steps 6 and 7 have not run yet:

- `[--] service: no arcgis.item_id configured yet. ...`
- `[--] sharing: no arcgis.item_id configured yet. ...`
- `[--] view: no arcgis.view_item_id configured ...` and `[--] dashboard: no arcgis.dashboard_id configured`
- `[--] collector logon type`, `[--] collector account` and `[--] collector install`: the collector
  task `TaskDash Collector` is not registered yet.

After step 6 sets `arcgis.item_id`, a missing collector task is a `[w]` line instead. Step 7
registers the task. These `[w]` lines are not problems:

- `base python`: the Python that made the virtual environment is under `C:\Users`, and the task
  account may not be able to run it. Install Python for all users, then run step 1 again.
- `language mode`: AppLocker or WDAC puts PowerShell in ConstrainedLanguage mode. TaskDash still
  collects. See "The language mode line warns" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- `health check`: `alerting.healthcheck_url` is empty. See "Set up an outside health check" after step 8.
- The `log roots` line: `metrics.log_roots` is empty, so TaskDash reads the logs of any folder that a task
  names. The line suggests the folders. See "Limit where TaskDash reads logs" in [CONFIGURE.md](CONFIGURE.md).
- `host name`: `host_name_override` differs from this computer's name. Clear the key, or see "Rename
  the computer" in [CONFIGURE.md](CONFIGURE.md).
- `dashboard` and `delete protection`: see "The dashboard line warns" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- `tested scale`: this computer has more tasks than the tested install, or tasks that repeat more
  often than hourly.
- `hand-off`: a sign-in hand-off file waits for the task account. The first collector run stores it.
  See "A hand-off file is waiting" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- `collector install`: the collector task starts in another folder than this install.

An `install folder` line that is `[!!]` means that Users, Authenticated Users, Everyone or another
non-administrator can write to the install folder, or that a non-administrator owns it. Run the
`icacls` command on that line in your administrator window. Step 4 shows the result.

### Run the checks as the task account

The checks above run as you. The collector task runs as the task account, which can see other files
and another Credential Manager. To see what the task account sees, without `runas`, run the check
as a temporary task. The collector task does not exist yet, so name the account with `--task-user`.
First print the plan. This changes nothing.

```powershell
& $td preflight --as-task --task-user DOMAIN\svc-taskdash
```

If the plan names the right account, run the check in your administrator window:

```powershell
& $td preflight --as-task --task-user DOMAIN\svc-taskdash --apply
```

TaskDash asks for the password of the task account. It prints the preflight output, and it deletes
the temporary task and its output file. If you used the hand-off in step 3, this is the first run as
the task account. The output shows the line that starts `Stored the sign-in` before the
`credentials` result. The collector task is not changed. After step 7, `--task-user` is optional.
The options are in [CONFIGURE.md](CONFIGURE.md). If the check fails, see "The as-task check does not
run" in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 6. Create the ArcGIS items

In your administrator window, first run setup without `--apply`. It signs in, checks that no name is taken,
and prints its plan.

```powershell
& $td setup
```

The plan lines start with `Plan:`. The last line is `DRY RUN - nothing written. Re-run with --apply.`
If setup stops with a message instead, the message names the clash and the fix.

Then create the items:

```powershell
& $td setup --apply
```

Setup does the following:

1. It backs up the config to a file named like `TaskDash.config.json.bak_setup_20261017_202238`.
2. It creates a folder named `TaskDash` in your ArcGIS content.
3. It creates the hosted service `TaskDash`, its read-only view `TaskDash_view`, a private group
   `TaskDash viewers` and the dashboard `TaskDash`.
4. It writes the three item IDs into the config. Each one prints as `Config: arcgis.<key> = <id>`.
5. It turns on delete protection for the service, the view and the dashboard.
6. It saves `taskdash_setup_manifest.json` beside the config. The file lists the items that setup
   created. It holds no secret. `taskdash remove` reads it to undo the install.

The end of the output looks like this:

```text
Open: https://myorg.maps.arcgis.com/apps/dashboards/<dashboard id>
Created by this run. To undo, run `taskdash remove`, then `taskdash remove --apply`; it turns off delete protection and deletes these:
    Folder           <id>  TaskDash
    Feature Service  <id>  TaskDash  (delete protection on)
    Feature Service  <id>  TaskDash_view  (delete protection on)
    Group            <id>  TaskDash viewers
    Dashboard        <id>  TaskDash  (delete protection on)
Manifest of the created items saved to C:\TaskDash\taskdash_setup_manifest.json. `taskdash remove` reads it.
Setup complete. The service is private; the view and dashboard are shared with the group 'TaskDash viewers' only. Add members to that group to give them access.
```

Keep the `Open:` link. Step 9 uses it.

To use other names, pass `--folder`, `--title-prefix` or `--group-title`.

If setup stops partway, it still lists what it created. To recover:

1. Run `& $td remove`, then `& $td remove --apply`, as Undo step 1 describes. Setup saves the
   manifest even when it stops partway. If you prefer, turn off delete protection in ArcGIS Online
   on the items marked in that list. Then delete only the items in that list, by hand.
2. Copy the newest `TaskDash.config.json.bak_setup_*` file over `TaskDash.config.json`.
3. Run `& $td setup --apply` again.

## 7. Register the hourly task

In your administrator window, run the installer in check mode. `-Python` names the `taskdash.exe` that the task runs.

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\TaskDash\venv\share\taskdash\install_task.ps1 `
    -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe -User DOMAIN\svc-taskdash
```

A wheel install, which this guide describes, has no `run_taskdash.cmd`, and the wheel does not ship
it. TaskDash keeps `run_taskdash.cmd` on purpose, as the wrapper of a folder install, which runs
`taskdash_publish.py` from a copy of the code. Without `-Python`, `install_task.ps1` runs the wrapper
if the folder holds one. Otherwise it runs `<InstallDir>\venv\Scripts\taskdash.exe`.

The TaskDash PowerShell scripts are not signed. If Group Policy sets the execution policy to
`AllSigned`, `-ExecutionPolicy Bypass` cannot override it and PowerShell refuses the script. Ask
your IT department for an exception for this one file.

It registers nothing. It prints the task it would register:

```text
Task name:         TaskDash Collector
Runs as:           DOMAIN\svc-taskdash (Password)
Command:           C:\TaskDash\venv\Scripts\taskdash.exe collect --apply
Working directory: C:\TaskDash
Trigger:           every 1 hour from <next full hour>, no end
Limits:            2 hour run limit, no second instance, catch up a missed run
Needs:             DOMAIN\svc-taskdash in the local Administrators group; a standard account misses other accounts' tasks
CHECK MODE - nothing registered. Re-run with -Apply.
```

Check that `Runs as` names your task account and that the working directory is `C:\TaskDash`. Then
run the same command with ` -Apply` added at the end. The script asks for the task account's
password and prints `Registered 'TaskDash Collector'. Principal.LogonType = Password`.

The script uses the Password logon type. It refuses S4U ("Do not store password"), because an S4U
task cannot read the saved sign-in from step 3.

With `-Apply`, the script stops before it registers anything if the window is not elevated, or if the
task account is not in the local Administrators group. Check mode prints the same problem as a
`WARNING` and still exits 0. If the account belongs to the group only through another group, the
script warns and continues, because it cannot read nested membership.

Two optional switches save the manual commands in step 5 and step 8. Both only print their plan in
check mode:

| Switch | What it does with `-Apply` |
|---|---|
| `-EnableEventLog` | Runs `taskdash preflight --enable-event-log --apply` before it registers the task. It uses the `taskdash.exe` from `-Python`, or `<InstallDir>\venv\Scripts\taskdash.exe`. If that command fails, nothing is registered. |
| `-StartNow` | Starts the task once after it registers, waits up to 10 minutes, and prints what `LastTaskResult` means. It exits with an error unless the result is `0`. |

For example, this command does steps 5 (event log), 7 and 8 in one run:

```powershell
powershell.exe -ExecutionPolicy Bypass -File C:\TaskDash\venv\share\taskdash\install_task.ps1 `
    -InstallDir C:\TaskDash -Python C:\TaskDash\venv\Scripts\taskdash.exe -User DOMAIN\svc-taskdash `
    -EnableEventLog -StartNow -Apply
```

Run it in check mode first, without `-Apply`. If `-StartNow` ends with an error, the task stays
registered. Use the `LastTaskResult` list in step 8 to find the cause.

### Why the task account must be an administrator

- **It must see every task.** Windows shows a standard account only some tasks. In a test, a
  standard account saw 170 of 229 tasks: none that other accounts had registered. Those tasks would
  be missing from the dashboard, and nothing would report an error.
- **It must log on as a batch job.** A task with a stored password logs on this way. By default,
  only Administrators, Backup Operators and Performance Log Users hold the right. Without it, the
  task never starts. `LastTaskResult` stays `267011` (0x41303, "has not yet run"), and Task
  Scheduler logs event 104 with error value `2147943785`.

To check the membership, run `net localgroup Administrators` in the administrator window. The list
must name the task account, or a domain group that contains it. To add the account, run
`net localgroup Administrators DOMAIN\svc-taskdash /add`.

## 8. Run it once and check

In the administrator window, start the task now instead of waiting for the hour:

```powershell
Start-ScheduledTask -TaskName "TaskDash Collector"
```

The first run takes one to five minutes. Repeat these two commands until the state is `Ready`:

```powershell
(Get-ScheduledTask -TaskName "TaskDash Collector").State
(Get-ScheduledTaskInfo -TaskName "TaskDash Collector").LastTaskResult
```

What `LastTaskResult` means:

- `267009` (0x41301): the task is still running. Wait and repeat.
- `0`: the run succeeded.
- `1`: TaskDash ran and stopped with an error. The newest file in `C:\TaskDash\Logs` names it in
  its last line.
- `267011` (0x41303): the task never started. See "Why the task account must be an
  administrator" in step 7.
- `2147943726` (0x8007052E): Windows rejected the task account's password. Run step 7 again with
  `-Replace -Apply` added, and type the right password. [ROTATE.md](ROTATE.md) describes it.
- Any other value: read the task's **History** tab in Task Scheduler, as described below.

If the state is still `Running` after 10 minutes, open the newest log file in `C:\TaskDash\Logs`
and read its last lines. If no log file has today's time, TaskDash did not start. Then open Task
Scheduler, select the task, and read the **History** tab. Event 101, 103 or 104 names the reason,
such as a wrong password.

When the run succeeded, check the result in your administrator window:

```powershell
& $td status
& $td preflight
```

`status` prints two lines like these:

```text
Local: taskdash_20261017_202547.log: <time> - INFO - TaskDash publish complete.
Service: last collector run started 2026-10-17 20:25:47 UTC, exit_status success; last success 47 min ago (host 'COLLECTOR-1')
```

The exit code of `status` is `0` when the newest run succeeded and is fresh. See [USE.md](USE.md) for the other codes.

Preflight now shows `[ok]` for the service, the view, sharing, the collector logon type and the
collector account. The collector account line checks that the task account is in the local
Administrators group. If that line starts `[w] collector account: WARNING:`, the account is not a
direct member, and preflight names the groups that may include it. Confirm that one of them does.

In the Task health table, the collector's own row reads **Running** until the second hourly run. It
is not hung: the collector is its own task. It publishes its row before its own run ends, so its first
run has no finished result to show yet.

If the collector logon type line still says `WARNING: collector task 'TaskDash Collector' is not
registered, or this window cannot see it`, this window may not be allowed to see a task that
runs as another account. Run `& $td preflight --offline` in the administrator window. That line must
be `[ok] collector logon type: task 'TaskDash Collector' logs on as Password`. If it still warns, set
`scheduler.collector_task_name` to the real name of the task.

### Set up an outside health check

If the collector never starts, nothing inside TaskDash can tell you. An expired password or a
disabled task causes this. Only an outside monitor notices. Create a check at a service such as
Healthchecks.io, then put its ping address in `alerting.healthcheck_url`. Use one check for each
computer: two computers that share an address hide a dead one. The
"Watch for a silent collector" section of [CONFIGURE.md](CONFIGURE.md) explains the pings. The address holds a
secret ID, so keep it out of tickets and chat. Run `& $td preflight`. The `health check` line then
says `alerting.healthcheck_url is set` and does not show the address.

## 9. Open the dashboard and add viewers

Open the `Open:` link from step 6. To find the dashboard again, open ArcGIS Online, select
**Content**, then open the `TaskDash` folder. The first run fills the health tables. The run history
fills as far back as the Windows event log goes, and the trends fill as tasks run. "The first hour and
the first day" in [USE.md](USE.md) lists when each tile fills.

To give people access, add them to the `TaskDash viewers` group. In ArcGIS Online, select
**Groups**, open `TaskDash viewers`, and select **Invite members**. Members can open the dashboard
and read the view. Nobody else can, and nobody can edit the data through the view. Viewers need an
ArcGIS Online account in your organization. See "Give people access" in
[CONFIGURE.md](CONFIGURE.md#give-people-access).

## Add another computer

One dashboard can show several computers. Each computer runs its own collector task and publishes to
the same service. Do not run `taskdash setup` on the second computer. Setup makes a second service,
a second view, a second dashboard and a second group, and it refuses when it finds the first ones.

Do these steps on the second computer. The quick install runs setup when `arcgis.item_id` is empty,
so use the numbered steps of this guide.

1. Do steps 1, 3, 4 and 5. In step 3, sign in as the same ArcGIS account that ran setup on the first
   computer. That account owns the items.
2. In step 2, copy the config of the first computer instead of the example. The file holds the ids
   of your items, so copy it by a trusted route and keep it out of tickets and chat. Then change it:
   - Keep `arcgis.url`, `arcgis.item_id`, `arcgis.view_item_id` and `arcgis.dashboard_id` as they are.
   - Set `host_name_override` to `""`, so TaskDash uses the name of this computer.
   - Set `alerting.healthcheck_url` to a new check for this computer, or to `""`.
   - Keep the `privacy`, `retention` and `scheduler` values the same as on the first computer.
     "Run TaskDash on several computers" in [CONFIGURE.md](CONFIGURE.md) lists them.
3. Skip step 6. The service, the view, the dashboard and the group exist.
4. Do steps 7 and 8. The new computer then shows in the **Host** filter of the dashboard.

The first run of the second computer reads its own event history. Its tasks and runs appear under
its own name.

## Undo the install

Run the steps in this order. The sign-in commands need the collector task, because they read the
task account from it.

Before step 1, open PowerShell as administrator and disable the collector task. Step 1 deletes the
service and empties the ids in the config. An hourly run that starts then fails and can send a
failure alert. A disabled task stays registered, so the sign-in commands still work.

```powershell
Disable-ScheduledTask -TaskName "TaskDash Collector"
```

1. Delete the ArcGIS items. Sign in as the account that ran setup. These items are shared: if
   another computer still publishes to the service, deleting them erases its history and its next
   runs fail. `remove --apply` refuses while another computer has a recent run. First run the
   check. It lists what it would delete and changes nothing. If the list is right, run it again
   with `--apply`:

   ```powershell
   & $td remove
   & $td remove --apply
   ```

   The command reads `taskdash_setup_manifest.json`, which setup saved beside the config. It deletes
   only the items in that file, only the ones you own, and only the ones that still exist. It turns
   off delete protection on each item first. It prints and skips any item it cannot delete. The
   order is the dashboard, the group, the view, the service, and then the folder if it is empty.
   After the deletes, it saves a backup of the config and empties `arcgis.item_id`,
   `arcgis.view_item_id` and `arcgis.dashboard_id` for the items it deleted. A new install then
   creates new items instead of reporting that the old ones exist.

   If setup ran before version 1.2, or the manifest is lost, delete by hand instead. Check first
   that no other computer publishes to the service. In ArcGIS
   Online, turn off delete protection on the dashboard, the view and the service. Then delete in
   this order: the `TaskDash` dashboard, the `TaskDash_view` view, the `TaskDash` service, the
   `TaskDash viewers` group, and then the empty `TaskDash` folder. Delete the view before the
   service, because the view reads the service's data. Then empty the ids in the config by hand.
2. Remove the saved sign-in and the saved mail password of each Windows account that stored them
   in step 3. In your administrator window, run the check and then the delete. Each command lists what it
   finds and asks for no password:

   ```powershell
   & $td signin --forget
   & $td signin --forget --apply
   ```

   For the task account, open PowerShell as administrator and run the two lines from step 1. The
   first command prints the plan and changes nothing. The second asks for the task account's
   password, runs the same command as that account in a temporary task, and deletes the task:

   ```powershell
   & $td signin --forget --for-task-account
   & $td signin --forget --for-task-account --apply
   ```

   If this fails, open a `runas` window as the task account. Run `& $td signin --forget --apply`
   there, after you set `$td` again.
3. In the administrator window, remove the collector task:

   ```powershell
   Unregister-ScheduledTask -TaskName "TaskDash Collector" -Confirm:$false
   ```

4. Uninstall the package from the same environment:

   ```powershell
   & "C:\TaskDash\venv\Scripts\python.exe" -m pip uninstall taskdash
   ```

5. Close every window that is in `C:\TaskDash`. Then delete the `C:\TaskDash` folder. This also
   removes `taskdash_handoff.bin`, the sealed sign-in, if it still waits for the task's first run.

The undo leaves three changes in place, because other software may use them:

- The larger, enabled Task Scheduler event log from step 5. It is harmless to keep.
- The task account's membership of the Administrators group. Remove it if nothing else needs it.
- Python. Uninstall it in **Settings > Apps** if nothing else needs it.

## Use a cloned ArcGIS Pro environment instead

If the computer has ArcGIS Pro, you can install TaskDash into a clone of Pro's Python environment
instead of the virtual environment of step 1. Pro already contains `arcgis` and `keyring`. In
step 1, run these commands in place of the `py -3.13 -m venv` and `pip install` lines. The clone
takes a few minutes and about 2 GB.

```powershell
& "C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\conda.exe" create --clone arcgispro-py3 -p C:\TaskDash\proenv --yes
Test-Path C:\TaskDash\proenv\python.exe
C:\TaskDash\proenv\python.exe -m pip install --no-deps $whl
C:\TaskDash\proenv\Scripts\taskdash.exe --version
```

- `Test-Path` must print `True`. A clone into a deep folder path can stop partway and still
  report success. `C:\TaskDash\proenv` is short enough, and the task account can read it.
- Use `--no-deps`. Without it, pip replaces packages that conda manages in the clone, such as
  `cffi` and `pywin32`, and the clone can break.
- Replace `C:\TaskDash\venv\Scripts\python.exe` with `C:\TaskDash\proenv\python.exe` wherever it
  appears in this guide, in [ROTATE.md](ROTATE.md) and in [UPGRADE.md](UPGRADE.md). Then replace every
  other `venv` in a path with `proenv`. That covers `venv\Scripts\taskdash.exe`, including the `$td`
  line of step 1, and `venv\share\taskdash`, the folder of `install_task.ps1`. The `$whl` line needs
  no change. In step 7, pass the same `taskdash.exe` as `-Python`.

## Known limits

- Several computers can publish to one dashboard. See "Add another computer". Each one needs its own
  install and its own task.
- The dashboard shows run times in the time zone of the viewer's device.
- ArcGIS Online only. ArcGIS Enterprise is not tested.
- Scrubbing secrets from command lines and log samples is best effort. Keep the privacy switches on.

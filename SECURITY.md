# Security policy

## Report a vulnerability

Report a vulnerability privately. Do not open an issue, because issues are visible to everyone with
access to the repository.

1. If the repository's **Security** tab offers **Report a vulnerability**, select it and describe the
   problem there.
2. If it does not, contact the repository owner through GitHub and ask for a private channel. Do not
   put the details in the first message.

Include the TaskDash version (`taskdash --version`), the steps that show the problem and what an
attacker gains. Do not include real passwords, tokens, host names or log contents. Use made-up
values.

The maintainer reads reports as time allows. TaskDash has no service-level agreement.

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes |
| 0.9.x and earlier | No |

Install the newest 1.x release. A fix ships as a new 1.x release.

## What TaskDash does to protect data

TaskDash reads every task on a computer, and tasks often carry passwords in their command lines.
These controls limit what leaves the computer and who can read it.

- **Read-only view.** The dashboard reads `TaskDash_view`, not the hosted service. The view allows
  queries only and cannot be edited. Setup creates it with the Query capability.
- **Owner-only service.** Setup never shares the editable service. If the service is shared with
  everyone, TaskDash refuses to create the view. Once a view is configured, it also refuses to
  publish, and it warns when the service is shared with the organization or a group. In both cases,
  `taskdash preflight` fails until the service is private again.
- **Private viewers group.** Setup shares the view and the dashboard only with the group
  `TaskDash viewers`. The group is private and by invitation. Setup never shares anything with
  everyone or with the organization.
- **Anonymous check.** `taskdash preflight` queries every table of the service and the view with no
  token. It fails unless every table refuses. `taskdash setup --apply` runs the same check at its end.
  `taskdash collect --check-anonymous` runs it alone and exits with 1 on a failure.
- **Privacy switches.** The `privacy` settings hide command lines, log sample lines, account names and
  paths. They are on by default, and a missing switch counts as on. TaskDash blanks the values before
  it publishes them, and the view hides the same fields.
- **Secret scrubbing, best effort.** TaskDash always removes secrets from the free text it
  publishes. This text is command lines, task arguments, descriptions, setting notes, change
  summaries, error text and log samples. It also removes secrets from the log lines in alert e-mails. It recognizes password
  flags, `key=value` pairs, authorization headers, encoded commands and URLs that contain a
  password. A secret in a form it does not recognize can pass. Scrubbing is not a reason to turn off
  the privacy switches. `taskdash collect --scrub-history` counts the stored rows that the current
  rules would change. With `--apply`, it rewrites them.
- **No plaintext ArcGIS password.** The install guide signs in with a saved profile,
  `arcgis.profile`. The password sits in the Windows Credential Manager of the account that created
  the profile, not in the config. Without a profile, `arcgis.username_env` and `arcgis.password_env`
  can name environment variables instead. If the config holds `arcgis.password` and no profile,
  TaskDash stops. To accept that risk, set `arcgis.allow_plaintext_password` to `true`.
- **Mail password in the Windows keyring.** The `smtp` section of the config holds no password.
  If `smtp.user` is set, TaskDash reads its password from the Windows keyring. If the connection is
  also unencrypted, TaskDash refuses to send the password. To accept that risk, set
  `smtp.allow_plaintext_auth` to `true`.
- **Locked install folder.** The install guide removes inherited permissions from `C:\TaskDash`.
  Only SYSTEM, the Administrators group, the installing user and the task account can open it.
  `icacls C:\TaskDash` shows the result.
- **Nothing is written without `--apply`.** Without `--apply`, `collect`, `setup` and `dashboard`
  write nothing to ArcGIS Online, and `collect` sends no e-mail. `preflight --enable-event-log`
  changes the event log only with `--apply`. Setup never updates an item it did not create in the
  same run.

## What TaskDash does not protect

- The task account is a local administrator. Protect that account like any administrator account.
- Anyone who can read `C:\TaskDash` can read the config, the logs and the saved state.
- Members of the viewers group can read everything the view shows. Add only people who may see your
  task names, results and metric values.
- An ArcGIS organization administrator can read every item in the organization.

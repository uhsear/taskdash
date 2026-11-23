# Install TaskDash

TaskDash watches the scheduled tasks on one Windows computer and publishes their health to ArcGIS
Online. These files are everything you need. The guides are here too, so you can read them before
the install.

1. Download `taskdash-docs-<version>.zip`, unzip it, and read `docs\INSTALL.md`. Give
   `docs\APPROVAL.md` to your IT or security team first if they must approve the install.
2. Download `Install-TaskDash.ps1`, `taskdash-<version>-py3-none-any.whl`, `requirements.lock` and
   `SHA256SUMS.txt` into one folder. The installer checks the wheel and the lock against
   `SHA256SUMS.txt` and stops if one is missing or differs.
3. Open PowerShell as administrator. Run the check command from the Quick install section of
   `INSTALL.md`. It changes nothing. When it passes, run the same command with `-Apply`.

The `.sigstore.json` files let you check who built the wheel and `SHA256SUMS.txt`. `INSTALL.md`
explains how. The `.cdx.json` file is the software bill of materials.

To upgrade or roll back, follow `docs\UPGRADE.md`. To report a vulnerability, follow `SECURITY.md`.

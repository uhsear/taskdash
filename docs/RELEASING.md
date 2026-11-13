# Release checklist

This checklist is for the TaskDash maintainer. It lists every step from the version bump to the check
of the public release page. Work through the steps in order, and stop at the first step that fails.

## How the release moves

A release passes through two repositories:

1. The **private build repository** runs the workflows in `.github/workflows`. A `v*` tag there
   runs `release.yml`. That workflow tests, builds, signs and publishes a private release.
2. The **public release repository**, `uhsear/taskdash-releases`, is where users download. The
   workflow does not write to it, because that would need a token for the second repository. The
   maintainer copies the files by hand.

`_tools/export_public.py` does not export to the public repository. Its name is older than the
split. It writes the committed files to a folder for the private build repository.

## 1. Prepare the version

1. Choose the version number in PEP 440 form: `1.2.0` for a release, `1.2.0rc1` for a candidate.
2. Set `TASKDASH_VERSION` in `taskdash_common.py` to that number. A test rejects any other form.
3. Move the `[Unreleased]` entries of `CHANGELOG.md` under the new version and date them.
   `CHANGELOG.md` is the one full set of release notes. The summary in `docs/INTRODUCTION.md` links
   to it. The release body holds `.github/release-notes.md` and the `CHANGELOG.md` section of the
   version, so write that section for readers. Open it with an **Upgrade notes** block when the
   release has upgrade steps: the schema change, the commands to run and the behaviour changes.
4. Run the full suite and fix every failure:

   ```powershell
   python -m pytest -q -p no:cacheprovider
   ```

5. Commit the changes to the main branch.

## 2. Export to the build repository

1. Run the export as a check. It lists what it would write and what it excludes:

   ```powershell
   python _tools/export_public.py --out C:\Temp\taskdash-export
   ```

2. Run it again with `--apply` and an empty output folder. Add `--private-list` with the path of
   your private deny list. The script then scans the output, and a deny-list hit makes it exit 1.
3. Read the credential-pattern lines the script prints. They are a list to review, not a gate.
4. Copy the output into a clone of the build repository, commit it and push it. The script never
   runs `git commit` or `git push`.

## 3. Tag and build

1. Tag the commit in the build repository. For a release candidate, write the tag as `v1.2.0-rc1`.
   The workflow turns it into the version `1.2.0rc1`, and publishes a pre-release.

   ```powershell
   git tag v1.2.0
   git push origin v1.2.0
   ```

2. Wait for the `release` workflow. Its jobs are `test`, `matrix`, `wheel`, `build`, `sign` and
   `publish`. `publish` starts only after the full CI matrix passes.
3. If the check of the tag against the package version fails, fix `TASKDASH_VERSION` or the tag. Delete
   the tag in the build repository, then tag again.

## 4. Copy the release to the public repository

1. Download every file of the private release into an empty folder: the wheel, `requirements.lock`,
   the `.cdx.json` SBOM, `Install-TaskDash.ps1`, the `taskdash-docs-<version>.zip` of the guides,
   `SHA256SUMS.txt` and the `.sigstore.json` bundles.
2. Create a release with the same tag in `uhsear/taskdash-releases`. Attach every file from the
   folder, unchanged. Mark a candidate as a pre-release.
3. Give the release the title of the private release. Its body can link to `CHANGELOG.md` and
   `docs/UPGRADE.md`, so the notes stay in one place.

## 5. Verify the public copy

Do this from a clean folder, with the files you download from the public release page. Do not use
the copies from step 4.

1. Compare each file with `SHA256SUMS.txt`:

   ```powershell
   Get-FileHash .\taskdash-*.whl -Algorithm SHA256
   ```

2. Check the signature of the wheel and of `SHA256SUMS.txt`, as `docs/INSTALL.md` describes. Each
   command must print `OK:`. The identity names the private build repository, not the public one.
3. Run the installer in check mode with the downloaded wheel. It must report that the SHA-256 matches
   `SHA256SUMS.txt`, and it must create nothing:

   ```powershell
   .\Install-TaskDash.ps1 -InstallDir C:\TaskDash -Wheel .\taskdash-<version>-py3-none-any.whl -TaskAccount DOMAIN\svc-taskdash
   ```

4. If any check fails, delete the public release and do not announce it. Find the cause in the
   build repository first.

## 6. After the release

1. Update the "not yet released" lines in `CHANGELOG.md` and `docs/INTRODUCTION.md` to the release date.
2. Open a new `[Unreleased]` section in `CHANGELOG.md`.

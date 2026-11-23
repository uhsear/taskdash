# Release checklist

This checklist is for the TaskDash maintainer. It lists every step from the version bump to the check
of the public release page. Work through the steps in order, and stop at the first step that fails.

## How the release moves

The public repository `uhsear/taskdash` holds the code and the releases:

1. `_tools/export_public.py` writes the committed files to a folder. It leaves out private files
   and scans the output against the deny list. Only that folder is pushed to the repository.
2. A `v*` tag in the repository runs `release.yml`. That workflow tests, builds, signs and
   publishes the release on the repository's release page, where users download it.

Releases up to `v1.1.0` were also copied by hand to `uhsear/taskdash-releases`, which is now
removed. That copy step is gone: the release workflow publishes to the public repository itself.

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

## 2. Export to the repository

1. Run the export as a check. It lists what it would write and what it excludes:

   ```powershell
   python _tools/export_public.py --out C:\Temp\taskdash-export
   ```

2. Run it again with `--apply` and an empty output folder. Add `--private-list` with the path of
   your private deny list. The script then scans the output, and a deny-list hit makes it exit 1.
3. Read the credential-pattern lines the script prints. They are a list to review, not a gate.
4. Copy the output into a clone of `uhsear/taskdash`, commit it and push it. The script never
   runs `git commit` or `git push`.

## 3. Tag and build

1. Tag the commit in the repository. For a release candidate, write the tag as `v1.2.0-rc1`.
   The workflow turns it into the version `1.2.0rc1`, and publishes a pre-release.

   ```powershell
   git tag v1.2.0
   git push origin v1.2.0
   ```

2. Wait for the `release` workflow. Its jobs are `test`, `matrix`, `wheel`, `build`, `sign` and
   `publish`. `publish` starts only after the full CI matrix passes.
3. If the check of the tag against the package version fails, fix `TASKDASH_VERSION` or the tag. Delete
   the tag in the repository, then tag again.

## 4. Verify the release

Do this from a clean folder, with the files you download from the release page.

1. Compare each file with `SHA256SUMS.txt`:

   ```powershell
   Get-FileHash .\taskdash-*.whl -Algorithm SHA256
   ```

2. Check the signature of the wheel and of `SHA256SUMS.txt` with their `.sigstore.json` bundles, as
   `docs/INSTALL.md` describes. Each
   command must print `OK:`.
3. Run the installer in check mode with the downloaded wheel. It must report that the SHA-256 matches
   `SHA256SUMS.txt`, and it must create nothing:

   ```powershell
   .\Install-TaskDash.ps1 -InstallDir C:\TaskDash -Wheel .\taskdash-<version>-py3-none-any.whl -TaskAccount DOMAIN\svc-taskdash
   ```

4. If any check fails, delete the release and do not announce it. Find the cause first.

## 5. After the release

1. Update the "not yet released" lines in `CHANGELOG.md` and `docs/INTRODUCTION.md` to the release date.
2. Open a new `[Unreleased]` section in `CHANGELOG.md`.

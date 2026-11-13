"""The GitHub workflows are the release gate, so their integrity properties are asserted as text."""
import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent / ".github" / "workflows"
FILES = ["install.yml", "release.yml"]


def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _jobs(text):
    """Map job name to its block of text. Jobs sit at two-space indent under `jobs:`."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


@pytest.mark.parametrize("name", FILES)
def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment(name):
    uses = [line.strip() for line in _text(name).splitlines() if re.match(r"\s*-?\s*uses:", line)]
    assert uses
    for line in uses:
        if re.match(r"uses: \./\.github/workflows/[\w.-]+\.yml$", line):
            continue  # a workflow of this repository is pinned by the tag's own commit
        assert re.search(r"uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", line), line


@pytest.mark.parametrize("name", FILES)
def test_every_checkout_drops_the_token_from_the_clone(name):
    text = _text(name)
    checkouts = text.count("uses: actions/checkout@")
    assert checkouts >= 1
    assert text.count("persist-credentials: false") == checkouts


@pytest.mark.parametrize("name", FILES)
def test_top_level_permissions_are_read_only(name):
    text = _text(name)
    assert re.search(r"^permissions:\n  contents: read\n(?!  )", text, flags=re.M)


def test_only_the_publish_job_can_write_and_it_waits_for_the_tests_the_matrix_the_build_and_the_signing():
    text = _text("release.yml")
    assert "contents: write" not in _text("install.yml")
    jobs = _jobs(text)
    assert set(jobs) == {"test", "matrix", "wheel", "build", "sign", "publish"}
    assert [n for n, body in jobs.items() if "contents: write" in body] == ["publish"]
    assert text.count("contents: write") == 1
    assert re.search(r"needs: \[test, matrix, build, sign\]", jobs["publish"])
    assert re.search(r"needs: \[?build\]?", jobs["sign"])
    assert re.search(r"needs: \[?wheel\]?", jobs["build"])
    assert re.search(r"needs: \[?test\]?", jobs["wheel"])
    assert "gh release create" in jobs["publish"] and "gh release create" not in jobs["build"]


def test_release_runs_the_same_full_suite_as_the_install_workflow():
    command = "python -m pytest -q -p no:cacheprovider"
    assert command in _text("install.yml")
    assert command in _jobs(_text("release.yml"))["test"]


def test_release_hands_the_wheel_and_checksum_to_publish_as_pinned_artifacts():
    jobs = _jobs(_text("release.yml"))
    assert "actions/upload-artifact@" in jobs["build"]
    assert "actions/download-artifact@" in jobs["publish"]
    assert "SHA256SUMS.txt" in jobs["build"]
    assert '`n")' in jobs["build"]  # LF line endings, see the comment in the workflow


def test_only_the_sign_job_gets_an_oidc_token_and_it_signs_the_wheel_and_checksums_with_sigstore():
    # Artifact attestations need a public repository or GitHub Enterprise Cloud, so release
    # provenance is a keyless Sigstore signature made with the job's GitHub OIDC identity.
    # The token lives in a job of its own, so the build tools (build, pip-tools, cyclonedx-bom)
    # and their unpinned dependencies never run where they could request it.
    text = _text("release.yml")
    assert "id-token" not in _text("install.yml")
    assert text.count("id-token: write") == 1
    jobs = _jobs(text)
    assert [n for n, body in jobs.items() if "id-token: write" in body] == ["sign"]
    build_code = "\n".join(l for l in jobs["build"].splitlines() if not l.strip().startswith("#"))
    assert "sigstore" not in build_code.lower()
    sign = jobs["sign"]
    assert re.search(r'pip install "sigstore==\d+\.\d+\.\d+"', sign)
    assert len(re.findall(r"pip install", sign)) == 1
    assert "actions/checkout@" not in sign, "the signing job needs no source tree"
    assert sign.index("actions/download-artifact@") < sign.index("python -m sigstore sign")
    command = re.search(r"python -m sigstore sign (.+)", sign)
    assert command and "SHA256SUMS.txt" in command.group(1) and "$wheel" in command.group(1)
    assert sign.index("python -m sigstore sign") < sign.index("actions/upload-artifact@")


def test_every_pip_install_in_the_release_workflow_is_pinned():
    text = _text("release.yml")
    lines = [line.strip() for line in text.splitlines() if re.search(r"-m pip install", line)]
    assert lines
    for line in lines:
        # The wheel and the lock are installed from files built or hashed in the same job.
        assert re.search(r"==\d|>=2\.4,<3|--require-hashes|--no-deps", line), line


def test_the_install_workflow_pins_the_build_tool_to_the_release_workflows_version():
    pins = {re.search(r'pip install "build==([\d.]+)"', _text(name)).group(1) for name in FILES}
    assert len(pins) == 1


def test_the_publish_job_attaches_the_sigstore_bundles_it_downloads_from_the_sign_job():
    jobs = _jobs(_text("release.yml"))
    assert "dist/*.sigstore.json" in jobs["sign"]
    assert jobs["publish"].count("actions/download-artifact@") == 2
    release = re.search(r"gh release create .+", jobs["publish"]).group(0)
    assert r"dist\*.sigstore.json" in release


def test_build_job_makes_a_hash_pinned_lock_with_pinned_pip_tools():
    jobs = _jobs(_text("release.yml"))
    build = jobs["build"]
    assert re.search(r'pip install "build==\d+\.\d+\.\d+"', jobs["wheel"])
    assert re.search(r'pip install "pip-tools==\d+\.\d+\.\d+"', build)
    compile_line = re.search(r"python -m piptools compile .+", build).group(0)
    for flag in ("--generate-hashes", "--strip-extras", "--extra smtp", "pyproject.toml"):
        assert flag in compile_line, flag
    assert "requirements.lock" in compile_line
    assert build.index("piptools compile") < build.index("actions/upload-artifact@")


def test_build_job_makes_a_cyclonedx_sbom_from_a_clean_venv_with_the_wheel_installed():
    build = _jobs(_text("release.yml"))["build"]
    assert re.search(r'pip install "cyclonedx-bom==\d+\.\d+\.\d+"', build)
    assert "-m venv" in build
    # The venv gets the locked packages first, then the wheel without a second resolve.
    assert build.index("--require-hashes -r") < build.index("--no-deps")
    sbom = re.search(r"cyclonedx-py environment .+", build).group(0)
    assert "--of JSON" in sbom and ".cdx.json" in sbom
    assert build.index("-m venv") < build.index("cyclonedx-py environment")


def test_lock_and_sbom_are_in_the_checksums_the_artifact_and_the_release():
    jobs = _jobs(_text("release.yml"))
    build = jobs["build"]
    # The checksum file is written after both files exist, and it covers both.
    assert build.index("piptools compile") < build.index("WriteAllText")
    assert build.index("cyclonedx-py environment") < build.index("WriteAllText")
    listing = re.search(r"\$lines = Get-ChildItem (.+) \|", build).group(1)
    assert r"dist\requirements.lock" in listing and r"dist\*.cdx.json" in listing
    assert "dist/requirements.lock" in build and "dist/*.cdx.json" in build
    # The sign job signs SHA256SUMS.txt, so the build must upload it, and the sign job needs the build.
    assert "dist/SHA256SUMS.txt" in build
    release = re.search(r"gh release create .+", jobs["publish"]).group(0)
    assert r"dist\requirements.lock" in release and r"dist\*.cdx.json" in release


def test_the_one_window_installer_is_a_release_asset_and_in_the_checksums():
    # INSTALL.md tells the user to download Install-TaskDash.ps1 from the release page, so the
    # workflow must publish it and SHA256SUMS.txt (which the sign job signs) must list it.
    jobs = _jobs(_text("release.yml"))
    build = jobs["build"]
    assert re.search(r"Copy-Item Install-TaskDash\.ps1 dist\\", build)
    assert build.index("Copy-Item Install-TaskDash.ps1") < build.index("WriteAllText")
    listing = re.search(r"\$lines = Get-ChildItem (.+) \|", build).group(1)
    assert r"dist\Install-TaskDash.ps1" in listing
    assert "dist/Install-TaskDash.ps1" in build
    release = re.search(r"gh release create .+", jobs["publish"]).group(0)
    assert r"dist\Install-TaskDash.ps1" in release


def test_install_guide_documents_the_locked_install():
    guide = (WORKFLOWS.parent.parent / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    for needle in ("python.exe -m pip install --require-hashes -r", "requirements.lock",
                   "--no-deps", ".cdx.json", "Python 3.13", "64-bit"):
        assert needle in guide, needle


def test_install_guide_documents_the_sigstore_verification_of_the_release_files():
    guide = (WORKFLOWS.parent.parent / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    for needle in ("python -m pip install sigstore", "python -m sigstore verify github",
                   "--cert-identity", "https://github.com/uhsear/taskdash/.github/workflows/release.yml@refs/tags/",
                   ".sigstore.json"):
        assert needle in guide, needle


def test_docs_point_downloads_at_the_public_release_repository():
    root = WORKFLOWS.parent.parent
    guide = (root / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "https://github.com/uhsear/taskdash-releases/releases/latest" in guide
    for path in [root / "README.md", *(root / "docs").glob("*.md")]:
        text = path.read_text(encoding="utf-8")
        assert "uhsear/taskdash/releases" not in text, path.name
        assert "repository is private" not in text, path.name
        assert "ask the TaskDash maintainer for" not in text, path.name
        assert "Readers are invited" not in text, path.name


def test_security_policy_says_the_signing_identity_is_public():
    policy = (WORKFLOWS.parent.parent / "SECURITY.md").read_text(encoding="utf-8")
    assert "transparency log" in policy and "Sigstore" in policy


def test_release_header_says_the_public_copy_is_manual():
    header = _text("release.yml").split("\nname:", 1)[0]
    assert "uhsear/taskdash-releases" in header and "maintainer" in header


def test_test_dependencies_pin_the_arcgis_major_version():
    assert 'pip install "arcgis>=2.4,<3" pytest' in _text("install.yml")


def test_gitleaks_archive_is_verified_against_its_checksums_before_extraction():
    step = _jobs(_text("install.yml"))["secrets-scan"]
    assert "checksums.txt" in step
    assert step.index("sha256sum -c") < step.index("tar -xz")


def test_the_guides_are_a_checksummed_release_asset_and_the_notes_come_from_a_file():
    # The code repository is private, so the release page is the only place a reader finds INSTALL.md.
    jobs = _jobs(_text("release.yml"))
    build = jobs["build"]
    assert re.search(r"Compress-Archive -Path docs, README.md, SECURITY.md, CHANGELOG.md -DestinationPath \"dist\\taskdash-docs-\$version.zip\"", build)
    assert build.index("Compress-Archive") < build.index("WriteAllText")
    listing = re.search(r"\$lines = Get-ChildItem (.+) \|", build).group(1)
    assert r"dist\taskdash-docs-*.zip" in listing
    assert "dist/taskdash-docs-*.zip" in build and "dist/release-notes.md" in build
    release = re.search(r"gh release create .+", jobs["publish"]).group(0)
    assert r"dist\taskdash-docs-*.zip" in release
    assert r"--notes-file dist\release-notes.md" in release and "--generate-notes" not in release
    # UPGRADE.md and SECURITY.md send readers to the release page, so the notes carry the version's section.
    assert "has no '## [$release]' section" in build
    # A candidate's wheel version is 1.2.0rc1, but CHANGELOG.md has only a 1.2.0 section.
    rule = re.search(r"\$release = \$version -replace '(.+)', ''", build).group(1)
    assert [re.sub(rule, "", v) for v in ("1.2.0rc1", "1.2.0b2", "1.2.0")] == ["1.2.0"] * 3
    assert build.index("ReadAllText(\"$PWD\\CHANGELOG.md\")") < build.index("WriteAllText(\"$PWD\\dist\\release-notes.md\"")
    notes = (WORKFLOWS.parent / "release-notes.md").read_text(encoding="utf-8")
    for needle in ("taskdash-docs-<version>.zip", "Install-TaskDash.ps1", "requirements.lock", "SHA256SUMS.txt", "-Apply"):
        assert needle in notes, needle


def test_security_policy_names_the_public_release_repository_as_the_channel():
    policy = (WORKFLOWS.parent.parent / "SECURITY.md").read_text(encoding="utf-8")
    assert "https://github.com/uhsear/taskdash-releases" in policy
    assert "Report a vulnerability" in policy

"""Release hardening from the product audit (TD-064, TD-065, TD-199, TD-203, TD-207): the release waits
for the CI matrix, the wheel is hashed away from the unlocked tools, a pre-release tag gets a PEP 440
version, and the install workflow runs the one-window installer and installs the smtp extra."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import taskdash_common

ROOT = Path(__file__).resolve().parent
WORKFLOWS = ROOT / ".github" / "workflows"
POWERSHELL = shutil.which("powershell.exe")


def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _jobs(text):
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


# ------------------------------------------------------------------ TD-064

def test_the_release_calls_the_ci_matrix_and_publish_waits_for_it():
    jobs = _jobs(_text("release.yml"))
    assert "uses: ./.github/workflows/matrix.yml" in jobs["matrix"]
    assert "contents: read" in jobs["matrix"] and "write" not in jobs["matrix"]
    assert "matrix" in re.search(r"needs: \[(.+)\]", jobs["publish"]).group(1)


# ------------------------------------------------------------------ TD-199

def test_the_wheel_is_built_and_hashed_in_a_job_that_installs_only_the_pinned_build_tool():
    wheel = _jobs(_text("release.yml"))["wheel"]
    installs = re.findall(r"-m pip install (.+)", wheel)
    assert installs and all(re.fullmatch(r'"build==[\d.]+"', i) for i in installs)
    assert "sha256: ${{ steps.hash.outputs.sha256 }}" in wheel
    assert "GITHUB_OUTPUT" in wheel and "installer_sha256=" in wheel


def test_the_build_job_checks_the_wheel_against_the_hash_before_it_writes_the_checksums():
    build = _jobs(_text("release.yml"))["build"]
    assert "WHEEL_SHA256: ${{ needs.wheel.outputs.sha256 }}" in build
    assert "-m build" not in build, "the wheel comes from the wheel job"
    assert build.index("-m piptools compile") < build.index("$env:WHEEL_SHA256") < build.index("WriteAllText")
    assert "INSTALLER_SHA256: ${{ needs.wheel.outputs.installer_sha256 }}" in build
    assert build.index("$env:INSTALLER_SHA256") < build.index("Copy-Item Install-TaskDash.ps1")


# ------------------------------------------------------------------ TD-203

def _expected_version(tag):
    block = re.search(r"^\s*(\$expected = .+)$", _text("release.yml"), flags=re.M).group(1)
    result = subprocess.run([POWERSHELL, "-NoProfile", "-Command", f"{block}; $expected"],
                            capture_output=True, text=True, timeout=60, env={"TAG": tag, "SystemRoot": "C:\\Windows"})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.skipif(not POWERSHELL, reason="powershell.exe not available")
@pytest.mark.parametrize("tag, version", [
    ("v1.2.0", "1.2.0"),
    ("v1.2.0-rc1", "1.2.0rc1"),
    ("v1.2.0-a2", "1.2.0a2"),
    ("v1.2.0-b3", "1.2.0b3"),
    ("v1.2.0rc1", "1.2.0rc1"),
])
def test_a_tag_names_its_pep_440_version(tag, version):
    assert _expected_version(tag) == version


def test_the_package_version_is_already_in_pep_440_form():
    # The installer compares the wheel file name with `taskdash --version`, so the two must agree.
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", taskdash_common.TASKDASH_VERSION)


def test_a_version_that_ends_in_a_pre_release_label_is_published_as_a_pre_release():
    line = re.search(r"if \(\$env:TAG -match '(.+)'\) \{ \$flags \+= \"--prerelease\" \}", _text("release.yml"))
    assert line
    for tag in ("v1.2.0-rc1", "v1.2.0rc1", "v1.2.0b2"):
        assert re.search(line.group(1), tag), tag
    assert not re.search(line.group(1), "v1.2.0")


# ------------------------------------------------------------------ TD-065 and TD-207

def test_the_install_workflow_installs_the_smtp_extra_like_a_user_with_mail_alerts():
    assert '-m pip install "${wheel}[smtp]"' in _text("install.yml")


def test_the_install_workflow_runs_the_one_window_installer_in_check_mode_only():
    install = _jobs(_text("install.yml"))["install"]
    step = install.split("- name: Run the one-window installer in check mode", 1)[1].split("\n      - name:", 1)[0]
    assert "-File .\\Install-TaskDash.ps1" in step
    assert "-Apply" not in step.replace("without -Apply", "")
    assert "SHA256SUMS.txt" in step and "CHECK MODE" in step
    assert "Test-Path C:\\TaskDashInstallCheck" in step, "check mode must leave the folder absent"
    assert "$LASTEXITCODE -ne 1" in step, "a wrong wheel hash must stop the installer"

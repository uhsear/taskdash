"""Security and release documentation from the product audit (TD-063, TD-196, TD-197, TD-198, TD-200,
TD-206): the preflight warning for unconfined log reads, the approval sheet's mail and account text,
the signer-identity sentence, the release checklist and the package metadata. No network."""
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import taskdash_common as common
import taskdash_preflight as pf

ROOT = Path(__file__).resolve().parent


def _read(*parts):
    return " ".join(ROOT.joinpath(*parts).read_text(encoding="utf-8").split())


def _ctx(config, tasks=None):
    return pf.Context(SimpleNamespace(config="", offline=False), config=config, tasks=tasks)


def _task(name, script, **extra):
    # The raw shape collect_tasks returns: the script path comes from the interpreter and its arguments.
    return {"task_name": name, "task_path": "\\", "enabled": True,
            "primary_executable": r"C:\Python\python.exe", "primary_arguments": f'"{script}"', **extra}


# ------------------------------------------------------------------ TD-196

def test_empty_log_roots_warn_and_suggest_the_script_folders_of_the_monitored_tasks():
    tasks = [_task("A", r"D:\Jobs\a\run_a.py"), _task("B", r"D:\Jobs\b\run_b.py"),
             _task("C", r"D:\Jobs\a\other.py"), _task("Edge", r"C:\Edge\u.exe", task_path="\\Microsoft\\Edge\\")]
    status, message = pf.check_log_roots(_ctx({"metrics": {"log_roots": []}}, tasks))
    assert status == pf.WARN and message.startswith("WARNING: metrics.log_roots is empty")
    assert '"D:\\Jobs\\a", "D:\\Jobs\\b"' in message and "Edge" not in message
    assert "Limit where TaskDash reads logs" in message


def test_a_missing_log_roots_key_warns_too_and_a_long_suggestion_list_is_cut():
    tasks = [_task(f"T{i}", rf"D:\Jobs\j{i}\run.py") for i in range(8)]
    status, message = pf.check_log_roots(_ctx({}, tasks))
    assert status == pf.WARN and "and 3 more" in message


def test_log_roots_without_tasks_still_warn_and_a_set_list_passes():
    assert pf.check_log_roots(_ctx({}, None))[0] == pf.WARN
    assert pf.check_log_roots(_ctx({"metrics": {"log_roots": ["  "]}}))[0] == pf.WARN
    status, message = pf.check_log_roots(_ctx({"metrics": {"log_roots": [r"D:\Jobs"]}}))
    assert status == pf.OK and "1 folder" in message


def test_the_log_roots_check_runs_after_the_task_read_and_warns_without_failing(tmp_path, monkeypatch):
    names = [name for name, _, _ in pf.CHECKS]
    assert names.index("task definitions") < names.index("log roots")
    monkeypatch.setattr(pf, "CHECKS", (("log roots", False, pf.check_log_roots),))
    config = tmp_path / "cfg.json"
    config.write_text('{"rules": {"profiles": []}}', encoding="utf-8")
    assert pf.run_preflight(SimpleNamespace(config=str(config), offline=True)) == 0


def test_the_docs_say_the_default_is_open_and_that_preflight_warns():
    assert "taskdash preflight` warns" in _read("docs", "APPROVAL.md")
    assert "`log roots` line" in _read("docs", "INSTALL.md")
    assert "`log roots` line" in _read("docs", "CONFIGURE.md")
    assert "warns while it is empty" in _read("SECURITY.md")


# ------------------------------------------------------------------ TD-197

def test_the_approval_sheet_does_not_claim_that_mail_is_always_encrypted():
    sheet = _read("docs", "APPROVAL.md")
    row = re.search(r"\| `smtp\.host` on `smtp\.port` \| (.+?) \| Only if alerts are on", sheet).group(1)
    assert "with STARTTLS or SSL" not in row
    for fact in ("STARTTLS is on by default", "SSL for port 465", "plain text", "mail_config_path", "gets no certificate check"):
        assert fact in row, fact


def test_the_mail_defaults_the_sheet_names_are_the_defaults_in_the_code(monkeypatch):
    mail = common._smtp_block_settings({"host": "h", "from": "f@example.org"})
    assert mail["use tls"] is True and mail["use ssl"] is False and mail["strict tls"] is True
    assert common._smtp_block_settings({"host": "h", "from": "f@example.org", "port": 465})["use ssl"] is True
    assert "TLS is not verified" in common.legacy_mail_warnings({})[0]


# ------------------------------------------------------------------ TD-198

def test_every_signing_doc_says_the_signer_is_the_release_workflow_of_the_public_repository():
    for parts in (("docs", "INSTALL.md"), ("docs", "APPROVAL.md"), ("SECURITY.md",)):
        text = _read(*parts)
        assert "release workflow" in text, parts
        assert not re.search(r"private (build )?repository|copies the signed files", text), parts


# ------------------------------------------------------------------ TD-200

def test_the_approval_sheet_recommends_a_dedicated_account_with_a_custom_role_and_rotation():
    sheet = _read("docs", "APPROVAL.md")
    assert "dedicated account" in sheet and "custom role" in sheet
    assert "(ROTATE.md)" in sheet
    assert len(pf.REQUIRED_PRIVILEGES) == 5


# ------------------------------------------------------------------ TD-063

def test_the_release_checklist_covers_every_step_and_names_the_real_files():
    text = _read("docs", "RELEASING.md")
    for needle in ("TASKDASH_VERSION", "CHANGELOG.md", "_tools/export_public.py", "--apply", "--private-list",
                   "v1.2.0-rc1", "1.2.0rc1", "uhsear/taskdash-releases", "SHA256SUMS.txt", "sigstore.json",
                   "Install-TaskDash.ps1", "Get-FileHash", "That copy step is"):
        assert needle in text, needle
    # Every file it names exists, so a rename breaks this test instead of the checklist.
    for name in re.findall(r"`((?:_tools|docs|\.github/workflows)/[\w./-]+\.(?:py|md|yml))`", text):
        assert (ROOT / name).exists(), name


def test_the_workflow_header_and_the_export_script_point_at_the_checklist():
    assert "docs/RELEASING.md" in (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "docs/RELEASING.md" in (ROOT / "_tools" / "export_public.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ TD-206

def test_the_package_names_its_release_page_and_that_it_is_for_windows():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["urls"]["Homepage"] == "https://github.com/uhsear/taskdash"
    assert project["urls"]["Releases"].startswith(project["urls"]["Homepage"])
    classifiers = project["classifiers"]
    assert "Operating System :: Microsoft :: Windows" in classifiers
    assert not any(c.startswith("License ::") for c in classifiers), "the SPDX license field replaces them"
    pythons = {c.rsplit(" ", 1)[1] for c in classifiers if c.startswith("Programming Language :: Python :: 3.")}
    assert pythons == {"3.11", "3.12", "3.13"}

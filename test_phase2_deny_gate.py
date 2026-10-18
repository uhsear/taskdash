"""Phase 2 deliverable 10c: the generic deny-list gate.

The code, tests, default rules and dashboard templates must carry no organization-specific term.
The reference profile under rules/ and the scanner file, which holds the list, are the
only exceptions.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "_tools"))

import deny_scan  # noqa: E402

import taskdash_common  # noqa: E402


def _targets():
    paths = [str(p) for p in sorted(ROOT.glob("*.py"))]
    paths += [str(p) for p in sorted((ROOT / "_tools").glob("*.py")) if p.name != "deny_scan.py"]
    paths += [str(ROOT / "rules" / "default.json"), str(ROOT / "dashboard_templates.json"),
              str(ROOT / "run_taskdash.cmd"), str(ROOT / "TaskDash.config.example.json"),
              str(ROOT / "tests"), str(ROOT / "taskdash_task.xml"), str(ROOT / "install_task.ps1"),
              str(ROOT / ".github" / "workflows" / "install.yml")]
    return paths


def test_no_deny_listed_term_in_code_tests_default_rules_or_templates():
    hits = deny_scan.scan(_targets(), deny_scan.load_terms(""))
    assert hits == []


def test_mail_settings_need_a_configured_source_not_a_built_in_path():
    assert taskdash_common.load_mail_settings({}) is None
    assert taskdash_common.load_mail_settings({"alerting": {}}) is None

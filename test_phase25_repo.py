"""Phase 2.5 repository hygiene: the ignore rules cover runtime state and secrets, and the example
config lists exactly the keys the loader accepts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import taskdash_common as common

ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / "TaskDash.config.example.json"
# A deployed copy (the task server) is not a git checkout; the ignore-rule tests apply only to the repository.
IN_GIT_REPO = (ROOT / ".git").exists()
needs_git = pytest.mark.skipif(not IN_GIT_REPO, reason="not a git checkout")

# The SDE extension is deny-listed in source, so the name is assembled.
MUST_BE_IGNORED = [
    "TaskDash.config.json", "TaskDash.config.json.old", "TaskDash.config.json.bak_1",
    "x.config.json.bak", "alert_state.json", "Logs/run.txt", "run.log", "a.bak", "a.bak_20261016",
    "dashboard_taskdash.json", "archive/old.json", "__pycache__/m.cpython-313.pyc", "m.pyc",
    ".pytest_cache/v", ".coverage", "conn." + "sd" + "e", ".claude/settings.local.json",
    "collector_runs_pending_prod.json",
]


def _ignored(name: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT).returncode == 0


@needs_git
@pytest.mark.parametrize("name", MUST_BE_IGNORED)
def test_runtime_state_and_secrets_are_ignored(name):
    assert _ignored(name), f"{name} must be listed in .gitignore"


@needs_git
def test_example_config_and_fixtures_are_not_ignored():
    names = ["TaskDash.config.example.json", ".gitignore", ".gitattributes"]
    names += [p.relative_to(ROOT).as_posix() for p in (ROOT / "tests" / "fixtures").glob("*.json")]
    assert len(names) > 3
    assert [n for n in names if _ignored(n)] == []


def _accepted_paths():
    paths = set(common._CONFIG_TOP_LEVEL_KEYS)
    for keys in (common._CONFIG_STRING_KEYS, common._CONFIG_NUMBER_KEYS, common._CONFIG_BOOL_KEYS,
                 common._CONFIG_STRING_LIST_KEYS, common._CONFIG_DICT_KEYS,
                 common._CONFIG_LIST_OF_DICT_KEYS):
        paths |= keys
    return paths - common._CONFIG_SECTION_KEYS


def _example_paths(node, prefix=""):
    """Dotted key paths in the example; an object value that is itself a typed key is a leaf."""
    leaves = set()
    for key, value in node.items():
        if key.startswith("_"):
            continue
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and dotted in common._CONFIG_SECTION_KEYS:
            leaves |= _example_paths(value, dotted + ".")
        else:
            leaves.add(dotted)
    return leaves


def _example():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_example_config_has_only_keys_the_loader_accepts():
    assert _example_paths(_example()) - _accepted_paths() == set()


def test_example_config_lists_every_key_the_loader_accepts():
    # Fails until every new key (privacy.hide_paths, smtp.ssl, smtp.allow_plaintext_auth) is in
    # taskdash_common's allow-lists and in the example.
    assert _accepted_paths() - _example_paths(_example()) == set()


def test_example_config_documents_every_key_and_stays_private_by_default():
    example = _example()
    assert _example_paths(example) - set(example["_notes"]) == set()
    assert all(example["privacy"][k] is True for k in
               ("hide_command_lines", "hide_sample_lines", "hide_accounts", "hide_paths"))
    assert example["arcgis"]["profile"] and not example["arcgis"]["password"]
    assert example["smtp"]["starttls"] is True and "password" not in example["smtp"]
    ids = [v for k, v in example["arcgis"].items() if k.endswith("item_id") or k == "dashboard_id"]
    # Empty, not placeholders: taskdash setup refuses a config that already names an item.
    assert ids and all(v == "" for v in ids)


def test_example_config_points_at_no_organization():
    example = _example()
    assert example["host_name_override"] == ""
    assert example["arcgis"]["url"] == "" and example["arcgis"]["service_description"] == ""
    assert example["alerting"]["dashboard_url"] == ""


def test_example_config_passes_the_loader():
    assert common.load_config(EXAMPLE)["rules"]["profiles"] == []

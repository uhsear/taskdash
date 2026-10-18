"""Docs coverage (roadmap Phase 4 item 8): every config key and CLI flag in docs/CONFIGURE.md, and
every SystemExit message in docs/TROUBLESHOOTING.md, or listed in tests/docs_pending.json. The
pending list may only shrink; Phase 4 is done when it is empty."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_tools"))

import doc_inventory  # noqa: E402

PENDING = json.loads(doc_inventory.PENDING.read_text(encoding="utf-8"))


def test_every_undocumented_item_is_listed_as_pending():
    missing = doc_inventory.uncovered()
    for kind, items in missing.items():
        unlisted = sorted(set(items) - set(PENDING.get(kind, [])))
        assert not unlisted, (f"{kind} not in the docs and not pending: {unlisted}. Document them "
                              f"(see _tools/doc_inventory.py for where), or add them to "
                              f"tests/docs_pending.json on purpose.")


def test_the_pending_list_holds_nothing_documented_or_gone():
    missing = doc_inventory.uncovered()
    for kind, items in PENDING.items():
        stale = sorted(set(items) - set(missing.get(kind, [])))
        assert not stale, (f"{kind} pending but already documented or no longer in the code: {stale}. "
                           f"Run python _tools/doc_inventory.py --write-pending.")


def test_inventory_finds_the_known_anchors():
    assert {"arcgis.url", "alerting.maintenance_windows", "scheduler.task_name_regex"} <= doc_inventory.config_keys()
    assert {"--apply", "--offline", "--title-prefix", "--enable-event-log"} <= doc_inventory.cli_flags()
    assert any("Set it, or use `taskdash status --offline`" in m for m in doc_inventory.exit_messages())

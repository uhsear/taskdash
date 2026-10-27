"""Undo a TaskDash setup: delete the ArcGIS items that setup recorded, and nothing else.

Driver: taskdash remove [--apply] (run by hand, as the account that ran setup)
Source: taskdash_setup_manifest.json next to the config, written by `taskdash setup --apply`
Target: the manifest's items in ArcGIS, in reverse creation order (dashboard, group, view, service,
        then the folder if it is empty). It never touches the Windows task or any file.

    taskdash remove            # check: sign in, list what it would delete, delete nothing
    taskdash remove --apply    # delete the listed items that still exist and belong to this account

An item that another account owns, that no longer has the manifest's type and title, or that ArcGIS
refuses to delete, is skipped and reported; the run goes on and exits 1. A service whose view was
skipped, or that has a view outside the manifest, is skipped too. Delete protection is turned off on each item just before its delete, and
turned back on if the delete fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from taskdash_common import (
    TASKDASH_VERSION, connect_agol, emit, load_config, normalize_text, see_troubleshooting,
)
from taskdash_dashboard import DEFAULT_COLLECTOR_TASK_NAME
from taskdash_setup import FEATURE_SERVICE, MANIFEST_FILE, config_file

# Folders have no delete protection. Groups have their own switch with no protect call, so classify
# skips a protected group instead of clearing it.
NO_PROTECTION = ("Folder", "Group")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="taskdash remove", allow_abbrev=False,
        description="Delete the items that taskdash setup created. Deletes nothing unless you pass --apply.")
    parser.add_argument("--config", default="", metavar="PATH",
                        help="Config file next to the manifest. Default: the config next to the scripts.")
    parser.add_argument("--apply", action="store_true", help="Delete the listed items.")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    return parser.parse_args(argv)


def load_manifest(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"There is no setup manifest at {path}, so there is nothing to remove. Setup "
                         f"saves {MANIFEST_FILE} next to the config. For an install made before "
                         f"version 1.2, delete the items by hand: see 'Undo the install' in docs/INSTALL.md.\n"
                         + see_troubleshooting("There is no setup manifest"))
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        entries = manifest["items"]
        for entry in entries:
            entry["order"], entry["id"], entry["type"], entry["title"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"The setup manifest {path} cannot be read: {type(exc).__name__}. Restore it "
                         f"from a backup, or delete the items by hand: see 'Undo the install' in "
                         f"docs/INSTALL.md.\n"
                         + see_troubleshooting("The setup manifest cannot be read")) from exc
    return manifest


def find_folder(gis: Any, entry: Dict[str, Any]) -> Any:
    """The folder by the manifest's id (ArcGIS matches a folder id as well as a name), else by title.
    The title fallback finds a same-name folder that classify then refuses because its id differs."""
    folders = gis.content.folders
    return (entry["id"] and folders.get(folder=entry["id"])) or folders.get(folder=entry["title"])


def classify(gis: Any, entry: Dict[str, Any], me: str, gone: Set[str]) -> Tuple[Any, str, str]:
    """Return (object, 'delete' | 'gone' | 'skip', reason). `gone` holds the ids already deleted or
    planned for deletion, so a folder holding only those counts as empty."""
    kind, item_id = entry["type"], entry["id"]
    if kind == "Folder":
        obj = find_folder(gis, entry)
    elif kind == "Group":
        obj = gis.groups.get(item_id)
    else:
        obj = gis.content.get(item_id)
    if obj is None:
        return None, "gone", "not found, already deleted"
    # A folder is looked up under the signed-in account, so it cannot belong to anyone else.
    # The id check refuses a same-title folder that setup did not create (made again, or by this account).
    if kind == "Folder" and entry["id"] and (obj.properties or {}).get("id") != entry["id"]:
        return obj, "skip", "a different folder has that name, so it is not the one setup created"
    if kind != "Folder" and obj.owner != me:
        return obj, "skip", f"owned by {obj.owner}, not {me}"
    # An id can be reused or the item retitled; delete only what still looks like what setup made.
    if kind != "Folder" and (normalize_text(obj.title) != normalize_text(entry["title"])
                             or (kind != "Group" and normalize_text(obj.type) != normalize_text(kind))):
        return obj, "skip", (f"it is now a {getattr(obj, 'type', 'Group')} titled '{obj.title}', not the "
                             f"{kind} '{entry['title']}' that setup created")
    if kind == "Group" and getattr(obj, "protected", False) is True:
        return obj, "skip", "delete protection is on; turn it off in the group's settings, then run again"
    if kind == "Folder" and any(item.id not in gone for item in obj.list()):
        return obj, "skip", "the folder is not empty; it holds items that are not in the manifest"
    return obj, "delete", ""


def unlisted_views(obj: Any, gone: Set[str]) -> str:
    """'' or why the service must stay: ArcGIS deletes a hosted layer's views with it, and a view made
    later (collect --create-view) is not in the manifest."""
    try:
        views = obj.related_items("Service2Service", "forward")
    except Exception as exc:
        return f"its views cannot be listed ({type(exc).__name__}), so a view outside the manifest may exist"
    extra = [view.id for view in views if view.id not in gone]
    return f"it has a view that is not in the manifest: {', '.join(extra)}" if extra else ""


def reprotect(obj: Any) -> str:
    """Turn delete protection back on after a failed delete; return '' or a note that it is still off."""
    try:
        if (obj.protect(enable=True) or {}).get("success"):
            return ""
    except Exception:
        pass
    return "; delete protection is off, so turn it on in Item > Settings > Delete protection"


def delete_one(obj: Any, kind: str) -> str:
    """Delete one object; return '' on success or the reason it was not deleted. When the delete
    fails after protection was turned off, protection goes back on, so a kept item stays guarded."""
    unprotected = False
    try:
        if kind not in NO_PROTECTION:
            if not (obj.protect(enable=False) or {}).get("success"):
                return ("could not turn off delete protection; turn it off in Item > Settings > "
                        "Delete protection, or ask an administrator to")
            unprotected = True
        reason = "ArcGIS refused the delete" if obj.delete() is False else ""
    except Exception as exc:
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
    return reason + reprotect(obj) if reason and unprotected else reason


def print_next_steps(task_name: str) -> None:
    emit("This command does not touch the Windows task or any file. To finish the undo:")
    emit(f'    Unregister-ScheduledTask -TaskName "{task_name}" -Confirm:$false')
    emit(f'    "{sys.executable}" -m pip uninstall taskdash')
    emit("Then delete the install folder and the saved sign-in, as 'Undo the install' in "
         "docs/INSTALL.md describes.")


def remove(gis: Any, manifest: Dict[str, Any], apply: bool, task_name: str) -> int:
    me = normalize_text(gis.users.me.username)
    entries = sorted(manifest["items"], key=lambda e: e["order"], reverse=True)
    emit(f"{'Deleting' if apply else 'Checking'} {len(entries)} manifest items as {me}, newest first:")
    gone: Set[str] = set()
    skipped = 0
    view_kept = False
    for entry in entries:
        obj, action, reason = classify(gis, entry, me, gone)
        # Newest first, so the first Feature Service is the view. Deleting the service under a view
        # that stays would leave that view broken.
        is_service = entry["type"] == FEATURE_SERVICE
        if is_service and action == "delete" and view_kept:
            action, reason = "skip", "its view still exists"
        elif is_service and action == "delete":
            reason = unlisted_views(obj, gone)
            action = "skip" if reason else action
        label = f"{entry['type']:16s} {entry['id']}  {entry['title']}"
        if action == "delete" and apply:
            reason = delete_one(obj, entry["type"])
            action = "skip" if reason else "done"
        view_kept = view_kept or (is_service and action == "skip")
        verb = {"delete": "Would delete", "done": "Deleted", "gone": "Already gone",
                "skip": "Skipped" if apply else "Would skip"}[action]
        if action in ("delete", "done", "gone"):
            gone.add(entry["id"])
        skipped += action == "skip"
        emit(f"    {verb}: {label}" + (f"  ({reason})" if reason else ""))
    if not apply:
        emit("Nothing deleted. Re-run with --apply to delete the items marked 'Would delete'.")
    elif skipped:
        emit(f"{skipped} item(s) were skipped and still exist. Fix the reason shown, then run "
             f"`taskdash remove --apply` again.\n" + see_troubleshooting("Remove skipped an item"))
    print_next_steps(task_name)
    return 1 if apply and skipped else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    path = config_file(args)
    manifest = load_manifest(path.with_name(MANIFEST_FILE))
    config = load_config(path)
    task_name = (normalize_text(config.get("scheduler", {}).get("collector_task_name"))
                 or DEFAULT_COLLECTOR_TASK_NAME)
    return remove(connect_agol(config), manifest, args.apply, task_name)


if __name__ == "__main__":
    raise SystemExit(main())

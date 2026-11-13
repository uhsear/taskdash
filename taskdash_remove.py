"""Undo a TaskDash setup: delete the ArcGIS items that setup recorded, and nothing else.

Driver: taskdash remove [--apply] (run by hand, as the account that ran setup)
Source: taskdash_setup_manifest.json next to the config, written by `taskdash setup --apply`
Target: the manifest's items in ArcGIS, in reverse creation order (dashboard, group, view, service,
        then the folder if it is empty). It never touches the Windows task. With --apply it also
        empties the config's item ids of the items it deleted, after a backup of the config.

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
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from taskdash_common import (
    CONFIG_ARG_HELP, TABLE_COLLECTOR_RUNS, TASKDASH_VERSION, command_text, connect_agol, emit, get_host_name, get_tables,
    host_clause, load_config, normalize_text, see_troubleshooting, utc_now,
)
from taskdash_dashboard import DEFAULT_COLLECTOR_TASK_NAME, DEFAULT_HEARTBEAT_IGNORE_DAYS
from taskdash_setup import FEATURE_SERVICE, MANIFEST_FILE, backup_config, config_file, write_config_key

# Folders have no delete protection. Groups have their own switch with no protect call, so classify
# skips a protected group instead of clearing it.
NO_PROTECTION = ("Folder", "Group")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="taskdash remove", allow_abbrev=False,
        description="Delete the items that taskdash setup created. Deletes nothing unless you pass --apply.")
    parser.add_argument("--config", default="", metavar="PATH",
                        help=CONFIG_ARG_HELP)
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


def other_active_hosts(gis: Any, manifest: Dict[str, Any], own_host: str, days: float,
                       own_previous_names: Sequence[str] = ()) -> List[str]:
    """Names of other computers with a CollectorRuns row in the last `days` days. The service is
    shared, so deleting it would wipe their history and fail their next publish. own_previous_names
    (host_previous_names) are this computer's earlier names: a rename leaves their runs in the window.
    Raises when the table cannot be read: the caller must not delete on an unanswered question."""
    services = [e for e in manifest["items"] if e["type"] == FEATURE_SERVICE]
    item = gis.content.get(min(services, key=lambda e: e["order"])["id"]) if services else None
    if item is None:
        return []
    runs = get_tables(item).get(TABLE_COLLECTOR_RUNS)
    if runs is None:
        return []
    cutoff = utc_now() - timedelta(days=days)
    result = runs.query(
        where=(f"{host_clause(own_host, exclude=True)} AND "
               f"started_utc >= TIMESTAMP '{cutoff.strftime('%Y-%m-%d %H:%M:%S')}'"),
        out_fields="host_name", return_geometry=False, return_distinct_values=True)
    names = {normalize_text(f.attributes.get("host_name")) for f in getattr(result, "features", None) or []}
    own = {normalize_text(n).lower() for n in (own_host, *own_previous_names)}
    return sorted(name for name in names if name and name.lower() not in own)


def refuse_shared_service(gis: Any, manifest: Dict[str, Any], config: Dict[str, Any], apply: bool) -> None:
    """Warn in check mode, and refuse --apply, when another computer still publishes to the service."""
    days = (config.get("scheduler") or {}).get("heartbeat_ignore_after_days", DEFAULT_HEARTBEAT_IGNORE_DAYS)
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days <= 0:
        days = DEFAULT_HEARTBEAT_IGNORE_DAYS
    try:
        hosts = other_active_hosts(gis, manifest, get_host_name(config), days,
                                   config.get("host_previous_names") or ())
    except Exception as exc:
        if not apply:
            emit(f"WARNING: could not check whether other computers publish to the service ({type(exc).__name__}).")
            return
        raise SystemExit("Cannot check whether other computers publish to the service, so nothing was "
                         f"deleted. Run the command again later. ArcGIS said: {type(exc).__name__}.\n"
                         + see_troubleshooting("Remove refuses a shared service")) from exc
    if not hosts:
        return
    message = (f"{', '.join(hosts)} published to this service in the last {days:g} days. Deleting it would "
               "erase their history and make their next runs fail. Stop each collector first, as step 1 of "
               "'Retire a computer' in docs/CONFIGURE.md says, and run this again after the computer has "
               f"been silent for {days:g} days.")
    if apply:
        raise SystemExit(f"Cannot remove: {message}\n"
                         + see_troubleshooting("Remove refuses a shared service"))
    emit(f"WARNING: {message} {command_text('remove', '--apply')} refuses until then.")


def print_next_steps(task_name: str) -> None:
    emit("This command does not touch the Windows task, the saved sign-ins or the package. To finish the "
         "undo, open PowerShell as administrator and run these in order. The sign-in commands come first, "
         "because the task account is read from the task:")
    emit("    taskdash signin --forget --apply")
    emit("    taskdash signin --forget --for-task-account --apply")
    emit(f'    Unregister-ScheduledTask -TaskName "{task_name}" -Confirm:$false')
    emit(f'    & "{sys.executable}" -m pip uninstall taskdash')
    emit("Then delete the install folder, as 'Undo the install' in docs/INSTALL.md describes.")


# The config keys that hold the id of an item that setup created.
CONFIG_ITEM_KEYS = ("item_id", "test_item_id", "view_item_id", "test_view_item_id", "dashboard_id")


def clear_deleted_ids(path: Path, config: Dict[str, Any], gone: Set[str]) -> None:
    """Empty the config ids of items that are now gone. A kept id would make a new `taskdash setup`
    print 'nothing to do' and make every hourly run fail on an item that no longer exists."""
    agol = config.get("arcgis") or {}
    keys = [key for key in CONFIG_ITEM_KEYS if normalize_text(agol.get(key)) in gone - {""}]
    if not keys:
        return
    backup_config(path)
    for key in keys:
        write_config_key(path, config, key, "")
    emit("Emptied the ids of the deleted items, so a new install starts clean.")


def remove(gis: Any, manifest: Dict[str, Any], apply: bool, task_name: str, demo: bool = False,
           gone: Optional[Set[str]] = None) -> int:
    """Delete (or, without apply, list) the manifest's items. `taskdash demo --remove` passes demo=True:
    its items are not a real install, so the real-task uninstall steps stay out and the re-run
    command names the demo. A caller that passes `gone` gets the ids deleted or already gone."""
    me = normalize_text(gis.users.me.username)
    entries = sorted(manifest["items"], key=lambda e: e["order"], reverse=True)
    emit(f"{'Deleting' if apply else 'Checking'} {len(entries)} manifest items as {me}, newest first:")
    gone = set() if gone is None else gone
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
        if not demo:
            emit("Before you do, disable the collector task, so that an hourly run cannot fail on the "
                 f'deleted service. In an administrator window, run: Disable-ScheduledTask -TaskName "{task_name}"')
    elif skipped:
        again = "taskdash demo --remove --apply" if demo else "taskdash remove --apply"
        emit(f"{skipped} item(s) were skipped and still exist. Fix the reason shown, then run "
             f"`{again}` again.\n" + see_troubleshooting("Remove skipped an item"))
    if not demo and not skipped:
        print_next_steps(task_name)
    return 1 if apply and skipped else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    path = config_file(args)
    manifest = load_manifest(path.with_name(MANIFEST_FILE))
    config = load_config(path)
    task_name = (normalize_text(config.get("scheduler", {}).get("collector_task_name"))
                 or DEFAULT_COLLECTOR_TASK_NAME)
    gis = connect_agol(config)
    refuse_shared_service(gis, manifest, config, args.apply)
    gone: Set[str] = set()
    code = remove(gis, manifest, args.apply, task_name, gone=gone)
    if args.apply:
        clear_deleted_ids(path, config, gone)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

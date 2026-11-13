"""Take a new organization from a filled-in config to a working, private TaskDash install.

Driver: taskdash_setup.py (run by hand, once per organization)
Source: TaskDash.config.json with the ArcGIS sign-in filled in and no arcgis item ids
Target: one folder holding the hosted service with its tables, its read-only view, a private viewers
        group and the dashboard; the three new ids are written back to the config

    python taskdash_setup.py            # check: sign in, run the refusals, print the plan
    python taskdash_setup.py --apply    # create everything and write the ids to the config

Only the view and the dashboard are shared, and only with the new group. The service is never
shared, and nothing is ever shared with everyone or the organization. Setup never updates an
item it did not create in the same run. It turns on delete protection for the service, the view and
the dashboard; turn it off in ArcGIS Online before deleting one of them, or run `taskdash remove`.
It also saves taskdash_setup_manifest.json next to the config, which `taskdash remove` reads.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import taskdash_dashboard as dashboard
import taskdash_publish as publish
from taskdash_common import (
    DEFAULT_CONFIG_FILE,
    TASKDASH_VERSION,
    CONFIG_ARG_HELP,
    atomic_write_json,
    command_text,
    connect_agol,
    emit,
    get_tables,
    load_config,
    normalize_text,
    runtime_dir,
    see_troubleshooting,
)

# A set key means this config already belongs to an install; setup would orphan it.
INSTALL_KEYS = ("item_id", "view_item_id", "dashboard_id")
FEATURE_SERVICE = "Feature Service"
DASHBOARD = "Dashboard"
GROUP_TAGS = "taskdash,monitoring"
# Saved next to the config, which setup already writes to; `taskdash remove` reads it.
MANIFEST_FILE = "taskdash_setup_manifest.json"

Created = List[Tuple[str, str, str]]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="taskdash setup", description="Set up a new, private TaskDash install.",
                                     allow_abbrev=False)
    parser.add_argument("--config", default="", metavar="PATH", help=CONFIG_ARG_HELP)
    parser.add_argument("--apply", action="store_true",
                        help="Create the folder, service, view, group and dashboard.")
    parser.add_argument("--folder", default="TaskDash", help="Folder that will hold the new items.")
    parser.add_argument("--title-prefix", default="", help="Text put before every item title.")
    parser.add_argument("--demo", action="store_true",
                        help="Mark the dashboard as invented data. `taskdash demo` passes it.")
    parser.add_argument("--group-title", default=None,
                        help="Title of the viewers group. Default: '<prefix>TaskDash viewers'.")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    args = parser.parse_args(argv)
    if args.group_title is None:
        args.group_title = f"{args.title_prefix}TaskDash viewers"
    return args


def planned_titles(prefix: str) -> Dict[str, str]:
    # The view title is derived by create_view from the service title.
    return {"service": f"{prefix}TaskDash", "view": f"{prefix}TaskDash_view",
            "dashboard": f"{prefix}TaskDash"}


# Appended to every refusal that a second computer meets: its setup would make a second dashboard.
SECOND_COMPUTER_HINT = ("\nIs this a second computer for an existing dashboard? Do not run setup: "
                        "see 'Add another computer' in docs/INSTALL.md.")


def refuse_existing_install(config: Dict[str, Any]) -> None:
    from taskdash_preflight import DASHBOARD_FIX  # preflight imports this module, so not at the top
    agol = config.get("arcgis", {})
    used = [f"arcgis.{k}" for k in INSTALL_KEYS if normalize_text(agol.get(k))]
    if used:
        raise SystemExit(f"The config already sets {', '.join(used)}, so an install exists. Setup is "
                         f"for a new install. To keep this install, maintain it with "
                         f"{command_text('collect', '--migrate', '--apply')}. If the dashboard is the "
                         f"problem: {DASHBOARD_FIX} "
                         f"To start again, delete the ArcGIS items and empty those keys.\n"
                         + see_troubleshooting("The config already holds an install") + SECOND_COMPUTER_HINT)


def refuse_name_clashes(gis: Any, args: argparse.Namespace) -> None:
    """Stop before any write when a planned name is taken, so setup cannot adopt or update it."""
    if not normalize_text(args.folder).strip("/"):
        raise SystemExit("--folder must be a folder name. Pass e.g. --folder TaskDash.\n"
                         + see_troubleshooting("The folder name is empty"))
    if gis.content.folders.get(folder=args.folder) is not None:
        raise SystemExit(f"The folder '{args.folder}' already exists for this account. Choose "
                         f"another with --folder, or delete the empty folder in ArcGIS Online.\n"
                         + see_troubleshooting("The folder already exists") + SECOND_COMPUTER_HINT)
    owner = gis.users.me.username
    titles = planned_titles(args.title_prefix)
    for title, item_type in ((titles["service"], FEATURE_SERVICE), (titles["view"], FEATURE_SERVICE),
                             (titles["dashboard"], DASHBOARD)):
        for item in gis.content.search(f'title:"{title}" AND owner:{owner}'):
            if item.title == title and item.type == item_type:
                raise SystemExit(f"{item_type} '{title}' already exists ({item.id}). Setup never "
                                 f"reuses an item: pass --title-prefix to pick other titles, or "
                                 f"delete that item in ArcGIS Online.\n"
                                 + see_troubleshooting("An item with the planned title already exists")
                                 + SECOND_COMPUTER_HINT)
    # Hosted service names are unique across the organization, so another account's service
    # blocks create_service after the folder exists; the owner search above cannot see it.
    for title in (titles["service"], titles["view"]):
        if not gis.content.is_service_name_available(title, "featureService"):
            raise SystemExit(f"Another account in this organization already has a hosted service named "
                             f"'{title}'. Pass --title-prefix to pick other names.\n"
                             + see_troubleshooting("Another account holds the service name"))
    for group in gis.groups.search(f'title:"{args.group_title}"'):
        if group.title == args.group_title:
            raise SystemExit(f"A group titled '{args.group_title}' already exists ({group.id}). "
                             f"Choose another with --group-title, or delete the group.\n"
                             + see_troubleshooting("The viewers group already exists") + SECOND_COMPUTER_HINT)


def print_plan(args: argparse.Namespace, config: Dict[str, Any]) -> None:
    titles = planned_titles(args.title_prefix)
    emit(f"Plan: folder '{args.folder}'.")
    emit(f"Plan: hosted service '{titles['service']}' in that folder, never shared, with "
         f"{len(publish.TABLE_SPECS)} tables:")
    for name in publish.TABLE_SPECS:
        indexes = "; ".join(f"{'unique ' if s['unique'] else ''}{', '.join(s['fields'])}"
                            for s in publish.TABLE_INDEXES[name])
        emit(f"    {name}: indexes on {indexes or 'none'}")
    emit("Plan: version stamp on the service (apply_migration).")
    emit(f"Plan: read-only view '{titles['view']}', capabilities {publish.VIEW_CAPABILITIES}.")
    for table, fields in publish.view_hidden_fields(config).items():
        emit(f"    hidden on the view: {table}: {', '.join(fields)}")
    emit(f"Plan: private group '{args.group_title}' (invitation only); the view and the dashboard "
         f"are shared with it and with nobody else.")
    emit(f"Plan: dashboard '{titles['dashboard']}' reading the view, in the same folder.")
    emit("Plan: delete protection on the service, the view and the dashboard.")
    emit("Plan: config keys written: " + ", ".join(f"arcgis.{k}" for k in INSTALL_KEYS))


# --------------------------------------------------------------------------- config write-back

def config_file(args: argparse.Namespace) -> Path:
    return Path(args.config) if args.config else runtime_dir() / DEFAULT_CONFIG_FILE


def backup_config(path: Path) -> Path:
    backup = path.with_name(f"{path.name}.bak_setup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, backup)
    emit(f"Config backed up to {backup}")
    return backup


def write_config_key(path: Path, config: Dict[str, Any], key: str, value: str) -> None:
    """Set one arcgis key in the raw file, so no other key or its order changes."""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    raw.setdefault("arcgis", {})[key] = value
    atomic_write_json(path, raw)
    config.setdefault("arcgis", {})[key] = value
    emit(f"Config: arcgis.{key} = {value}")


# --------------------------------------------------------------------------- apply steps

def create_service_item(gis: Any, config: Dict[str, Any], path: Path, args: argparse.Namespace,
                        created: Created) -> Any:
    title = planned_titles(args.title_prefix)["service"]
    item = publish.create_service(gis, title, config, folder=args.folder)
    created.append((title, FEATURE_SERVICE, item.id))
    write_config_key(path, config, "item_id", item.id)
    # The three tables create_service makes are not all the collector writes to.
    publish.apply_migration(get_tables(item), item=item)
    return item


def create_view_item(gis: Any, item: Any, config: Dict[str, Any], path: Path,
                     args: argparse.Namespace, created: Created) -> Any:
    # Recorded the moment it exists: a later wait, field hide or share can still fail, and the
    # "Created by this run" list must name the view for the undo.
    view = publish.create_view(gis, item, config, apply=True, target="prod", folder=args.folder,
                               on_create=lambda v: created.append((v.title, FEATURE_SERVICE, v.id)))
    write_config_key(path, config, "view_item_id", view.id)
    return view


def create_group(gis: Any, args: argparse.Namespace, created: Created) -> Any:
    group = gis.groups.create(title=args.group_title, tags=GROUP_TAGS,
                              description="Members can open the TaskDash dashboard and its read-only view.",
                              snippet="TaskDash viewers", access="private", is_invitation_only=True)
    if group is None:
        raise SystemExit(f"ArcGIS did not create the group '{args.group_title}'. Check that the "
                         f"account may create groups. Then run {command_text('remove')}, then "
                         f"{command_text('remove', '--apply')} to delete what is listed above, and run "
                         f"setup again.\n"
                         + see_troubleshooting("The group could not be created"))
    created.append((args.group_title, "Group", group.id))
    return group


def create_dashboard_item(gis: Any, view: Any, folder: Any, group: Any, config: Dict[str, Any],
                          path: Path, args: argparse.Namespace, created: Created) -> Any:
    """Build exactly as taskdash_dashboard.main does for prod, reading the view."""
    title = planned_titles(args.title_prefix)["dashboard"]
    templates = dashboard.load_templates(gis, "")
    layer_ids = dashboard.resolve_layer_ids(gis, view.id, "prod")
    definition = dashboard.build(view.id, templates, layer_ids, portal_url=gis.url, config=config,
                                 demo=args.demo)
    dashboard.validate_definition(definition, layer_ids, config, data_item=view.id)
    dash_id = dashboard.post_dashboard(gis, definition, title, None, config, demo=args.demo)
    created.append((title, DASHBOARD, dash_id))
    write_config_key(path, config, "dashboard_id", dash_id)
    item = gis.content.get(dash_id)
    if item is None:
        raise SystemExit(f"The new dashboard {dash_id} is not visible to this account. Find it in "
                         f"ArcGIS Online, move it into '{args.folder}' and share it with the group.\n"
                         + see_troubleshooting("The new dashboard is not visible"))
    moved = item.move(folder) or {}
    if not moved.get("success", True):
        raise SystemExit(f"Could not move dashboard {dash_id} into '{args.folder}': {moved}\n"
                         + see_troubleshooting("The dashboard could not be moved"))
    publish.share_item(item, groups=[group])
    return item


def protect_items(items: List[Any], protected: List[str]) -> None:
    """Turn on delete protection, so a stray click in ArcGIS Online cannot remove the install.
    Last, after the sharing checks: protection makes the undo list need one more step."""
    for item in items:
        try:
            done = (item.protect(enable=True) or {}).get("success")
        except Exception as exc:  # noqa: BLE001 - any arcgis error here must keep the undo list below
            done = False
            emit(f"WARNING: protecting item {item.id} failed ({type(exc).__name__}).")
        if not done:
            raise SystemExit(f"Could not turn on delete protection for item {item.id}. Turn it on by hand "
                             f"(Item > Settings > Delete protection), or ask an administrator to.\n"
                             + see_troubleshooting("Delete protection could not be turned on"))
        protected.append(item.id)


def print_created(created: Created, protected: Sequence[str] = (), demo: bool = False) -> None:
    undo = (command_text("demo", "--remove", "--apply") if demo else
            f"{command_text('remove')}, then {command_text('remove', '--apply')}")
    emit(f"Created by this run. To undo, run {undo}; it turns off delete protection and deletes these:"
         if created else "This run created nothing.")
    for title, kind, item_id in created:
        note = "  (delete protection on)" if item_id in protected else ""
        emit(f"    {kind:16s} {item_id}  {title}{note}")


def write_manifest(config_path: Path, args: argparse.Namespace, created: Created) -> None:
    """Save the created list as the only record `taskdash remove` trusts. It holds ids, types and
    titles, never a secret, and is written even after a partial run so the undo can still find them."""
    if not created:
        return
    manifest = {"manifest_version": 1, "taskdash_version": TASKDASH_VERSION, "folder": args.folder,
                "group_id": next((item_id for _, kind, item_id in created if kind == "Group"), ""),
                "items": [{"order": order, "id": item_id, "type": kind, "title": title}
                          for order, (title, kind, item_id) in enumerate(created, 1)]}
    target = config_path.with_name(MANIFEST_FILE)
    atomic_write_json(target, manifest)
    emit(f"Manifest of the created items saved to {target}. `taskdash remove` reads it.")


def replace_manifest_view(config_path: Path, old_id: str, view: Any) -> bool:
    """Swap the manifest entry of a replaced view for the new view, keeping its place in the order,
    so `taskdash remove` still deletes the view before the service. False when no entry matches."""
    path = config_path.with_name(MANIFEST_FILE)
    if not path.is_file():
        return False
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    entry = next((e for e in manifest["items"] if e["id"] == old_id and e["type"] == FEATURE_SERVICE), None)
    if entry is None:
        return False
    entry["id"], entry["title"] = view.id, view.title
    atomic_write_json(path, manifest)
    return True


def apply_setup(gis: Any, config: Dict[str, Any], path: Path, args: argparse.Namespace,
                created: Created, protected: List[str]) -> None:
    backup_config(path)
    folder = gis.content.folders.create(args.folder)
    created.append((args.folder, "Folder", (folder.properties or {}).get("id", "")))
    item = create_service_item(gis, config, path, args, created)
    view = create_view_item(gis, item, config, path, args, created)
    group = create_group(gis, args, created)
    # The Group object, not its id: the old Item.share() looked an id up in the search index, which
    # did not list a group created seconds ago ("Cannot find group with id", first TDI_ run).
    publish.share_item(view, groups=[group])
    dash = create_dashboard_item(gis, view, folder, group, config, path, args, created)
    publish.require_no_anonymous_access(publish.table_urls(item, view))
    protect_items([item, view, dash], protected)


def setup(gis: Any, config: Dict[str, Any], path: Path, args: argparse.Namespace) -> int:
    refuse_existing_install(config)
    refuse_name_clashes(gis, args)
    print_plan(args, config)
    if not args.apply:
        emit("DRY RUN - nothing written. Re-run with --apply.")
        return 0
    created: Created = []
    protected: List[str] = []
    try:
        apply_setup(gis, config, path, args, created, protected)
    finally:
        print_created(created, protected, args.demo)
        write_manifest(path, args, created)
    emit("Setup complete. The service is private; the view and dashboard are shared with the "
         f"group '{args.group_title}' only. Add members to that group to give them access.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    path = config_file(args)
    config = load_config(path)
    refuse_existing_install(config)
    return setup(connect_agol(config), config, path, args)


if __name__ == "__main__":
    raise SystemExit(main())

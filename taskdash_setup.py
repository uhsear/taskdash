"""Take a new organization from a filled-in config to a working, private TaskDash install.

Driver: taskdash_setup.py (run by hand, once per organization)
Source: TaskDash.config.json with the ArcGIS sign-in filled in and no arcgis item ids
Target: one folder holding the hosted service (5 tables), its read-only view, a private viewers
        group and the dashboard; the three new ids are written back to the config

    python taskdash_setup.py            # check: sign in, run the refusals, print the plan
    python taskdash_setup.py --apply    # create everything and write the ids to the config

Only the view and the dashboard are shared, and only with the new group. The service is never
shared, and nothing is ever shared with everyone or the organization. Setup never updates an
item it did not create in the same run.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import taskdash_dashboard as dashboard
import taskdash_publish as publish
from taskdash_common import (
    DEFAULT_CONFIG_FILE,
    TASKDASH_VERSION,
    atomic_write_json,
    connect_agol,
    emit,
    get_tables,
    load_config,
    normalize_text,
    runtime_dir,
)

# A set key means this config already belongs to an install; setup would orphan it.
INSTALL_KEYS = ("item_id", "view_item_id", "dashboard_id")
FEATURE_SERVICE = "Feature Service"
DASHBOARD = "Dashboard"
GROUP_TAGS = "taskdash,monitoring"

Created = List[Tuple[str, str, str]]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set up a new, private TaskDash install.",
                                     allow_abbrev=False)
    parser.add_argument("--config", default="", metavar="PATH",
                        help="Config file to read and update. Default: the config next to the scripts.")
    parser.add_argument("--apply", action="store_true",
                        help="Create the folder, service, view, group and dashboard.")
    parser.add_argument("--folder", default="TaskDash", help="Folder that will hold the new items.")
    parser.add_argument("--title-prefix", default="", help="Text put before every item title.")
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


def refuse_existing_install(config: Dict[str, Any]) -> None:
    agol = config.get("arcgis", {})
    used = [f"arcgis.{k}" for k in INSTALL_KEYS if normalize_text(agol.get(k))]
    if used:
        raise SystemExit(f"The config already sets {', '.join(used)}, so an install exists. Setup is "
                         f"for a new install: use a config without those keys, or maintain this one "
                         f"with taskdash_publish.py --migrate and taskdash_dashboard.py.")


def refuse_name_clashes(gis: Any, args: argparse.Namespace) -> None:
    """Stop before any write when a planned name is taken, so setup cannot adopt or update it."""
    if not normalize_text(args.folder).strip("/"):
        raise SystemExit("--folder must be a folder name. Pass e.g. --folder TaskDash.")
    if gis.content.folders.get(folder=args.folder) is not None:
        raise SystemExit(f"The folder '{args.folder}' already exists for this account. Choose "
                         f"another with --folder, or delete the empty folder in ArcGIS Online.")
    owner = gis.users.me.username
    titles = planned_titles(args.title_prefix)
    for title, item_type in ((titles["service"], FEATURE_SERVICE), (titles["view"], FEATURE_SERVICE),
                             (titles["dashboard"], DASHBOARD)):
        for item in gis.content.search(f'title:"{title}" AND owner:{owner}'):
            if item.title == title and item.type == item_type:
                raise SystemExit(f"{item_type} '{title}' already exists ({item.id}). Setup never "
                                 f"reuses an item: pass --title-prefix to pick other titles, or "
                                 f"delete that item in ArcGIS Online.")
    for group in gis.groups.search(f'title:"{args.group_title}"'):
        if group.title == args.group_title:
            raise SystemExit(f"A group titled '{args.group_title}' already exists ({group.id}). "
                             f"Choose another with --group-title, or delete the group.")


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
    # The three tables create_service makes are not the five the collector writes to.
    publish.apply_migration(get_tables(item), item=item)
    return item


def create_view_item(gis: Any, item: Any, config: Dict[str, Any], path: Path,
                     created: Created) -> Any:
    view = publish.create_view(gis, item, config, apply=True, target="prod")
    created.append((view.title, FEATURE_SERVICE, view.id))
    write_config_key(path, config, "view_item_id", view.id)
    return view


def create_group(gis: Any, args: argparse.Namespace, created: Created) -> Any:
    group = gis.groups.create(title=args.group_title, tags=GROUP_TAGS,
                              description="Members can open the TaskDash dashboard and its read-only view.",
                              snippet="TaskDash viewers", access="private", is_invitation_only=True)
    if group is None:
        raise SystemExit(f"ArcGIS did not create the group '{args.group_title}'. Check that the "
                         f"account may create groups, then re-run; delete anything listed above first.")
    created.append((args.group_title, "Group", group.id))
    return group


def create_dashboard_item(gis: Any, view: Any, folder: Any, group: Any, config: Dict[str, Any],
                          path: Path, args: argparse.Namespace, created: Created) -> str:
    """Build exactly as taskdash_dashboard.main does for prod, reading the view."""
    title = planned_titles(args.title_prefix)["dashboard"]
    templates = dashboard.load_templates(gis, "")
    layer_ids = dashboard.resolve_layer_ids(gis, view.id, "prod")
    definition = dashboard.build(view.id, templates, layer_ids, portal_url=gis.url, config=config)
    dashboard.validate_definition(definition, layer_ids, config, data_item=view.id)
    dash_id = dashboard.post_dashboard(gis, definition, title, None, config)
    created.append((title, DASHBOARD, dash_id))
    write_config_key(path, config, "dashboard_id", dash_id)
    item = gis.content.get(dash_id)
    if item is None:
        raise SystemExit(f"The new dashboard {dash_id} is not visible to this account. Find it in "
                         f"ArcGIS Online, move it into '{args.folder}' and share it with the group.")
    moved = item.move(folder) or {}
    if not moved.get("success", True):
        raise SystemExit(f"Could not move dashboard {dash_id} into '{args.folder}': {moved}")
    publish.share_item(item, groups=[group])
    return dash_id


def print_created(created: Created) -> None:
    emit("Created by this run (delete these in ArcGIS Online to undo):" if created
         else "This run created nothing.")
    for title, kind, item_id in created:
        emit(f"    {kind:16s} {item_id}  {title}")


def apply_setup(gis: Any, config: Dict[str, Any], path: Path, args: argparse.Namespace,
                created: Created) -> None:
    backup_config(path)
    folder = gis.content.folders.create(args.folder)
    created.append((args.folder, "Folder", (folder.properties or {}).get("id", "")))
    item = create_service_item(gis, config, path, args, created)
    view = create_view_item(gis, item, config, path, created)
    group = create_group(gis, args, created)
    # The Group object, not its id: share() looks an id up in the search index, which does not
    # list a group created seconds ago ("Cannot find group with id", first TDI_ run).
    publish.share_item(view, groups=[group])
    create_dashboard_item(gis, view, folder, group, config, path, args, created)
    publish.require_no_anonymous_access(publish.table_urls(item, view))


def setup(gis: Any, config: Dict[str, Any], path: Path, args: argparse.Namespace) -> int:
    refuse_existing_install(config)
    refuse_name_clashes(gis, args)
    print_plan(args, config)
    if not args.apply:
        emit("DRY RUN - nothing written. Re-run with --apply.")
        return 0
    created: Created = []
    try:
        apply_setup(gis, config, path, args, created)
    finally:
        print_created(created)
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

"""Show TaskDash working on synthetic data, fully apart from the real install.

Driver: `taskdash demo` (or `python taskdash_demo.py`)
Source: taskdash_synthetic.py (invented hosts, runs and logs); arcgis.url and
        arcgis.profile read from the real config in memory
Target: <home>/demo only: its own TaskDash.config.json, Logs, alert_state.json and fixtures;
        with --apply, DEMO_ items in the ArcGIS folder TaskDash_Demo

    taskdash demo            # check: build the data, dry-run collect, print the tiles, task rows,
                             # changes and alert e-mail previews as text, build the dashboard offline
    taskdash demo --apply    # create the DEMO_ items and publish the synthetic data to them
    taskdash demo --remove   # check: list the DEMO_ items it would delete
    taskdash demo --remove --apply   # delete them

The demo config never has alerting on and never holds a password. The real config is read for
two keys and never written. The real alert_state.json is never opened.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import taskdash_common as common
import taskdash_synthetic as generator
from taskdash_common import (
    DEFAULT_CONFIG_FILE, TASKDASH_VERSION, atomic_write_json, emit, normalize_text, see_troubleshooting,
)

TITLE_PREFIX = "DEMO_"
FOLDER = "TaskDash_Demo"
ID_KEYS = ("item_id", "test_item_id", "view_item_id", "test_view_item_id", "dashboard_id")
# Sign-in keys the demo config must not carry: it signs in through the profile alone.
DROPPED_ARCGIS_KEYS = ("username", "password", "username_env", "password_env", "allow_plaintext_password")
# The demo publishes once, with the clock of that run. The real limits (a collector every hour, a
# silent computer left out after 7 days) would turn its heartbeat Late, then Down, then "no data"
# within days. These stretch them: Late after 45 days, Down after 90, and a computer is left out after
# a year, so the page stays as it was built until it is re-run.
DEMO_INTERVAL_MINUTES = 30 * 24 * 60
DEMO_IGNORE_AFTER_DAYS = 365
DEMO_MAINTENANCE_DAYS = 7
# The fixtures of one host: the capture before this one (or None), then the current one.
Fixtures = List[Tuple[Optional[Path], Path]]
STATUS_ORDER = ("Failed", "Overdue", "Stuck", "Blocked", "Warning", "Running", "Not yet run", "Disabled", "OK")
STATUS_WORDS = {"FAILED": "Failed", "OVERDUE": "Overdue", "STUCK": "Stuck", "BLOCKED": "Blocked",
                "WARNING": "Warning"}
OTHER_STATES = {"Running": "Running", "NotYetRun": "Not yet run", "Disabled": "Disabled"}
CHANGE_WORDS = {"became_failing": "Started failing", "recovered": "Recovered",
                "definition_changed": "Task changed", "became_overdue": "Became overdue"}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="taskdash demo", allow_abbrev=False,
                                     description="Run TaskDash on synthetic data in its own folder.")
    parser.add_argument("--apply", action="store_true",
                        help="Create the DEMO_ items in ArcGIS and publish the synthetic data to them. "
                             "With --remove, delete them.")
    parser.add_argument("--remove", action="store_true",
                        help="Delete the DEMO_ items that an earlier --apply created. Lists them "
                             "unless you also pass --apply.")
    parser.add_argument("--config", default="", metavar="PATH",
                        help="The real config to read arcgis.url and arcgis.profile from. It is never "
                             "written. Default: the install's config.")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    return parser.parse_args(argv)


def real_arcgis_keys(config_arg: str = "", strict: bool = True) -> Dict[str, Any]:
    """The real config's arcgis section, read in memory, or {} when the install has no config.
    A config that cannot be read stops a run that signs in (strict). Without a sign-in the demo
    runs offline and needs no real key, so it warns and goes on."""
    path = Path(config_arg) if config_arg else common.home_dir() / DEFAULT_CONFIG_FILE
    if not config_arg and not path.is_file():
        return {}
    try:
        return dict(common.load_config(path).get("arcgis", {}))
    except SystemExit as exc:
        if strict:
            raise
        emit(f"WARNING: the real config was not read, so this offline demo uses the example's "
             f"arcgis.url and arcgis.profile. {exc}")
        return {}


def item_ids(arcgis: Dict[str, Any]) -> set:
    return {normalize_text(arcgis.get(key)) for key in ID_KEYS} - {""}


def read_demo_arcgis(config_path: Path) -> Dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8-sig")).get("arcgis", {})
    except (OSError, ValueError, AttributeError) as exc:
        raise SystemExit(f"The demo config {config_path} cannot be read: {type(exc).__name__}. Delete any "
                         "DEMO_ items it names, then delete the file and run the command again.\n"
                         + see_troubleshooting("The demo install is incomplete")) from exc


def demo_maintenance_windows(now: datetime) -> List[Dict[str, Any]]:
    """One dated window and one open-ended mute, so the Why column shows both kinds of note."""
    start = generator.iso(now)
    return [{"task": "^Weekly Cleanup$", "start": start,
             "end": generator.iso(now + timedelta(days=DEMO_MAINTENANCE_DAYS)),
             "reason": "Demo: planned maintenance"},
            {"task": "^Legacy Sync$", "start": start, "end": None,
             "reason": "Demo: retired task, muted until this entry is removed"}]


def build_demo_config(demo: Path, real: Dict[str, Any], overrides: Dict[str, str],
                      previous: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    config = json.loads((common.data_dir() / "TaskDash.config.example.json").read_text(encoding="utf-8-sig"))
    config.pop("_notes", None)
    config["host_name_override"] = ""
    arcgis = config["arcgis"]
    for key in DROPPED_ARCGIS_KEYS:
        arcgis.pop(key, None)
    for key in ("url", "profile"):
        arcgis[key] = normalize_text(real.get(key)) or arcgis[key]
    for key in ID_KEYS:
        arcgis[key] = normalize_text(previous.get(key))
    config["alerting"].update(enabled=False, to=[], healthcheck_url="",
                              maintenance_windows=demo_maintenance_windows(now or datetime.now(timezone.utc)))
    config["scheduler"].update(collector_interval_minutes=DEMO_INTERVAL_MINUTES,
                               heartbeat_ignore_after_days=DEMO_IGNORE_AFTER_DAYS)
    config["logging"]["log_dir"] = str(demo / "Logs")
    config["metrics"]["log_directory_overrides"] = overrides
    return config


def write_json(path: Path, payload: Dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=0, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


def write_demo_home(demo: Path, real: Dict[str, Any]) -> Fixtures:
    """Write fixtures, metric logs and the config; return each host's fixtures, the capture before
    this one first. Publishing that pair in order gives the Recent changes rows: the first publish
    is a baseline, the second is a change. The config keeps item ids a previous --apply stored and
    takes only arcgis.url and arcgis.profile from the real one."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fixtures = demo / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    pairs: Fixtures = []
    for host_no, host in enumerate(generator.HOSTS, start=1):
        stem = f"synthetic_{host.lower().replace('-', '_')}"
        payload = generator.build_host(host_no, now, 1)
        before = generator.prior_fixture(payload)
        pairs.append((write_json(fixtures / f"{stem}_before.json", before) if before else None,
                      write_json(fixtures / f"{stem}.json", payload)))
    overrides = generator.write_metric_logs(demo / "metric_logs", now, generic=True)
    config_path = demo / DEFAULT_CONFIG_FILE
    atomic_write_json(config_path, build_demo_config(demo, real, overrides, read_demo_arcgis(config_path), now))
    (demo / "Logs").mkdir(exist_ok=True)
    return pairs


def refuse_shared_ids(demo_arcgis: Dict[str, Any], real: Dict[str, Any]) -> None:
    shared = item_ids(demo_arcgis) & item_ids(real)
    if shared:
        raise SystemExit(f"The demo config uses item id(s) {', '.join(sorted(shared))} that the real config "
                         "also uses, so publishing would overwrite real data. Delete the demo folder's "
                         "TaskDash.config.json and run `taskdash demo --apply` again.\n"
                         + see_troubleshooting("The demo and the real install share an item"))


def run_publish(publish: Any, argv: List[str]) -> None:
    code = publish.main(argv)
    if code:
        raise SystemExit(f"The demo collect run exited with code {code}. Read the newest log in "
                         f"{common.home_dir() / 'demo' / 'Logs'}, fix the cause, then re-run `taskdash demo`.\n"
                         + see_troubleshooting("The demo collect run failed"))


def collect_quietly(config: Dict[str, Any], fixture: Path) -> Dict[str, Any]:
    """The collector's own derivation of one fixture. Its per-task lines go to the log, not the screen."""
    import taskdash_publish as publish

    with contextlib.redirect_stdout(io.StringIO()):
        return publish.collect_everything(config, fixture, first_run_budget=False)


def state_rows(inventory: List[Dict[str, Any]]) -> List[Tuple[str, str, str, str]]:
    """(status word, task, host, why) for each task a viewer would notice, in Task health order."""
    rows = []
    for task in inventory:
        word = STATUS_WORDS.get(task.get("attention_kind")) or OTHER_STATES.get(task.get("health_status"), "")
        notes = [text for text in (
            task.get("attention_reason") or "",
            f"{task['latest_log_error_lines']} log errors" if task.get("latest_log_error_lines") else "",
            "setting notes" if task.get("config_warnings") else "") if text]
        if word or notes:
            rows.append((word or "OK", task["task_name"], task["host_name"], "; ".join(notes)[:110]))
    return sorted(rows, key=lambda row: STATUS_ORDER.index(row[0]))


def print_summary(config: Dict[str, Any], fixtures: Fixtures) -> None:
    """The tiles, the task rows and the change rows as text: what `--apply` shows on the dashboard.
    A dry run writes no dashboard, so this is how an offline reader sees the result."""
    import taskdash_publish as publish

    privacy = config.get("privacy") or {}
    inventory: List[Dict[str, Any]] = []
    changes: List[Dict[str, Any]] = []
    for before, fixture in fixtures:
        current = collect_quietly(config, fixture)
        audit = current["audit_dt"]
        with contextlib.redirect_stdout(io.StringIO()):
            publish.apply_mute_notes(current["inventory"], config, audit)
        inventory += current["inventory"]
        if before:
            prior = {row["task_full_name"]: row for row in collect_quietly(config, before)["inventory"]}
            changes += publish.detect_task_changes(
                prior, current["inventory"], current["host_name"], audit.isoformat(), {},
                publish.privacy_hides(privacy, "hide_command_lines"),
                publish.privacy_hides(privacy, "hide_accounts"), publish.privacy_hides(privacy, "hide_paths"))
    kinds = [task.get("attention_kind") for task in inventory]
    needs = sum(1 for task in inventory if task.get("needs_attention"))
    emit(f"Tiles (invented data): Needs attention {needs} (Failed {kinds.count('FAILED')}, "
         f"Overdue {kinds.count('OVERDUE')}, Other warnings {needs - kinds.count('FAILED') - kinds.count('OVERDUE')}), "
         f"Disabled {sum(1 for task in inventory if task.get('health_status') == 'Disabled')}, "
         f"Tasks tracked {len(inventory)}.")
    emit("Task health rows that show a status or a note:")
    for word, task, host, why in state_rows(inventory):
        emit(f"    {word:12s} {task:18s} {host}  {why}")
    emit("Recent changes rows:")
    for change in sorted(changes, key=lambda row: (row["host_name"], row["task_name"])):
        emit(f"    {CHANGE_WORDS.get(change['change_kind'], change['change_kind']):16s} "
             f"{change['task_name']:18s} {change['host_name']}")


def preview_alerts(demo: Path, fixtures: Fixtures) -> List[Path]:
    """Run --preview-alert on each host's fixture and keep each preview under its own name: two runs in
    one second would write the same time-stamped file. Nothing is sent, and no SMTP setting is read."""
    import taskdash_publish as publish

    logs = demo / "Logs"
    # With no alert state a preview lists every failure as already open and shows no incident, so
    # the demo starts as if a healthy run came first.
    state_path = logs / "alert_state.json"
    if not state_path.exists():
        common.atomic_write_json(state_path, {"signature": [], "updated_utc": common.utc_now().isoformat()})
    kept: List[Path] = []
    for _, fixture in fixtures:
        run_publish(publish, ["--preview-alert", "--fixture", str(fixture),
                              "--config", str(demo / DEFAULT_CONFIG_FILE)])
        fresh = sorted(logs.glob("alert_preview_[0-9]*.txt"), key=lambda path: path.stat().st_mtime_ns)
        if fresh:
            kept.append(fresh[-1].replace(logs / f"alert_preview_{fixture.stem.removeprefix('synthetic_')}.txt"))
    return kept


def check_run(demo: Path, fixtures: Fixtures) -> None:
    import taskdash_dashboard as dashboard
    import taskdash_publish as publish

    config_path = demo / DEFAULT_CONFIG_FILE
    for _, fixture in fixtures:
        run_publish(publish, ["--fixture", str(fixture), "--config", str(config_path)])
    # Not `dashboard --offline`: it writes its definition into the real install's folder.
    layer_ids = dict(dashboard.DEFAULT_LAYER_IDS)
    config = common.load_config(config_path)
    definition = dashboard.build(dashboard.OFFLINE_ITEM_ID, dashboard.load_templates(None, ""), layer_ids,
                                 config=config, demo=True)
    dashboard.validate_definition(definition, layer_ids, config)
    out_path = demo / dashboard.OUTPUT_JSON
    out_path.write_text(json.dumps(definition, indent=1), encoding="utf-8")
    emit(f"Demo dashboard definition written to {out_path}")
    print_summary(config, fixtures)
    previews = preview_alerts(demo, fixtures)
    emit("Alert e-mail previews (nothing is sent; the demo config has alerting off):")
    for path in previews:
        emit(f"    {path}")


def apply_run(demo: Path, fixtures: Fixtures, real: Dict[str, Any]) -> None:
    import taskdash_publish as publish
    import taskdash_setup as setup

    config_path = demo / DEFAULT_CONFIG_FILE
    if not (real.get("url") and real.get("profile")):
        raise SystemExit("--apply signs in only through the real config's arcgis.url and arcgis.profile, "
                         "and one of them is empty. A username or password is never copied to the demo. "
                         "Store a profile as INSTALL.md step 3 describes, set arcgis.profile, then re-run "
                         "`taskdash demo --apply`.\n" + see_troubleshooting("The demo needs a real sign-in"))
    refuse_shared_ids(read_demo_arcgis(config_path), real)
    stored = read_demo_arcgis(config_path)
    missing = [key for key in setup.INSTALL_KEYS if not normalize_text(stored.get(key))]
    if 0 < len(missing) < len(setup.INSTALL_KEYS):
        raise SystemExit(f"The demo config lacks arcgis.{', arcgis.'.join(missing)}. A failed demo setup "
                         f"left a partial install. Delete the {TITLE_PREFIX} items in the {FOLDER} folder "
                         f"and {config_path}, then re-run `taskdash demo --apply`.\n"
                         + see_troubleshooting("The demo install is incomplete"))
    if missing:
        setup.main(["--config", str(config_path), "--apply", "--folder", FOLDER, "--title-prefix", TITLE_PREFIX,
                    "--demo"])
        refuse_shared_ids(read_demo_arcgis(config_path), real)
    for before, fixture in fixtures:
        # The capture before this one goes first so the second publish finds changes to record. A refresh
        # skips it: the stored inventory is already the current capture, and a repeat would invent changes.
        for path in ((before, fixture) if missing else (fixture,)):
            if path:
                run_publish(publish, ["--fixture", str(path), "--config", str(config_path), "--apply",
                                      "--target", "prod"])


def print_apply_plan(demo: Path) -> None:
    emit(f"Plan for --apply: folder '{FOLDER}', items titled '{TITLE_PREFIX}TaskDash' (service, view, "
         f"dashboard) and a private group '{TITLE_PREFIX}TaskDash viewers', created by `taskdash setup`.")
    emit(f"Plan for --apply: the synthetic data in {demo / 'fixtures'} is published to those items only.")
    emit("The text above is the same data the dashboard shows. The dashboard itself exists only after --apply.")
    emit("DRY RUN - nothing was signed in to or written to ArcGIS. Re-run with --apply.")


def forget_demo_items(config_path: Path) -> None:
    """Empty the item ids after the items are deleted, so the next --apply creates new ones. Left in
    place, they would send the next publish to items that no longer exist."""
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    raw.setdefault("arcgis", {}).update({key: "" for key in ID_KEYS})
    atomic_write_json(config_path, raw)


def remove_run(demo: Path, real: Dict[str, Any], apply: bool) -> int:
    import taskdash_remove as remover
    import taskdash_setup as setup

    config_path = demo / DEFAULT_CONFIG_FILE
    manifest = remover.load_manifest(demo / setup.MANIFEST_FILE)
    refuse_shared_ids(read_demo_arcgis(config_path), real)
    config = common.load_config(config_path)
    code = remover.remove(remover.connect_agol(config), manifest, apply, "", demo=True)
    if apply and code == 0:
        forget_demo_items(config_path)
        emit("The demo items are removed. Run `taskdash demo --apply` to make them again.")
    return code


def finish_apply(demo: Path, fixtures: Fixtures) -> None:
    import taskdash_dashboard as dashboard
    import taskdash_publish as publish

    for path in preview_alerts(demo, fixtures):
        emit(f"Alert e-mail preview (nothing was sent): {path}")
    emit("Demo published. Next, go on with your own install at step 4 of docs/INSTALL.md.")
    late, down = (minutes / 1440 for minutes in dashboard.heartbeat_thresholds(DEMO_INTERVAL_MINUTES))
    emit(f"The dashboard keeps the time of this run. Its heartbeat reads OK for {late:g} days, Late until "
         f"{down:g} days, then Down. Run `taskdash demo --apply` again to refresh it. To delete it, run "
         "`taskdash demo --remove --apply`.")
    emit(f"Open: {publish.alert_dashboard_link(common.load_config(demo / DEFAULT_CONFIG_FILE))}")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    demo = common.home_dir() / "demo"
    demo.mkdir(parents=True, exist_ok=True)
    real = real_arcgis_keys(args.config, strict=args.apply)
    if args.remove:
        return remove_run(demo, real, args.apply)
    fixtures = write_demo_home(demo, real)
    emit(f"Demo home: {demo}")
    if args.apply:
        apply_run(demo, fixtures, real)
        finish_apply(demo, fixtures)
        return 0
    check_run(demo, fixtures)
    print_apply_plan(demo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Show TaskDash working on synthetic data, fully apart from the real install.

Driver: `taskdash demo` (or `python taskdash_demo.py`)
Source: taskdash_synthetic.py (invented hosts, runs and logs); arcgis.url and
        arcgis.profile read from the real config in memory
Target: <home>/demo only: its own TaskDash.config.json, Logs, alert_state.json and fixtures;
        with --apply, DEMO_ items in the ArcGIS folder TaskDash_Demo

    taskdash demo            # check: build the data, dry-run collect, build the dashboard offline
    taskdash demo --apply    # create the DEMO_ items and publish the synthetic data to them

The demo config never has alerting on and never holds a password. The real config is read for
two keys and never written. The real alert_state.json is never opened.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="taskdash demo", allow_abbrev=False,
                                     description="Run TaskDash on synthetic data in its own folder.")
    parser.add_argument("--apply", action="store_true",
                        help="Create the DEMO_ items in ArcGIS and publish the synthetic data to them.")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    return parser.parse_args(argv)


def real_arcgis_keys() -> Dict[str, Any]:
    """The real config's arcgis section, read in memory, or {} when the install has no config."""
    path = common.home_dir() / DEFAULT_CONFIG_FILE
    return dict(common.load_config(path).get("arcgis", {})) if path.is_file() else {}


def item_ids(arcgis: Dict[str, Any]) -> set:
    return {normalize_text(arcgis.get(key)) for key in ID_KEYS} - {""}


def read_demo_arcgis(config_path: Path) -> Dict[str, Any]:
    if not config_path.is_file():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8-sig")).get("arcgis", {})


def build_demo_config(demo: Path, real: Dict[str, Any], overrides: Dict[str, str],
                      previous: Dict[str, Any]) -> Dict[str, Any]:
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
    config["alerting"].update(enabled=False, to=[], healthcheck_url="")
    config["logging"]["log_dir"] = str(demo / "Logs")
    config["metrics"]["log_directory_overrides"] = overrides
    return config


def write_demo_home(demo: Path, real: Dict[str, Any]) -> List[Path]:
    """Write fixtures, metric logs and the config; return the fixture paths. The config keeps item
    ids a previous --apply stored and takes only arcgis.url and arcgis.profile from the real one."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    fixtures = demo / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    paths = []
    for host_no, host in enumerate(generator.HOSTS, start=1):
        path = fixtures / f"synthetic_{host.lower().replace('-', '_')}.json"
        path.write_text(json.dumps(generator.build_host(host_no, now, 1), indent=0, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
        paths.append(path)
    overrides = generator.write_metric_logs(demo / "metric_logs", now)
    config_path = demo / DEFAULT_CONFIG_FILE
    atomic_write_json(config_path, build_demo_config(demo, real, overrides, read_demo_arcgis(config_path)))
    (demo / "Logs").mkdir(exist_ok=True)
    return paths


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


def check_run(demo: Path, fixtures: List[Path]) -> None:
    import taskdash_dashboard as dashboard
    import taskdash_publish as publish

    config_path = demo / DEFAULT_CONFIG_FILE
    for fixture in fixtures:
        run_publish(publish, ["--fixture", str(fixture), "--config", str(config_path)])
    # Not `dashboard --offline`: it writes its definition into the real install's folder.
    layer_ids = dict(dashboard.DEFAULT_LAYER_IDS)
    config = common.load_config(config_path)
    definition = dashboard.build(dashboard.OFFLINE_ITEM_ID, dashboard.load_templates(None, ""), layer_ids,
                                 config=config)
    dashboard.validate_definition(definition, layer_ids, config)
    out_path = demo / dashboard.OUTPUT_JSON
    out_path.write_text(json.dumps(definition, indent=1), encoding="utf-8")
    emit(f"Demo dashboard definition written to {out_path}")


def apply_run(demo: Path, fixtures: List[Path], real: Dict[str, Any]) -> None:
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
        setup.main(["--config", str(config_path), "--apply", "--folder", FOLDER, "--title-prefix", TITLE_PREFIX])
        refuse_shared_ids(read_demo_arcgis(config_path), real)
    for fixture in fixtures:
        run_publish(publish, ["--fixture", str(fixture), "--config", str(config_path), "--apply", "--target", "prod"])


def print_apply_plan(demo: Path) -> None:
    emit(f"Plan for --apply: folder '{FOLDER}', items titled '{TITLE_PREFIX}TaskDash' (service, view, "
         f"dashboard) and a private group '{TITLE_PREFIX}TaskDash viewers', created by `taskdash setup`.")
    emit(f"Plan for --apply: the synthetic data in {demo / 'fixtures'} is published to those items only.")
    emit("DRY RUN - nothing was signed in to or written to ArcGIS. Re-run with --apply.")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    demo = common.home_dir() / "demo"
    demo.mkdir(parents=True, exist_ok=True)
    real = real_arcgis_keys()
    fixtures = write_demo_home(demo, real)
    emit(f"Demo home: {demo}")
    if args.apply:
        apply_run(demo, fixtures, real)
        emit("Demo published. Open the DEMO_TaskDash dashboard in the TaskDash_Demo folder.")
        return 0
    check_run(demo, fixtures)
    print_apply_plan(demo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

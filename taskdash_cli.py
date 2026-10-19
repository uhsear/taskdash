"""Command-line entry point for an installed TaskDash.

Driver: the `taskdash` console script (pip install) or `python taskdash_cli.py`
Source: argv; the newest local run log; the CollectorRuns table (read-only, `status` only)
Target: the subcommand's own main(); this module writes nothing

    taskdash collect --apply --target prod     # taskdash_publish.main
    taskdash setup | preflight | dashboard     # taskdash_setup / taskdash_preflight / taskdash_dashboard
    taskdash demo [--apply]                    # taskdash_demo: synthetic data in its own folder
    taskdash status [--offline]                # last local run result + newest CollectorRuns row
    taskdash test-alert [--apply]              # check the alert e-mail path; --apply sends one test message

Every subcommand receives the rest of argv unchanged. Subcommand modules are imported only
when called, so a module missing from an install fails that one subcommand and nothing else.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from taskdash_common import TABLE_COLLECTOR_RUNS, TASKDASH_VERSION, emit, runtime_dir, write_bootstrap_fail

# subcommand -> module whose main(argv) -> int does the work
SUBCOMMAND_MODULES: Dict[str, str] = {
    "collect": "taskdash_publish",
    "setup": "taskdash_setup",
    "preflight": "taskdash_preflight",
    "dashboard": "taskdash_dashboard",
    "demo": "taskdash_demo",
}
USAGE = (
    "usage: taskdash [--version] <command> [args...]\n"
    "commands: collect, setup, preflight, dashboard, demo, status, test-alert\n"
    "Run `taskdash <command> --help` for a command's own options."
)


def _run_module(module_name: str, rest: List[str]) -> int:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            write_bootstrap_fail(traceback.format_exc())
            raise
        message = (f"{module_name}.py is not part of this install, so this command is unavailable. "
                   "Reinstall TaskDash from a complete wheel.")
        write_bootstrap_fail(message)
        raise SystemExit(message)
    except Exception:
        write_bootstrap_fail(f"{module_name} failed to import. Reinstall TaskDash; if it persists, "
                             f"send this traceback to the maintainer.\n{traceback.format_exc()}")
        raise
    return int(module.main(rest) or 0)


def newest_run_log(log_dir: Path) -> Optional[Path]:
    logs = sorted(log_dir.glob("taskdash_*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def last_result_line(log_path: Path) -> str:
    lines = [line.strip() for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()]
    return next((line for line in reversed(lines) if line), "(log is empty)")


def _local_status(log_dir: Path) -> None:
    log_path = newest_run_log(log_dir)
    if log_path is None:
        emit(f"Local: no taskdash_*.log in {log_dir} yet. Run `taskdash collect` first.")
        return
    emit(f"Local: {log_path.name}: {last_result_line(log_path)}")


def _epoch_text(value: object) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def newest_collector_run(table: object) -> Optional[Dict[str, object]]:
    result = table.query(where="1=1", out_fields="started_utc,exit_status", return_geometry=False,
                         order_by_fields="started_utc DESC", result_record_count=1)
    features = getattr(result, "features", None) or []
    return dict(features[0].attributes) if features else None


def _remote_status(config: dict, target: str) -> None:
    from taskdash_common import connect_agol, get_tables, normalize_text

    key = "item_id" if target == "prod" else "test_item_id"
    item_id = normalize_text(config.get("arcgis", {}).get(key))
    if not item_id:
        raise SystemExit(f"No arcgis.{key} in the config, so there is no {target} service to read. "
                         "Set it, or use `taskdash status --offline`.")
    item = connect_agol(config).content.get(item_id)
    if item is None:
        raise SystemExit(f"Service item {item_id} was not found or is not visible to this account.")
    table = get_tables(item).get(TABLE_COLLECTOR_RUNS)
    if table is None:
        raise SystemExit(f"The service has no {TABLE_COLLECTOR_RUNS} table. Run `taskdash collect --migrate --apply` to create it.")
    row = newest_collector_run(table)
    if row is None:
        emit(f"Service: {TABLE_COLLECTOR_RUNS} has no rows yet.")
        return
    emit(f"Service: last collector run started {_epoch_text(row.get('started_utc'))} UTC, "
         f"exit_status {row.get('exit_status')}")


def _status_log_dir(config_path: str, offline: bool) -> Tuple[Path, Optional[dict]]:
    """(log dir, config). Offline works without a config, as CI and a fresh install have none."""
    from taskdash_common import DEFAULT_CONFIG_FILE, load_config
    import taskdash_publish

    path = Path(config_path) if config_path else runtime_dir() / DEFAULT_CONFIG_FILE
    if offline and not path.exists():
        return runtime_dir() / "Logs", None
    config = load_config(path)
    return taskdash_publish.resolve_log_dir(config), config


def status(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="taskdash status", allow_abbrev=False,
                                     description="Read-only: last local run result and newest CollectorRuns row.")
    parser.add_argument("--offline", action="store_true", help="Print only the local part; never sign in.")
    parser.add_argument("--target", choices=("prod", "test"), default="prod")
    parser.add_argument("--config", default="", metavar="PATH", help="Config file. Default: the install's config.")
    args = parser.parse_args(argv)
    log_dir, config = _status_log_dir(args.config, args.offline)
    _local_status(log_dir)
    if not args.offline:
        _remote_status(config, args.target)
    return 0


def send_test_alert(argv: Sequence[str]) -> int:
    """Check by default: print who would get a test message and through which server. --apply
    sends it with the same send_email the hourly run uses, so a pass proves the real path."""
    parser = argparse.ArgumentParser(
        prog="taskdash test-alert", allow_abbrev=False,
        description="Check the alert e-mail path. Sends nothing unless you pass --apply.")
    parser.add_argument("--apply", action="store_true", help="Send one short test message.")
    parser.add_argument("--config", default="", metavar="PATH", help="Config file. Default: the install's config.")
    args = parser.parse_args(argv)

    import taskdash_common as common
    import taskdash_publish

    path = Path(args.config) if args.config else runtime_dir() / common.DEFAULT_CONFIG_FILE
    config = common.load_config(path)
    alerting = config.get("alerting", {})
    recipients = [r for r in (alerting.get("to") or []) if common.normalize_text(r)]
    if not recipients:
        raise SystemExit("alerting.to is empty, so the test message has no recipient. "
                         "Add at least one address to alerting.to in the config.")
    mail = common.load_mail_settings(config)
    common.warn_legacy_mail(config)
    if not mail:
        raise SystemExit("There are no usable mail settings, so the test message cannot be sent. "
                         "Fix the smtp block in the config (host, from, user and the stored "
                         "password); the WARNING above says which part is missing.")
    if not alerting.get("enabled"):
        emit("WARNING: alerting.enabled is false, so the hourly run sends no alerts yet.")

    host = common.get_host_name(config)
    subject = f"TaskDash test alert from {host}"
    body = (f"This is a test message from TaskDash on {host}. If you can read it, "
            "alert e-mails from this host reach you. No action is needed.")
    server = f"{mail['smtp server']}:{mail.get('smtp port', 587)}"
    if not args.apply:
        emit(f"Would send one test message '{subject}' to {', '.join(recipients)} "
             f"from {mail['from address']} through {server}.")
        emit("Nothing sent. Re-run with --apply to send it.")
        return 0

    common.last_send_error = ""
    if not common.send_email(mail, recipients, subject, body):
        reason = common.last_send_error or "the mail server did not accept the message"
        emit(f"Test alert failed: {taskdash_publish._scrub_secrets(reason)}. Check smtp.host, "
             f"smtp.port, smtp.from and smtp.user in the config and the stored SMTP password.")
        return 1
    emit(f"Test alert sent to {', '.join(recipients)} through {server}.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    if command == "--version":
        print(f"TaskDash {TASKDASH_VERSION}")
        return 0
    if command == "status":
        return status(rest)
    if command == "test-alert":
        return send_test_alert(rest)
    if command not in SUBCOMMAND_MODULES:
        print(f"taskdash: unknown command {command!r}\n{USAGE}", file=sys.stderr)
        return 2
    return _run_module(SUBCOMMAND_MODULES[command], rest)


if __name__ == "__main__":
    sys.exit(main())

"""Command-line entry point for an installed TaskDash.

Driver: the `taskdash` console script (pip install) or `python taskdash_cli.py`
Source: argv; the newest local run log; the CollectorRuns table (read-only, `status` only)
Target: the subcommand's own main(); this module writes nothing, except that
        `status --prune --apply` deletes TaskDash's own old backups and logs

    taskdash collect --apply --target prod     # taskdash_publish.main
    taskdash setup | preflight | dashboard     # taskdash_setup / taskdash_preflight / taskdash_dashboard
    taskdash demo [--apply | --remove [--apply]]  # taskdash_demo: synthetic data in its own folder
    taskdash remove [--apply]                  # undo setup from its manifest: list (--apply: delete) the items it made
    taskdash signin [--apply]                  # check the ArcGIS account; --apply stores the sign-in profile
    taskdash status [--offline] [--host NAME]  # last local run result + newest CollectorRuns row; exit 0-3
    taskdash status --prune [--apply] [--keep N]  # list (--apply: remove) old backups and logs
    taskdash test-alert [--apply]              # check the alert e-mail path; --apply sends one test message

Every subcommand receives the rest of argv unchanged. Subcommand modules are imported only
when called, so a module missing from an install fails that one subcommand and nothing else.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import importlib
import json
import os
import re
import stat
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

def _write_import_fallback(message: str) -> None:
    """Standard library only: keep a failed import of taskdash_common as Logs/bootstrap_FAIL_<stamp>.log.

    write_bootstrap_fail lives in taskdash_common, so it cannot report that module's own import
    failure. This copy follows its rules: wheel installs only, same folder as home_dir (TASKDASH_HOME,
    the working directory, or the folder that owns the running venv), never raises. The traceback is
    not scrubbed: scrubbing needs the modules that just failed to import.
    """
    if (Path(__file__).resolve().parent / "rules" / "default.json").is_file():
        return
    try:
        if os.environ.get("TASKDASH_HOME"):
            home = Path(os.environ["TASKDASH_HOME"]).resolve()
        else:
            home = Path.cwd().resolve()
            # Keep in step with home_dir() in taskdash_common, which cannot be imported here.
            venv_parent = Path(sys.prefix).resolve().parent
            if (sys.prefix != sys.base_prefix and not (home / "TaskDash.config.json").is_file()
                    and (venv_parent / "TaskDash.config.json").is_file()):
                home = venv_parent
        log_dir = home / "Logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        (log_dir / f"bootstrap_FAIL_{stamp}.log").write_text(message.rstrip() + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"TaskDash could not write bootstrap_FAIL log: {exc}", file=sys.stderr)


try:
    from taskdash_common import (
        CONFIG_ARG_HELP, MANUAL_LOG_PREFIX, TABLE_COLLECTOR_RUNS, TASKDASH_VERSION, RunLockHeld, command_text,
        emit, run_lock, runtime_dir, see_troubleshooting, write_bootstrap_fail,
    )
except Exception:
    _write_import_fallback("taskdash_common failed to import. Reinstall TaskDash; if it persists, "
                           f"send this traceback to the maintainer.\n{traceback.format_exc()}")
    raise

# subcommand -> module whose main(argv) -> int does the work
SUBCOMMAND_MODULES: Dict[str, str] = {
    "collect": "taskdash_publish",
    "setup": "taskdash_setup",
    "preflight": "taskdash_preflight",
    "dashboard": "taskdash_dashboard",
    "demo": "taskdash_demo",
    "remove": "taskdash_remove",
    "signin": "taskdash_signin",
}
USAGE = (
    "usage: taskdash [--version] <command> [args...]\n"
    "Commands, in install order (docs/INSTALL.md has the steps):\n"
    "  signin      check the ArcGIS account and store the sign-in\n"
    "  preflight   check the computer, the config and the sign-in\n"
    "  setup       create the service, view, group and dashboard\n"
    "  collect     collect task health and publish it (the hourly task runs this)\n"
    "  dashboard   rebuild the dashboard\n"
    "  status      show the last run and its age\n"
    "  test-alert  check the alert e-mail path\n"
    "  demo        try TaskDash on invented data\n"
    "  remove      undo setup\n"
    "  task        show the scripts that register, change and remove the Windows task\n"
    "Run `taskdash <command> --help` for a command's own options; `taskdash help` shows this list."
)


def show_task_scripts() -> int:
    """`taskdash task`: the Windows task and the installer are PowerShell scripts, not commands, so
    point at them. Reads nothing from the config and changes nothing."""
    from taskdash_common import data_dir
    share = data_dir()
    emit("The scheduled task is registered, changed and removed by PowerShell scripts, not by a taskdash "
         "command. Run them in a PowerShell window as administrator.")
    emit(f"  Register or replace the hourly task: {share / 'install_task.ps1'}")
    emit("    Without -Apply it only prints the plan. -Replace changes the task account password: see ROTATE.md.")
    emit("  Install on a new computer: Install-TaskDash.ps1 from the release page. See INSTALL.md (Quick "
         "install). Upgrade an install by hand: see UPGRADE.md.")
    emit("  Remove: run `taskdash remove --apply`, then `taskdash signin --forget --apply`, then "
         "Unregister-ScheduledTask. See 'Undo the install' in INSTALL.md.")
    emit(f"  The guides are in {share / 'docs'}.")
    return 0


def _run_module(module_name: str, rest: List[str]) -> int:
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            write_bootstrap_fail(traceback.format_exc())
            raise SystemExit(f"{module_name} needs the Python package '{exc.name}', which is not installed. "
                             "Reinstall TaskDash with its dependencies: python -m pip install --force-reinstall "
                             "<wheel>.\n"
                             + see_troubleshooting("A package that TaskDash needs is missing")) from exc
        message = (f"{module_name}.py is not part of this install, so this command is unavailable. "
                   "Reinstall TaskDash from a complete wheel.")
        write_bootstrap_fail(message)
        raise SystemExit(message + "\n" + see_troubleshooting("A command says it is not part of this install"))
    except Exception:
        write_bootstrap_fail(f"{module_name} failed to import. Reinstall TaskDash; if it persists, "
                             f"send this traceback to the maintainer.\n{traceback.format_exc()}")
        raise
    return int(module.main(rest) or 0)


def newest_run_log(log_dir: Path) -> Optional[Path]:
    """The newest log of a scheduled run. A manual run (dry run, test run, --migrate) writes
    taskdash_manual_*.log and is skipped: it would hide what the hourly task did."""
    logs = sorted((p for p in log_dir.glob("taskdash_*.log") if not p.name.startswith(MANUAL_LOG_PREFIX + "_")),
                  key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


_LOG_RECORD_START = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def last_result_line(log_path: Path) -> str:
    """The last non-empty log line. A refusal ends with a 'See TROUBLESHOOTING.md' line that names no
    error, so there the whole last record is joined into one line: the 'TaskDash exiting:' text first."""
    lines = [line.strip() for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "(log is empty)"
    start = len(lines) - 1
    if lines[start].startswith("See TROUBLESHOOTING.md"):
        while start > 0 and not _LOG_RECORD_START.match(lines[start]):
            start -= 1
    return " ".join(lines[start:])


def _collector_interval(config: Optional[dict]) -> float:
    from taskdash_dashboard import DEFAULT_COLLECTOR_INTERVAL_MINUTES

    return ((config or {}).get("scheduler") or {}).get("collector_interval_minutes", DEFAULT_COLLECTOR_INTERVAL_MINUTES)


def _local_status(log_dir: Path, interval: float, offline: bool = True) -> int:
    """Print the newest scheduled run's last line; return the exit code: 0 fresh and no ERROR line,
    1 the log holds an ERROR line (the run failed), 2 no log or older than 2 collector intervals.
    Online (offline False) only the "Local:" line prints: the service row decides, and this
    computer may not be the collector, so its log must not produce advice or a verdict."""
    log_path = newest_run_log(log_dir)
    if log_path is None:
        if not offline:
            emit(f"Local: no scheduled run log (taskdash_<time>.log) in {log_dir}.")
            return 2
        emit(f"Local: no scheduled run log (taskdash_<time>.log) in {log_dir} yet. Run "
             f"{command_text('collect', '--apply')} once, or wait for the hourly task.")
        return 2
    emit(f"Local: {log_path.name}: {last_result_line(log_path)}")
    if not offline:
        return 0
    if " - ERROR - " in log_path.read_text(encoding="utf-8", errors="replace"):
        emit("The newest scheduled run failed: its log holds an ERROR line. Open the log and search for "
             "the first ERROR line.\n" + see_troubleshooting("The newest run failed"))
        return 1
    age = (time.time() - log_path.stat().st_mtime) / 60
    if age > 2 * interval:
        emit(f"The newest scheduled run is {_age_text(age)} old, more than {2 * interval:g} min "
             "(2 x scheduler.collector_interval_minutes). Check that the collector task still runs.\n"
             + see_troubleshooting("The newest success is too old"))
        return 2
    return 0


def _handoff_status() -> None:
    """Say so when a sign-in hand-off file waits: it is easy to forget, and it holds sealed passwords."""
    from taskdash_handoff import handoff_waiting_text
    text = handoff_waiting_text()
    if text:
        emit(f"WARNING: {text} " + see_troubleshooting("A hand-off file is waiting"))


def _unsent_runs(log_dir: Path, target: str) -> List[dict]:
    """The CollectorRuns rows a run saved because it could not reach the service (oldest first)."""
    import taskdash_publish

    path = log_dir / taskdash_publish.PENDING_COLLECTOR_RUNS_FILE.format(target=target)
    return taskdash_publish._read_pending_collector_runs(path)


def _unsent_status(log_dir: Path, target: str) -> int:
    """Print the runs saved because they could not reach the service; return how many wait."""
    unsent = _unsent_runs(log_dir, target)
    if unsent:
        newest = unsent[-1]
        emit(f"Unsent: {len(unsent)} collector run(s) could not be saved to the service and wait for the next "
             f"run that reaches it. Newest ({newest.get('exit_status')}): {newest.get('error_text') or 'no error text'}")
    return len(unsent)


def _epoch_text(value: object) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def newest_collector_run(table: object, where: str = "1=1") -> Optional[Dict[str, object]]:
    result = table.query(where=where, out_fields="started_utc,exit_status", return_geometry=False,
                         order_by_fields="started_utc DESC", result_record_count=1)
    features = getattr(result, "features", None) or []
    return dict(features[0].attributes) if features else None


def _age_text(minutes: float) -> str:
    if minutes < 120:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h" if minutes < 2880 else f"{minutes / 1440:.1f} days"


def _minutes_since(epoch_ms: object) -> float:
    return (datetime.now(timezone.utc).timestamp() * 1000 - float(epoch_ms)) / 60000


def _remote_status(config: dict, target: str, host: Optional[str], unsent: int = 0) -> int:
    """Print the newest CollectorRuns row and the age of the newest success; return the exit code:
    0 fresh success, 1 newest run failed, 2 newest success older than 2 collector intervals or no run."""
    from taskdash_common import connect_agol, get_host_name, get_tables, host_clause, normalize_text

    key = "item_id" if target == "prod" else "test_item_id"
    item_id = normalize_text(config.get("arcgis", {}).get(key))
    if not item_id:
        raise SystemExit(f"No arcgis.{key} in the config, so there is no {target} service to read. "
                         "Set it, or use `taskdash status --offline`.\n"
                         + see_troubleshooting("Status has no service to read"))
    item = connect_agol(config).content.get(item_id)
    if item is None:
        raise SystemExit(f"Service item {item_id} was not found or is not visible to this account.\n"
                         + see_troubleshooting("Status cannot find the service item"))
    table = get_tables(item).get(TABLE_COLLECTOR_RUNS)
    if table is None:
        raise SystemExit(f"The service has no {TABLE_COLLECTOR_RUNS} table. Run `taskdash collect --migrate --target {target} --apply` to create it.\n"
                         + see_troubleshooting("The service has no CollectorRuns table"))
    # The default is this install's own host: on a shared service another host's runs must not
    # hide this one's failure. `--host all` reads every host.
    host = host or get_host_name(config)
    where = "1=1" if host.lower() == "all" else host_clause(host)
    scope = "any host" if where == "1=1" else f"host '{host}'"
    row = newest_collector_run(table, where)
    if row is None:
        emit(f"Service: {TABLE_COLLECTOR_RUNS} has no rows for {scope}. Run `taskdash collect --apply`, "
             "or name the collector's host with --host (`--host all` reads every host).")
        return 2
    succeeded = row.get("exit_status") == "success"
    success = row if succeeded else newest_collector_run(table, f"{where} AND exit_status = 'success'")
    age = _minutes_since(success["started_utc"]) if success else None
    emit(f"Service: last collector run started {_epoch_text(row.get('started_utc'))} UTC, "
         f"exit_status {row.get('exit_status')}; "
         + (f"last success {_age_text(age)} ago" if success else "last success: none found") + f" ({scope})")
    if not succeeded:
        emit(f"The newest run failed. Run {command_text('status', '--offline')} for the last line of its "
             "log, then read the log in the log folder.\n"
             + see_troubleshooting("The newest run failed"))
        return 1
    interval = _collector_interval(config)
    if age > 2 * interval:
        emit(f"The newest success is older than {2 * interval:g} min (2 x scheduler.collector_interval_minutes). "
             + ("The collector runs but cannot publish: read the Unsent line above and fix that error."
                if unsent else "Check that the collector task still runs."))
        return 2
    return 0


def _status_log_dir(config_path: str, offline: bool) -> Tuple[Path, Optional[dict]]:
    """(log dir, config). Offline works without a config, as CI and a fresh install have none."""
    from taskdash_common import DEFAULT_CONFIG_FILE, load_config
    import taskdash_publish

    path = Path(config_path) if config_path else runtime_dir() / DEFAULT_CONFIG_FILE
    if offline and not path.exists():
        return runtime_dir() / "Logs", None
    config = load_config(path)
    return taskdash_publish.resolve_log_dir(config), config


# Only files with these exact names are ever pruned. Keep in step with what the code writes:
# backup_dashboard (taskdash_dashboard), backup_config (taskdash_setup), PENDING_COLLECTOR_RUNS_FILE
# (taskdash_publish), and the bootstrap logs of run_taskdash.cmd / write_bootstrap_fail.
PRUNE_MIN_AGE_DAYS = 7
DEFAULT_KEEP_BACKUPS = 10
UNREADABLE_EXIT = 3
_DASHBOARD_BACKUP = re.compile(r"dashboard_backup_(?P<item>[A-Za-z0-9]+)_\d{8}_\d{6}\.json")
_PENDING_RUNS = re.compile(r"collector_runs_pending_(?:prod|test)\.json")
_BOOTSTRAP_LOG = re.compile(r"bootstrap(?:_(?!FAIL_)\w+)?\.log")
_BOOTSTRAP_FAIL = re.compile(r"bootstrap_FAIL_\w+\.log")


def _is_plain_file(path: Path) -> bool:
    """A regular file that is not a link or other reparse point: prune never follows links."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _plain_files(folder: Path, pattern: re.Pattern[str]) -> List[Path]:
    """Plain files in folder whose whole name matches pattern, in name order (oldest stamp first)."""
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    return [p for p in entries if pattern.fullmatch(p.name) and _is_plain_file(p)]


def _idle_for_days(path: Path, now: float) -> bool:
    try:
        return path.lstat().st_mtime < now - PRUNE_MIN_AGE_DAYS * 86400
    except OSError:
        return False


def _pending_is_empty(path: Path) -> bool:
    """False for a pending file that still holds rows, and for one that cannot be read: the
    next run that reaches the service sends those rows, so they are in use."""
    try:
        return json.loads(path.read_text(encoding="utf-8")) == []
    except (OSError, ValueError):
        return False


def prune_candidates(log_dir: Path, config_path: Path, keep: int, now: float,
                     include_pending: bool = True) -> List[Tuple[Path, str]]:
    found: List[Tuple[Path, str]] = []
    by_item: Dict[str, List[Path]] = {}
    for path in _plain_files(log_dir, _DASHBOARD_BACKUP):
        by_item.setdefault(_DASHBOARD_BACKUP.fullmatch(path.name).group("item"), []).append(path)
    for item, paths in by_item.items():
        found += [(p, f"dashboard backup older than the newest {keep} of {item}") for p in paths[:-keep]]
    config_backup = re.compile(re.escape(config_path.name) + r"\.bak_setup_\d{8}_\d{6}")
    found += [(p, f"config backup older than the newest {keep}")
              for p in _plain_files(config_path.parent, config_backup)[:-keep]]
    if include_pending:
        found += [(p, f"no saved rows, idle {PRUNE_MIN_AGE_DAYS} days")
                  for p in _plain_files(log_dir, _PENDING_RUNS) if _idle_for_days(p, now) and _pending_is_empty(p)]
    return found + _prune_install_logs(config_path.parent / "Logs", keep, now)


def _mtime(path: Path) -> float:
    try:
        return path.lstat().st_mtime
    except OSError:
        return 0.0


def _prune_install_logs(install_logs: Path, keep: int, now: float) -> List[Tuple[Path, str]]:
    """Bootstrap logs in <install>\\Logs, whichever folder logging.log_dir names. run_taskdash.cmd
    writes a bootstrap log there on every run, so the folder is live: only a file older than 7 days
    goes, and the newest `keep` of each kind stay. cleanup_old_logs ages out only log_dir, so after
    log_dir moves nothing else removes them. The folder comes from the config's location, not the
    working directory: a wheel install's runtime_dir() is the directory the command runs in. A
    crash record (bootstrap_FAIL_*) is the only trace of a failure before logging starts."""
    found: List[Tuple[Path, str]] = []
    for pattern, label in ((_BOOTSTRAP_LOG, "bootstrap log"), (_BOOTSTRAP_FAIL, "crash record")):
        files = sorted(_plain_files(install_logs, pattern), key=_mtime)
        found += [(p, f"{label} older than {PRUNE_MIN_AGE_DAYS} days, not among the newest {keep}")
                  for p in files[:-keep] if _idle_for_days(p, now)]
    return found


def _hold_collector_lock(stack: contextlib.ExitStack, log_dir: Path) -> str:
    """Take the collector's single-writer lock until the prune ends, so no run writes a pending
    file between the emptiness check and the delete. Return '' when held, or why the pending files
    must be skipped: the lock is a Windows byte lock, so it cannot be probed any other way."""
    if not log_dir.is_dir():
        return ""
    try:
        stack.enter_context(run_lock(log_dir))
    except RunLockHeld:
        return "a collector run holds its lock"
    except (OSError, ImportError) as exc:
        return f"the collector lock could not be checked ({type(exc).__name__})"
    return ""


def _prune(args: argparse.Namespace) -> int:
    from taskdash_common import DEFAULT_CONFIG_FILE

    config_path = Path(args.config) if args.config else runtime_dir() / DEFAULT_CONFIG_FILE
    if not config_path.is_file():
        raise SystemExit(f"Config not found at {config_path}, so TaskDash cannot tell which folders to "
                         f"prune. Pass --config with the path of the install's config, or run the "
                         f"command from the TaskDash folder (the folder TASKDASH_HOME names).\n"
                         + see_troubleshooting("Prune cannot find the config"))
    log_dir, _ = _status_log_dir(args.config, offline=True)
    with contextlib.ExitStack() as stack:
        # Only --apply takes the lock: a check writes nothing, and taking it creates taskdash.lock.
        skipped_pending = _hold_collector_lock(stack, log_dir) if args.apply else ""
        if skipped_pending:
            emit(f"Pending-row files skipped: {skipped_pending}. Run the command again when no "
                 f"collector run is in progress.")
        found = prune_candidates(log_dir, config_path, args.keep or DEFAULT_KEEP_BACKUPS, time.time(),
                                 include_pending=not skipped_pending)
        if not found:
            emit("Prune: nothing to remove.")
            return 0
        for path, reason in found:
            emit(f"{'Removing' if args.apply else 'Would remove'} {path} ({reason})")
        if not args.apply:
            emit(f"Nothing removed: {len(found)} file(s) are old. Re-run with --apply to remove them.")
            return 0
        removed = 0
        for path, _reason in found:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                emit(f"Could not remove {path}: {exc}. Close any program that holds the file, then run again.")
        emit(f"Removed {removed} file(s) of {len(found)}.")
        return 0 if removed == len(found) else 1


def status(argv: Sequence[str]) -> int:
    """Exit 0 fresh success, 1 newest run failed, 2 stale or no run, 3 cannot sign in or read.
    --offline judges the newest scheduled run's log; otherwise the CollectorRuns table decides."""
    parser = argparse.ArgumentParser(
        prog="taskdash status", allow_abbrev=False,
        description="Read-only: last local run result and newest CollectorRuns row. "
                    "With --prune it lists old TaskDash backups and logs instead; --apply removes them.")
    parser.add_argument("--offline", action="store_true", help="Print only the local part; never sign in.")
    parser.add_argument("--target", choices=("prod", "test"), default="prod",
                        help="Which configured service to read.")
    parser.add_argument("--host", default=None, metavar="NAME",
                        help="Read the runs of this host. Default: this install's host. 'all' reads every host.")
    parser.add_argument("--prune", action="store_true", help="List old backups and logs. Removes nothing without --apply.")
    parser.add_argument("--apply", action="store_true", help="With --prune: remove the files it lists.")
    parser.add_argument("--keep", type=int, default=None, metavar="N",
                        help=f"With --prune: how many to keep of each kind: dashboard backups per dashboard, "
                             f"config backups, bootstrap logs and crash records. Default {DEFAULT_KEEP_BACKUPS}.")
    parser.add_argument("--config", default="", metavar="PATH", help=CONFIG_ARG_HELP)
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    args = parser.parse_args(argv)
    if args.offline and args.host is not None:
        parser.error("--host selects a CollectorRuns host, and --offline reads no service. Drop one of them.")
    if (args.apply or args.keep is not None) and not args.prune:
        parser.error("--apply and --keep work only with --prune.")
    if args.keep is not None and args.keep < 1:
        parser.error("--keep must be 1 or more; 0 would remove every backup.")
    if args.prune:
        return _prune(args)
    try:
        log_dir, config = _status_log_dir(args.config, args.offline)
        local_code = _local_status(log_dir, _collector_interval(config), args.offline)
        unsent = _unsent_status(log_dir, args.target)
        _handoff_status()
        return local_code if args.offline else _remote_status(config, args.target, args.host, unsent)
    except SystemExit as exc:
        emit(str(exc.code))
    except Exception as exc:
        emit(f"Cannot read the service: {exc}. Check the sign-in and the network, then run again, or run "
             f"{command_text('status', '--offline')} to read only the local log.")
    return UNREADABLE_EXIT


def send_test_alert(argv: Sequence[str]) -> int:
    """Check by default: print who would get a test message and through which server. --apply
    sends it with the same send_email the hourly run uses, so a pass proves the real path."""
    parser = argparse.ArgumentParser(
        prog="taskdash test-alert", allow_abbrev=False,
        description="Check the alert e-mail path. Sends nothing unless you pass --apply.")
    parser.add_argument("--apply", action="store_true", help="Send one short test message.")
    parser.add_argument("--config", default="", metavar="PATH", help=CONFIG_ARG_HELP)
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    args = parser.parse_args(argv)

    import taskdash_common as common
    import taskdash_publish

    path = Path(args.config) if args.config else runtime_dir() / common.DEFAULT_CONFIG_FILE
    config = common.load_config(path)
    alerting = config.get("alerting", {})
    recipients = [r for r in (alerting.get("to") or []) if common.normalize_text(r)]
    if not recipients:
        raise SystemExit("alerting.to is empty, so the test message has no recipient. "
                         "Add at least one address to alerting.to in the config.\n"
                         + see_troubleshooting("The test alert has no recipient"))
    mail = common.load_mail_settings(config)
    common.warn_legacy_mail(config)
    if not mail:
        raise SystemExit("There are no usable mail settings, so the test message cannot be sent. "
                         "Fix the smtp block in the config (host, from, user and the stored "
                         "password); the WARNING above says which part is missing.\n"
                         + see_troubleshooting("The test alert has no mail settings"))
    if not alerting.get("enabled"):
        emit("WARNING: alerting.enabled is false, so the hourly run sends no alerts yet.")

    host = common.get_host_name(config)
    subject = f"[TaskDash] test alert from {host}"
    body = (f"This is a test message from TaskDash on {host}. If you can read it, "
            "alert e-mails from this host reach you. No action is needed.")
    server = f"{mail['smtp server']}:{mail.get('smtp port', 587)}"
    # Each Windows account has its own keyring: a pass here says nothing about the task account's password.
    # A relay with no login has no password to read.
    what = "mail settings and password" if mail.get("smtp password") else "mail settings"
    emit(f"The {what} were read as {getpass.getuser()}. The hourly task reads "
         "those of its own account: run 'taskdash preflight --as-task --apply' to check that account.")
    if not args.apply:
        emit(f"Would send one test message '{subject}' to {', '.join(recipients)} "
             f"from {mail['from address']} through {server}.")
        emit("Nothing sent. Re-run with --apply to send it.")
        return 0

    common.last_send_error = ""
    if not common.send_email(mail, recipients, subject, body):
        reason = common.last_send_error or "the mail server did not accept the message"
        emit(f"Test alert failed: {taskdash_publish._scrub_secrets(reason)}. "
             f"{common.mail_failure_hint(reason)}")
        return 1
    emit(f"Test alert sent to {', '.join(recipients)} through {server}.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
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
    if command == "task":
        return show_task_scripts()
    if command not in SUBCOMMAND_MODULES:
        print(f"taskdash: unknown command {command!r}\n{USAGE}", file=sys.stderr)
        return 2
    return _run_module(SUBCOMMAND_MODULES[command], rest)


def _scrubbed(text: str, withheld: str) -> str:
    """`text` without secrets, or `withheld` when the scrubber fails. The scrubber is imported here, so a
    broken taskdash_publish (the usual cause) still leaves the other commands usable."""
    try:
        from taskdash_publish import _scrub_secrets
        return _scrub_secrets(text)
    except Exception:
        return withheld


def _print_scrubbed_traceback(exc_type: object, exc: object, tb: object) -> None:
    # A library error can echo a request URL with ?token= in it, and people paste this into tickets.
    text = "".join(traceback.format_exception(exc_type, exc, tb))  # type: ignore[arg-type]
    print(_scrubbed(text, "log line withheld: secret scrubbing failed\n"), file=sys.stderr, end="")


def run() -> int:
    """The `taskdash` console script: main() with the same secret scrubbing as the collector script."""
    sys.excepthook = _print_scrubbed_traceback
    try:
        return main()
    except SystemExit as exc:
        # The interpreter prints a str exit code to stderr as it is.
        if not isinstance(exc.code, str):
            raise
        raise SystemExit(_scrubbed(
            exc.code, "message withheld: secret scrubbing failed. Reinstall TaskDash from a complete "
                      "wheel (python -m pip install --force-reinstall <wheel>).\n"
                      + see_troubleshooting("A module failed to import"))) from None


if __name__ == "__main__":
    sys.exit(run())

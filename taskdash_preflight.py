"""TaskDash install checker: one line per check, `[ok]`, `[!!]` or `[--]`, each `[!!]` ending with the fix.

Driver: manual, before the first scheduled run and after any config, account or task change
Source: the TaskDash config, the local Task Scheduler, ArcGIS Online and the SMTP relay
Target: stdout only. Every check reads. The only write path is `--enable-event-log --apply`,
which runs one `wevtutil sl` command.

    python taskdash_preflight.py                        # every check
    python taskdash_preflight.py --offline              # skip the network checks (shown as [--])
    python taskdash_preflight.py --enable-event-log     # show the Task Scheduler log state and the fix
    python taskdash_preflight.py --enable-event-log --apply [--max-size-mb 64]

Exit code: 0 when no check failed, else 1. One line is a warning that does NOT set exit 1: the
tested-scale line. It prints `[!!]` but begins "WARNING"; a larger install may work, and exiting 1
would block a working install from automation. A config warning is treated the same way. Every other
`[!!]` line sets exit 1.

A crash inside one check becomes `[!!] <check>: <ExceptionType>: <message>`, and the next check still runs.
"""

from __future__ import annotations

import argparse
import re
import smtplib
import ssl
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import taskdash_collect as collect
from taskdash_common import (
    SCHEMA_VERSION,
    TASKDASH_VERSION,
    connect_agol,
    emit,
    get_tables,
    load_config,
    load_mail_settings,
    normalize_text,
)
from taskdash_dashboard import DEFAULT_COLLECTOR_TASK_NAME
from taskdash_publish import (
    TABLE_INDEXES,
    TABLE_SPECS,
    check_anonymous_access,
    diff_schema,
    ensure_service,
    missing_indexes,
    service_schema_version,
    view_key,
    view_shown_hidden_fields,
)

OK, FAIL, SKIP, WARN = "ok", "fail", "skip", "warn"
MARKS = {OK: "[ok]", FAIL: "[!!]", SKIP: "[--]", WARN: "[!!]"}

EVENT_LOG_NAME = collect.OPERATIONAL_LOG_NAME
# Windows ships this log at 1 MB (10 MB on some builds) and overwrites the oldest events when it
# fills. The log carries ~920 events/day on a busy task host (see taskdash_collect). At an estimated
# 1 to 2 KB per event that is up to ~2 MB/day, so 64 MB holds about a month: the default
# history.days_back window survives a collector outage of a few weeks.
RECOMMENDED_LOG_MB = 64
EVENT_LOG_FIX = "python taskdash_preflight.py --enable-event-log --apply"

# What was run at the reference install. Beyond it TaskDash is untested, not known to fail.
TESTED_TASK_COUNT = 29
TESTED_MIN_REPEAT_SECONDS = 3600

# Portal privilege -> what TaskDash does with it.
REQUIRED_PRIVILEGES = (
    ("portal:user:createItem", "create items"),
    ("portal:user:createGroup", "create groups"),
    ("portal:user:shareToGroup", "share items to groups"),
    ("features:user:edit", "edit features"),
)

Result = Tuple[str, str]


@dataclass
class Context:
    args: argparse.Namespace
    config: Optional[Dict[str, Any]] = None
    config_warnings: List[str] = field(default_factory=list)
    gis: Any = None
    item: Any = None
    tables: Dict[str, Any] = field(default_factory=dict)
    tasks: Optional[List[Dict[str, Any]]] = None


# --------------------------------------------------------------------------- checks

def check_config(ctx: Context) -> Result:
    path = Path(ctx.args.config) if ctx.args.config else None
    ctx.config, ctx.config_warnings = load_config(path, defer_warnings=True)
    if ctx.config_warnings:
        return WARN, "config loaded with warnings. Fix each one:\n       " + "\n       ".join(ctx.config_warnings)
    return OK, "config loaded, no warnings"


def check_credentials(ctx: Context) -> Result:
    ctx.gis = connect_agol(ctx.config)
    me = ctx.gis.users.me
    return OK, (f"signed in as {me.username} (user type {getattr(me, 'userType', None) or 'unknown'}, "
                f"role {getattr(me, 'role', None) or 'unknown'})")


def _need_gis(ctx: Context) -> Optional[Result]:
    if ctx.gis is None:
        return FAIL, "no ArcGIS sign-in to check with. Fix the credentials check above, then re-run."
    return None


def check_privileges(ctx: Context) -> Result:
    if (blocked := _need_gis(ctx)):
        return blocked
    privileges = getattr(ctx.gis.users.me, "privileges", None)
    if not isinstance(privileges, (list, tuple, set)) or not privileges:
        return FAIL, "could not read privileges; confirm in ArcGIS Online"
    missing = [f"{key} ({what})" for key, what in REQUIRED_PRIVILEGES if key not in privileges]
    if missing:
        return FAIL, ("account lacks privilege(s): " + ", ".join(missing)
                      + ". Ask an organization administrator to add them to the account's role.")
    return OK, "account can create items, create groups, share to groups and edit features"


def read_log_state(log_name: str = EVENT_LOG_NAME) -> Tuple[bool, int]:
    """(enabled, max size in bytes) from the read-only `wevtutil gl`."""
    done = subprocess.run(["wevtutil", "gl", log_name], capture_output=True, text=True,
                          timeout=30, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"wevtutil gl exited {done.returncode}: {done.stderr.strip()[:200]}")
    enabled = re.search(r"^enabled:\s*(\w+)", done.stdout, re.MULTILINE)
    size = re.search(r"^\s*maxSize:\s*(\d+)", done.stdout, re.MULTILINE)
    if not enabled or not size:
        raise RuntimeError("wevtutil gl output has no enabled/maxSize line")
    return enabled.group(1).lower() == "true", int(size.group(1))


def check_event_log(ctx: Context) -> Result:
    enabled, max_bytes = read_log_state()
    problems = []
    if not enabled:
        problems.append("is disabled")
    if max_bytes < RECOMMENDED_LOG_MB * 1024 * 1024:
        problems.append(f"holds {max_bytes // 1024 // 1024} MB, below the recommended {RECOMMENDED_LOG_MB} MB")
    if problems:
        return FAIL, (f"{EVENT_LOG_NAME} {' and '.join(problems)}. "
                      f"From an elevated prompt run: {EVENT_LOG_FIX}")
    return OK, f"{EVENT_LOG_NAME} is enabled, max size {max_bytes // 1024 // 1024} MB"


def check_tasks(ctx: Context) -> Result:
    timeout = int((ctx.config or {}).get("powershell_timeout_seconds", 180))
    ctx.tasks = collect.collect_tasks(timeout)
    if not ctx.tasks:
        return FAIL, ("Task Scheduler returned no tasks. Run from the account the collector runs as, "
                      "in a session that can read Task Scheduler.")
    return OK, f"{len(ctx.tasks)} task definition(s) readable"


def check_log_folders(ctx: Context) -> Result:
    overrides = ((ctx.config or {}).get("metrics") or {}).get("log_directory_overrides") or {}
    if not overrides:
        return SKIP, "no metrics.log_directory_overrides configured"
    bad = []
    for task, folder in overrides.items():
        path = Path(normalize_text(folder))
        try:
            next(path.iterdir(), None)
        except OSError as exc:
            bad.append(f"{task}: {folder} ({type(exc).__name__})")
    if bad:
        return FAIL, ("log folder(s) missing or unreadable: " + "; ".join(bad)
                      + ". Fix the path in metrics.log_directory_overrides or grant this account read access.")
    return OK, f"{len(overrides)} configured log folder(s) exist and are readable"


def probe_smtp(mail: Dict[str, Any]) -> None:
    """Connect, EHLO, STARTTLS, QUIT. Never logs in, never sends. Mirrors send_email's TLS rules."""
    strict = mail.get("strict tls", False)
    context = ssl.create_default_context() if strict else None
    port = int(mail.get("smtp port", 587))
    use_ssl = strict and mail.get("use ssl", False)
    if use_ssl:
        connection = smtplib.SMTP_SSL(mail["smtp server"], port, timeout=30, context=context)
    else:
        connection = smtplib.SMTP(mail["smtp server"], port, timeout=30)
    with connection as smtp:  # leaving the block sends QUIT
        smtp.ehlo()
        if mail.get("use tls", True) and not use_ssl:
            smtp.starttls(context=context)
            smtp.ehlo()


def check_smtp(ctx: Context) -> Result:
    if not (ctx.config or {}).get("alerting", {}).get("enabled"):
        return SKIP, "alerting.enabled is not true"
    mail = load_mail_settings(ctx.config)
    if mail is None:
        return FAIL, ("alerting is enabled but the mail settings are unusable (see the WARNING above). "
                      "Fix the smtp block or alerting.mail_config_path, or set alerting.enabled to false.")
    try:
        probe_smtp(mail)
    except (OSError, smtplib.SMTPException) as exc:  # ssl.SSLError is an OSError
        return FAIL, (f"cannot reach {mail['smtp server']}:{mail.get('smtp port', 587)}: "
                      f"{type(exc).__name__}: {exc}. Check smtp.host, smtp.port, the firewall and that "
                      "the relay's certificate is trusted.")
    return OK, f"{mail['smtp server']} answered EHLO (nothing sent, no login)"


def check_service(ctx: Context) -> Result:
    if (blocked := _need_gis(ctx)):
        return blocked
    ctx.item = ensure_service(ctx.gis, ctx.config, "prod")
    ctx.tables = get_tables(ctx.item)
    problems = []
    missing_tables = [name for name in TABLE_SPECS if name not in ctx.tables]
    if missing_tables:
        problems.append("missing table(s): " + ", ".join(missing_tables))
    for name in TABLE_SPECS:
        if name not in ctx.tables:
            continue
        fields = [f["name"] for f in diff_schema(ctx.tables[name], TABLE_SPECS[name][0]())]
        indexes = ["+".join(s["fields"]) for s in missing_indexes(ctx.tables[name], TABLE_INDEXES[name])
                   if s["unique"]]
        if fields:
            problems.append(f"{name} missing field(s): {', '.join(fields)}")
        if indexes:
            problems.append(f"{name} missing unique index: {', '.join(indexes)}")
    if problems:
        return FAIL, "; ".join(problems) + ". Run: python taskdash_publish.py --migrate --apply"
    stamp = service_schema_version(ctx.item)
    if stamp is not None and stamp > SCHEMA_VERSION:
        return FAIL, (f"service schema version {stamp} is newer than this TaskDash ({TASKDASH_VERSION}, "
                      f"schema {SCHEMA_VERSION}). Upgrade TaskDash.")
    note = ("unstamped; the next --migrate --apply stamps it" if stamp is None
            else f"schema version {stamp}")
    return OK, f"service {ctx.item.id} has every table, field and unique index ({note})"


def _view_capabilities(view: Any) -> str:
    from arcgis.features import FeatureLayerCollection

    return normalize_text(FeatureLayerCollection.fromitem(view).properties.capabilities)


def _view_item(ctx: Context) -> Any:
    view_id = normalize_text(ctx.config.get("arcgis", {}).get(view_key("prod")))
    if not view_id:
        return None
    view = ctx.gis.content.get(view_id)
    if view is None:
        raise LookupError(f"view item {view_id} not found or not visible to this account. "
                          "Fix arcgis.view_item_id or share the view with this account.")
    return view


def check_view(ctx: Context) -> Result:
    if (blocked := _need_gis(ctx)):
        return blocked
    view = _view_item(ctx)
    if view is None:
        return SKIP, "no arcgis.view_item_id configured; the dashboard would read the service directly"
    view_tables = get_tables(view)
    problems = []
    absent = sorted(set(TABLE_SPECS) - set(view_tables))
    if absent:
        problems.append(f"view has no table(s) {', '.join(absent)}")
    shown = view_shown_hidden_fields(view_tables, ctx.config)
    if shown:
        problems.append("view still shows field(s) the privacy switches hide: "
                        + "; ".join(f"{t}: {', '.join(f)}" for t, f in shown.items()))
    wrong = {"view": _view_capabilities(view)}
    wrong.update({n: normalize_text(getattr(t.properties, "capabilities", None)) or "Query"
                  for n, t in view_tables.items()})
    bad = [f"{n}={c}" for n, c in wrong.items() if {p.strip() for p in c.split(",")} != {"Query"}]
    if bad:
        problems.append("capabilities are not Query only: " + ", ".join(bad))
    if problems:
        return FAIL, "; ".join(problems) + ". Run: python taskdash_publish.py --migrate --apply"
    return OK, f"view {view.id} hides the private fields and allows Query only"


def check_sharing(ctx: Context) -> Result:
    if (blocked := _need_gis(ctx)):
        return blocked
    if ctx.item is None:
        return FAIL, "the service was not read (see the service check above), so sharing cannot be judged."
    view = _view_item(ctx)
    urls = [t.url for t in ctx.tables.values()] + ([t.url for t in get_tables(view).values()] if view else [])
    problems = []
    access = getattr(ctx.item, "access", None)
    if view is not None and access != "private":
        problems.append(f"the editable service is shared ('{access}')")
    problems += check_anonymous_access(urls)
    if problems:
        return FAIL, ("; ".join(problems) + ". Set the service sharing to Owner and the view to your "
                      "organization or groups in ArcGIS Online, then re-run.")
    return OK, f"service is private where a view exists and {len(urls)} table(s) refused an anonymous query"


def _uses_stored_credentials(config: Dict[str, Any]) -> Optional[str]:
    if normalize_text(config.get("arcgis", {}).get("profile")):
        return "arcgis.profile"
    smtp = config.get("smtp") or {}
    if normalize_text(smtp.get("user")) and config.get("alerting", {}).get("enabled"):
        return "the smtp.user keyring entry"
    return None


def check_logon_type(ctx: Context) -> Result:
    if ctx.tasks is None:
        return FAIL, "task definitions were not read (see the task check above), so the logon type is unknown."
    name = normalize_text(ctx.config.get("scheduler", {}).get("collector_task_name")) or DEFAULT_COLLECTOR_TASK_NAME
    task = next((t for t in ctx.tasks if normalize_text(t.get("task_name")).lower() == name.lower()), None)
    if task is None:
        return FAIL, (f"collector task '{name}' not found. Register it with install_task.ps1, or set "
                      "scheduler.collector_task_name to its real name.")
    logon, stored = normalize_text(task.get("logon_type")), _uses_stored_credentials(ctx.config)
    if logon.lower() == "s4u" and stored:
        return FAIL, (f"task '{name}' uses S4U and the config reads {stored}. S4U logon cannot read stored "
                      "credentials (Windows gives S4U no access to encrypted files). Re-register the "
                      "task with the Password logon type.")
    return OK, f"task '{name}' logs on as {logon or 'unknown'}"


def check_scale(ctx: Context) -> Result:
    if ctx.tasks is None:
        return FAIL, "task definitions were not read (see the task check above), so the scale is unknown."
    scheduler = ctx.config.get("scheduler", {})
    monitored = [t for t in ctx.tasks if collect.should_include_task(t, scheduler)]
    fast = [t for t in monitored
            if (collect.shortest_repetition_seconds(t.get("trigger_details")) or float("inf"))
            < TESTED_MIN_REPEAT_SECONDS]
    if len(monitored) > TESTED_TASK_COUNT or fast:
        return WARN, (f"WARNING: {len(monitored)} task(s) monitored, {len(fast)} repeating more often than "
                      f"hourly; TaskDash was tested with {TESTED_TASK_COUNT} tasks and no more than hourly "
                      "repetition. It may work above the tested scale. Report results.")
    return OK, f"{len(monitored)} task(s) monitored, none repeating more often than hourly (tested scale)"


CHECKS: Sequence[Tuple[str, bool, Callable[[Context], Result]]] = (
    # (name, needs network, function); check_config runs first, outside this list.
    ("credentials", True, check_credentials),
    ("privileges", True, check_privileges),
    ("event log", False, check_event_log),
    ("task definitions", False, check_tasks),
    ("log folders", False, check_log_folders),
    ("smtp", True, check_smtp),
    ("service", True, check_service),
    ("view", True, check_view),
    ("sharing", True, check_sharing),
    ("collector logon type", False, check_logon_type),
    ("tested scale", False, check_scale),
)


# --------------------------------------------------------------------------- runner

def run_check(check: Callable[[Context], Result], ctx: Context) -> Result:
    try:
        return check(ctx)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - one broken check must not hide the rest
        text = exc.code if isinstance(exc, SystemExit) else exc
        return FAIL, f"{type(exc).__name__}: {text}"


def run_preflight(args: argparse.Namespace) -> int:
    ctx = Context(args)
    results = [("config", *run_check(check_config, ctx))]
    if ctx.config is not None:
        for name, needs_network, check in CHECKS:
            skipped = needs_network and args.offline
            results.append((name, *((SKIP, "skipped with --offline") if skipped else run_check(check, ctx))))
    for name, status, message in results:
        emit(f"{MARKS[status]} {name}: {message}")
    failed = sum(status == FAIL for _, status, _ in results)
    warned = sum(status == WARN for _, status, _ in results)
    emit(f"Preflight: {failed} problem(s), {warned} warning(s).")
    return 1 if failed else 0


# --------------------------------------------------------------------------- event log setup

def run_enable_event_log(apply: bool, max_size_mb: Optional[int]) -> int:
    enabled, current = read_log_state()
    emit(f"{EVENT_LOG_NAME}: enabled={str(enabled).lower()}, max size {current} bytes.")
    # Never shrink a log the owner already enlarged, unless they ask for a size.
    wanted = max_size_mb * 1024 * 1024 if max_size_mb else max(current, RECOMMENDED_LOG_MB * 1024 * 1024)
    command = ["wevtutil", "sl", EVENT_LOG_NAME, "/e:true", f"/ms:{wanted}"]
    if not apply:
        emit("Check mode, nothing changed. Would run: " + " ".join(command))
        emit(f"Run it from an elevated prompt with: {EVENT_LOG_FIX}")
        return 0
    done = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        denied = done.returncode == 5 or "access is denied" in done.stderr.lower()
        emit("wevtutil sl failed: " + ("run from an elevated prompt." if denied
                                       else f"exit {done.returncode}: {done.stderr.strip()[:200]}"))
        return 1
    enabled, current = read_log_state()
    emit(f"{EVENT_LOG_NAME}: enabled={str(enabled).lower()}, max size {current} bytes.")
    return 0 if enabled and current == wanted else 1


# --------------------------------------------------------------------------- entry point

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only TaskDash install checker.",
        allow_abbrev=False,  # --apply must never be reachable by abbreviation
    )
    parser.add_argument("--config", default="", help="Path to TaskDash.config.json.")
    parser.add_argument("--offline", action="store_true",
                        help="Skip every check that needs the network (credentials, privileges, smtp, "
                             "service, view, sharing).")
    parser.add_argument("--enable-event-log", action="store_true",
                        help="Show the Task Scheduler log state and the command that enables it. "
                             "Runs alone, without the other checks.")
    parser.add_argument("--apply", action="store_true", help="With --enable-event-log: run the command.")
    parser.add_argument("--max-size-mb", type=int, default=0,
                        help=f"With --enable-event-log: log size in MB (default {RECOMMENDED_LOG_MB}).")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    args = parser.parse_args(argv)
    if (args.apply or args.max_size_mb) and not args.enable_event_log:
        parser.error("--apply and --max-size-mb only work with --enable-event-log.")
    if args.max_size_mb < 0:
        parser.error("--max-size-mb must be a positive number of megabytes.")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if not args.enable_event_log:
        return run_preflight(args)
    try:
        return run_enable_event_log(args.apply, args.max_size_mb or None)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        emit(f"Could not run wevtutil: {type(exc).__name__}: {exc}. Run it on Windows from an elevated prompt.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

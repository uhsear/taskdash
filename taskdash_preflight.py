"""TaskDash install checker: one line per check, `[ok]`, `[w]`, `[!!]` or `[--]`, each `[!!]` ending with the fix.

Driver: manual, before the first scheduled run and after any config, account or task change
Source: the TaskDash config, the local Task Scheduler, ArcGIS Online and the SMTP relay
Target: stdout only. Every check reads. The write paths are `--enable-event-log --apply`, which runs
one `wevtutil sl` command, and `--as-task --apply`, which registers and removes one temporary task.

    python taskdash_preflight.py                        # every check
    python taskdash_preflight.py --offline              # skip the network checks (shown as [--])
    python taskdash_preflight.py --enable-event-log     # show the Task Scheduler log state and the fix
    python taskdash_preflight.py --enable-event-log --apply [--max-size-mb 64]
    python taskdash_preflight.py --config-diff          # unknown, renamed and absent config keys; exit 0
    python taskdash_preflight.py --describe             # tasks with no description, and a starter file; exit 0
    python taskdash_preflight.py --as-task [--apply]    # run every check as the task account (taskdash_astask)

The sign-in hand-off file is stored and deleted only by a run with `--consume-handoff`, which
`--as-task` passes to its child. A plain run never changes it.

Exit code: 0 when no check failed, else 1. A warning prints `[w]` and never sets exit 1: it names
something to look at, such as the tested-scale line (a larger install may work, and exiting 1 would
block a working install from automation) or a config warning. Every `[!!]` line sets exit 1.

`[--]` means "does not apply, or not set up yet": a check the config turns off, `--offline`, and a
fresh install that has no service id, dashboard id or collector task yet. It never sets exit 1.
A missing collector task counts as "not set up" only while the service id is empty. After that it is
a WARNING, because a normal window cannot see a task of another account.

A check that cannot run because an earlier one failed prints `[--] <check>: needs: <that check>` and is not
counted, so one cause is one problem.

A crash inside one check becomes `[!!] <check>: <ExceptionType>: <message>`, and the next check still runs.
"""

from __future__ import annotations

import argparse
import json
import re
import ntpath
import os
import smtplib
import ssl
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import taskdash_collect as collect
import taskdash_metrics as metrics
from taskdash_common import (
    CONFIG_DEFAULTS,
    DEFAULT_CONFIG_FILE,
    SCHEMA_VERSION,
    TASKDASH_VERSION,
    command_text,
    config_default,
    config_diff,
    arcgis_version_warning,
    connect_agol,
    emit,
    get_tables,
    home_dir,
    legacy_mail_warnings,
    load_config,
    load_mail_settings,
    mail_failure_hint,
    normalize_text,
    powershell_language_mode,
    run_powershell_json,
    runtime_dir,
    see_troubleshooting,
    system32_exe,
)
from taskdash_dashboard import DEFAULT_COLLECTOR_TASK_NAME
from taskdash_handoff import _bare_name, consume_handoff, handoff_waiting, handoff_waiting_text
from taskdash_setup import INSTALL_KEYS
from taskdash_publish import (
    OPTIONAL_TABLES,
    REQUIRED_TABLES,
    TABLE_INDEXES,
    TABLE_SPECS,
    check_anonymous_access,
    diff_schema,
    ensure_service,
    host_mismatch,
    missing_indexes,
    newer_schema_warning,
    service_key,
    service_schema_version,
    view_key,
    view_shown_hidden_fields,
)

OK, FAIL, SKIP, WARN = "ok", "fail", "skip", "warn"
MARKS = {OK: "[ok]", FAIL: "[!!]", SKIP: "[--]", WARN: "[w]"}

EVENT_LOG_NAME = collect.OPERATIONAL_LOG_NAME
# Windows ships this log at 1 MB (10 MB on some builds) and overwrites the oldest events when it
# fills. The log carries ~920 events/day on a busy task host (see taskdash_collect). At an estimated
# 1 to 2 KB per event that is up to ~2 MB/day, so 64 MB holds about a month: the default
# history.days_back window survives a collector outage of a few weeks.
RECOMMENDED_LOG_MB = 64
EVENT_LOG_FIX = command_text("preflight", "--enable-event-log", "--apply")
MIGRATE_FIX = command_text("collect", "--migrate", "--apply")

# What was run at the reference install. Beyond it TaskDash is untested, not known to fail.
TESTED_TASK_COUNT = 29
TESTED_MIN_REPEAT_SECONDS = 3600

# Portal privilege -> what TaskDash does with it.
REQUIRED_PRIVILEGES = (
    ("portal:user:createItem", "create items"),
    ("portal:publisher:publishFeatures", "publish hosted feature layers"),
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
    # Plain preflight only reads. The --as-task child passes --consume-handoff because it is the first
    # run as the task account, before the collector has ever run, and so the first that can store a
    # hand-off file. For any other account consume_handoff leaves the file alone.
    if getattr(ctx.args, "consume_handoff", False):
        consume_handoff()
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
    return OK, "account can create items, publish hosted feature layers, create groups, share to groups and edit features"


def read_log_state(log_name: str = EVENT_LOG_NAME) -> Tuple[bool, int]:
    """(enabled, max size in bytes) from the read-only `wevtutil gl`."""
    done = subprocess.run([system32_exe("wevtutil.exe"), "gl", log_name], capture_output=True, text=True,
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
    timeout = int((ctx.config or {}).get("powershell_timeout_seconds", config_default("powershell_timeout_seconds")))
    scheduler = (ctx.config or {}).get("scheduler") or {}
    exclude = scheduler.get("exclude_microsoft_tasks", config_default("scheduler.exclude_microsoft_tasks"))
    ctx.tasks = collect.collect_tasks(timeout, exclude)
    if ctx.tasks:
        return OK, f"{len(ctx.tasks)} task definition(s) readable"
    # A fresh server can hold only \Microsoft\ tasks, which the filter drops. That must not block
    # the install, so an unfiltered read tells "nothing to monitor yet" from "cannot read".
    if exclude and collect.collect_tasks(timeout, False):
        return OK, "Task Scheduler is readable; no task outside \\Microsoft\\ exists yet"
    return FAIL, ("Task Scheduler returned no tasks. Run from the account the collector runs as, "
                  "in a session that can read Task Scheduler.")


HIDDEN_TASKS_NOTE = ("A normal window can miss tasks that other accounts own, so before you change a key, "
                     "run this check again from an administrator window or with --as-task.")


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
    unmatched = metrics.unmatched_override_keys(overrides, [t.get("task_full_name") for t in ctx.tasks or ()])
    if unmatched and ctx.tasks:
        return WARN, ("WARNING: metrics.log_directory_overrides key(s) match no task: " + ", ".join(map(repr, unmatched))
                      + ". A key is the full task path, like '\\Folder\\Task'. The case does not matter. "
                      + HIDDEN_TASKS_NOTE)
    return OK, f"{len(overrides)} configured log folder(s) exist and are readable"


def check_descriptions(ctx: Context) -> Result:
    """workflow_descriptions.json: unknown or mistyped keys, bad values, and keys that name no task."""
    if not (runtime_dir() / "workflow_descriptions.json").exists():
        return SKIP, "no workflow_descriptions.json; tasks show their Windows Description"
    problems: List[str] = []
    descriptions = collect.load_descriptions(warnings=problems)
    scheduler = (ctx.config or {}).get("scheduler") or {}
    if ctx.tasks:
        key_problems = collect.description_key_warnings(
            descriptions, [t for t in ctx.tasks if collect.should_include_task(t, scheduler)])
        problems += key_problems + ([HIDDEN_TASKS_NOTE] if key_problems else [])
    if problems:
        return WARN, "workflow_descriptions.json has problems. Fix each one:\n       " + "\n       ".join(problems)
    return OK, f"workflow_descriptions.json holds {len(descriptions)} description(s), no problems"


MAX_SUGGESTED_ROOTS = 5


def _suggested_log_roots(ctx: Context) -> List[str]:
    """The folders that hold the monitored tasks' scripts, which is where their logs usually are."""
    scheduler = (ctx.config or {}).get("scheduler") or {}
    folders = {ntpath.dirname(collect.classify_command_target(task.get("primary_executable"),
                                                              task.get("primary_arguments"))[0])
               for task in ctx.tasks or ()
               if collect.should_include_task(task, scheduler)}
    return sorted(folder for folder in folders if folder)


def check_log_roots(ctx: Context) -> Result:
    roots = [normalize_text(root) for root in ((ctx.config or {}).get("metrics") or {}).get("log_roots") or ()]
    roots = [root for root in roots if root]
    if roots:
        return OK, f"metrics.log_roots lists {len(roots)} folder(s); logs are read only inside them"
    suggested = _suggested_log_roots(ctx)
    hint = ""
    if suggested:
        shown = ", ".join(f'"{folder}"' for folder in suggested[:MAX_SUGGESTED_ROOTS])
        more = len(suggested) - MAX_SUGGESTED_ROOTS
        hint = f" The monitored tasks' scripts sit in: {shown}{f' and {more} more' if more > 0 else ''}."
    return WARN, ("WARNING: metrics.log_roots is empty, so a task author can make TaskDash read, and mail, "
                  "the logs of any folder. List the folders that hold your logs in metrics.log_roots. "
                  "See 'Limit where TaskDash reads logs' in docs/CONFIGURE.md." + hint)


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


def _is_example_address(value: Any) -> bool:
    """The shipped placeholder, as an address or a host name: example.com and its subdomains."""
    text = normalize_text(value).lower()
    return text == "example.com" or text.endswith(("@example.com", ".example.com"))


def check_smtp(ctx: Context) -> Result:
    alerting = (ctx.config or {}).get("alerting", {})
    if not alerting.get("enabled"):
        return SKIP, "alerting.enabled is not true"
    recipients = [r for r in (alerting.get("to") or []) if normalize_text(r)]
    if not recipients:
        # Otherwise the first real failure exits 1 with nobody told, which is what alerts are for.
        return FAIL, ("alerting is enabled but alerting.to has no address, so nobody would receive an "
                      "alert. Add at least one address to alerting.to, or set alerting.enabled to false.")
    mail = load_mail_settings(ctx.config)
    if mail is None:
        return FAIL, ("alerting is enabled but the mail settings are unusable (see the WARNING above). "
                      "Fix the smtp block or alerting.mail_config_path, or set alerting.enabled to false.")
    # Mirrors send_email: the legacy file never turns SSL on, so only the strict smtp block can.
    encrypted = bool((mail.get("strict tls") and mail.get("use ssl")) or mail.get("use tls", True))
    if not encrypted and mail.get("smtp username") and mail.get("smtp password"):
        return FAIL, ("the mail login would send its password in plain text: neither STARTTLS nor SSL is on. "
                      "Set smtp.starttls to true (or smtp.ssl to true), and clear smtp.allow_plaintext_auth. "
                      "Or clear smtp.user to send without a login.")
    smtp = ctx.config.get("smtp") or {}
    placeholders = (["alerting.to"] if any(_is_example_address(r) for r in recipients) else [])
    placeholders += [f"smtp.{key}" for key in ("host", "from", "user") if _is_example_address(smtp.get(key))]
    placeholder_note = (f"WARNING: {', '.join(placeholders)} still hold example.com placeholders, so alerts "
                        "would go nowhere real. Put the real values in the config.")
    if "smtp.host" in placeholders:
        # The probe below would fail on the placeholder host and hide this note: say it instead.
        # FAIL, like an empty alerting.to: with a placeholder host no alert can ever be sent.
        return FAIL, placeholder_note + " (smtp.host is a placeholder, so no connection was tried)"
    try:
        probe_smtp(mail)
    except (OSError, smtplib.SMTPException) as exc:  # ssl.SSLError is an OSError
        reason = f"{type(exc).__name__}: {exc}"
        # A server without STARTTLS answered; blaming the firewall would send the admin the wrong way.
        fix = (mail_failure_hint(reason) if "STARTTLS" in reason else
               "Check smtp.host, smtp.port, the firewall and that the relay's certificate is trusted.")
        return FAIL, f"cannot reach {mail['smtp server']}:{mail.get('smtp port', 587)}: {reason}. {fix}"
    answered = f"{mail['smtp server']} answered EHLO (nothing sent, no login)"
    notes = []
    if not mail.get("strict tls"):
        notes.append("WARNING: " + " WARNING: ".join(legacy_mail_warnings(mail)))
    if not encrypted:
        notes.append("WARNING: neither STARTTLS nor SSL is on, so alert mail, which names tasks and log "
                     "lines, crosses the network in plain text. Set smtp.starttls to true if the server allows it.")
    if placeholders:
        notes.append(placeholder_note)
    return (WARN, " ".join(notes) + f" ({answered})") if notes else (OK, answered)


def _service_not_set_up(ctx: Context) -> Optional[Result]:
    # "Not set up yet" is [--], not [!!]: a fresh install has no service until `setup --apply`.
    agol = (ctx.config or {}).get("arcgis", {})
    if normalize_text(agol.get(service_key("prod"))):
        return None
    # Another install key without the service id is a damaged install, not a fresh one.
    if (others := [f"arcgis.{k}" for k in INSTALL_KEYS if normalize_text(agol.get(k))]):
        return FAIL, (f"the config sets {', '.join(others)} but no arcgis.{service_key('prod')}, so every "
                      "'collect --apply' refuses. Put the feature layer's item id back in "
                      f"arcgis.{service_key('prod')}. " + see_troubleshooting("The service is not in the config"))
    return SKIP, (f"no arcgis.{service_key('prod')} configured yet. 'taskdash setup --apply' creates the "
                  "service and fills the id in.")


def check_service(ctx: Context) -> Result:
    if (not_yet := _service_not_set_up(ctx)):
        return not_yet
    if (blocked := _need_gis(ctx)):
        return blocked
    ctx.item = ensure_service(ctx.gis, ctx.config, "prod")
    ctx.tables = get_tables(ctx.item)
    problems = []
    missing_tables = [name for name in REQUIRED_TABLES if name not in ctx.tables]
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
        return FAIL, "; ".join(problems) + ". Run: " + MIGRATE_FIX
    stamp = service_schema_version(ctx.item)
    if stamp is not None and stamp > SCHEMA_VERSION:
        # A rolled-back collector keeps publishing the fields it knows, so this is a warning, not a failure.
        return WARN, newer_schema_warning(ctx.item, stamp)
    note = ("unstamped; the next --migrate --apply stamps it" if stamp is None
            else f"schema version {stamp}")
    later = [name for name in OPTIONAL_TABLES if name not in ctx.tables]
    if later:
        # Not a failure: the collector and the dashboard work without it (see OPTIONAL_TABLES).
        return OK, (f"service {ctx.item.id} has every required table, field and unique index ({note}); "
                    f"optional table(s) not created yet: {', '.join(later)}. Run: {MIGRATE_FIX}")
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
    absent = sorted(set(REQUIRED_TABLES) - set(view_tables))
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
        return FAIL, "; ".join(problems) + ". Run: " + MIGRATE_FIX
    return OK, f"view {view.id} hides the private fields and allows Query only"


def check_sharing(ctx: Context) -> Result:
    if (not_yet := _service_not_set_up(ctx)):
        return not_yet
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


DASHBOARD_FIX = (f"Rebuild it with: {command_text('dashboard', '--target', 'prod', '--apply')}. "
                 "See 'Rebuild or move the ArcGIS items' in docs/TROUBLESHOOTING.md.")


def _dashboard_view_ids(item: Any) -> set:
    definition = item.get_data() or {}
    desktop = definition.get("desktopView")
    if not desktop:
        return set()
    # Read with .get: a hand-edited widget can lack datasets or dataSource, and this check only warns.
    widgets = ((desktop.get("widgets") or []) + ((desktop.get("sidebar") or {}).get("selectors") or [])
               + ((definition.get("mobileView") or {}).get("widgets") or []))
    sources = [(d or {}).get("dataSource") or {} for w in widgets for d in (w or {}).get("datasets") or []]
    return {s.get("itemId") for s in sources if s.get("type") == "layerDataSource"}


# item.access, narrowest first. A dashboard shared more widely than its view shows some viewers no data.
_ACCESS_RANK = {"private": 0, "shared": 1, "org": 2, "public": 3}


def _shared_group_ids(item: Any) -> Optional[set]:
    """The ids of the groups the item is shared with, or None when the item cannot say."""
    shared_with = getattr(item, "shared_with", None)
    return None if shared_with is None else {g.id for g in shared_with.get("groups") or []}


def dashboard_sharing_gaps(dash: Any, view: Any) -> List[str]:
    """Why people who can open the dashboard may still see nothing, from the sharing of the two items."""
    access = getattr(dash, "access", None)
    if access == "private":
        return ["it is shared with nobody, so only its owner can open it"]
    if view is None:
        return []
    view_access = getattr(view, "access", None)
    if access in _ACCESS_RANK and view_access in _ACCESS_RANK and _ACCESS_RANK[access] > _ACCESS_RANK[view_access]:
        return [f"it is shared more widely ('{access}') than its view ('{view_access}'), so some people "
                "who open it see no data"]
    if access == view_access == "shared":
        dash_groups, view_groups = _shared_group_ids(dash), _shared_group_ids(view)
        if dash_groups is not None and view_groups is not None and dash_groups - view_groups:
            return ["it is shared with a group that the view is not shared with, so that group sees no data"]
    return []


def check_dashboard(ctx: Context) -> Result:
    if (blocked := _need_gis(ctx)):
        return blocked
    agol = ctx.config.get("arcgis", {})
    dash_id = normalize_text(agol.get("dashboard_id"))
    if not dash_id:
        return SKIP, "no arcgis.dashboard_id configured"
    item = ctx.gis.content.get(dash_id)
    if item is None:
        return WARN, (f"WARNING: dashboard item {dash_id} not found or not visible to this account. "
                      f"If arcgis.dashboard_id is wrong, fix it. {DASHBOARD_FIX}")
    problems = []
    if getattr(item, "access", None) == "public":
        problems.append("it is shared with everyone (public); set its sharing to your group or organization")
    view_id = normalize_text(agol.get(view_key("prod")))
    if view_id and view_id not in _dashboard_view_ids(item):
        problems.append(f"its data sources do not read the view {view_id}")
    problems += dashboard_sharing_gaps(item, ctx.gis.content.get(view_id) if view_id else None)
    if problems:
        return WARN, f"WARNING: dashboard {dash_id}: " + "; ".join(problems) + f". {DASHBOARD_FIX}"
    return OK, f"dashboard {dash_id} exists, is not public" + (" and reads the view" if view_id else "")


def check_delete_protection(ctx: Context) -> Result:
    if (blocked := _need_gis(ctx)):
        return blocked
    view = _view_item(ctx)
    items = [(kind, item) for kind, item in (("service", ctx.item), ("view", view)) if item is not None]
    if not items:
        return SKIP, "no service or view item was read, so delete protection is unknown"
    off = [f"{kind} {item.id}" for kind, item in items if not getattr(item, "protected", False)]
    if off:
        return WARN, ("WARNING: delete protection is off for " + ", ".join(off) + ". Anyone who can edit the "
                      "item can delete it. Turn it on in ArcGIS Online: Item > Settings > Delete protection. "
                      "A new install gets it from 'taskdash setup'.")
    return OK, "delete protection is on for " + " and ".join(f"the {kind}" for kind, _ in items)


def _uses_stored_credentials(config: Dict[str, Any]) -> Optional[str]:
    if normalize_text(config.get("arcgis", {}).get("profile")):
        return "arcgis.profile"
    smtp = config.get("smtp") or {}
    if normalize_text(smtp.get("user")) and config.get("alerting", {}).get("enabled"):
        return "the smtp.user keyring entry"
    return None


def _collector_task(ctx: Context) -> Tuple[str, Optional[Dict[str, Any]]]:
    name = normalize_text(ctx.config.get("scheduler", {}).get("collector_task_name")) or DEFAULT_COLLECTOR_TASK_NAME
    task = next((t for t in ctx.tasks or [] if normalize_text(t.get("task_name")).lower() == name.lower()), None)
    return name, task


def _no_collector_task(ctx: Context, name: str) -> Result:
    # [--] only while the install is not set up. Once the service exists the task should too, and a
    # standard window cannot see a task registered under another account, so [--] would hide both
    # that and a wrong scheduler.collector_task_name.
    if _service_not_set_up(ctx):
        return SKIP, (f"collector task '{name}' is not registered yet. Register it with install_task.ps1, or "
                      "set scheduler.collector_task_name to its real name.")
    return WARN, (f"WARNING: collector task '{name}' is not registered, or this window cannot see it. Run "
                  "preflight in an administrator window to see tasks of other accounts. If it still "
                  "does not show, register the task with install_task.ps1, or set "
                  "scheduler.collector_task_name to its real name.")


def check_logon_type(ctx: Context) -> Result:
    if ctx.tasks is None:
        return FAIL, "task definitions were not read (see the task check above), so the logon type is unknown."
    name, task = _collector_task(ctx)
    if task is None:
        return _no_collector_task(ctx, name)
    logon, stored = normalize_text(task.get("logon_type")), _uses_stored_credentials(ctx.config)
    if logon.lower() == "s4u" and stored:
        return FAIL, (f"task '{name}' uses S4U and the config reads {stored}. S4U logon cannot read stored "
                      "credentials (Windows gives S4U no access to encrypted files). Re-register the "
                      "task with the Password logon type.")
    return OK, f"task '{name}' logs on as {logon or 'unknown'}"


def read_admin_members() -> List[Tuple[str, str]]:
    """(name, object class) of each local Administrators member. Reading a group needs no elevation."""
    rows = run_powershell_json("@(Get-LocalGroupMember -SID S-1-5-32-544 | "
                               "Select-Object @{n='name';e={[string]$_.Name}}, "
                               "@{n='kind';e={[string]$_.ObjectClass}}) | ConvertTo-Json -Compress", 60)
    return [(normalize_text(r.get("name")), normalize_text(r.get("kind"))) for r in rows]


def check_admin_account(ctx: Context) -> Result:
    # Measured in a clean Windows: a standard account saw 170 of 229 tasks (none registered by
    # other accounts) and, without "Log on as a batch job", never started. Neither shows anywhere
    # else, so this is the only line that can catch it.
    if ctx.tasks is None:
        return FAIL, "task definitions were not read (see the task check above), so the account is unknown."
    name, task = _collector_task(ctx)
    if task is None:
        return _no_collector_task(ctx, name)
    user =normalize_text(task.get("run_as_user"))
    if not user:
        return WARN, (f"WARNING: Task Scheduler did not report the account task '{name}' runs as. Confirm it "
                      "is in the local Administrators group with: net localgroup Administrators")
    if user.lower() in {"system", "nt authority\\system", "s-1-5-18"}:
        return OK, f"task '{name}' runs as SYSTEM"
    fix = f"From an elevated prompt run: net localgroup Administrators {user} /add"
    try:
        members = read_admin_members()
    except (OSError, SystemExit, subprocess.SubprocessError) as exc:
        return WARN, (f"WARNING: could not list the local Administrators group ({type(exc).__name__}). "
                      f"Confirm '{user}' is a member with: net localgroup Administrators")
    short = user.split("\\")[-1].lower()
    # The short-name match is only for an account Task Scheduler reports without a domain: with
    # one, CORP\svc must not pass on a different local PC\svc.
    bare = "\\" not in user
    if any(kind.lower() == "user" and (member.lower() == user.lower()
                                       or (bare and member.split("\\")[-1].lower() == short))
           for member, kind in members):
        return OK, f"task account '{user}' is in the local Administrators group"
    groups = [member for member, kind in members if kind.lower() == "group"]
    if groups:
        return WARN, (f"WARNING: task account '{user}' is not a direct member of the local Administrators "
                      f"group; confirm it belongs through {', '.join(groups[:5])}. A standard account misses "
                      f"other accounts' tasks. {fix}")
    return FAIL, (f"task account '{user}' is not in the local Administrators group, so it misses other "
                  f"accounts' tasks and cannot log on as a batch job. {fix}")


def check_collector_install(ctx: Context) -> Result:
    """The collector task must run this install's folder: it reads that folder's config and writes
    its logs there. A task left from another install, or moved, publishes with the wrong settings."""
    if ctx.tasks is None:
        return FAIL, "task definitions were not read (see the task check above), so the task's action is unknown."
    name, task = _collector_task(ctx)
    if task is None:
        return _no_collector_task(ctx, name)
    home = ntpath.normcase(ntpath.normpath(str(home_dir())))
    workdir = collect.strip_wrapping_quotes(task.get("primary_working_directory"))
    program = collect.strip_wrapping_quotes(task.get("primary_executable"))
    problems = []
    if not workdir:
        problems.append("it sets no working folder, so it starts where it finds no config")
    elif ntpath.normcase(ntpath.normpath(workdir)) != home:
        problems.append(f"it starts in '{workdir}', not in this install's folder '{home_dir()}'")
    # install_task.ps1 -Python <interpreter> registers cmd.exe and names run_taskdash.cmd in the arguments.
    arguments = ntpath.normcase(collect.strip_wrapping_quotes(task.get("primary_arguments")).replace('"', ""))
    via_wrapper = (ntpath.basename(ntpath.normcase(program)) == "cmd.exe"
                   and (home + "\\run_taskdash.cmd") in arguments)
    if program and not via_wrapper and not ntpath.normcase(program).startswith(home + "\\"):
        problems.append(f"it runs '{program}', which is outside this install's folder")
    if problems:
        # -Replace keeps the old action and folder, so only a fresh registration repairs them.
        return WARN, (f"WARNING: task '{name}': " + "; ".join(problems) + f". Remove it with "
                      f"Unregister-ScheduledTask -TaskName '{name}' from an elevated prompt, then register it "
                      "again with install_task.ps1 -Apply from this install (without -Replace), or run "
                      "preflight from the install folder the task uses.")
    return OK, f"task '{name}' runs {program or 'its program'} in this install's folder"


def check_handoff(ctx: Context) -> Result:
    waiting = handoff_waiting()
    if waiting is None:
        return SKIP, "no sign-in hand-off file is waiting"
    _, task = _collector_task(ctx) if ctx.tasks is not None else ("", None)
    runs_as = normalize_text((task or {}).get("run_as_user"))
    wrong = ""
    if waiting["account"] and runs_as and _bare_name(waiting["account"]) != _bare_name(runs_as):
        wrong = (f" The collector task runs as {runs_as}, so it will not store the file. Make it again with "
                 f"{command_text('signin', '--for-task-account', '--task-user', runs_as, '--apply')}.")
    return WARN, (f"WARNING: {handoff_waiting_text()}{wrong} Start the collector task. "
                  + see_troubleshooting("A hand-off file is waiting"))


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


def check_health_check(ctx: Context) -> Result:
    # Print only set / not set: the address carries a secret ping ID.
    if normalize_text((ctx.config or {}).get("alerting", {}).get("healthcheck_url")):
        return OK, "alerting.healthcheck_url is set"
    return WARN, ("WARNING: alerting.healthcheck_url is not set. If the collector never starts, such as "
                  "after a password expires or someone disables the task, nothing tells you. Set it to the "
                  "ping address of an outside monitor, such as a Healthchecks.io check. See "
                  "'Watch for a silent collector' in docs/CONFIGURE.md.")


def check_host_name(ctx: Context) -> Result:
    # The same test as the publish refusal: collect --apply stops on this mismatch.
    mismatch = host_mismatch(ctx.config or {})
    if not mismatch:
        return OK, "host_name_override is empty or matches this computer"
    override, actual = mismatch
    return WARN, (f"WARNING: host_name_override is '{override}' but this computer is '{actual}'. "
                  f"collect --apply refuses to run until they match. Clear host_name_override, or after a "
                  f"rename add '{override}' to host_previous_names and set host_name_override to '{actual}'. "
                  "Pass --allow-host-mismatch only on purpose.")


def check_base_python(ctx: Context) -> Result:
    # A per-user Python lives under C:\Users\<name>; the task account usually has no right to run it.
    # ntpath keeps the test the same on any OS the tests run on.
    users = ntpath.normcase(os.environ.get("SystemDrive") or "C:") + "\\users\\"
    base = sys.base_prefix
    if not ntpath.normcase(base).startswith(users):
        return OK, f"the base Python is outside C:\\Users ({base})"
    return WARN, (f"WARNING: the base Python is installed for one user ({base}). The task account may not "
                  "be able to run it. Install Python for all users, then build the virtual environment "
                  "again. See the Python requirement and step 1 in docs/INSTALL.md.")


def check_arcgis_version(ctx: Context) -> Result:
    warning = arcgis_version_warning()
    return (WARN, warning) if warning else (OK, "the arcgis package is in the tested range (>=2.4,<3)")


def check_language_mode(ctx: Context) -> Result:
    # AppLocker or WDAC puts PowerShell in ConstrainedLanguage. TaskDash still collects there (see
    # POWERSHELL_WRAPPER in taskdash_common), so this is a warning, never a failure.
    mode = powershell_language_mode()
    if mode == "FullLanguage":
        return OK, "PowerShell runs in FullLanguage mode"
    if mode == "ConstrainedLanguage":
        return WARN, ("WARNING: PowerShell runs in ConstrainedLanguage mode, so AppLocker or WDAC is on. "
                      "TaskDash cannot call .NET methods there, so it reads event fields by position and "
                      "returns each result through a temporary file in the TaskDash folder. If a "
                      "collection fails, ask your administrator to add an allow rule for the TaskDash "
                      "folder, or run the collector as an account that the policy exempts. See "
                      "'The language mode line warns' in docs/TROUBLESHOOTING.md.")
    return WARN, (f"WARNING: could not read the PowerShell language mode ({mode or 'no answer'}). Run "
                  "$ExecutionContext.SessionState.LanguageMode in PowerShell as the task account. "
                  "FullLanguage is the expected answer.")


# Principals that must not write to the install folder, as icacls prints them: by English name, or by
# SID when Windows cannot resolve one. local_names adds the names in this computer's own language.
LOOSE_PRINCIPALS = {"users", "authenticated users", "everyone", "s-1-1-0", "s-1-5-11", "s-1-5-32-545"}
LOOSE_SIDS = ("S-1-5-32-545", "S-1-5-11", "S-1-1-0")
# SYSTEM and Administrators are in every sane install folder's list; seeing none of them means the
# names were not understood.
TRUSTED_PRINCIPALS = {"system", "administrators", "s-1-5-18", "s-1-5-32-544"}
TRUSTED_SIDS = ("S-1-5-18", "S-1-5-32-544")
# icacls rights that change files: simple F, M, W and the specific write rights.
WRITE_RIGHTS = {"F", "M", "W", "WD", "AD", "WEA", "DC", "DE", "WA", "WDAC", "WO", "GW", "GA"}


def local_names(sids: Sequence[str]) -> set:
    """Lower-case names Windows prints for well-known SIDs in this computer's language."""
    try:
        import win32security
    except ImportError:
        return set()
    names = set()
    for sid in sids:
        try:
            names.add(win32security.LookupAccountSid(None, win32security.ConvertStringSidToSid(sid))[0].lower())
        except Exception:  # noqa: BLE001 - pywintypes.error; the English names and the unrecognised-ACL warning remain
            continue
    return names


def read_acl(path: str) -> str:
    """icacls output for one folder. Read-only; the full path stops a planted icacls.exe from running.
    icacls writes in the OEM code page, so decode with it or accented local names would not match."""
    done = subprocess.run([system32_exe("icacls.exe"), path], capture_output=True, encoding="oem",
                          errors="replace", timeout=30, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"icacls exited {done.returncode}: {done.stderr.strip()[:200]}")
    return done.stdout


def acl_entries(acl_text: str, path: str) -> List[Tuple[str, str, set]]:
    """(principal as printed, its bare lower-case name, rights) for each entry in icacls output."""
    entries = []
    for line in acl_text.splitlines():
        line = line.strip()
        if line.startswith(path):  # icacls prints the path before the first principal only
            line = line[len(path):].strip()
        cut = line.find(":(")
        if cut < 0:
            continue
        who, groups = line[:cut].strip(), re.findall(r"\(([^)]*)\)", line[cut:])
        rights = {right for group in groups for right in group.split(",")}
        entries.append((who, who.rsplit("\\", 1)[-1].lstrip("*").lower(), rights))
    return entries


def loose_writers(acl_text: str, path: str) -> List[str]:
    """Principals from icacls output that hold a write right and are loose principals, by English or
    local name. A DENY entry grants nothing, so it is skipped."""
    loose = LOOSE_PRINCIPALS | local_names(LOOSE_SIDS)
    return list(dict.fromkeys(who for who, name, rights in acl_entries(acl_text, path)
                              if "DENY" not in rights and name in loose and rights & WRITE_RIGHTS))


def trusted_listed(acl_text: str, path: str) -> bool:
    trusted = TRUSTED_PRINCIPALS | local_names(TRUSTED_SIDS)
    return any(name in trusted for _, name, _ in acl_entries(acl_text, path))


# SYSTEM, Administrators and TrustedInstaller. Any other owner must be an administrator itself.
TRUSTED_OWNER_SIDS = ("S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")


def read_owner(path: str) -> Tuple[str, str]:
    """(SID, DOMAIN\\name or "") of a folder's owner, read through the Win32 API. icacls does not print
    the owner, and pywin32 is not a dependency. Read-only."""
    import ctypes
    from ctypes import wintypes
    advapi, kernel = ctypes.WinDLL("advapi32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    get_info = advapi.GetNamedSecurityInfoW
    get_info.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.POINTER(ctypes.c_void_p)] * 5
    get_info.restype = wintypes.DWORD
    owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    # 1 = SE_FILE_OBJECT, 1 = OWNER_SECURITY_INFORMATION. The owner points into the descriptor.
    code = get_info(path, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
    if code:
        raise ctypes.WinError(code)
    try:
        to_string = advapi.ConvertSidToStringSidW
        to_string.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        text = ctypes.c_void_p()
        if not to_string(owner, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.wstring_at(text.value)
        kernel.LocalFree(text)
        lookup = advapi.LookupAccountSidW
        lookup.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                           wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        sizes, kind = (wintypes.DWORD(256), wintypes.DWORD(256)), wintypes.DWORD()
        # An unresolved SID (a deleted account) has no name; the SID still identifies it.
        if not lookup(None, owner, name, ctypes.byref(sizes[0]), domain, ctypes.byref(sizes[1]), ctypes.byref(kind)):
            return sid, ""
        return sid, f"{domain.value}\\{name.value}" if domain.value else name.value
    finally:
        kernel.LocalFree(descriptor)


def untrusted_owner(path: str) -> Optional[Result]:
    """None when SYSTEM, Administrators, TrustedInstaller or a direct member of Administrators owns the
    folder. Otherwise (status, message): FAIL for an owner that is not an administrator, WARN when the
    owner or the Administrators group cannot be read. A folder's owner can always change its
    permissions, so a locked folder owned by an ordinary user is not locked against that user."""
    fix = f'From an elevated prompt run: icacls "{path}" /setowner *S-1-5-32-544'
    try:
        sid, name = read_owner(path)
    except OSError as exc:
        return WARN, (f"WARNING: could not read the owner of {path} ({type(exc).__name__}). Run (Get-Acl '{path}').Owner "
                      "in PowerShell and confirm that it is Administrators or an administrator.")
    if sid in TRUSTED_OWNER_SIDS:
        return None
    try:
        members = read_admin_members()
    except (OSError, SystemExit, subprocess.SubprocessError):
        return WARN, (f"WARNING: could not list the Administrators group, so this check cannot tell whether "
                      f"{name or sid}, the owner of {path}, is an administrator. Run net localgroup Administrators.")
    if name and any(member.lower() == name.lower() for member, kind in members if kind.lower() == "user"):
        return None
    groups = [member for member, kind in members if kind.lower() == "group"]
    if groups:
        # The usual domain setup: the account is an administrator through a group, which this list cannot expand.
        return WARN, (f"WARNING: {path} is owned by {name or sid}, which is not a direct member of the "
                      f"Administrators group; confirm that it belongs through {', '.join(groups[:5])}. {fix}")
    return FAIL, (f"{path} is owned by {name or sid}, which is not SYSTEM, Administrators or a direct member "
                  "of the Administrators group. The owner can always change the folder's permissions, so a "
                  f"non-admin owner could put code there that the task runs as an administrator. {fix}")


# Placeholders and the system installer: an entry for one of them grants no ordinary user a write right.
IGNORED_WRITERS = {"creator owner", "trustedinstaller", "s-1-3-0"}
IGNORED_SIDS = ("S-1-3-0",)


def other_writers(acl_text: str, path: str) -> List[str]:
    """Principals that hold a write right but are not SYSTEM, Administrators or a placeholder. Each must
    still be an administrator itself. A DENY entry grants nothing, so it is skipped."""
    skip = TRUSTED_PRINCIPALS | IGNORED_WRITERS | local_names(TRUSTED_SIDS + IGNORED_SIDS)
    return list(dict.fromkeys(who for who, name, rights in acl_entries(acl_text, path)
                              if "DENY" not in rights and name not in skip and rights & WRITE_RIGHTS))


def check_install_folder_acl(ctx: Context) -> Result:
    # The task runs as an administrator and loads code and config from here, so a folder that
    # a non-administrator can write to is a way to run code as an administrator.
    path = str(home_dir())
    by_hand = f'icacls "{path}" and confirm that only SYSTEM, Administrators and administrators can write.'
    try:
        acl = read_acl(path)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return WARN, f"WARNING: could not read the permissions of {path} ({type(exc).__name__}). Run {by_hand}"
    writers = loose_writers(acl, path)
    if not writers and not trusted_listed(acl, path):
        return WARN, (f"WARNING: could not recognise SYSTEM or Administrators in the permissions of {path}, so "
                      f"this check cannot tell who can write there. Run {by_hand}")
    if writers:
        return FAIL, (f"{path} lets {', '.join(writers)} write. The task runs as an administrator and loads code "
                      "and config from this folder, so any of them could run code as an administrator. From an "
                      f'elevated prompt run: icacls "{path}" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" '
                      '"*S-1-5-32-544:(OI)(CI)F" /remove:g "*S-1-5-32-545" "*S-1-5-11" "*S-1-1-0"')
    # The task account needs Modify here (log and state writes), so it is never an outsider.
    _, task = _collector_task(ctx) if ctx.tasks is not None else ("", None)
    user = normalize_text((task or {}).get("run_as_user")).lower()
    candidates = [who for who in other_writers(acl, path)
                  if not user or (who.lower() != user and ("\\" in user or who.split("\\")[-1].lower() != user))]
    if candidates:
        try:
            members = read_admin_members()
            admins = {member.lower() for member, _ in members}
        except (OSError, SystemExit, subprocess.SubprocessError) as exc:
            return WARN, (f"WARNING: could not list the Administrators group ({type(exc).__name__}), so this check "
                          f"cannot tell whether {', '.join(candidates)} may write to {path}. Run net localgroup "
                          "Administrators and confirm that each is a member.")
        outsiders = [who for who in candidates if who.lower() not in admins]
        groups = [member for member, kind in members if kind.lower() == "group"]
        if outsiders and groups:
            return WARN, (f"WARNING: {path} lets {', '.join(outsiders)} write, and they are not direct members of the "
                          f"Administrators group. Confirm that each belongs through {', '.join(groups[:5])}. "
                          "Write access to this folder is control of the task account, which runs as an administrator.")
        if outsiders:
            return FAIL, (f"{path} lets {', '.join(outsiders)} write, and they are not administrators. Write access "
                          "to this folder is control of the task account, which runs as an administrator. From an "
                          f'elevated prompt run: icacls "{path}" /grant:r "{outsiders[0]}:(OI)(CI)RX"')
    return untrusted_owner(path) or (OK, f"{path} is writable only by SYSTEM, Administrators and administrators")


CHECKS: Sequence[Tuple[str, bool, Callable[[Context], Result]]] = (
    # (name, needs network, function); check_config runs first, outside this list.
    ("credentials", True, check_credentials),
    ("privileges", True, check_privileges),
    ("event log", False, check_event_log),
    ("task definitions", False, check_tasks),
    ("log folders", False, check_log_folders),
    ("descriptions", False, check_descriptions),
    ("log roots", False, check_log_roots),
    ("smtp", True, check_smtp),
    ("service", True, check_service),
    ("view", True, check_view),
    ("sharing", True, check_sharing),
    ("dashboard", True, check_dashboard),
    ("delete protection", True, check_delete_protection),
    ("collector logon type", False, check_logon_type),
    ("collector account", False, check_admin_account),
    ("collector install", False, check_collector_install),
    ("hand-off", False, check_handoff),
    ("tested scale", False, check_scale),
    ("health check", False, check_health_check),
    ("host name", False, check_host_name),
    ("base python", False, check_base_python),
    ("arcgis version", False, check_arcgis_version),
    ("language mode", False, check_language_mode),
    ("install folder", False, check_install_folder_acl),
)


# --------------------------------------------------------------------------- runner

def run_check(check: Callable[[Context], Result], ctx: Context) -> Result:
    try:
        return check(ctx)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - one broken check must not hide the rest
        text = exc.code if isinstance(exc, SystemExit) else exc
        return FAIL, f"{type(exc).__name__}: {text}"


# A check that cannot answer when the check it reads from failed. It prints [--] and is not counted, so a
# missing sign-in shows as one problem, on the credentials line.
NEEDS = {
    "privileges": "credentials", "service": "credentials", "view": "credentials", "sharing": "credentials",
    "dashboard": "credentials", "delete protection": "credentials",
    "collector logon type": "task definitions", "collector account": "task definitions",
    "collector install": "task definitions", "tested scale": "task definitions",
}


def run_preflight(args: argparse.Namespace) -> int:
    ctx = Context(args)
    results = [("config", *run_check(check_config, ctx))]
    if ctx.config is not None:
        for name, needs_network, check in CHECKS:
            root = NEEDS.get(name)
            if needs_network and args.offline:
                results.append((name, SKIP, "skipped with --offline"))
            elif root and any(n == root and status == FAIL for n, status, _ in results):
                results.append((name, SKIP, f"needs: {root}. Fix the {root} line above first."))
            else:
                results.append((name, *run_check(check, ctx)))
    for name, status, message in results:
        emit(f"{MARKS[status]} {name}: {message}")
    failed = sum(status == FAIL for _, status, _ in results)
    warned = sum(status == WARN for _, status, _ in results)
    emit(f"Preflight: {failed} problem(s), {warned} warning(s).")
    return 1 if failed else 0


# --------------------------------------------------------------------------- config diff

def default_wording(value: Any) -> str:
    """A code default as the Default column of docs/CONFIGURE.md words it."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, list, dict)) and not value:
        return {str: "empty", list: "empty list", dict: "empty object"}[type(value)]
    return f"{value:g}" if isinstance(value, float) else str(value)


def run_config_diff(config_arg: str) -> int:
    """Print config keys TaskDash does not know, known keys that are absent with their default,
    and renamed keys. Reads the raw JSON, not load_config: a typo'd key must show here even when
    load_config would stop on it. Prints no value from the config, only key names and code defaults."""
    path = Path(config_arg) if config_arg else runtime_dir() / DEFAULT_CONFIG_FILE
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        emit(f"Config not found at {exc.filename}: copy TaskDash.config.example.json to "
             f"{DEFAULT_CONFIG_FILE} and fill it in, or pass --config.")
        return 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        emit(f"Cannot read the config at {path}: not valid JSON or not UTF-8 text "
             f"({type(exc).__name__}). Fix the file, then run again.")
        return 1
    if not isinstance(config, dict):
        emit(f"Config at {path} must be a JSON object. Fix the file, then run again.")
        return 1
    diff = config_diff(config)
    emit(f"Config diff for {path} (information only):")
    emit("Unknown keys (TaskDash ignores them; check for a typo):")
    for key in diff["unknown"] or ["none"]:
        emit(f"  {key}")
    emit("Renamed keys (old -> new; move the value to the new name):")
    for old, new in diff["renamed"] or [("none", None)]:
        emit(f"  {old} -> {new}" if new else f"  {old}")
    emit("Absent keys (TaskDash uses the default shown):")
    for key in diff["absent"]:
        emit(f"  {key} (default: {default_wording(CONFIG_DEFAULTS[key])})" if key in CONFIG_DEFAULTS else f"  {key}")
    if not diff["absent"]:
        emit("  none")
    return 0


def undescribed_tasks(tasks: Sequence[Dict[str, Any]], scheduler: Dict[str, Any],
                      descriptions: Dict[str, Any]) -> List[str]:
    """Names of the monitored tasks that have no description in workflow_descriptions.json and no
    Description of their own in Task Scheduler."""
    return sorted({normalize_text(task.get("task_name")) for task in tasks
                   if collect.should_include_task(task, scheduler)
                   and not normalize_text(task.get("description"))
                   and not collect.lookup_description(descriptions, *collect.description_names(task))},
                  key=str.lower)


def run_describe(config_arg: str) -> int:
    """Print the monitored tasks that have no description, then a starter workflow_descriptions.json with an
    empty text for each. Reads the config and Task Scheduler; writes no file."""
    try:
        config, _ = load_config(Path(config_arg) if config_arg else None, defer_warnings=True)
        scheduler = config.get("scheduler") or {}
        tasks = collect.collect_tasks(int(config.get("powershell_timeout_seconds", config_default("powershell_timeout_seconds"))),
                                      scheduler.get("exclude_microsoft_tasks", config_default("scheduler.exclude_microsoft_tasks")))
        names = undescribed_tasks(tasks, scheduler, collect.load_descriptions(warnings=[]))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - say what to fix, not a traceback
        emit(f"Cannot list the tasks: {type(exc).__name__}: {exc.code if isinstance(exc, SystemExit) else exc}")
        return 1
    if not names:
        emit("Every monitored task has a description.")
        return 0
    emit(f"{len(names)} monitored task(s) have no description. The dashboard shows 'No description yet' for them.")
    emit("Save the text below as workflow_descriptions.json in the TaskDash folder, or add its lines to your "
         "file, then write a sentence for each task. TaskDash ignores a task whose text stays empty.")
    emit(json.dumps({"tasks": {name: "" for name in names}}, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- event log setup

def run_enable_event_log(apply: bool, max_size_mb: Optional[int]) -> int:
    enabled, current = read_log_state()
    emit(f"{EVENT_LOG_NAME}: enabled={str(enabled).lower()}, max size {current} bytes.")
    # Never shrink a log the owner already enlarged, unless they ask for a size.
    wanted = max_size_mb * 1024 * 1024 if max_size_mb else max(current, RECOMMENDED_LOG_MB * 1024 * 1024)
    args = ["sl", EVENT_LOG_NAME, "/e:true", f"/ms:{wanted}"]
    command = [system32_exe("wevtutil.exe"), *args]
    if not apply:
        emit("Check mode, nothing changed. Would run: " + " ".join(["wevtutil", *args]))
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
        prog="taskdash preflight",
        description="Check a TaskDash install. Changes nothing unless you pass --apply with "
                    "--enable-event-log or --as-task.",
        allow_abbrev=False,  # --apply must never be reachable by abbreviation
    )
    parser.add_argument("--config", default="", metavar="PATH",
                        help="Config file. Default: the install's config.")
    parser.add_argument("--offline", action="store_true",
                        help="Skip every check that needs the network ("
                             + ", ".join(name for name, needs_network, _ in CHECKS if needs_network) + ").")
    parser.add_argument("--enable-event-log", action="store_true",
                        help="Show the Task Scheduler log state and the command that enables it. "
                             "Runs alone, without the other checks.")
    parser.add_argument("--apply", action="store_true",
                        help="With --enable-event-log or --as-task: run the command.")
    parser.add_argument("--max-size-mb", type=int, default=0,
                        help=f"With --enable-event-log: log size in MB (default {RECOMMENDED_LOG_MB}).")
    parser.add_argument("--config-diff", action="store_true",
                        help="Compare the config keys with the keys TaskDash knows: unknown, absent "
                             "and renamed. Prints no secret value. Runs alone.")
    parser.add_argument("--describe", action="store_true",
                        help="List the monitored tasks that have no description and print a starter "
                             "workflow_descriptions.json for them. Reads only. Runs alone.")
    parser.add_argument("--as-task", action="store_true",
                        help="Run the preflight checks as the task account, in a temporary task, without "
                             "runas. Prints the plan and changes nothing without --apply. Runs alone.")
    parser.add_argument("--task-user", default="", metavar="NAME",
                        help="With --as-task: the account to run as, DOMAIN\\name. Default: the account "
                             "of the collector task.")
    parser.add_argument("--consume-handoff", action="store_true",
                        help=argparse.SUPPRESS)  # internal: --as-task passes it to the check it runs
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    args = parser.parse_args(argv)
    if (args.apply and not (args.enable_event_log or args.as_task)) or (args.max_size_mb and not args.enable_event_log):
        parser.error("--apply works only with --enable-event-log or --as-task, and --max-size-mb "
                     "only with --enable-event-log.")
    if args.task_user and not args.as_task:
        parser.error("--task-user only works with --as-task.")
    if args.describe and (args.enable_event_log or args.config_diff or args.as_task or args.offline or args.apply
                          or args.consume_handoff):
        parser.error("--describe runs alone. Drop the other flags except --config.")
    if args.as_task and (args.enable_event_log or args.config_diff):
        parser.error("--as-task runs alone. Drop --enable-event-log and --config-diff.")
    if args.consume_handoff and (args.enable_event_log or args.config_diff or args.as_task):
        parser.error("--consume-handoff runs with the plain checks only. Drop --enable-event-log, "
                     "--config-diff and --as-task.")
    if args.config_diff and (args.enable_event_log or args.offline):
        parser.error("--config-diff runs alone. Drop --enable-event-log and --offline.")
    if args.max_size_mb < 0:
        parser.error("--max-size-mb must be a positive number of megabytes.")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.config_diff:
        return run_config_diff(args.config)
    if args.describe:
        return run_describe(args.config)
    if args.as_task:
        from taskdash_astask import run_as_task  # only --as-task needs pywin32
        return run_as_task(args)
    if not args.enable_event_log:
        return run_preflight(args)
    try:
        return run_enable_event_log(args.apply, args.max_size_mb or None)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        emit(f"Could not run wevtutil: {type(exc).__name__}: {exc}. Run it on Windows from an elevated prompt.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

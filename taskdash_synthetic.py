"""Synthetic fixture generator: invented hosts, tasks, runs and log lines.

Driver: `taskdash demo` builds its data with it; a developer or CI can also run it by hand
Source: nothing -- every host, task, run and log line is invented here
Target: one fixture JSON per host in --out-dir, in the shape `taskdash_publish.py --fixture` reads

    python -m taskdash_synthetic --out-dir tests/fixtures --now 2026-10-16T12:00:00Z

Output is a pure function of --now and --seed, so a CI run can freeze it. The fixture format
carries tasks and events only. Two inputs cannot ride in it, so the generator supplies them apart:
  * the "before" state a change is diffed against comes from the hosted TaskInventory
    table, never from the fixture. Each host file carries it as two extra keys that
    collect_everything ignores: "prior_tasks" (the task rows as they were) and
    "prior_drop_instances" (run instances that had not happened yet). prior_fixture() turns the
    two into a fixture of the earlier capture; diff its inventory against the current one to get
    the "Task changed", "Started failing" and "Recovered" rows.
  * log lines are mined from files on disk, found by script path or by
    metrics.log_directory_overrides. write_metric_logs() writes them for a given real clock and
    returns that override map.
"""

from __future__ import annotations

import argparse
import copy
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HOSTS = ("APP-HOST-01", "APP-HOST-02")
ACCOUNT = "SYNTH\\svc-tasks"
ACCESS_DENIED = 2147942522
FILE_NOT_FOUND = 2147942402
RUNNING_RESULT = 267009
NOT_YET_RUN_RESULT = 267011


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def daily_trigger(hour: int, minute: int, **extra: Any) -> Dict[str, Any]:
    base = {"type": "Daily", "start": f"2025-01-05T{hour:02d}:{minute:02d}:00", "end": "",
            "enabled": True, "days_interval": "1", "days_of_week": "", "weeks_interval": "",
            "repetition_interval": "", "repetition_duration": "", "random_delay": "", "delay": "",
            "months": "", "days_of_month": "", "weeks_of_month": "", "month_days_of_week": ""}
    base.update(extra)
    return base


def raw_task(host_no: int, name: str, script: str, triggers: List[Dict[str, Any]], **kw: Any) -> Dict[str, Any]:
    # Each host gets its own task folder, so no two tasks share a path across hosts.
    path = f"\\AppHost{host_no:02d}\\"
    script_path = f"X:\\synthetic\\host{host_no:02d}\\{script}"
    interpreter = "X:\\synthetic\\python\\python.exe"
    token = triggers[0]["type"] if triggers else ""
    row = {
        "task_name": name, "task_path": path, "task_full_name": path + name, "state": "Ready",
        "enabled": True, "hidden": False, "author": ACCOUNT, "run_as_user": ACCOUNT,
        "description": "", "action_count": 1,
        "actions": f"{interpreter} {script_path} --mode run",
        "action_details": [], "primary_executable": interpreter,
        "primary_arguments": f"{script_path} --mode run", "primary_working_directory": "",
        "trigger_count": len(triggers), "trigger_details": triggers, "trigger_types": token,
        "logon_type": "Password", "run_level": "Limited", "execution_time_limit": "PT1H",
        "multiple_instances_policy": "StopExisting", "start_when_available": True,
        "last_run_time": None, "next_run_time": None, "last_task_result": 0,
        "number_of_missed_runs": 0,
    }
    row.update(kw)
    return row


def host_specs(host_no: int) -> List[Dict[str, Any]]:
    """Task blueprints. `case` marks the seeded cases; everything else is a healthy task."""
    if host_no == 1:
        return [
            {"name": "Inventory Export", "script": "inventory_export.py", "minute": 5, "hour": 2},
            {"name": "Nightly Backup", "script": "nightly_backup.py", "minute": 30, "hour": 1},
            {"name": "Index Rebuilder", "script": "index_rebuilder.py", "minute": 0, "hour": 3},
            {"name": "Log Rotator", "script": "log_rotator.py", "minute": 45, "hour": 4},
            {"name": "Heartbeat Check", "script": "heartbeat_check.py", "every": "PT1H", "case": "key_missing"},
            {"name": "Metric Loader", "script": "metric_loader.py", "minute": 15, "hour": 5, "case": "far_below"},
            {"name": "Report Mailer", "script": "report_mailer.py", "minute": 10, "hour": 6, "case": "failure"},
            {"name": "Data Archiver", "script": "data_archiver.py", "minute": 20, "hour": 7, "case": "stuck"},
            {"name": "Weekly Cleanup", "script": "weekly_cleanup.py", "weekly": True, "case": "overdue"},
            {"name": "Legacy Sync", "script": "legacy_sync.py", "minute": 40, "hour": 8, "case": "disabled"},
            {"name": "Cache Refresher", "script": "cache_refresher.py", "minute": 50, "hour": 9, "case": "setting_risk"},
            {"name": "Config Publisher", "script": "config_publisher.py", "minute": 25, "hour": 10, "case": "definition_change"},
            {"name": "Queue Drainer", "script": "queue_drainer.py", "minute": 35, "hour": 11, "case": "log_errors"},
        ]
    return [
        {"name": "Archive Copy", "script": "archive_copy.py", "minute": 5, "hour": 1, "case": "shared_cause"},
        {"name": "Report Builder", "script": "report_builder.py", "minute": 15, "hour": 2, "case": "shared_cause"},
        {"name": "Export Packager", "script": "export_packager.py", "minute": 25, "hour": 3, "case": "shared_cause"},
        {"name": "Mirror Sync", "script": "mirror_sync.py", "minute": 35, "hour": 4, "case": "shared_cause"},
        {"name": "Metrics Rollup", "script": "metrics_rollup.py", "minute": 45, "hour": 5},
        {"name": "Session Cleaner", "script": "session_cleaner.py", "minute": 55, "hour": 6, "case": "missed_runs"},
        {"name": "Cert Checker", "script": "cert_checker.py", "minute": 5, "hour": 7, "case": "not_yet_run"},
        {"name": "Disk Reporter", "script": "disk_reporter.py", "minute": 15, "hour": 8, "case": "running"},
        {"name": "Status Poller", "script": "status_poller.py", "every": "PT1H", "case": "blocked"},
        {"name": "Backup Verifier", "script": "backup_verifier.py", "minute": 25, "hour": 9, "case": "recovered"},
        {"name": "Audit Collector", "script": "audit_collector.py", "minute": 35, "hour": 10},
        {"name": "Queue Monitor", "script": "queue_monitor.py", "minute": 45, "hour": 11},
    ]


def build_events(task: Dict[str, Any], starts: List[datetime], durations: List[float], rng: random.Random,
                 last_result: int, left_running: bool, previous_result: int = 0,
                 skipped: Tuple[datetime, ...] = ()) -> List[Dict[str, Any]]:
    """Run events for `starts`. `previous_result` is the result of the run before the newest one.
    `skipped` are launches Task Scheduler refused (event 322) because an earlier run was going."""
    events: List[Dict[str, Any]] = []
    full = task["task_full_name"]
    for index, start in enumerate(starts):
        instance = "{" + str(uuid.UUID(int=rng.getrandbits(128))).upper() + "}"
        newest = index == len(starts) - 1
        end = start + timedelta(seconds=durations[index])
        code = last_result if newest else previous_result if index == len(starts) - 2 else 0

        def event(event_id: int, when: datetime, result: Optional[int] = None) -> Dict[str, Any]:
            return {"event_id": event_id, "event_record_id": 0, "event_time_utc": iso(when),
                    "task_full_name": full, "instance_id": instance, "user_context": ACCOUNT,
                    "action_name": task["primary_executable"], "result_code": result,
                    "computer_name": ""}

        events.append(event(100, start))
        if newest and left_running:
            continue
        events.append(event(201, end, code))
        events.append(event(102, end))
    for when in skipped:
        instance = "{" + str(uuid.UUID(int=rng.getrandbits(128))).upper() + "}"
        events.append({"event_id": 322, "event_record_id": 0, "event_time_utc": iso(when),
                       "task_full_name": full, "instance_id": instance, "user_context": ACCOUNT,
                       "action_name": task["primary_executable"], "result_code": None, "computer_name": ""})
    return events


def schedule_for(spec: Dict[str, Any], now: datetime, rng: random.Random) -> Tuple[List[datetime], timedelta]:
    """Past run starts (oldest first) and the gap to the next run."""
    if spec.get("every"):
        hours = 1
        newest = now - timedelta(minutes=rng.randint(10, 50))
        return [newest - timedelta(hours=hours * k) for k in range(35, -1, -1)], timedelta(hours=hours)
    if spec.get("weekly"):
        newest = now - timedelta(days=7, hours=5)
        return [newest - timedelta(days=7 * k) for k in range(3, -1, -1)], timedelta(days=7)
    newest = now - timedelta(hours=rng.randint(2, 20), minutes=rng.randint(0, 59))
    return [newest - timedelta(days=k) for k in range(9, -1, -1)], timedelta(days=1)


def build_host(host_no: int, now: datetime, seed: int) -> Dict[str, Any]:
    rng = random.Random(f"{seed}-{host_no}")
    tasks: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    # What the capture before this one held: the newest run of each task here had not happened yet,
    # so its prior row shows the run before it.
    prior_rows: Dict[str, Dict[str, Any]] = {}
    not_yet_happened: List[str] = []

    for spec in host_specs(host_no):
        case = spec.get("case", "")
        if spec.get("every"):
            trigger = daily_trigger(0, 0, repetition_interval=spec["every"], repetition_duration="P1D")
        elif spec.get("weekly"):
            trigger = {**daily_trigger(3, 0), "type": "Weekly", "days_interval": "", "weeks_interval": "1",
                       "days_of_week": "64"}
        else:
            trigger = daily_trigger(spec["hour"], spec["minute"])

        starts, gap = schedule_for(spec, now, rng)
        task = raw_task(host_no, spec["name"], spec["script"], [trigger])
        base_seconds = rng.randint(240, 600)
        durations = [base_seconds + rng.randint(-30, 30) for _ in starts]
        newest = starts[-1]
        task["last_run_time"] = iso(newest)
        task["next_run_time"] = iso(newest + gap)
        last_result, left_running, previous_result, skipped = 0, False, 0, ()

        if case == "failure":
            last_result = FILE_NOT_FOUND
        elif case == "shared_cause":
            last_result = ACCESS_DENIED
        elif case == "stuck":
            starts[-1] = now - timedelta(hours=3)
            task.update(state="Running", last_run_time=iso(starts[-1]),
                        next_run_time=iso(starts[-1] + gap))
            last_result, left_running = RUNNING_RESULT, True
        elif case == "overdue":
            task["next_run_time"] = iso(now - timedelta(hours=5))
        elif case == "disabled":
            starts = [now - timedelta(days=30 + k) for k in range(3, 0, -1)]
            task.update(enabled=False, state="Disabled", last_run_time=iso(starts[-1]), next_run_time=None)
            durations = durations[:len(starts)]
        elif case == "setting_risk":
            # One hung run blocks every later run: the check_silent_skip_settings rule.
            task.update(multiple_instances_policy="IgnoreNew", execution_time_limit="PT0S")
        elif case == "missed_runs":
            task["number_of_missed_runs"] = 2
        elif case == "not_yet_run":
            starts, durations, last_result = [], [], NOT_YET_RUN_RESULT
            task.update(last_run_time=None, next_run_time=iso(now + timedelta(hours=6)))
        elif case == "running":
            # Well inside its usual run time, so it is Running and not Stuck.
            starts[-1] = now - timedelta(minutes=4)
            task.update(state="Running", last_run_time=iso(starts[-1]), next_run_time=iso(starts[-1] + gap))
            last_result, left_running = RUNNING_RESULT, True
        elif case == "blocked":
            # Two hourly launches were refused because the run before was still going.
            skipped = (now - timedelta(hours=2), now - timedelta(hours=1))
        elif case == "recovered":
            previous_result = FILE_NOT_FOUND

        task["last_task_result"] = last_result
        tasks.append(task)
        task_events = build_events(task, starts, durations, rng, last_result, left_running,
                                   previous_result, skipped)
        events.extend(task_events)
        if case in ("failure", "recovered"):
            not_yet_happened.append(task_events[-1]["instance_id"])
            prior_rows[spec["name"]] = {"last_run_time": iso(starts[-2]),
                                        "last_task_result": FILE_NOT_FOUND if case == "recovered" else 0}

    events.sort(key=lambda e: e["event_time_utc"])
    for number, event in enumerate(events, start=1000):
        event["event_record_id"] = number
        event["computer_name"] = HOSTS[host_no - 1]

    payload: Dict[str, Any] = {"synthetic": True, "host_name": HOSTS[host_no - 1],
                               "captured_at_utc": iso(now), "tasks": tasks, "events": events}
    prior = copy.deepcopy(tasks)
    for row in prior:
        row.update(prior_rows.get(row["task_name"], {}))
        if row["task_name"] == "Config Publisher":
            row["primary_arguments"] = row["primary_arguments"].replace("--mode run", "--mode dry")
            row["actions"] = row["actions"].replace("--mode run", "--mode dry")
            # The privacy switches hide the command line, so a change the viewer can read needs one more.
            row["execution_time_limit"] = "PT30M"
    payload["prior_tasks"] = prior
    payload["prior_drop_instances"] = sorted(not_yet_happened)
    return payload


def prior_fixture(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The fixture of the capture before `payload`, or None when it carries no prior_tasks. Collect
    it, then diff its inventory against the current one with detect_task_changes."""
    if "prior_tasks" not in payload:
        return None
    dropped = set(payload.get("prior_drop_instances", ()))
    before = {k: v for k, v in payload.items() if k not in ("prior_tasks", "prior_drop_instances")}
    before["tasks"] = payload["prior_tasks"]
    before["events"] = [e for e in payload["events"] if e["instance_id"] not in dropped]
    return before


SCALE_HOST = "SCALE-HOST-01"


def scale_spec(index: int) -> Dict[str, Any]:
    """Blueprint for task `index` (from 1) of the scale host. Odd cases sit at fixed strides so a
    host of any size holds them: wildcard characters every 100th, a non-ASCII name every 111th,
    the \\MicrosoftSync\\ folder every 50th, an hourly task every 10th, a failing task every 25th."""
    name = f"Scale Task {index:04d}"
    if index % 100 == 0:
        name = f"Scale [{index:04d}] Task"
    elif index % 111 == 0:
        name = f"Scale Café {index:04d}"
    spec: Dict[str, Any] = {
        "name": name, "script": f"scale_job_{index:04d}.py", "hour": index % 24, "minute": (index * 7) % 60,
        "folder": "\\MicrosoftSync\\" if index % 50 == 0 else f"\\ScaleFolder{index % 10:02d}\\"}
    if index % 10 == 0:
        spec["every"] = "PT1H"
    if index % 25 == 0:
        spec["case"] = "failure"
    return spec


def build_scale_host(task_count: int, now: datetime, seed: int) -> Dict[str, Any]:
    """One fixture for a single host of `task_count` tasks, in the shape build_host returns. A pure
    function of its arguments, like build_host. A daily task has 30 events, an hourly task 108."""
    rng = random.Random(f"{seed}-scale")
    tasks: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for index in range(1, task_count + 1):
        spec = scale_spec(index)
        if spec.get("every"):
            trigger = daily_trigger(0, 0, repetition_interval=spec["every"], repetition_duration="P1D")
        else:
            trigger = daily_trigger(spec["hour"], spec["minute"])
        starts, gap = schedule_for(spec, now, rng)
        task = raw_task(1, spec["name"], spec["script"], [trigger], task_path=spec["folder"],
                        task_full_name=spec["folder"] + spec["name"])
        last_result = FILE_NOT_FOUND if spec.get("case") == "failure" else 0
        task.update(last_run_time=iso(starts[-1]), next_run_time=iso(starts[-1] + gap),
                    last_task_result=last_result)
        durations = [rng.randint(60, 600) for _ in starts]
        tasks.append(task)
        events.extend(build_events(task, starts, durations, rng, last_result, False))

    events.sort(key=lambda e: e["event_time_utc"])
    for number, event in enumerate(events, start=1000):
        event["event_record_id"] = number
        event["computer_name"] = SCALE_HOST
    return {"synthetic": True, "host_name": SCALE_HOST, "captured_at_utc": iso(now),
            "tasks": tasks, "events": events}


def metric_series(case: str, rng: random.Random) -> List[int]:
    """Oldest-first values; "far_below" ends 90 percent under its median of about 1050, and
    "far_above" ends at about five times a small problem count."""
    if case == "far_above":
        return [rng.randint(10, 14) for _ in range(6)] + [60]
    history = [rng.randint(1000, 1100) for _ in range(6)]
    return history + ([90] if case == "far_below" else [rng.randint(1000, 1100)])


# Generic prose a typical script prints, per host-1 task: (line template, intended default.json key).
# A None key marks noise the miner must ignore. Templates take the day's value as {n}.
GENERIC_TASKS = {
    "Nightly Backup": [("Updated {n} records", "updated_record_count"),
                       ("Processed {n} rows", "processed_rows"),
                       ("Duration: {s} seconds", "duration_seconds"),
                       ("Python version 3.11.4", None), ("PID: 4821", None),
                       ("Listening on port: 8080", None), ("Run date: 2026-01-01", None)],
    "Index Rebuilder": [("Inserted {n} features", "inserted_features"),
                        ("Deleted {n} records", "deleted_records"),
                        ("Process completed in {s}s", "duration_seconds"),
                        ("Step 3 of 5", None), ("Progress: 85%", None), ("Retry attempt 2 of 3", None)],
    "Log Rotator": [("Exported {n} rows", "exported_rows"),
                    ("Added {n} records", "added_records"),
                    ("Elapsed time: 3.5 minutes", "duration_seconds"),
                    ("Memory used: 512 MB", None), ("Connected to server v2.4", None)],
}


def generic_metric_keys() -> List[str]:
    return sorted({key for lines in GENERIC_TASKS.values() for _, key in lines if key})


def write_metric_logs(root: Path, now: datetime, seed: int = 1, generic: bool = False) -> Dict[str, str]:
    """Write one log file per day for four tasks of host 1, each carrying a `TASKDASH key=N` line:
    Metric Loader (rows_processed far below usual), Inventory Export (steady), Queue Drainer
    (failed_items far over usual, and error lines in its newest log) and Heartbeat Check (a
    rows_deleted value in every log but the newest). With generic=True, also write prose logs for
    the GENERIC_TASKS tasks. File mtimes are relative to `now`, which must be the real clock the
    miner reads. Returns the metrics.log_directory_overrides map for these tasks."""
    import os

    rng = random.Random(f"{seed}-logs")
    overrides: Dict[str, str] = {}
    cases = {"Metric Loader": "far_below", "Inventory Export": "", "Queue Drainer": "far_above",
             "Heartbeat Check": "key_missing"}
    for name, case in cases.items():
        folder = root / name.replace(" ", "_").lower()
        folder.mkdir(parents=True, exist_ok=True)
        values = metric_series(case, rng)
        for index, value in enumerate(values):
            age_days = len(values) - 1 - index
            newest = index == len(values) - 1
            pairs = f"failed_items={value}" if case == "far_above" else f"rows_processed={value}"
            if case == "key_missing" and not newest:
                pairs += f" rows_deleted={rng.randint(5, 9)}"
            body = f"2026-01-01 02:00:00 INFO start\nTASKDASH {pairs}\n"
            if case == "far_above" and newest:
                body += ("2026-01-01 02:03:00 ERROR Queue server did not answer\n"
                         "2026-01-01 02:04:00 ERROR Retry limit reached\n")
            log = folder / f"run_{index:02d}.log"
            log.write_text(body + "2026-01-01 02:05:00 INFO done\n", encoding="utf-8")
            stamp = (now - timedelta(days=age_days, hours=1)).timestamp()
            os.utime(log, (stamp, stamp))
        overrides[f"\\AppHost01\\{name}"] = str(folder)
    if generic:
        _write_generic_logs(root, now, rng, overrides)
    return overrides


def _write_generic_logs(root: Path, now: datetime, rng: random.Random, overrides: Dict[str, str]) -> None:
    import os

    for name, lines in GENERIC_TASKS.items():
        folder = root / name.replace(" ", "_").lower()
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(7):
            body = ["2026-01-01 02:00:00 INFO start"]
            for template, _ in lines:
                body.append("2026-01-01 02:00:01 INFO " + template.format(n=rng.randint(50, 500),
                                                                           s=rng.randint(20, 90)))
            log = folder / f"run_{index:02d}.log"
            log.write_text("\n".join(body) + "\n", encoding="utf-8")
            stamp = (now - timedelta(days=6 - index, hours=1)).timestamp()
            os.utime(log, (stamp, stamp))
        overrides[f"\\AppHost01\\{name}"] = str(folder)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Write the synthetic golden fixture, one JSON per host.",
                                     allow_abbrev=False)
    parser.add_argument("--out-dir", required=True, help="Folder for the two fixture files.")
    parser.add_argument("--now", default="", help="Frozen capture time, ISO-8601 (default: the current time).")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)

    try:
        now = parse_now(args.now) if args.now else datetime.now(timezone.utc).replace(microsecond=0)
    except ValueError:
        raise SystemExit(f"--now must be ISO-8601, e.g. 2026-10-16T12:00:00Z (got {args.now!r}).")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for host_no, host in enumerate(HOSTS, start=1):
        payload = build_host(host_no, now, args.seed)
        out = out_dir / f"synthetic_{host.lower().replace('-', '_')}.json"
        out.write_text(json.dumps(payload, indent=0, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print(f"{out}: {len(payload['tasks'])} tasks, {len(payload['events'])} events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

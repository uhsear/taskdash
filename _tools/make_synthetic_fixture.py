"""Synthetic golden fixture generator (roadmap Phase 2, deliverable 2).

Driver: manual, or CI with a frozen --now
Source: nothing -- every host, task, run and log line is invented here
Target: one fixture JSON per host in --out-dir, in the shape `taskdash_publish.py --fixture` reads

    python _tools/make_synthetic_fixture.py --out-dir tests/fixtures --now 2026-10-16T12:00:00Z

Output is a pure function of --now and --seed, so a CI run can freeze it. The fixture format
carries tasks and events only. Two inputs cannot ride in it, so the generator supplies them apart:
  * the "before" state a definition change is diffed against comes from the hosted TaskInventory
    table, never from the fixture. The host-01 file carries it as an extra "prior_tasks" key,
    which collect_everything ignores; a test collects it as its own fixture and diffs the two.
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
            {"name": "Heartbeat Check", "script": "heartbeat_check.py", "every": "PT1H"},
            {"name": "Metric Loader", "script": "metric_loader.py", "minute": 15, "hour": 5, "case": "far_below"},
            {"name": "Report Mailer", "script": "report_mailer.py", "minute": 10, "hour": 6, "case": "failure"},
            {"name": "Data Archiver", "script": "data_archiver.py", "minute": 20, "hour": 7, "case": "stuck"},
            {"name": "Weekly Cleanup", "script": "weekly_cleanup.py", "weekly": True, "case": "overdue"},
            {"name": "Legacy Sync", "script": "legacy_sync.py", "minute": 40, "hour": 8, "case": "disabled"},
            {"name": "Cache Refresher", "script": "cache_refresher.py", "minute": 50, "hour": 9, "case": "setting_risk"},
            {"name": "Config Publisher", "script": "config_publisher.py", "minute": 25, "hour": 10, "case": "definition_change"},
            {"name": "Queue Drainer", "script": "queue_drainer.py", "minute": 35, "hour": 11},
        ]
    return [
        {"name": "Archive Copy", "script": "archive_copy.py", "minute": 5, "hour": 1, "case": "shared_cause"},
        {"name": "Report Builder", "script": "report_builder.py", "minute": 15, "hour": 2, "case": "shared_cause"},
        {"name": "Export Packager", "script": "export_packager.py", "minute": 25, "hour": 3, "case": "shared_cause"},
        {"name": "Mirror Sync", "script": "mirror_sync.py", "minute": 35, "hour": 4, "case": "shared_cause"},
        {"name": "Metrics Rollup", "script": "metrics_rollup.py", "minute": 45, "hour": 5},
        {"name": "Session Cleaner", "script": "session_cleaner.py", "minute": 55, "hour": 6},
        {"name": "Cert Checker", "script": "cert_checker.py", "minute": 5, "hour": 7},
        {"name": "Disk Reporter", "script": "disk_reporter.py", "minute": 15, "hour": 8},
        {"name": "Status Poller", "script": "status_poller.py", "every": "PT1H"},
        {"name": "Backup Verifier", "script": "backup_verifier.py", "minute": 25, "hour": 9},
        {"name": "Audit Collector", "script": "audit_collector.py", "minute": 35, "hour": 10},
        {"name": "Queue Monitor", "script": "queue_monitor.py", "minute": 45, "hour": 11},
    ]


def build_events(task: Dict[str, Any], starts: List[datetime], durations: List[float], rng: random.Random,
                 last_result: int, left_running: bool) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    full = task["task_full_name"]
    for index, start in enumerate(starts):
        instance = "{" + str(uuid.UUID(int=rng.getrandbits(128))).upper() + "}"
        newest = index == len(starts) - 1
        end = start + timedelta(seconds=durations[index])
        code = last_result if newest else 0

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
        last_result, left_running = 0, False

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

        task["last_task_result"] = last_result
        tasks.append(task)
        events.extend(build_events(task, starts, durations, rng, last_result, left_running))

    events.sort(key=lambda e: e["event_time_utc"])
    for number, event in enumerate(events, start=1000):
        event["event_record_id"] = number
        event["computer_name"] = HOSTS[host_no - 1]

    payload: Dict[str, Any] = {"synthetic": True, "host_name": HOSTS[host_no - 1],
                               "captured_at_utc": iso(now), "tasks": tasks, "events": events}
    if host_no == 1:
        prior = copy.deepcopy(tasks)
        for row in prior:
            if row["task_name"] == "Config Publisher":
                row["primary_arguments"] = row["primary_arguments"].replace("--mode run", "--mode dry")
                row["actions"] = row["actions"].replace("--mode run", "--mode dry")
        payload["prior_tasks"] = prior
    return payload


def metric_series(case: str, rng: random.Random) -> List[int]:
    """Oldest-first row counts; "far_below" ends 90 percent under its median."""
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
    """Write one log file per day for the Metric Loader and Inventory Export tasks of host 1, each
    carrying a `TASKDASH rows_processed=N` line. With generic=True, also write prose logs for the
    GENERIC_TASKS tasks. File mtimes are relative to `now`, which must be the real clock the miner
    reads. Returns the metrics.log_directory_overrides map for these tasks."""
    import os

    rng = random.Random(f"{seed}-logs")
    overrides: Dict[str, str] = {}
    cases = {"Metric Loader": "far_below", "Inventory Export": ""}
    for name, case in cases.items():
        folder = root / name.replace(" ", "_").lower()
        folder.mkdir(parents=True, exist_ok=True)
        values = metric_series(case, rng)
        for index, value in enumerate(values):
            age_days = len(values) - 1 - index
            log = folder / f"run_{index:02d}.log"
            log.write_text(f"2026-01-01 02:00:00 INFO start\nTASKDASH rows_processed={value}\n"
                           "2026-01-01 02:05:00 INFO done\n", encoding="utf-8")
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

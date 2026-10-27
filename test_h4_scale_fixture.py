"""H4-10: a synthetic host with hundreds of tasks goes through the offline passes inside a time budget.

The 500-task run is opt-in because it takes minutes: set TASKDASH_SCALE_TASKS=500 (the CI scale job
does). TASKDASH_SCALE_BUDGET_SECONDS overrides the 900 s ceiling per pass. Without the variable the
same passes run on SMALL_N tasks, so the default suite proves the fixture and the passes still work
and stays fast. No network: the hosted table is an in-memory fake.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone

import pytest

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_publish as publish  # noqa: E402
import taskdash_synthetic as synthetic  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)   # conftest.FROZEN_NOW, the clock every test runs on
SMALL_N = 40
SCALE_N = int(os.environ.get("TASKDASH_SCALE_TASKS") or 0)
BUDGET_SECONDS = float(os.environ.get("TASKDASH_SCALE_BUDGET_SECONDS") or 900)
HOST = synthetic.SCALE_HOST


# ------------------------------------------------------------------ the generator

def test_the_scale_host_has_the_requested_tasks_with_unique_names_and_the_odd_cases():
    payload = synthetic.build_scale_host(300, NOW, 1)
    names = {t["task_full_name"] for t in payload["tasks"]}
    assert len(payload["tasks"]) == 300 and len(names) == 300
    assert any("[" in t["task_name"] for t in payload["tasks"]), "wildcard characters in a name"
    assert any(not t["task_name"].isascii() for t in payload["tasks"]), "a non-ASCII name"
    assert any(t["task_path"] == "\\MicrosoftSync\\" for t in payload["tasks"]), "a Microsoft-like folder"
    assert {e["task_full_name"] for e in payload["events"]} <= names
    assert {e["computer_name"] for e in payload["events"]} == {HOST}


def test_the_scale_host_is_a_pure_function_of_now_and_seed():
    first = synthetic.build_scale_host(60, NOW, 3)
    assert first == synthetic.build_scale_host(60, NOW, 3)
    assert first != synthetic.build_scale_host(60, NOW, 4)


def test_a_scale_fixture_holds_only_the_synthetic_account_and_paths():
    tasks = synthetic.build_scale_host(120, NOW, 1)["tasks"]
    assert {t["run_as_user"] for t in tasks} == {synthetic.ACCOUNT}
    assert all(t["primary_executable"].startswith("X:\\synthetic\\") for t in tasks)


# ------------------------------------------------------------------ the passes

class _Feature:
    def __init__(self, attributes):
        self.attributes = attributes


class _Result:
    def __init__(self, rows):
        self.features = [_Feature(r) for r in rows]


class _Props:
    objectIdField = "OBJECTID"
    maxRecordCount = 1000


class MemoryTable:
    """A hosted table that keeps rows. It answers the one where-clause the keyed upsert builds:
    `run_key IN ('a','b')`. A real service pages at maxRecordCount, so this one does too."""

    properties = _Props()

    def __init__(self):
        self.rows = {}
        self.next_oid = 1

    def query(self, *, where="1=1", out_fields="*", result_offset=0, result_record_count=None, **kw):
        match = re.fullmatch(r"run_key IN \((.*)\)", where)
        keys = set(re.findall(r"'((?:[^']|'')*)'", match.group(1))) if match else None
        found = [r for r in self.rows.values() if keys is None or r["run_key"].replace("'", "''") in keys]
        found.sort(key=lambda r: r["OBJECTID"])
        return _Result(found[result_offset:result_offset + self.properties.maxRecordCount])

    def edit_features(self, adds=None, updates=None, rollback_on_failure=True):
        for item in adds or []:
            row = {**item["attributes"], "OBJECTID": self.next_oid}
            self.rows[self.next_oid] = row
            self.next_oid += 1
        for item in updates or []:
            self.rows[item["attributes"]["OBJECTID"]].update(item["attributes"])
        return {"addResults": [{"success": True}] * len(adds or []),
                "updateResults": [{"success": True}] * len(updates or [])}


def _timed(timings, name, call):
    started = time.perf_counter()
    result = call()
    timings[name] = time.perf_counter() - started
    return result


def run_passes(tmp_path, task_count):
    """Collect, diff, alert and upsert a task_count-task fixture. Returns the seconds each pass took."""
    fixture = tmp_path / "scale.json"
    fixture.write_text(json.dumps(synthetic.build_scale_host(task_count, NOW, 1)), encoding="utf-8")
    timings = {}

    collected = _timed(timings, "collect", lambda: publish.collect_everything({}, fixture))
    inventory, runs = collected["inventory"], collected["runs"]
    assert len(inventory) == task_count and len({r["task_full_name"] for r in inventory}) == task_count
    assert len(runs) > task_count, "every task contributes at least one run"
    assert any("[" in r["task_name"] for r in inventory) or task_count < 100

    again = publish.collect_everything({}, fixture)
    old_by_key = {r["task_full_name"]: r for r in again["inventory"]}
    changes = _timed(timings, "diff", lambda: publish.detect_task_changes(
        old_by_key, inventory, HOST, NOW.isoformat()))
    assert not [c for c in changes if c["change_kind"] in ("task_added", "task_removed", "definition_changed")]

    signature = _timed(timings, "alert", lambda: publish.alert_signature(inventory))
    failing = sum(1 for r in inventory if r["last_run_failed"])
    assert len(signature) >= failing >= task_count // 25

    table = MemoryTable()
    first = _timed(timings, "upsert", lambda: publish.upsert_by_key(
        table, runs, ["run_key"], chunk_size=common.DEFAULT_CHUNK_SIZE, label="TaskRuns",
        order_field="max_event_record_id"))
    assert first == {"added": len(runs), "updated": 0, "deleted": 0}
    second = publish.upsert_by_key(table, runs, ["run_key"], chunk_size=common.DEFAULT_CHUNK_SIZE,
                                   label="TaskRuns", order_field="max_event_record_id")
    assert second == {"added": 0, "updated": len(runs), "deleted": 0}, "a repeat pass must add nothing"
    assert len(table.rows) == len(runs)
    return timings


def test_the_offline_passes_work_on_a_small_host(tmp_path):
    timings = run_passes(tmp_path, SMALL_N)
    assert set(timings) == {"collect", "diff", "alert", "upsert"}


@pytest.mark.skipif(not SCALE_N, reason="opt-in: set TASKDASH_SCALE_TASKS=500 (the CI scale job does)")
def test_the_offline_passes_finish_inside_the_budget_at_scale(tmp_path):
    timings = run_passes(tmp_path, SCALE_N)
    print(f"scale {SCALE_N} tasks: " + ", ".join(f"{name} {seconds:.1f}s" for name, seconds in timings.items()))
    slow = {name: round(seconds, 1) for name, seconds in timings.items() if seconds >= BUDGET_SECONDS}
    assert not slow, f"pass(es) over the {BUDGET_SECONDS:.0f} s budget: {slow}"
    assert sum(timings.values()) < BUDGET_SECONDS, f"all passes together took {sum(timings.values()):.0f} s"

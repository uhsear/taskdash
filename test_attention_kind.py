"""Regression tests for TaskInventory.attention_kind (the single primary reason a task needs
attention, by precedence FAILED > OVERDUE > STUCK > BLOCKED > WARNING).

Builds task rows directly, the same way test_tables_hardening.py exercises
apply_stuck_or_blocked_warning / enrich_inventory -- no ArcGIS calls, no collect.py plumbing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import taskdash_publish as publish

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(publish, "utc_now", lambda: NOW)


def _run(task="\\T", hours_ago=0.0, status=None, is_error=0):
    row = {"task_full_name": task, "duration_seconds": None, "is_error": is_error,
          "start_time_utc": (NOW - timedelta(hours=hours_ago)).isoformat()}
    if status:
        row["run_status"] = status
    return row


def _base_task(**overrides):
    task = {"task_full_name": "\\T", "task_name": "T", "health_status": "Success",
           "needs_attention": 0, "last_run_failed": 0, "is_overdue": 0, "state": "Ready"}
    task.update(overrides)
    return task


# ==================================================================== single-condition kinds

def test_failed_task_gets_attention_kind_failed(frozen_now):
    task = _base_task(health_status="Error", last_run_failed=1, needs_attention=1, is_overdue=1)
    publish.enrich_inventory([task], {}, {})
    assert task["attention_kind"] == "FAILED", \
        "FAILED must win even though is_overdue is also set"


def test_stuck_task_gets_attention_kind_stuck(frozen_now):
    running = _run(hours_ago=2.0, status="Running")
    stats = publish.summarize_runs([running])
    task = _base_task(health_status="Warning", needs_attention=1, state="Running")
    publish.enrich_inventory([task], stats, {})
    assert task["attention_kind"] == "STUCK"


def test_blocked_task_gets_attention_kind_blocked(frozen_now):
    skipped = [_run(hours_ago=1.0, status="Skipped-AlreadyRunning"),
              _run(hours_ago=2.0, status="Skipped-AlreadyRunning")]
    stats = publish.summarize_runs(skipped)
    task = _base_task(health_status="Warning", needs_attention=1)
    publish.enrich_inventory([task], stats, {})
    assert task["attention_kind"] == "BLOCKED"


def test_overdue_beats_blocked_when_both_fire(frozen_now):
    """Skips from the last 24h can outlive the running instance, so a Ready task can be both
    blocked and overdue. The layout counts it on the Overdue tile, not Other Warnings."""
    skipped = [_run(hours_ago=1.0, status="Skipped-AlreadyRunning"),
              _run(hours_ago=2.0, status="Skipped-AlreadyRunning")]
    stats = publish.summarize_runs(skipped)
    task = _base_task(health_status="Warning", needs_attention=1, is_overdue=1)
    publish.enrich_inventory([task], stats, {})
    assert task["attention_kind"] == "OVERDUE"


def test_overdue_without_needs_attention_gets_no_kind(frozen_now):
    """collect.py returns health Running for result 0x41301 before its overdue check, but sets the
    row's is_overdue separately -- so needs_attention=0 with is_overdue=1 is real data."""
    task = _base_task(health_status="Running", needs_attention=0, is_overdue=1)
    publish.enrich_inventory([task], {}, {})
    assert task["attention_kind"] == ""


def test_overdue_task_gets_attention_kind_overdue(frozen_now):
    task = _base_task(health_status="Warning", needs_attention=1, is_overdue=1)
    publish.enrich_inventory([task], {}, {})
    assert task["attention_kind"] == "OVERDUE"


def test_other_warning_gets_attention_kind_warning(frozen_now):
    """needs_attention=1 with none of FAILED/STUCK/BLOCKED/OVERDUE true -- e.g. a warning result
    code or a missing expected next-run, set upstream in taskdash_collect.py."""
    task = _base_task(health_status="Warning", needs_attention=1)
    publish.enrich_inventory([task], {}, {})
    assert task["attention_kind"] == "WARNING"


def test_healthy_task_gets_empty_attention_kind(frozen_now):
    task = _base_task()
    publish.enrich_inventory([task], {}, {})
    assert task["attention_kind"] == ""


def test_stuck_beats_blocked_when_both_fire(frozen_now):
    combined = [_run(hours_ago=2.0, status="Running"),
               _run(hours_ago=1.0, status="Skipped-AlreadyRunning"),
               _run(hours_ago=1.5, status="Skipped-AlreadyRunning")]
    stats = publish.summarize_runs(combined)
    task = _base_task(health_status="Warning", needs_attention=1, state="Running")
    publish.enrich_inventory([task], stats, {})
    assert task["attention_kind"] == "STUCK"


def test_config_warnings_never_set_attention_kind(frozen_now):
    """Config-level warnings (3.3) are definition-risk, reported through config_warnings, and
    must never make an otherwise-healthy task need attention."""
    task = _base_task(config_warnings="run_as_user looks like a service account")
    publish.enrich_inventory([task], {}, {})
    assert task["attention_kind"] == ""
    assert task["needs_attention"] == 0


def test_attention_kind_is_idempotent_across_repeated_enrichment(frozen_now):
    """enrich_inventory runs twice per publish; attention_kind must land on the same value."""
    running = _run(hours_ago=2.0, status="Running")
    stats = publish.summarize_runs([running])
    task = _base_task(health_status="Warning", needs_attention=1, state="Running")
    publish.enrich_inventory([task], stats, {})
    once = task["attention_kind"]
    publish.enrich_inventory([task], stats, {})
    publish.enrich_inventory([task], stats, {})
    assert task["attention_kind"] == once == "STUCK"


# ==================================================================== agreement with needs_attention

def test_attention_kind_agrees_with_needs_attention_for_every_kind(frozen_now):
    for task in (
        _base_task(health_status="Error", last_run_failed=1, needs_attention=1),
        _base_task(health_status="Warning", needs_attention=1, is_overdue=1),
        _base_task(health_status="Warning", needs_attention=1),
        _base_task(),
        _base_task(health_status="Running", is_overdue=1),
    ):
        publish.enrich_inventory([task], {}, {})
        assert (task["attention_kind"] != "") == bool(task["needs_attention"])


def test_overdue_and_other_warning_tiles_do_not_double_count(frozen_now):
    """The dashboard's Overdue tile and its 'other warnings' tile (STUCK|BLOCKED|WARNING) must
    partition needs_attention with FAILED, not overlap or drop rows: count(FAILED) +
    count(OVERDUE) + count(STUCK|BLOCKED|WARNING) == count(needs_attention == 1), for any mix of
    kinds including a healthy task, a stuck+blocked running task, and an overdue row that needs
    no attention (0x41301 "Running" with a stale state).
    """
    combined_stuck_blocked = [_run(task="\\C", hours_ago=2.0, status="Running"),
                              _run(task="\\C", hours_ago=1.0, status="Skipped-AlreadyRunning"),
                              _run(task="\\C", hours_ago=1.5, status="Skipped-AlreadyRunning")]
    stats = publish.summarize_runs(combined_stuck_blocked)

    inventory = [
        _base_task(task_full_name="\\F", health_status="Error", last_run_failed=1, needs_attention=1),
        _base_task(task_full_name="\\O", health_status="Warning", needs_attention=1, is_overdue=1),
        _base_task(task_full_name="\\W", health_status="Warning", needs_attention=1),
        _base_task(task_full_name="\\H"),
        _base_task(task_full_name="\\C", health_status="Warning", needs_attention=1,
                  state="Running"),
        _base_task(task_full_name="\\R", health_status="Running", is_overdue=1),
    ]
    publish.enrich_inventory(inventory, stats, {})

    kinds = {t["task_full_name"]: t["attention_kind"] for t in inventory}
    assert kinds == {"\\F": "FAILED", "\\O": "OVERDUE", "\\W": "WARNING", "\\H": "",
                     "\\C": "STUCK", "\\R": ""}

    count_failed = sum(1 for t in inventory if t["attention_kind"] == "FAILED")
    count_overdue = sum(1 for t in inventory if t["attention_kind"] == "OVERDUE")
    count_other_warning = sum(1 for t in inventory if t["attention_kind"] in
                              ("STUCK", "BLOCKED", "WARNING"))
    count_needs_attention = sum(1 for t in inventory if t["needs_attention"])

    assert count_failed + count_overdue + count_other_warning == count_needs_attention == 4

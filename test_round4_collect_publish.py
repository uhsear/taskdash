"""Round-4 audit fixes in taskdash_collect.py / taskdash_publish.py: the single-failure floor on
the lowest 7-day success rate, the deleted (false) trigger-collision note, the "Was: " prefix on
a recovered row's old reason, the split settings change-summary, and per-note config-warning
change detection.

No network, no ArcGIS Online, no SMTP: fakes throughout.
"""

from __future__ import annotations

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402
import taskdash_publish as publish  # noqa: E402


# ==================================================================== 1. lowest 7-day success rate

def _row(runs, fails):
    return {"runs_last_7_days": runs, "failures_last_7_days": fails}


def test_lowest_rate_needs_two_failures_not_one():
    """indicators-lowest-rate-flags-single-failure: one accidental failure of a task with 9 or
    fewer runs in 7 days used to drop the tile to Low/Poor for a week; it now needs >=2
    failures. Values from the lead's own examples."""
    inventory = [_row(7, 1)]
    assert publish.stamp_success_rate(inventory) is not None
    assert inventory[0]["lowest_success_rate_7d"] == 100.0

    inventory = [_row(7, 2)]
    publish.stamp_success_rate(inventory)
    assert inventory[0]["lowest_success_rate_7d"] == 71.43

    inventory = [_row(1, 1)]  # Replica Keep-Alive Job
    publish.stamp_success_rate(inventory)
    assert inventory[0]["lowest_success_rate_7d"] == 100.0


def test_lowest_rate_is_still_the_worst_qualifying_task():
    inventory = [_row(343, 3), _row(7, 7)]
    publish.stamp_success_rate(inventory)
    assert inventory[0]["lowest_success_rate_7d"] == 0.0


def test_lowest_rate_is_none_when_nothing_ran():
    inventory = [_row(0, 0)]
    publish.stamp_success_rate(inventory)
    assert inventory[0]["lowest_success_rate_7d"] is None


# ==================================================================== 2. false collision note removed

def test_check_repetition_period_collision_is_deleted():
    """health_table-setting-note-collision-false / collector_tab-collision-note-false-skip:
    reproduced against truth4/gt_events.json -- every Task Dash event id 322 shares its
    instance_id with the event id 100 in the same second, so Task Scheduler refused only a
    duplicate launch request and no hourly run was lost. The note was false, not just worded
    badly, so the check is removed rather than reworded."""
    assert not hasattr(collect, "check_repetition_period_collision")
    task_dash_trigger = {"type": "Weekly", "days_interval": "", "days_of_week": "127",
                         "weeks_interval": "1", "repetition_interval": "PT1H",
                         "repetition_duration": "P1D", "enabled": True,
                         "start": "2024-01-05T07:00:00", "end": "", "random_delay": "", "delay": ""}
    raw = {"multiple_instances_policy": "IgnoreNew", "trigger_details": [task_dash_trigger]}
    warnings = collect.build_config_warnings(raw, "Daily", exists=lambda p: True)
    assert "collide" not in warnings.lower()


# ==================================================================== 3. settings change summary split

def test_a_settings_change_shows_only_the_changed_setting():
    """collector_tab-change-summary-separator-and-jargon: "Settings: A | B | C" used to repeat
    every setting for a one-setting edit, nesting a second " | " inside one part."""
    old = {"settings_detail": "If already running: unknown | Time limit: none | Catch-up: off"}
    new = {"settings_detail": "If already running: unknown | Time limit: 3 h | Catch-up: off"}
    old_summary, new_summary = publish._definition_change_summaries(old, new)
    assert old_summary == "Time limit: none"
    assert new_summary == "Time limit: 3 h"


# ==================================================================== 4. per-note config-warning changes

def test_a_second_config_note_on_an_already_noted_task_is_detected():
    """collector_tab-r3regress-03-note-split-not-landed: comparing the whole "; "-joined string
    only caught an empty<->non-empty transition; a second note landing on a task that already
    had one stayed silent."""
    base = {"task_full_name": "\\T", "task_name": "T",
           "definition_hash": publish.DEFINITION_HASH_PREFIX + "x" * 62,
           "last_run_failed": 0, "is_overdue": 0,
           "config_warnings": "Time limit (2 h) is longer than the repetition interval (1 h)"}
    new = dict(base, config_warnings=base["config_warnings"] +
              "; script not found: C:\\Jobs\\missing.py")
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-28T00:00:00+00:00")
    added = [c for c in changes if c["change_kind"] == "config_warning_added"]
    assert len(added) == 1
    assert added[0]["new_summary"] == "script not found: C:\\Jobs\\missing.py"


def test_a_cleared_note_among_several_is_detected_alone():
    base = {"task_full_name": "\\T", "task_name": "T",
           "definition_hash": publish.DEFINITION_HASH_PREFIX + "x" * 62,
           "last_run_failed": 0, "is_overdue": 0,
           "config_warnings": "Note A; Note B"}
    new = dict(base, config_warnings="Note A")
    changes = publish.detect_task_changes({"\\T": base}, [new], "H", "2026-09-28T00:00:00+00:00")
    assert [c["change_kind"] for c in changes] == ["config_warning_cleared"]
    assert changes[0]["old_summary"] == "Note B"

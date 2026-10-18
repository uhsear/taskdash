"""Phase 0 wording: no ".;" in attention_reason, and a fast run never reads "0.0 times"."""
from __future__ import annotations

from pathlib import Path

import pytest

import conftest
import taskdash_dashboard as dashboard
import taskdash_publish as publish

SCRIPT_FAILED = "Script exited with code 1."


def _enrich(task_fields, last, median, p5=None):
    task = {"task_full_name": "\\T", "task_name": "T", "health_status": "Error",
            "needs_attention": 1, "last_run_failed": 1, "is_overdue": 0, "state": "Ready",
            "last_result_text": SCRIPT_FAILED, "attention_reason": SCRIPT_FAILED,
            "last_run_time": None}
    task.update(task_fields)
    stats = {"last_duration_seconds": last, "baseline_median_seconds": median,
             "baseline_p95_seconds": median * 2, "baseline_p5_seconds": p5 or median,
             "baseline_run_count": 5}
    publish.enrich_inventory([task], {"\\T": stats}, {})
    return task


def test_tiny_ratio_reads_under_one_percent_and_no_dot_semicolon():
    task = _enrich({}, 9.0, 2280.0)  # 9 s against a 38 min median
    reason = task["attention_reason"]
    assert "under 1% of the usual 38 min" in reason
    assert "0.0 times" not in reason and ".;" not in reason
    assert "Script exited with code 1; Last run took 9 s" in reason


def test_mid_ratio_reads_as_a_floored_percent():
    assert _enrich({}, 7.0, 28.0 * 1)["attention_reason"].count("about 25% of the usual 28 s") == 1


@pytest.mark.parametrize("ratio,text", [(5.0, "about 5.0 times the usual"),
                                        (1.0, "about 1.0 times the usual"),
                                        (0.25, "about 25% of the usual"),
                                        (0.29, "about 29% of the usual"),  # 0.29 * 100 == 28.999...
                                        (0.004, "under 1% of the usual")])
def test_versus_usual_wording(ratio, text):
    assert publish._versus_usual(ratio) == text


def test_a_lone_part_keeps_its_final_period():
    task = _enrich({}, 30.0, 30.0)  # not an anomaly
    assert task["attention_reason"].endswith(".")


def test_reason_change_does_not_touch_definition_hash_or_log_false_changes():
    raw = {"actions": "run.py", "trigger_details": [], "run_as_user": "u",
           "logon_type": "Password", "run_level": "Limited", "enabled": True}
    before = publish.compute_definition_hash(raw)
    assert publish.compute_definition_hash(dict(raw, attention_reason="x.; y")) == before
    old = {"task_full_name": "\\T", "definition_hash": before, "last_run_failed": 1,
           "is_overdue": 0, "health_status": "Error",
           "attention_reason": "Script exited with code 1.; Last run took 9 s, about 0.0 times the usual 38 min"}
    new = dict(old, attention_reason="Script exited with code 1; Last run took 9 s, "
                                     "under 1% of the usual 38 min")
    assert publish.detect_task_changes({"\\T": old}, [new], "H", "2026-10-16T00:00:00+00:00") == []


def test_task_detail_card_uses_percent_for_a_fast_run():
    source = Path(dashboard.__file__).read_text(encoding="utf-8")
    assert 'Floor(Round(ratio * 100, 6)) < 1, "Duration: under 1% of the usual"' in source
    assert '"% of the usual"' in source and 'times the usual' in source

"""Phase 2: the documented "TASKDASH key=value" metric line."""

from __future__ import annotations

from pathlib import Path

import taskdash_metrics as tm

HERE = Path(__file__).parent
MARKER = "TASKDASH" + " "


def _scan(tmp_path: Path, text: str) -> dict:
    log = tmp_path / "run.log"
    log.write_text(text, encoding="utf-8")
    return tm.scan_log_file(log, 100)


def test_single_and_multiple_pairs(tmp_path):
    result = _scan(tmp_path, "start\nTASKDASH rows_updated=1234\nTASKDASH Skipped=3 ratio=0.5 delta=-2 big=1,234,567\n")
    metrics = {key: value for key, (value, _line) in result["metrics"].items()}
    assert metrics == {"rows_updated": 1234.0, "skipped": 3.0, "ratio": 0.5, "delta": -2.0, "big": 1234567.0}


def test_preamble_and_comma_separated_pairs(tmp_path):
    result = _scan(tmp_path, "2026-10-16 03:00:01,123 - INFO - TASKDASH a_b=12, c=3\n")
    assert {k: v[0] for k, v in result["metrics"].items()} == {"a_b": 12.0, "c": 3.0}


def test_non_pairs_and_mid_line_marker_ignored(tmp_path):
    result = _scan(tmp_path, "TASKDASH note=abc x=1.2.3 =5 ok=7\nsaw TASKDASH here_count=9\n")
    assert {k: v[0] for k, v in result["metrics"].items() if k in {"note", "x", "ok", "here_count"}} == {"ok": 7.0}


def test_explicit_line_beats_rule_mined_value(tmp_path):
    result = _scan(tmp_path, "Records updated: 5\nupdated_record_count: 6\nTASKDASH updated_record_count=99\nupdated_record_count: 7\n")
    assert result["metrics"]["updated_record_count"][0] == 99.0


def test_taskdash_line_is_not_mined_twice(tmp_path):
    result = _scan(tmp_path, "TASKDASH rows_updated=4\n")
    assert list(result["metrics"]) == ["rows_updated"]


def test_logs_without_marker_unchanged(tmp_path):
    lines = [
        "2026-10-16 03:00:01 - INFO - Duration: 0:00:07",
        "2026-10-16 03:00:02 - INFO - Rows loaded: 1,200",
        "SUMMARY updated_record_count: 4",
        "Process completed in 71.6s",
    ]
    result = _scan(tmp_path, "\n".join(lines) + "\n")
    expected = {}
    for line in lines:
        for raw, value in tm.extract_metrics_from_line(line):
            key = tm.canonicalize_metric_key(raw, line)
            if key:
                expected[key] = value
    assert {k: v[0] for k, v in result["metrics"].items()} == expected
    assert tm.extract_taskdash_line(lines[0]) == []


def test_no_existing_fixture_uses_the_marker():
    own = {"test_phase2_metric_line.py", "taskdash_metrics.py"}
    offenders = [p.name for p in HERE.glob("*.py") if p.name not in own and MARKER in p.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == []


def test_taskdash_line_with_a_zero_failed_pair_is_not_an_error_line(tmp_path):
    result = _scan(tmp_path, "TASKDASH failed=0 rows=10\n")
    assert result["error_lines"] == 0 and result["warning_lines"] == 0
    assert {k: v[0] for k, v in result["metrics"].items()} == {"failed": 0.0, "rows": 10.0}

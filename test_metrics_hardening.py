"""Regression tests for the taskdash_metrics.py hardening pass (plan items 1.8a-1.8h, 1.9).

Each test targets one plan item and fails against the pre-hardening implementation. Reuses
fixtures/helpers from test_taskdash.py where convenient; otherwise builds tmp_path fixtures
directly, matching that file's style.
"""

from __future__ import annotations

import pytest

import taskdash_metrics as metrics


# ---------------------------------------------------------------- 1.8a tail, not head (metrics-02)

def test_scan_log_file_reads_tail_not_head(tmp_path):
    """A head read of a 20-line file capped at 5 lines would see the run's FIRST value (1),
    not its last (20) -- exactly backwards for "what happened most recently in this log"."""
    log = tmp_path / "big.log"
    log.write_text("".join(f"Records updated: {i}\n" for i in range(1, 21)), encoding="utf-8")
    result = metrics.scan_log_file(log, max_lines=5)
    assert result["metrics"]["updated_record_count"][0] == 20.0
    assert result["lines_scanned"] == 5


def test_read_last_lines_unlimited_reads_whole_file(tmp_path):
    log = tmp_path / "small.log"
    log.write_text("a\nb\nc\n", encoding="utf-8")
    assert metrics.read_last_lines(log, max_lines=0) == ["a", "b", "c"]


# ---------------------------------------------------------------- 1.8b encodings (metrics-04)

def test_scan_log_file_reads_utf16_le_with_bom(tmp_path):
    log = tmp_path / "ps.log"
    log.write_bytes(b"\xff\xfe" + "Records updated: 42\n".encode("utf-16-le"))
    result = metrics.scan_log_file(log, max_lines=0)
    assert result["metrics"]["updated_record_count"][0] == 42.0


def test_scan_log_file_reads_utf16_le_without_bom(tmp_path):
    """PowerShell's `>>` redirection sometimes drops the BOM but keeps 2-byte code units."""
    log = tmp_path / "ps_nobom.log"
    log.write_bytes("Records updated: 42\n".encode("utf-16-le"))
    result = metrics.scan_log_file(log, max_lines=0)
    assert result["metrics"]["updated_record_count"][0] == 42.0


def test_scan_log_file_reads_utf8_bom(tmp_path):
    log = tmp_path / "utf8bom.log"
    log.write_bytes(b"\xef\xbb\xbf" + "Records updated: 42\n".encode("utf-8"))
    result = metrics.scan_log_file(log, max_lines=0)
    assert result["metrics"]["updated_record_count"][0] == 42.0


def test_scan_log_file_tail_read_respects_utf16_encoding(tmp_path):
    """The seek-from-end tail path (max_lines > 0) must decode utf-16 too, not just the
    unlimited whole-file path."""
    log = tmp_path / "ps_tail.log"
    text = "".join(f"Records updated: {i}\n" for i in range(1, 11))
    log.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    result = metrics.scan_log_file(log, max_lines=3)
    assert result["metrics"]["updated_record_count"][0] == 10.0


# ------------------------------------------------ 1.8b2 chunk-boundary alignment (metrics-13)
# Every fixture above is far below the default 64 KB chunk_size, so the backward-seeking loop
# in read_last_lines() only ever runs once. A small chunk_size here forces the multi-chunk path.

def test_read_last_lines_utf8_multibyte_survives_chunk_boundary(tmp_path):
    """UTF-8 self-synchronises (a byte is unambiguously a continuation byte or not), so a chunk
    boundary landing inside a multibyte character must not corrupt the surrounding lines."""
    lines = [f"Line {i}: café résumé 日本語 {i}" for i in range(1, 21)]
    log = tmp_path / "multibyte.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = metrics.read_last_lines(log, max_lines=5, chunk_size=7)
    assert result == lines[-5:]


def test_read_last_lines_utf16_le_survives_chunk_boundary(tmp_path):
    """metrics-13: utf-16 code units are 2 bytes; an odd chunk_size used to leave pos misaligned
    with the file's real 2-byte groupings after the first backward step, corrupting every
    character decoded from that point on (not just one, unlike UTF-8's self-sync)."""
    lines = [f"Records updated: {i}" for i in range(1, 21)]
    log = tmp_path / "wide.log"
    log.write_bytes(b"\xff\xfe" + ("\n".join(lines) + "\n").encode("utf-16-le"))
    result = metrics.read_last_lines(log, max_lines=5, chunk_size=7)
    assert result == lines[-5:]


def test_read_last_lines_utf16_handles_odd_length_truncated_file(tmp_path):
    """A UTF-16 log truncated mid-write (killed mid-flush) can end on an odd byte count -- one
    dangling half of the final 2-byte code unit. pos must still stay 2-byte aligned relative to
    the REST of the file, or every earlier, complete line decodes as junk too."""
    lines = [f"Records updated: {i}" for i in range(1, 21)]
    encoded = ("\n".join(lines) + "\n").encode("utf-16-le")[:-1]  # drop 1 byte: odd length
    log = tmp_path / "truncated.log"
    log.write_bytes(b"\xff\xfe" + encoded)
    result = metrics.read_last_lines(log, max_lines=6, chunk_size=7)
    assert result == lines[-6:]


# ---------------------------------------------------------------- 1.8c metric keys (metrics-05)

def test_verb_object_excludes_trailing_duration_digits():
    """Root cause of metrics-05: 'in N seconds' used to glue N onto the metric key, so every
    run with a different duration minted a brand-new raw key (records_in_5, records_in_12...).
    The record COUNT (the value right after the verb) must still come through correctly."""
    a = dict(metrics.extract_metrics_from_line("Copied 200 records in 5.4 seconds"))
    b = dict(metrics.extract_metrics_from_line("Copied 300 records in 12.7 seconds"))
    assert set(a) == set(b), "the raw key must be stable across runs with different durations"
    assert not any(ch.isdigit() for key in a for ch in key)
    assert a["copied_records_in"] == 200.0
    assert b["copied_records_in"] == 300.0


@pytest.mark.parametrize("line,expected_key,expected_value", [
    ("Copied 200 records in 5.4 seconds", "copied_record_count", 200.0),
    ("Updated 69 in 5.2s", "updated_record_count", 69.0),
    ("Added 43 new segments in 12 s", "new_segment_count", 43.0),
])
def test_verb_in_duration_keys_canonicalize_to_stable_series(line, expected_key, expected_value):
    """The VERB_VALUE_RE fix (excluding digits from the object phrase) must not make the
    CANONICAL_METRIC_RULES entries for these three verbs unreachable: the raw key it now
    produces (e.g. copied_records_in, with no trailing digits) must still collapse onto the
    same canonical series a differently-worded/differently-timed run always used to reach."""
    found = {}
    for raw_key, value in metrics.extract_metrics_from_line(line):
        canonical = metrics.canonicalize_metric_key(raw_key, line)
        if canonical:
            found[canonical] = value
    assert found.get(expected_key) == pytest.approx(expected_value)


# ---------------------------------------------------------------- 1.8d units (metrics-06)

def test_label_value_normalizes_milliseconds_to_seconds():
    found = dict(metrics.extract_metrics_from_line("Processing time: 45000 ms"))
    assert found["processing_time"] == pytest.approx(45.0)


def test_label_value_normalizes_minutes_to_seconds():
    found = dict(metrics.extract_metrics_from_line("Elapsed time: 3 minutes"))
    assert found["elapsed_time"] == pytest.approx(180.0)


def test_label_value_normalizes_hours_to_seconds():
    found = dict(metrics.extract_metrics_from_line("Total time: 2 hours"))
    assert found["total_time"] == pytest.approx(7200.0)


def test_label_value_leaves_seconds_unconverted():
    found = dict(metrics.extract_metrics_from_line("Duration: 90 seconds"))
    assert found["duration"] == pytest.approx(90.0)


def test_meaningful_label_with_date_value_is_skipped():
    """'Records updated: 2026-09-25' used to mine a bogus updated_record_count of 2026 (the
    year) -- the label looks meaningful, so it survived the noise filter unlike 'start_time'."""
    found = dict(metrics.extract_metrics_from_line("Records updated: 2026-09-25 in this run"))
    assert "records_updated" not in found


def test_meaningful_label_with_clock_value_is_skipped():
    found = dict(metrics.extract_metrics_from_line("Records updated: 14:32:07 today"))
    assert "records_updated" not in found


def test_meaningful_label_with_us_format_date_value_is_skipped():
    """metrics-12: this county's logs commonly use US-format dates (MM/DD/YYYY), not just ISO
    -- 'Last updated: 09/25/2026' used to mine a bogus last_updated of 9.0 (the month) because
    the old date-tail check only recognised the ISO '-MM-DD' shape."""
    found = dict(metrics.extract_metrics_from_line("Last updated: 09/25/2026 by the nightly job"))
    assert "last_updated" not in found


# ---------------------------------------------------------------- 1.8e error-line counting (metrics-07)

@pytest.mark.parametrize("line", [
    "2026-08-11 17:45:13 | ERROR | something broke",
    "2026-08-11 17:45:13 ERROR - something broke",
])
def test_error_detection_covers_pipe_and_space_dash_formats(tmp_path, line):
    log = tmp_path / "run.log"
    log.write_text(line + "\n", encoding="utf-8")
    assert metrics.scan_log_file(log, max_lines=0)["error_lines"] == 1


@pytest.mark.parametrize("line", [
    "2026-08-11 17:45:13 - INFO - 0 failed, 12 succeeded",
    "2026-08-11 17:45:13 - INFO - errors: 0",
    "2026-08-11 17:45:13 - INFO - failed: 0",
])
def test_error_detection_does_not_count_zero_failures(tmp_path, line):
    log = tmp_path / "run.log"
    log.write_text(line + "\n", encoding="utf-8")
    assert metrics.scan_log_file(log, max_lines=0)["error_lines"] == 0


def test_error_detection_still_counts_nonzero_failures(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("2026-08-11 17:45:13 - INFO - 3 failed, 9 succeeded\n", encoding="utf-8")
    assert metrics.scan_log_file(log, max_lines=0)["error_lines"] == 1


@pytest.mark.parametrize("line", [
    # a real failure count sits alongside an unrelated zero-count phrase on the same line --
    # ZERO_COUNT_RE.search() on the whole line used to suppress the real failure it never named.
    "2026-08-11 17:45:13 - INFO - 12 succeeded, 3 failed, 0 errors",
    "2026-08-11 17:45:13 - INFO - Errors: 0, Failed: 2",
    # an explicit ERROR level line must never be suppressed by a zero-count phrase in its text.
    "2026-08-11 17:45:13 | ERROR | Append failed: 0 features written",
])
def test_error_detection_does_not_let_a_zero_count_phrase_hide_a_real_failure(tmp_path, line):
    log = tmp_path / "run.log"
    log.write_text(line + "\n", encoding="utf-8")
    assert metrics.scan_log_file(log, max_lines=0)["error_lines"] == 1


# ---------------------------------------------------------------- 1.8f own-log filter (metrics-01)

def test_own_log_filter_does_not_exclude_a_sibling_project_under_a_shared_root(tmp_path, monkeypatch):
    """metrics-01: TaskDash "installed at" (own_dir) the shared repo root must not exclude a
    monitored task's logs just because they live somewhere under that same root."""
    fake_root = tmp_path  # simulate TaskDash being installed directly at a shared root
    other_project = fake_root / "SomeOtherProject"
    (other_project / "Logs").mkdir(parents=True)
    (other_project / "Logs" / "run.log").write_text("Records updated: 5\n", encoding="utf-8")
    (other_project / "run.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    rows, summary = metrics.build_metric_rows(
        [{"task_name": "Other", "task_full_name": "\\Other",
          "command_target_path": str(other_project / "run.py")}],
        host_name="H")

    assert rows and rows[0]["metric_value"] == 5.0
    assert summary["\\Other"]["log_files_scanned"] == 1


def test_own_log_filter_still_excludes_taskdashs_own_logs_dir(tmp_path, monkeypatch):
    """Narrowing the exclusion to "exactly TaskDash's own log dir" must still exclude it."""
    fake_root = tmp_path
    (fake_root / "Logs").mkdir()
    (fake_root / "Logs" / "taskdash_run.log").write_text(
        "TaskRuns: added 49, updated 72\n", encoding="utf-8")
    (fake_root / "taskdash_publish.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    rows, summary = metrics.build_metric_rows(
        [{"task_name": "Task Dash", "task_full_name": "\\Task Dash",
          "command_target_path": str(fake_root / "taskdash_publish.py")}],
        host_name="H")

    assert rows == [] and summary == {}


def test_own_log_dir_override_excludes_a_relocated_taskdash_log_dir(tmp_path, monkeypatch):
    """metrics-09: taskdash_publish.py's logging.log_dir config can relocate TaskDash's own
    logs away from own_dir/Logs (e.g. into its working directory's Logs instead). A caller
    that resolves and passes that path via own_log_dir must have it excluded too -- the
    hardcoded own_dir/Logs default alone misses it and TaskDash's own summary lines
    ("TaskRuns: added 49, updated 72") come back as junk metric rows."""
    fake_root = tmp_path / "TaskDash"
    fake_root.mkdir()
    (fake_root / "taskdash_publish.py").write_text("", encoding="utf-8")
    runtime_dir = tmp_path / "Runtime"  # sibling of fake_root, so it's not rejected as an
    relocated_logs = runtime_dir / "Logs"  # ancestor of the script dir
    relocated_logs.mkdir(parents=True)
    (relocated_logs / "taskdash_run.log").write_text(
        "TaskRuns: added 49, updated 72\n", encoding="utf-8")

    monkeypatch.setattr(metrics, "__file__", str(fake_root / "taskdash_metrics.py"))
    inventory_rows = [{"task_name": "Task Dash", "task_full_name": "\\Task Dash",
                       "command_target_path": str(fake_root / "taskdash_publish.py"),
                       "command_target_working_directory": str(runtime_dir)}]

    # Without the own_log_dir override, the relocated dir isn't own_dir/Logs, so it is mined.
    rows_without_override, _ = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert rows_without_override, "test setup must actually discover the relocated Logs dir"

    # A stray bare "Runtime" candidate (the working directory itself, discovered alongside
    # Runtime/Logs but holding no files of its own) can still leave a "0 files" summary entry
    # -- that is the pre-existing, correct "resolved but empty" signal, not the bug under test.
    # What must not happen is TaskDash's own summary line coming back as a metric row.
    rows, _ = metrics.build_metric_rows(
        inventory_rows, host_name="H", own_log_dir=str(relocated_logs))
    assert rows == [], "own_log_dir must exclude the relocated log dir from mining"


# ---------------------------------------------------------------- 1.8g per-file errors (metrics-10)

def test_scan_log_file_marks_read_error_without_crashing(tmp_path, monkeypatch):
    """An OSError anywhere in the read must be isolated to this file, not raised -- and must
    not be reported as a clean 0."""
    log = tmp_path / "locked.log"
    log.write_text("Records updated: 1\n", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("simulated read failure")

    monkeypatch.setattr(metrics, "read_last_lines", boom)
    result = metrics.scan_log_file(log, max_lines=0)
    assert result["read_error"] is True
    assert result["metrics"] == {}


def test_build_metric_rows_reports_unknown_not_zero_when_newest_log_unreadable(tmp_path, monkeypatch):
    script_dir = tmp_path / "Proj"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    (logs / "run.log").write_text("Records updated: 1\n", encoding="utf-8")

    original_scan = metrics.scan_log_file

    def failing_scan(path, max_lines, task_settings=None):
        return {"metrics": {}, "lines_scanned": 0, "error_lines": 0,
                "warning_lines": 0, "success_lines": 0, "read_error": True}

    monkeypatch.setattr(metrics, "scan_log_file", failing_scan)
    inventory_rows = [{"task_name": "Proj", "task_full_name": "\\Proj",
                       "command_target_path": str(script_dir / "run.py")}]
    _, summary = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert summary["\\Proj"]["latest_log_error_lines"] is None
    assert summary["\\Proj"]["latest_log_lines_scanned"] is None
    monkeypatch.setattr(metrics, "scan_log_file", original_scan)


def test_one_bad_file_does_not_stop_the_others_from_being_mined(tmp_path, monkeypatch):
    """metrics-10: a per-file read failure (simulated via read_last_lines, the point where a
    real OSError would surface) must not abort the rest of the task's files."""
    script_dir = tmp_path / "Proj"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    good = logs / "good.log"
    bad = logs / "bad.log"
    good.write_text("Records updated: 9\n", encoding="utf-8")
    bad.write_text("Records updated: 1\n", encoding="utf-8")

    original_read = metrics.read_last_lines

    def selective_fail(path, max_lines):
        if path.name == "bad.log":
            raise OSError("simulated")
        return original_read(path, max_lines)

    monkeypatch.setattr(metrics, "read_last_lines", selective_fail)
    inventory_rows = [{"task_name": "Proj", "task_full_name": "\\Proj",
                       "command_target_path": str(script_dir / "run.py")}]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert any(r["log_file_name"] == "good.log" and r["metric_value"] == 9.0 for r in rows)
    assert not any(r["log_file_name"] == "bad.log" for r in rows)
    assert summary["\\Proj"]["log_files_scanned"] == 2


def test_build_metric_rows_read_error_true_when_newest_file_vanishes_mid_loop(tmp_path, monkeypatch):
    """metrics-10: newest.stat() before the loop can succeed and the SAME file's stat() inside
    the per-file loop can then fail (rotated away in between). newest_scan is left None, and
    the fallback log_summary must report UNKNOWN (read_error True), never a clean 0 -- a clean
    0 for a file that was never actually read hides a real problem behind a healthy-looking row."""
    script_dir = tmp_path / "Proj"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    (logs / "run.log").write_text("Records updated: 1\n", encoding="utf-8")

    import pathlib
    original_stat = pathlib.Path.stat
    calls = {"n": 0}

    def flaky_stat(self, *a, **k):
        # Only count calls against the log file itself (not the directories discovery walks).
        # Path.stat() on this one file is called 4 times: is_file() and the mtime cutoff check
        # inside list_recent_log_files, then the pre-loop newest.stat() (which must still
        # succeed), then the per-file loop's stat() -- the 4th call is where this must fail.
        if self.name == "run.log":
            calls["n"] += 1
            if calls["n"] == 4:
                raise OSError("simulated: file rotated away")
        return original_stat(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "stat", flaky_stat)
    inventory_rows = [{"task_name": "Proj", "task_full_name": "\\Proj",
                       "command_target_path": str(script_dir / "run.py")}]
    _, summary = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert summary["\\Proj"]["latest_log_error_lines"] is None
    assert summary["\\Proj"]["latest_log_lines_scanned"] is None


# ---------------------------------------------------------------- 1.8h only real logs (metrics-08)

def test_txt_outside_log_dir_is_ignored(tmp_path):
    script_dir = tmp_path / "Proj"
    script_dir.mkdir()
    (script_dir / "run.py").write_text("", encoding="utf-8")
    (script_dir / "README.txt").write_text("Records updated: 999\n", encoding="utf-8")
    inventory_rows = [{"task_name": "Proj", "task_full_name": "\\Proj",
                       "command_target_path": str(script_dir / "run.py")}]
    rows, _ = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert rows == []


def test_txt_inside_logs_dir_is_still_mined(tmp_path):
    script_dir = tmp_path / "Proj2"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    (logs / "run.txt").write_text("Records updated: 12\n", encoding="utf-8")
    inventory_rows = [{"task_name": "Proj2", "task_full_name": "\\Proj2",
                       "command_target_path": str(script_dir / "run.py")}]
    rows, _ = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert any(r["metric_value"] == 12.0 for r in rows)


def test_log_suffix_is_still_recognised_outside_a_log_dir(tmp_path):
    """.log is trusted anywhere, unlike .txt -- this must not regress."""
    script_dir = tmp_path / "Proj3"
    script_dir.mkdir()
    (script_dir / "run.py").write_text("", encoding="utf-8")
    (script_dir / "run.log").write_text("Records updated: 7\n", encoding="utf-8")
    inventory_rows = [{"task_name": "Proj3", "task_full_name": "\\Proj3",
                       "command_target_path": str(script_dir / "run.py")}]
    rows, _ = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert any(r["metric_value"] == 7.0 for r in rows)


def test_txt_is_mined_in_an_override_directory_named_anything(tmp_path):
    """metrics-09: an operator pointing log_directory_overrides at, say, D:/App/output (which
    doesn't match LOG_DIR_NAMES) must still get its .txt logs mined -- an override is a
    deliberate operator choice, not shared-folder guesswork, so it is trusted like a
    directory literally named Logs."""
    straggler_dir = tmp_path / "output"
    straggler_dir.mkdir()
    (straggler_dir / "run.txt").write_text("Records updated: 44\n", encoding="utf-8")

    inventory_rows = [{"task_name": "Straggler", "task_full_name": "\\Straggler",
                       "command_target_path": r"C:\does\not\exist\run.py"}]
    rows, _ = metrics.build_metric_rows(
        inventory_rows, host_name="H",
        log_directory_overrides={"\\Straggler": str(straggler_dir)})
    assert any(r["metric_value"] == 44.0 for r in rows)


# ---------------------------------------------------------------- 1.9 shared-folder attribution (metrics-03)

def test_shared_folder_attributes_logs_by_script_stem(tmp_path):
    shared = tmp_path / "Shared"
    shared.mkdir()
    (shared / "collect_a.py").write_text("", encoding="utf-8")
    (shared / "collect_b.py").write_text("", encoding="utf-8")
    (shared / "collect_a.log").write_text("Records updated: 11\n", encoding="utf-8")
    (shared / "collect_b.log").write_text("Records updated: 22\n", encoding="utf-8")

    # Task names deliberately unrelated to the "collect_a"/"collect_b" script stems and to each
    # other, so this test isolates script-stem matching from the task-name-token fallback.
    inventory_rows = [
        {"task_name": "Alpha Run", "task_full_name": "\\Alpha Run",
         "command_target_path": str(shared / "collect_a.py")},
        {"task_name": "Bravo Run", "task_full_name": "\\Bravo Run",
         "command_target_path": str(shared / "collect_b.py")},
    ]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H", days_back=30)
    assert summary["\\Alpha Run"]["latest_log_name"] == "collect_a.log"
    assert summary["\\Bravo Run"]["latest_log_name"] == "collect_b.log"
    a_values = {r["metric_value"] for r in rows if r["task_full_name"] == "\\Alpha Run"}
    b_values = {r["metric_value"] for r in rows if r["task_full_name"] == "\\Bravo Run"}
    assert a_values == {11.0}
    assert b_values == {22.0}


def test_shared_folder_attributes_logs_by_task_name_tokens(tmp_path):
    """Log files named after the TASK, not the script, in a folder shared by several tasks."""
    shared = tmp_path / "Shared"
    shared.mkdir()
    (shared / "run.py").write_text("", encoding="utf-8")
    (shared / "run2.py").write_text("", encoding="utf-8")
    (shared / "Parcels_Refresh_2026-09-01.log").write_text("Records updated: 5\n", encoding="utf-8")
    (shared / "Streets_Rebuild_2026-09-01.log").write_text("Records updated: 9\n", encoding="utf-8")

    inventory_rows = [
        {"task_name": "Parcels Refresh", "task_full_name": "\\Parcels Refresh",
         "command_target_path": str(shared / "run.py")},
        {"task_name": "Streets Rebuild", "task_full_name": "\\Streets Rebuild",
         "command_target_path": str(shared / "run2.py")},
    ]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H", days_back=30)
    assert summary["\\Parcels Refresh"]["latest_log_name"] == "Parcels_Refresh_2026-09-01.log"
    assert summary["\\Streets Rebuild"]["latest_log_name"] == "Streets_Rebuild_2026-09-01.log"


def test_single_task_folder_keeps_unfiltered_behaviour(tmp_path):
    """A folder used by only one task must keep mining every log in it, unfiltered by name --
    the shared-folder attribution logic must not regress the common (non-shared) case."""
    script_dir = tmp_path / "Solo"
    logs = script_dir / "Logs"
    logs.mkdir(parents=True)
    (script_dir / "run.py").write_text("", encoding="utf-8")
    (logs / "whatever_name.log").write_text("Records updated: 3\n", encoding="utf-8")

    inventory_rows = [{"task_name": "Solo Task", "task_full_name": "\\Solo Task",
                       "command_target_path": str(script_dir / "run.py")}]
    rows, summary = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert rows and rows[0]["metric_value"] == 3.0
    assert summary["\\Solo Task"]["latest_log_name"] == "whatever_name.log"


def test_no_recent_logs_warning_names_shared_folder_attribution(tmp_path, caplog):
    """When a shared directory's only file is excluded by name attribution for a task, the "no
    recent log files" warning must say so -- otherwise it sends the operator chasing rotation
    or a wrong folder, neither of which is the actual cause."""
    shared = tmp_path / "Shared"
    shared.mkdir()
    (shared / "collect_a.py").write_text("", encoding="utf-8")
    (shared / "collect_b.py").write_text("", encoding="utf-8")
    (shared / "collect_a.log").write_text("Records updated: 5\n", encoding="utf-8")

    inventory_rows = [
        {"task_name": "Alpha Run", "task_full_name": "\\Alpha Run",
         "command_target_path": str(shared / "collect_a.py")},
        {"task_name": "Bravo Run", "task_full_name": "\\Bravo Run",
         "command_target_path": str(shared / "collect_b.py")},
    ]
    caplog.set_level("INFO")  # emit() logs at INFO; the "WARNING:" text is only a prefix
    _, summary = metrics.build_metric_rows(inventory_rows, host_name="H")
    assert summary["\\Bravo Run"]["log_files_scanned"] == 0
    warning = next(r.message for r in caplog.records if "Bravo Run" in r.message)
    assert "shared" in warning.lower()

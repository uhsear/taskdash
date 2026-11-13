"""Audit step "Fix collection accuracy": log classification, attribution and discovery (TD-038..041, TD-150)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import taskdash_metrics as metrics


# ---------------------------------------------------------------- TD-039 INFO lines that say "failed"

@pytest.mark.parametrize("line,expected", [
    ("2026-10-01 02:00:00 - INFO - Retrying the failed upload", (False, False)),
    ("2026-10-01 02:00:00 - DEBUG - upload failed, will retry", (False, False)),
    ("2026-10-01 02:00:00 - INFO - 0 uploads failed", (False, False)),
    ("2026-10-01 02:00:00 - INFO - 0 x failed, 5 ok", (False, False)),
    ("2026-10-01 02:00:00 - INFO - 3 failed, 9 succeeded", (True, False)),
    ("2026-10-01 02:00:00 - INFO - Failed: 2", (True, False)),
    ("2026-10-01 02:00:00 - ERROR - upload failed", (True, False)),
    ("2026-10-01 02:00:00 - WARNING - upload failed, retrying", (False, True)),
    ("INFO:root:Retrying the failed upload", (False, False)),
    ("INFO - upload failed, will retry", (False, False)),
    ("DEBUG:root:warning: slow disk", (False, False)),
    ("INFO:root:3 failed, 9 succeeded", (True, False)),
    ("ERROR:root:upload failed", (True, False)),
    ("upload failed", (True, False)),
    ("Failed to execute (RebuildAddressLocator).", (True, False)),
    ("2026-10-01 02:00:00 - INFO - Traceback (most recent call last):", (True, False)),
])
def test_td039_a_level_decides_before_the_word_failed(line, expected):
    assert metrics.classify_line(line) == expected


# ---------------------------------------------------------------- helpers

def _local_stamp(moment: datetime) -> str:
    """A log timestamp: naive, in the server's local time, as a script writes it."""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _task(name, script, **extra):
    return {"task_name": name, "task_full_name": f"\\{name}", "command_target_path": str(script), **extra}


# ---------------------------------------------------------------- TD-038 one log appended on every run

def _appended_log(tmp_path, last_run, earlier):
    script_dir = tmp_path / "Proj"
    script_dir.mkdir()
    (script_dir / "run.py").write_text("", encoding="utf-8")
    old = _local_stamp(earlier)
    new = _local_stamp(last_run + timedelta(seconds=5))
    (script_dir / "run.log").write_text(
        f"{old} - ERROR - boom\n{old} - INFO - Records updated: 5\n"
        f"{new} - INFO - Records updated: 9\n", encoding="utf-8")
    return script_dir


def test_td038_an_appended_log_speaks_for_its_last_run_only(tmp_path, caplog):
    last_run = datetime.now(timezone.utc) - timedelta(hours=1)
    script_dir = _appended_log(tmp_path, last_run, last_run - timedelta(days=1))
    caplog.set_level("INFO")
    rows, summary = metrics.build_metric_rows(
        [_task("Proj", script_dir / "run.py", last_run_time=last_run.isoformat())], host_name="H")

    assert {r["metric_key"]: r["metric_value"] for r in rows} == {"updated_record_count": 9.0}
    assert summary["\\Proj"]["latest_log_error_lines"] == 0
    assert any("earlier runs" in r.message and "one log file per run" in r.message for r in caplog.records)


def test_td038_a_per_run_log_is_read_whole(tmp_path, caplog):
    last_run = datetime.now(timezone.utc) - timedelta(hours=1)
    script_dir = tmp_path / "Proj"
    script_dir.mkdir()
    (script_dir / "run.py").write_text("", encoding="utf-8")
    stamp = _local_stamp(last_run + timedelta(seconds=5))
    (script_dir / "run.log").write_text(f"{stamp} - ERROR - boom\n{stamp} - INFO - Records updated: 9\n",
                                        encoding="utf-8")
    caplog.set_level("INFO")
    _, summary = metrics.build_metric_rows(
        [_task("Proj", script_dir / "run.py", last_run_time=last_run.isoformat())], host_name="H")

    assert summary["\\Proj"]["latest_log_error_lines"] == 1
    assert not any("earlier runs" in r.message for r in caplog.records)


def test_td038_a_log_whose_clock_is_far_off_is_read_whole_not_as_a_clean_zero(tmp_path):
    last_run = datetime.now(timezone.utc) - timedelta(hours=1)
    script_dir = _appended_log(tmp_path, last_run, last_run - timedelta(days=1))
    # A different time zone puts every stamp hours before the run; nothing may be reported as 0 lines.
    (script_dir / "run.log").write_text(
        f"{_local_stamp(last_run - timedelta(hours=5))} - ERROR - boom\n", encoding="utf-8")
    _, summary = metrics.build_metric_rows(
        [_task("Proj", script_dir / "run.py", last_run_time=last_run.isoformat())], host_name="H")

    assert summary["\\Proj"]["latest_log_error_lines"] == 1


def test_td038_scan_log_file_keeps_unstamped_lines_of_the_run_it_reads(tmp_path):
    base = datetime.now(timezone.utc)
    log = tmp_path / "run.log"
    log.write_text(
        f"{_local_stamp(base - timedelta(hours=3))} - ERROR - old\n  old detail failed\n"
        f"{_local_stamp(base)} - ERROR - new\nTraceback (most recent call last):\n", encoding="utf-8")
    result = metrics.scan_log_file(log, 0, since=base - timedelta(hours=1))

    assert (result["lines_scanned"], result["error_lines"], result["earlier_run_lines"]) == (2, 2, 2)


# ---------------------------------------------------------------- TD-040 shared-folder attribution

def _shared(tmp_path, *names):
    shared = tmp_path / "Shared"
    shared.mkdir()
    for name in names:
        (shared / name).write_text("Records updated: 4\n", encoding="utf-8")
    return shared


def test_td040_one_common_word_does_not_claim_a_log(tmp_path):
    shared = _shared(tmp_path, "daily_report.log", "extract.py", "loader.py")
    tasks = [_task("Daily Backup", shared / "extract.py"), _task("Daily Report", shared / "loader.py")]
    rows, summary = metrics.build_metric_rows(tasks, host_name="H")

    assert summary["\\Daily Report"]["latest_log_name"] == "daily_report.log"
    assert summary["\\Daily Backup"]["log_files_scanned"] == 0
    assert {r["task_full_name"] for r in rows} == {"\\Daily Report"}


def test_td040_a_script_stem_wins_over_task_name_tokens_and_the_longest_stem_wins(tmp_path):
    shared = _shared(tmp_path, "run.py", "run2.py", "run2.log")
    tasks = [_task("Alpha", shared / "run.py"), _task("Bravo", shared / "run2.py")]
    _, summary = metrics.build_metric_rows(tasks, host_name="H")

    assert summary["\\Bravo"]["latest_log_name"] == "run2.log"
    assert summary["\\Alpha"]["log_files_scanned"] == 0


def test_td040_a_file_two_tasks_claim_goes_to_neither_and_the_warning_names_the_override(tmp_path, caplog):
    shared = _shared(tmp_path, "etl.py", "etl.log")
    tasks = [_task("Load East", shared / "etl.py"), _task("Load West", shared / "etl.py")]
    caplog.set_level("INFO")
    rows, summary = metrics.build_metric_rows(tasks, host_name="H")

    assert rows == []
    assert [s["log_files_scanned"] for s in summary.values()] == [0, 0]
    warnings = [r.message for r in caplog.records if "matches 2 tasks" in r.message]
    assert len(warnings) == 1 and "log_directory_overrides" in warnings[0]


def test_td040_an_override_folder_of_its_own_ends_the_ambiguity(tmp_path):
    shared = _shared(tmp_path, "etl.py", "etl.log")
    own = tmp_path / "east_logs"
    own.mkdir()
    (own / "east.log").write_text("Records updated: 8\n", encoding="utf-8")
    tasks = [_task("Load East", shared / "etl.py"), _task("Load West", shared / "etl.py")]
    rows, _ = metrics.build_metric_rows(
        tasks, host_name="H", log_directory_overrides={"\\Load East": str(own)})

    # Load West is now alone in the shared folder, so it keeps today's unfiltered read.
    assert {(r["task_full_name"], r["metric_value"]) for r in rows} == {("\\Load East", 8.0), ("\\Load West", 4.0)}


# ---------------------------------------------------------------- TD-041 relative scripts and interpreters

def test_td041_a_relative_script_resolves_against_the_start_in_folder(tmp_path):
    work = tmp_path / "Work"
    (work / "bin").mkdir(parents=True)
    (work / "bin" / "Logs").mkdir()
    found = metrics.discover_log_directories(r"bin\run.py", str(work))

    assert [p.name for p in found] == ["bin", "Logs"]


def test_td041_an_environment_variable_path_is_expanded_before_the_relative_test(tmp_path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    work = tmp_path / "Work"
    (work / "Logs").mkdir(parents=True)
    monkeypatch.setenv("TD_SCRIPTS", str(scripts))
    found = metrics.discover_log_directories(r"%TD_SCRIPTS%\job.py", str(work))

    assert [p.resolve() for p in found] == [scripts.resolve(), work.resolve(), (work / "Logs").resolve()]


def test_td041_an_unset_variable_path_keeps_the_start_in_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("TD_NOT_SET", raising=False)
    work = tmp_path / "Work"
    (work / "Logs").mkdir(parents=True)
    found = metrics.discover_log_directories(r"%TD_NOT_SET%\job.py", str(work))

    assert [p.resolve() for p in found] == [work.resolve(), (work / "Logs").resolve()]


def test_td041_a_relative_script_without_a_start_in_folder_finds_nothing(tmp_path, monkeypatch):
    (tmp_path / "Logs").mkdir()
    monkeypatch.chdir(tmp_path)  # TaskDash's own folder must never stand in for the task's

    assert metrics.discover_log_directories("run.py") == []
    assert metrics.discover_log_directories("powershell.exe") == []


def test_td041_an_interpreter_folder_is_not_a_log_root(tmp_path):
    interpreter = tmp_path / "Python313"
    interpreter.mkdir()
    (interpreter / "python.exe").write_text("", encoding="utf-8")
    (interpreter / "python.log").write_text("Records updated: 3\n", encoding="utf-8")
    task = _task("Inline", interpreter / "python.exe", command_target_type="Python")
    rows, summary = metrics.build_metric_rows([task], host_name="H")

    assert rows == [] and "\\Inline" not in summary


def test_td041_an_interpreter_task_still_reads_its_start_in_folder(tmp_path):
    interpreter = tmp_path / "Python313"
    interpreter.mkdir()
    work = tmp_path / "Work"
    work.mkdir()
    (work / "job.log").write_text("Records updated: 6\n", encoding="utf-8")
    task = _task("Inline", interpreter / "python.exe", command_target_type="Python",
                 command_target_working_directory=str(work))
    rows, _ = metrics.build_metric_rows([task], host_name="H")

    assert [r["metric_value"] for r in rows] == [6.0]


# ---------------------------------------------------------------- TD-150 one listing per folder per pass

def test_td150_a_shared_folder_is_listed_once_per_pass(tmp_path, monkeypatch):
    shared = _shared(tmp_path, "a.py", "b.py", "c.py", "a.log", "b.log", "c.log")
    tasks = [_task(name, shared / f"{name.lower()}.py") for name in ("A", "B", "C")]
    listed = []
    original = Path.iterdir

    def counting(self):
        listed.append(self)
        return original(self)

    monkeypatch.setattr(Path, "iterdir", counting)
    _, summary = metrics.build_metric_rows(tasks, host_name="H")

    assert sorted(s["latest_log_name"] for s in summary.values()) == ["a.log", "b.log", "c.log"]
    assert listed.count(shared) == 1

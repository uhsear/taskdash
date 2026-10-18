"""Phase 2.5 metrics: level-only metric-line prefixes, disabled tasks and unmatched metrics.tasks keys."""

from __future__ import annotations

import pytest

import taskdash_metrics as tm

MARKER = "TASKDASH" + " "


@pytest.mark.parametrize("prefix", [
    "",
    "2026-10-17 10:00:00,123 - INFO - ",
    "INFO:root:",
    "INFO:my.module:",
    "[INFO] ",
    "INFO - ",
    "WARNING: ",
    "ERROR ",
])
def test_taskdash_line_accepts_documented_prefixes(prefix):
    assert tm.extract_taskdash_line(f"{prefix}{MARKER}rows=5") == [("rows", 5.0)]


@pytest.mark.parametrize("line", [
    "NOT_" + MARKER + "rows=5",
    "xINFO:root:" + MARKER + "rows=5",
    "INFO" + MARKER + "rows=5",
    "NOTICE: " + MARKER + "rows=5",
    "saw INFO:root:" + MARKER + "rows=5",
])
def test_taskdash_line_rejects_other_prefixes(line):
    assert tm.extract_taskdash_line(line) == []


@pytest.fixture
def settings(monkeypatch):
    table = {}
    monkeypatch.setattr(tm, "TASK_SETTINGS", table)
    return table


def _inventory(tmp_path, name="Job"):
    (tmp_path / "job.py").write_text("", encoding="utf-8")
    logs = tmp_path / "Logs"
    logs.mkdir(exist_ok=True)
    (logs / "job.log").write_text("Rows loaded: 12\n", encoding="utf-8")
    return [{"task_name": name, "task_full_name": f"\\{name}",
             "command_target_path": str(tmp_path / "job.py")}]


def test_enabled_task_files_are_examined(tmp_path, settings):
    examined = set()
    rows, _ = tm.build_metric_rows(_inventory(tmp_path), host_name="H", examined=examined)
    assert rows
    assert examined == {("\\Job", "job.log")}


def test_disabled_task_files_are_not_examined(tmp_path, settings):
    settings["\\Job"] = {"enabled": False, "prose_count_rules": [], "noise_metric_key_patterns": []}
    examined = set()
    rows, summary = tm.build_metric_rows(_inventory(tmp_path), host_name="H", examined=examined)
    assert rows == []
    assert examined == set()
    assert summary["\\Job"]["latest_log_name"] == "job.log"


def test_unmatched_metrics_tasks_key_warns_once(tmp_path, settings, monkeypatch):
    messages = []
    monkeypatch.setattr(tm, "emit", messages.append)
    settings["Job"] = {"enabled": True, "prose_count_rules": [], "noise_metric_key_patterns": []}
    settings["\\Job"] = {"enabled": True, "prose_count_rules": [], "noise_metric_key_patterns": []}
    tm.build_metric_rows(_inventory(tmp_path), host_name="H")
    hits = [m for m in messages if "metrics.tasks key" in m]
    assert len(hits) == 1
    assert "'Job'" in hits[0] and "\\Folder\\Task" in hits[0]


@pytest.mark.parametrize("cut", [240, 500])
def test_a_url_credential_crossing_a_cut_is_scrubbed(tmp_path, settings, cut):
    import taskdash_publish as publish

    secret = "Dummy0Pw9"  # not a real secret
    head = "Rows loaded: 12 "
    url = f"ftp://svc:{secret}@host/x"
    # The cut falls inside the password: cut first, the URL pattern no longer sees its "@".
    line = head + "x" * (cut - len(head) - len("ftp://svc:") - 3) + " " + url
    assert line.index(secret) < cut < line.index(secret) + len(secret)
    inventory = _inventory(tmp_path)
    (tmp_path / "Logs" / "job.log").write_text(line + "\n", encoding="utf-8")
    rows, _ = tm.build_metric_rows(inventory, host_name="H")
    assert rows
    published = publish.prepare_rows(rows, publish.TABLE_METRICS, "2026-10-16T12:00:00+00:00",
                                     {"hide_sample_lines": False})
    for row in rows + published:
        assert "Dum" not in row["sample_line"]

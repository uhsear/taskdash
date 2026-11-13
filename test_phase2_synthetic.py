"""Replay of the synthetic golden fixture (roadmap Phase 2, deliverable 2): every seeded case must
show up in the rows collect_everything derives, with no network and no real data."""

from __future__ import annotations

import importlib.util
import statistics
from datetime import datetime, timezone
from pathlib import Path

import pytest

import taskdash_common
import taskdash_publish as publish

taskdash_common.disable_arcpy_import()

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "tests" / "fixtures"
HOST1 = FIXTURES / "synthetic_app_host_01.json"
HOST2 = FIXTURES / "synthetic_app_host_02.json"
FROZEN_NOW = "2026-10-16T12:00:00Z"


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "_tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_tool("make_synthetic_fixture")
deny_scan = _load_tool("deny_scan")


def _collect(path, config=None):
    return publish.collect_everything(config or {}, path)


def _by_name(collected):
    return {row["task_name"]: row for row in collected["inventory"]}


@pytest.fixture(scope="module")
def host1():
    return _collect(HOST1)


@pytest.fixture(scope="module")
def host2():
    return _collect(HOST2)


def test_committed_fixture_matches_the_generator(tmp_path):
    gen.main(["--out-dir", str(tmp_path), "--now", FROZEN_NOW])
    for committed in (HOST1, HOST2):
        assert (tmp_path / committed.name).read_bytes() == committed.read_bytes(), (
            f"{committed.name} is stale: rerun _tools/make_synthetic_fixture.py "
            f"--out-dir tests/fixtures --now {FROZEN_NOW}")


def test_same_seed_and_now_give_the_same_bytes_and_a_new_seed_changes_them(tmp_path):
    gen.main(["--out-dir", str(tmp_path / "a"), "--now", FROZEN_NOW, "--seed", "7"])
    gen.main(["--out-dir", str(tmp_path / "b"), "--now", FROZEN_NOW, "--seed", "7"])
    gen.main(["--out-dir", str(tmp_path / "c"), "--now", FROZEN_NOW, "--seed", "8"])
    name = HOST1.name
    assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert (tmp_path / "a" / name).read_bytes() != (tmp_path / "c" / name).read_bytes()


def test_two_hosts_about_twenty_five_tasks_and_unique_task_paths(host1, host2):
    assert (host1["host_name"], host2["host_name"]) == ("APP-HOST-01", "APP-HOST-02")
    rows = host1["inventory"] + host2["inventory"]
    assert len(rows) == 25
    assert len({r["task_full_name"] for r in rows}) == 25
    assert {r["task_path"] for r in host1["inventory"]}.isdisjoint({r["task_path"] for r in host2["inventory"]})


def test_failure_shows_as_failed(host1):
    task = _by_name(host1)["Report Mailer"]
    assert (task["health_status"], task["last_run_failed"], task["attention_kind"]) == ("Error", 1, "FAILED")
    assert task["failures_last_7_days"] == 1


def test_stuck_run_shows_as_stuck(host1):
    task = _by_name(host1)["Data Archiver"]
    assert task["attention_kind"] == "STUCK"
    assert task["health_status"] == "Warning"
    assert "Still running" in task["attention_reason"]


def test_overdue_task_shows_as_overdue(host1):
    task = _by_name(host1)["Weekly Cleanup"]
    assert (task["is_overdue"], task["attention_kind"]) == (1, "OVERDUE")
    assert task["health_status"] == "Warning"


def test_disabled_task_is_disabled_and_quiet(host1):
    task = _by_name(host1)["Legacy Sync"]
    assert (task["health_status"], task["needs_attention"], task["attention_kind"]) == ("Disabled", 0, "")


def test_setting_risk_is_a_config_warning_not_a_health_change(host1):
    task = _by_name(host1)["Cache Refresher"]
    assert "No time limit" in task["config_warnings"]
    assert (task["health_status"], task["attention_kind"]) == ("Success", "")


def test_healthy_tasks_stay_healthy(host1, host2):
    seeded = {"Report Mailer", "Data Archiver", "Weekly Cleanup", "Legacy Sync",
              "Archive Copy", "Report Builder", "Export Packager", "Mirror Sync",
              "Session Cleaner", "Status Poller", "Cert Checker", "Disk Reporter"}
    for row in host1["inventory"] + host2["inventory"]:
        if row["task_name"] in seeded:
            continue
        assert row["attention_kind"] == "", row["task_name"]
        assert row["config_warnings"] == "" or row["task_name"] == "Cache Refresher", row["task_name"]


def _changes(path, current, tmp_path):
    import json
    prior_path = tmp_path / f"prior_{path.name}"
    prior_path.write_text(json.dumps(gen.prior_fixture(json.loads(path.read_text(encoding="utf-8")))),
                          encoding="utf-8")
    old_by_key = {r["task_full_name"]: r for r in _collect(prior_path)["inventory"]}
    return publish.detect_task_changes(old_by_key, current["inventory"], current["host_name"],
                                       "2026-10-16T12:00:00+00:00")


def test_the_prior_capture_gives_every_kind_of_change_row(host1, host2, tmp_path):
    changes = _changes(HOST1, host1, tmp_path) + _changes(HOST2, host2, tmp_path)
    assert sorted((c["task_name"], c["change_kind"]) for c in changes) == [
        ("Backup Verifier", "recovered"), ("Config Publisher", "definition_changed"),
        ("Report Mailer", "became_failing")]
    # The privacy switches hide the command line, so the row must show a setting a viewer can read.
    changed = next(c for c in changes if c["change_kind"] == "definition_changed")
    assert changed["old_summary"] != changed["new_summary"]


def test_every_documented_state_is_seeded(host1, host2):
    rows = {**_by_name(host1), **_by_name(host2)}
    assert (rows["Status Poller"]["attention_kind"], rows["Status Poller"]["health_status"]) == ("BLOCKED", "Warning")
    assert "2 missed runs" in rows["Session Cleaner"]["attention_reason"]
    assert (rows["Session Cleaner"]["attention_kind"], rows["Session Cleaner"]["health_status"]) == ("WARNING", "Warning")
    assert rows["Cert Checker"]["health_status"] == "NotYetRun"
    assert (rows["Disk Reporter"]["health_status"], rows["Disk Reporter"]["attention_kind"]) == ("Running", "")
    assert rows["Backup Verifier"]["failures_last_7_days"] == 1 and rows["Backup Verifier"]["attention_kind"] == ""


def test_the_log_cases_show_errors_and_the_metric_flags(tmp_path):
    import taskdash_metrics

    taskdash_metrics.configure_rules({"rules": {"profiles": []}})
    overrides = gen.write_metric_logs(tmp_path, datetime.now(timezone.utc))
    collected = _collect(HOST1, {"metrics": {"log_directory_overrides": overrides}})
    assert _by_name(collected)["Queue Drainer"]["latest_log_error_lines"] == 2
    assert _by_name(collected)["Queue Drainer"]["attention_kind"] == ""
    rows = [{**m, "observed_at_utc": datetime.fromisoformat(m["observed_at_utc"]).timestamp() * 1000}
            for m in collected["metrics"]]
    change = {r["metric_key"]: r for r in taskdash_metrics.compute_metric_change(rows)
              if r["task_name"] in ("Queue Drainer", "Heartbeat Check")}
    drain = change["failed_items"]
    # Far over usual: 50 percent or more above a usual value of 10 or more, and above every earlier value.
    assert drain["usual_value"] >= 10 and drain["latest_value"] >= 1.5 * drain["usual_value"]
    assert drain["latest_value"] > drain["highest_value"]
    # Not in last run: logged in all but the newest log, so it ends well before the task's newest value.
    deleted, newest = change["rows_deleted"], change["rows_processed"]
    assert deleted["history_count"] >= 3 and newest["latest_at_utc"] - deleted["latest_at_utc"] >= 12 * 3600 * 1000


def test_shared_cause_incident_names_one_cause(host2):
    inventory = host2["inventory"]
    current = publish.alert_signature(inventory)
    assert sorted(current) == ["FAILED: \\AppHost02\\Archive Copy", "FAILED: \\AppHost02\\Export Packager",
                               "FAILED: \\AppHost02\\Mirror Sync", "FAILED: \\AppHost02\\Report Builder"]
    assert len({r["last_result_text"] for r in inventory if r["last_run_failed"]}) == 1
    incident = publish.shared_cause(inventory, current, 3, causes=publish.failure_causes(inventory))
    assert incident["count"] == 4
    assert incident["cause"] == "network"


def test_far_below_usual_metric_from_synthetic_logs(tmp_path):
    now = datetime.now(timezone.utc)
    overrides = gen.write_metric_logs(tmp_path, now)
    collected = _collect(HOST1, {"metrics": {"log_directory_overrides": overrides}})

    def series(task_name):
        rows = [m for m in collected["metrics"] if m["task_name"] == task_name]
        assert {m["metric_key"] for m in rows} == {"rows_processed"}
        return [m["metric_value"] for m in sorted(rows, key=lambda m: m["observed_at_utc"])]

    # The dashboard's Arcade rule (METRIC_CHANGE_*): latest at or below -50 percent of the median
    # of the earlier values, usual at least 10, and below every earlier value.
    loader = series("Metric Loader")
    latest, earlier = loader[-1], loader[:-1]
    usual = statistics.median(earlier)
    assert len(earlier) >= 3 and usual >= 10
    assert 100 * (latest - usual) / usual <= -50 and latest < min(earlier)

    steady = series("Inventory Export")
    assert steady[-1] >= 0.5 * statistics.median(steady[:-1])


def test_default_rules_alone_mine_every_intended_generic_metric(tmp_path):
    import taskdash_metrics

    taskdash_metrics.configure_rules({"rules": {"profiles": []}})
    overrides = gen.write_metric_logs(tmp_path, datetime.now(timezone.utc), generic=True)
    collected = _collect(HOST1, {"metrics": {"log_directory_overrides": overrides}})

    found = {m["metric_key"] for m in collected["metrics"]}
    intended = set(gen.generic_metric_keys()) | {"rows_processed", "failed_items", "rows_deleted"}
    assert intended <= found, f"default.json missed: {sorted(intended - found)}"
    assert len(found - intended) <= 2, f"noise metrics: {sorted(found - intended)}"


def test_deny_scan_finds_nothing_in_the_synthetic_fixture(capsys):
    assert deny_scan.main([str(FIXTURES)]) == 0
    assert "0 hit(s)" in capsys.readouterr().err


def test_deny_scan_reports_one_planted_hit_with_file_and_line(tmp_path, capsys):
    planted = tmp_path / "note.txt"
    planted.write_text("clean line\nsee the " + "Mar" + "ion share\nclean again\n", encoding="utf-8")
    assert deny_scan.main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == f"{planted}:2: " + "mar" + "ion"
    assert "1 hit(s)" in captured.err


def test_deny_scan_reads_an_extra_list_by_path(tmp_path, capsys):
    (tmp_path / "terms.txt").write_text("# private\n\nZebraCorp\n", encoding="utf-8")
    target = tmp_path / "scan" / "a.txt"
    target.parent.mkdir()
    target.write_text("owned by zebracorp\n", encoding="utf-8")
    assert deny_scan.main([str(target)]) == 0
    assert deny_scan.main([str(target), "--extra-list", str(tmp_path / "terms.txt")]) == 1
    capsys.readouterr()


def test_deny_scan_names_a_missing_path_instead_of_passing(tmp_path):
    with pytest.raises(SystemExit, match="Path not found"):
        deny_scan.main([str(tmp_path / "nope")])


def test_deny_scan_prints_a_private_term_by_position_not_by_name(tmp_path, capsys):
    (tmp_path / "terms.txt").write_text("ZebraCorp\n", encoding="utf-8")
    target = tmp_path / "scan" / "a.txt"
    target.parent.mkdir()
    target.write_text("owned by zebracorp\n", encoding="utf-8")
    assert deny_scan.main([str(target), "--extra-list", str(tmp_path / "terms.txt")]) == 1
    assert capsys.readouterr().out.strip() == f"{target}:1: private term #1"


def test_deny_scan_reads_a_utf16_file(tmp_path, capsys):
    target = tmp_path / "a.ps1"
    target.write_bytes(("see the " + "Mar" + "ion share\n").encode("utf-16"))
    assert deny_scan.main([str(target)]) == 1
    capsys.readouterr()

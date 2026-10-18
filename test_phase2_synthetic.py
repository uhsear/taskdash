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
              "Archive Copy", "Report Builder", "Export Packager", "Mirror Sync"}
    for row in host1["inventory"] + host2["inventory"]:
        if row["task_name"] in seeded:
            continue
        assert row["attention_kind"] == "", row["task_name"]
        assert row["config_warnings"] == "" or row["task_name"] == "Cache Refresher", row["task_name"]


def test_definition_change_needs_the_prior_inventory_the_fixture_carries(host1, tmp_path):
    import json
    prior_payload = json.loads(HOST1.read_text(encoding="utf-8"))
    prior_payload["tasks"] = prior_payload.pop("prior_tasks")
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(json.dumps(prior_payload), encoding="utf-8")
    prior = _collect(prior_path)

    old_by_key = {r["task_full_name"]: r for r in prior["inventory"]}
    changes = publish.detect_task_changes(old_by_key, host1["inventory"], "APP-HOST-01",
                                          "2026-10-16T12:00:00+00:00")
    changed = [(c["task_name"], c["change_kind"]) for c in changes if c["change_kind"] == "definition_changed"]
    assert changed == [("Config Publisher", "definition_changed")]
    # Nothing else moved between the two captures, so the diff holds no other definition change.
    assert not [c for c in changes if c["change_kind"] in ("task_added", "task_removed")]


def test_shared_cause_incident_names_one_cause(host2):
    inventory = host2["inventory"]
    current = publish.alert_signature(inventory)
    assert sorted(current) == ["FAILED: Archive Copy", "FAILED: Export Packager",
                               "FAILED: Mirror Sync", "FAILED: Report Builder"]
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
    intended = set(gen.generic_metric_keys()) | {"rows_processed"}
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

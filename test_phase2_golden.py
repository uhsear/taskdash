"""Golden replay harness (roadmap Phase 2, deliverable 1): replay a fixture twice, compare, and
check that a planted difference is reported unless the approved list names it."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_app_host_01.json"


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "_tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


golden = _load_tool("golden_replay")


@pytest.fixture()
def config_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"rules": {"profiles": []}, "alerting": {"dashboard_url": "https://example.invalid/dash"}}),
                    encoding="utf-8")
    return path


def _replay(config_path, out, *extra):
    args = ["replay", "--config", str(config_path), "--fixture", str(FIXTURE), "--out", str(out), *extra]
    assert golden.main(args) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_two_replays_are_identical_and_compare_exits_zero(config_path, tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _replay(config_path, a)
    _replay(config_path, b)
    assert a.read_bytes() == b.read_bytes()
    assert golden.main(["compare", str(a), str(b)]) == 0
    assert "All differences approved (or none)." in capsys.readouterr().out


def test_alert_section_present_with_placeholders(config_path, tmp_path):
    data = _replay(config_path, tmp_path / "a.json")
    assert set(data) == {"inventory", "runs", "metrics", "changes", "alert"}
    assert data["inventory"] and data["runs"]
    assert data["alert"]["subject"].startswith("[TaskDash]")
    assert "https://example.invalid/dash" in data["alert"]["body"]
    assert "<TIME>" in data["alert"]["body"]


def test_volatile_fields_are_dropped(config_path, tmp_path):
    data = _replay(config_path, tmp_path / "a.json")
    for row in data["inventory"] + data["runs"] + data["metrics"]:
        assert not set(golden.VOLATILE_FIELDS) & set(row)


def test_planted_change_is_reported_then_approved(config_path, tmp_path, capsys):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    data = _replay(config_path, a)
    victim = data["inventory"][0]
    data["inventory"][0]["state"] = "Planted"
    _write(b, data)
    assert golden.main(["compare", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "changed:" in out and "state" in out and "Planted" not in out

    approved = _write(tmp_path / "ok.json", [{
        "section": "inventory", "key": {"task_full_name": victim["task_full_name"]},
        "field": "state", "new": "Planted"}])
    assert golden.main(["compare", str(a), str(b), "--approved", str(approved)]) == 0
    assert "approved change" in capsys.readouterr().out

    wrong = _write(tmp_path / "wrong.json", [{
        "section": "inventory", "key": {"task_full_name": victim["task_full_name"]},
        "field": "state", "new": "Other"}])
    assert golden.main(["compare", str(a), str(b), "--approved", str(wrong)]) == 1


def test_added_and_removed_rows_are_reported_and_approvable(config_path, tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    data = _replay(config_path, a)
    gone = data["runs"].pop()
    _write(b, data)
    assert golden.main(["compare", str(a), str(b)]) == 1
    approved = _write(tmp_path / "ok.json", [{
        "section": "runs", "key": {"run_key": gone["run_key"]}, "field": "_removed"}])
    assert golden.main(["compare", str(a), str(b), "--approved", str(approved)]) == 0


def test_metric_rows_that_differ_only_by_log_file_pair_by_log_file(tmp_path, capsys):
    row = {"host_name": "h", "task_full_name": "t", "metric_key": "m", "observed_at_utc": "x",
           "metric_value": 1}
    one, two = {**row, "log_file_name": "a.log"}, {**row, "log_file_name": "b.log"}
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    _write(a, {"metrics": [one, two]})
    _write(b, {"metrics": [one, {**two, "metric_value": 2}]})
    assert golden.main(["compare", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "b.log" in out and "a.log" not in out


def test_task_changes_show_a_definition_change_against_prior_inventory(config_path, tmp_path):
    base = _replay(config_path, tmp_path / "a.json")
    prior = [dict(r) for r in base["inventory"]]
    prior[0]["definition_hash"] = prior[0]["definition_hash"][:-4] + "0000"
    prior[0]["actions"] = "X:\\synthetic\\old\\run.exe"
    prior_path = _write(tmp_path / "prior.json", prior)
    data = _replay(config_path, tmp_path / "b.json", "--prior-inventory", str(prior_path))
    kinds = {(c["task_full_name"], c["change_kind"]) for c in data["changes"]}
    assert (prior[0]["task_full_name"], "definition_changed") in kinds
    assert base["changes"] == []


def test_alert_state_changes_the_alert_section(config_path, tmp_path):
    base = _replay(config_path, tmp_path / "a.json")
    state = _write(tmp_path / "state.json", {"signature": ["FAILED: Retired Task"]})
    data = _replay(config_path, tmp_path / "b.json", "--alert-state", str(state))
    assert data["alert"] != base["alert"]

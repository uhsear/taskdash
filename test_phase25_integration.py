"""Phase 2.5 integration checks that span the files the parallel agents owned."""

from __future__ import annotations

import argparse
import json

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
import taskdash_publish as publish  # noqa: E402
from test_phase25_dashboard import _templates  # noqa: E402


def test_interactive_pattern_false_passes_validation(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"rules": {"profiles": []}, "scheduler": {"interactive_required_pattern": False}}),
                    encoding="utf-8")
    common.load_config(path)


def test_hide_paths_build_reads_no_path_field_and_passes_the_hidden_check():
    config = {"privacy": {"hide_command_lines": False, "hide_sample_lines": False,
                          "hide_accounts": False, "hide_paths": True}}
    definition = dash.build("TESTITEM", _templates(), config=config)
    dash.check_hidden_fields(definition, config)
    blob = json.dumps(definition)
    for field in ("command_target_working_directory", "latest_log_path", "command_target_path"):
        assert f'$datapoint[\\"{field}\\"]' not in blob, field


def test_refresh_metrics_reads_live_tasks_when_paths_are_hidden(monkeypatch):
    seen = {}

    class _Table:
        properties = {"fields": []}

    monkeypatch.setattr(publish, "connect_agol", lambda c: object())
    monkeypatch.setattr(publish, "ensure_service", lambda g, c, t: object())
    monkeypatch.setattr(publish, "get_tables", lambda i: {publish.TABLE_METRICS: _Table(), publish.TABLE_INVENTORY: _Table()})
    monkeypatch.setattr(publish, "check_schema_drift", lambda t: None)
    monkeypatch.setattr(publish, "collect_everything",
                        lambda c, f, **k: seen.setdefault("live", {"inventory": [], "kwargs": k}))
    monkeypatch.setattr(publish, "query_all_rows", lambda t, **k: seen.setdefault("stored", []) if "where" in k else [])
    monkeypatch.setattr(publish, "mine_metrics", lambda c, inv, h, ex: ([], None))
    monkeypatch.setattr(publish, "object_id_field", lambda t: "OBJECTID")
    publish.run_refresh_metrics({"privacy": {"hide_paths": True}}, argparse.Namespace(apply=False, target="test"))
    assert "live" in seen and "stored" not in seen
    assert seen["live"]["kwargs"] == {"first_run_budget": False}  # never halves prod's hourly budget
    seen.clear()
    publish.run_refresh_metrics({"privacy": {"hide_paths": False}}, argparse.Namespace(apply=False, target="test"))
    assert "stored" in seen and "live" not in seen

"""Phase 2.5 dashboard: host check, view warning, retention and daily-e-mail help, CLI, privacy default."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
import taskdash_publish as publish  # noqa: E402

REFERENCE = {"alerting": {"heartbeat_hour": 7}, "retention": {"runs_days": 90}}


def _templates():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    return {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
            "serial_config": by_name["Schedule Mix"]["chartConfig"]}


def _build(config=None):
    return dash.build("TESTITEM", _templates(), config=config)


def _blob(definition):
    return json.dumps([w.get("description", "") for w in dash.all_items(definition)]
                      + [w.get("noDataState", {}).get("text", "") for w in dash.all_items(definition)]
                      + [w.get("topCaption", "") for w in dash.all_items(definition)])


# ------------------------------------------------------------------ 1: hardcoded host check

@pytest.fixture
def synthetic_host(monkeypatch):
    monkeypatch.setattr(dash.socket, "gethostname", lambda: "SYNTHBOX")


def test_clean_build_passes_the_host_check(synthetic_host):
    dash.validate_definition(_build(), config={"host_name_override": "HOSTX"})


@pytest.mark.parametrize("text", ["host_name = 'HOSTX'", "var h = \"hostx\"; return h;"])
def test_host_in_a_where_clause_or_arcade_literal_is_flagged(synthetic_host, text):
    definition = _build()
    definition["note"] = text
    with pytest.raises(SystemExit, match="hardcoded host filter"):
        dash.validate_definition(definition, config={"host_name_override": "HOSTX"})


def test_the_machines_own_name_is_flagged_without_an_override(monkeypatch):
    monkeypatch.setattr(dash.socket, "gethostname", lambda: "Synthbox")
    definition = _build()
    definition["note"] = "host_name = 'SYNTHBOX'"
    with pytest.raises(SystemExit, match="hardcoded host filter"):
        dash.validate_definition(definition, config={})


def test_short_host_names_are_ignored(monkeypatch):
    monkeypatch.setattr(dash.socket, "gethostname", lambda: "ab")
    dash.validate_definition(_build(), config={"host_name_override": "ta"})


# ------------------------------------------------------------------ 2: view warning

def test_missing_view_id_warns_once_and_falls_back_to_the_service(monkeypatch):
    messages = []
    monkeypatch.setattr(dash, "emit", messages.append)
    assert dash.data_item_id({publish.service_key("prod"): "SVC"}, "prod") == "SVC"
    warnings = [m for m in messages if m.startswith("WARNING")]
    assert len(warnings) == 1
    assert "--create-view" in warnings[0] and "view_item_id" in warnings[0]


def test_configured_view_id_does_not_warn(monkeypatch):
    messages = []
    monkeypatch.setattr(dash, "emit", messages.append)
    assert dash.data_item_id({publish.view_key("prod"): "VIEW", publish.service_key("prod"): "SVC"}, "prod") == "VIEW"
    assert messages == []


# ------------------------------------------------------------------ 3: help literals

def test_runs_days_replaces_the_retention_phrase_everywhere():
    blob = _blob(_build({"retention": {"runs_days": 30}}))
    assert "saved history (up to 30 days)" in blob
    assert "up to 90 days" not in blob


def test_no_heartbeat_hour_means_no_daily_email_text():
    blob = _blob(_build({}))
    assert "There is no daily e-mail." in blob
    assert "7 AM" not in blob and "sent every day" not in blob


def test_reference_config_output_is_byte_identical_to_the_raw_text():
    terms = dash.help_terms(REFERENCE)
    for name, texts in dash.HELP.items():
        for text in filter(None, texts):
            assert dash.localize_help(text, terms) == text, name
    for text in [*dash.EMPTY_STATES.values(), *dash.MOBILE_EMPTY_STATES.values()]:
        assert dash.localize_retention(text, terms) == text
    blob = _blob(_build(REFERENCE))
    assert "saved history (up to 90 days)" in blob and "for the 7 AM daily check, sent every day." in blob
    assert _blob(_build(REFERENCE)) == _blob(_build({**REFERENCE, "retention": {}}))


# ------------------------------------------------------------------ 4: CLI

def test_version_prints_the_common_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        dash.main(["--version"])
    assert exit_info.value.code == 0
    assert taskdash_common.TASKDASH_VERSION in capsys.readouterr().out


def test_help_describes_target_title_and_config(capsys):
    with pytest.raises(SystemExit):
        dash.main(["--help"])
    out = capsys.readouterr().out
    assert "--config PATH" in out and "--version" in out
    assert "test (default) or prod" in out and "Dashboard title" in out


def test_config_path_is_passed_to_load_config(monkeypatch, tmp_path):
    seen = []

    def fake_load(path=None, **kwargs):
        seen.append(path)
        return {"arcgis": {}}

    monkeypatch.setattr(dash, "load_config", fake_load)
    target = tmp_path / "other.config.json"
    with pytest.raises(SystemExit, match="No arcgis"):
        dash.main(["--config", str(target)])
    with pytest.raises(SystemExit, match="No arcgis"):
        dash.main([])
    assert seen == [target, None]


# ------------------------------------------------------------------ 5: privacy defaults

def test_dashboard_reads_privacy_switches_only_through_the_publish_helper():
    source = Path(dash.__file__).read_text(encoding="utf-8")
    assert "view_hidden_fields" in source and 'get("privacy"' not in source


# EXPECTED TO FAIL until the publish.py change lands (a missing privacy switch must mean HIDDEN):
# the dashboard has no privacy logic of its own and takes its hidden fields from this helper.
def test_config_without_a_privacy_section_hides_the_fields():
    hidden = {f for fields in publish.view_hidden_fields({}).values() for f in fields}
    assert {"latest_log_path", "command_target_path", "command_target_working_directory"} <= hidden

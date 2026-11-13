"""Phase 2 deliverable 6: config keys for the literals that named one organization.

Each key defaults to the value the code used before, so an install without the keys is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
import taskdash_publish as publish  # noqa: E402


def _definition(config=None):
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return dash.build("TESTITEM", templates, config=config)


def test_alert_link_default_uses_the_default_portal_and_dashboard_id():
    link = publish.alert_dashboard_link({"arcgis": {"dashboard_id": "abc123"}})
    assert link == "https://www.arcgis.com/apps/dashboards/abc123"


def test_alert_link_derives_from_the_configured_portal_url():
    config = {"arcgis": {"url": "https://org.example.test/", "dashboard_id": "abc123"}}
    assert publish.alert_dashboard_link(config) == "https://org.example.test/apps/dashboards/abc123"


def test_alert_link_explicit_override_wins():
    config = {"arcgis": {"url": "https://org.example.test", "dashboard_id": "abc123"},
              "alerting": {"dashboard_url": "https://dash.example.test/x"}}
    assert publish.alert_dashboard_link(config) == "https://dash.example.test/x"


class _FakeContent:
    def __init__(self):
        self.kwargs = None

    def search(self, *args, **kwargs):
        return []

    def create_service(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(id="svc1")


def _create(monkeypatch, config):
    pytest.importorskip("arcgis.features")
    content = _FakeContent()
    gis = SimpleNamespace(content=content, users=SimpleNamespace(me=SimpleNamespace(username="u")))
    fake_flc = SimpleNamespace(manager=SimpleNamespace(add_to_definition=lambda d: None))
    import arcgis.features as features
    monkeypatch.setattr(features.FeatureLayerCollection, "fromitem", staticmethod(lambda item: fake_flc))
    publish.create_service(gis, "svc", config)
    return content.kwargs


def test_service_description_and_tags_default_to_the_previous_values(monkeypatch):
    kwargs = _create(monkeypatch, None)
    assert kwargs["service_description"] == "TaskDash - scheduled task monitoring"
    assert kwargs["create_params"]["serviceDescription"] == "TaskDash - scheduled task monitoring"
    assert kwargs["tags"] == "taskdash,monitoring,scheduled tasks"


def test_service_description_and_tags_overrides_take_effect(monkeypatch):
    config = {"arcgis": {"service_description": "Custom text", "service_tags": "a,b"}}
    kwargs = _create(monkeypatch, config)
    assert kwargs["service_description"] == kwargs["create_params"]["serviceDescription"] == "Custom text"
    assert kwargs["tags"] == "a,b"


class _FakeCon:
    def __init__(self):
        self.params = None

    def post(self, url, params):
        self.params = params
        return {"success": True, "id": "dash1"}


def _post(config):
    con = _FakeCon()
    gis = SimpleNamespace(_portal=SimpleNamespace(resturl="https://x.test/"), _con=con, url="https://x.test",
                          users=SimpleNamespace(me=SimpleNamespace(username="u")))
    dash.post_dashboard(gis, {}, "t", None, config)
    return con.params


def test_dashboard_tags_default_and_override():
    assert _post(None)["tags"] == "taskdash,monitoring"
    assert _post({"arcgis": {"dashboard_tags": "x,y"}})["tags"] == "x,y"


def _post_update(config):
    con = _FakeCon()
    gis = SimpleNamespace(_portal=SimpleNamespace(resturl="https://x.test/"), _con=con, url="https://x.test",
                          users=SimpleNamespace(me=SimpleNamespace(username="u")))
    dash.post_dashboard(gis, {}, "t", SimpleNamespace(owner="o", id="dash1"), config)
    return con.params


def test_dashboard_update_keeps_the_owners_snippet_and_tags_unless_tags_are_configured():
    created = _post(None)
    assert created["snippet"] and created["tags"] == "taskdash,monitoring"
    updated = _post_update(None)
    assert "snippet" not in updated and "tags" not in updated and updated["title"] == "t"
    assert _post_update({"arcgis": {"dashboard_tags": "x,y"}})["tags"] == "x,y"


def test_validator_passes_a_clean_definition_with_or_without_a_host_name():
    definition = _definition()
    dash.validate_definition(definition)
    dash.validate_definition(definition, config={"host_name_override": "SYNTH-HOST-01"})


def test_validator_rejects_the_configured_host_name_in_a_filter():
    definition = _definition()
    definition["desktopView"]["widgets"][0]["note"] = "SYNTH-HOST-01"
    with pytest.raises(SystemExit, match="hardcoded host filter"):
        dash.validate_definition(definition, config={"host_name_override": "SYNTH-HOST-01"})


def test_validator_rejects_a_server_name_filter_without_config():
    definition = _definition()
    definition["desktopView"]["widgets"][0]["where"] = {"server_name": "SYNTH-HOST-02"}
    with pytest.raises(SystemExit, match="hardcoded host filter"):
        dash.validate_definition(definition)


def test_help_change_delay_follows_the_collector_interval():
    text = "up to 1 hour after it happened"
    assert dash.localize_help(text, dash.help_terms({})) == text
    config = {"scheduler": {"collector_interval_minutes": 15}}
    assert dash.localize_help(text, dash.help_terms(config)) == "up to 15 min after it happened"


def test_new_keys_are_registered_with_the_config_validator():
    config = {"arcgis": {"service_tags": 5}, "alerting": {"dashboard_url": 7}}
    errors = taskdash_common._validate_config(config)
    assert any("arcgis.service_tags" in e for e in errors)
    assert any("alerting.dashboard_url" in e for e in errors)


def test_wrapper_keeps_the_pro_interpreter_as_default_and_honours_the_override():
    text = (Path(dash.__file__).parent / "run_taskdash.cmd").read_text(encoding="utf-8")
    default = text.index('set "PY=')
    override = text.index("if defined TASKDASH_PYTHON")
    assert default < override < text.index('"%PY%" taskdash_publish.py')


def test_the_demo_dashboard_summary_says_the_data_is_invented():
    con = _FakeCon()
    gis = SimpleNamespace(_portal=SimpleNamespace(resturl="https://x.test/"), _con=con, url="https://x.test",
                          users=SimpleNamespace(me=SimpleNamespace(username="u")))
    dash.post_dashboard(gis, {}, "t", None, None, demo=True)
    assert con.params["snippet"].startswith("DEMO - invented data.")
    assert _post(None)["snippet"] == "Scheduled task health and workflow metrics."

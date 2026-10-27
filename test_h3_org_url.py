"""H3-9: signin offers the ArcGIS Online URL, reads the organization's own URL after the sign-in, and
writes only arcgis.url (with a backup) under --apply. The setup refusal for an existing install says
what to do. Every ArcGIS call is a fake.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import taskdash_setup as setup
import taskdash_signin as signin
from test_h3_signin import Fakes, _config

ORG = SimpleNamespace(urlKey="acme1", customBaseUrl="maps.arcgis.com")
ORG_URL = "https://acme1.maps.arcgis.com"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run(tmp_path, capsys, *flags, **arcgis):
    path = _config(tmp_path, **arcgis)
    code = signin.main(["--config", str(path), *flags])
    return path, code, capsys.readouterr().out


def test_blank_url_offers_arcgis_online_and_enter_accepts_it(tmp_path, monkeypatch, capsys):
    fakes = Fakes(monkeypatch)
    _run(tmp_path, capsys, url="")
    assert "https://www.arcgis.com" in fakes.prompts[0]
    assert fakes.opened[0][0] == "https://www.arcgis.com"


def test_org_url_is_printed_and_the_config_is_untouched_without_apply(tmp_path, monkeypatch, capsys):
    Fakes(monkeypatch, props=ORG)
    path, code, out = _run(tmp_path, capsys, url="")
    assert code == 0 and f"Set arcgis.url to {ORG_URL}" in out and "--apply" in out
    assert _read(path)["arcgis"]["url"] == ""
    assert list(tmp_path.glob("*.bak_setup_*")) == []


def test_apply_writes_only_the_url_and_keeps_a_backup(tmp_path, monkeypatch, capsys):
    Fakes(monkeypatch, props=ORG)
    path = _config(tmp_path, url="")
    before = _read(path)
    assert signin.main(["--config", str(path), "--apply"]) == 0
    after = _read(path)
    assert after["arcgis"]["url"] == ORG_URL
    after["arcgis"]["url"] = ""
    assert after == before
    assert len(list(tmp_path.glob("TaskDash.config.json.bak_setup_*"))) == 1


def test_a_configured_url_is_never_rewritten(tmp_path, monkeypatch, capsys):
    Fakes(monkeypatch, props=ORG)
    path, code, out = _run(tmp_path, capsys, "--apply")
    assert code == 0 and _read(path)["arcgis"]["url"] == "https://org.example.com"
    assert "Set arcgis.url" not in out and list(tmp_path.glob("*.bak_setup_*")) == []


@pytest.mark.parametrize("props", [None, SimpleNamespace(urlKey="acme1"), SimpleNamespace(customBaseUrl="x.com")])
def test_without_both_org_properties_the_typed_url_is_printed(tmp_path, monkeypatch, capsys, props):
    Fakes(monkeypatch, props=props)
    path, code, out = _run(tmp_path, capsys, url="")
    assert code == 0 and "Set arcgis.url to https://www.arcgis.com" in out


def test_the_refusal_names_the_commands_for_both_install_forms():
    with pytest.raises(SystemExit) as exc:
        setup.refuse_existing_install({"arcgis": {"item_id": "x"}})
    text = str(exc.value)
    for part in ("arcgis.item_id", "taskdash collect --migrate --apply", "taskdash dashboard --target prod",
                 "folder install", "python taskdash_publish.py --migrate", "python taskdash_dashboard.py",
                 "To start again", "TROUBLESHOOTING.md"):
        assert part in text

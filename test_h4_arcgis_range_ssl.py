"""H4-9: SSLError names the fix, and an untested arcgis version raises one WARNING."""
from __future__ import annotations

import sys
import types

import pytest
import requests

import taskdash_common as common
import taskdash_preflight as preflight


@pytest.mark.parametrize("version,tested", [
    ("2.4.0", True), ("2.4.3", True), ("2.9.1", True), ("2.4.0.post1", True),
    ("2.3.9", False), ("3.0.0", False), ("3.1", False), ("junk", False),
])
def test_arcgis_version_range(monkeypatch, version, tested):
    monkeypatch.setattr(common.importlib.metadata, "version", lambda name: version)
    warning = common.arcgis_version_warning()
    assert (warning is None) == tested
    if warning:
        assert warning.startswith("WARNING:") and ">=2.4,<3" in warning and "\n" not in warning


def test_arcgis_missing_is_not_a_warning(monkeypatch):
    def missing(name):
        raise common.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(common.importlib.metadata, "version", missing)
    assert common.arcgis_version_warning() is None


def test_version_check_never_imports_arcgis(monkeypatch):
    """Importing arcgis before the ArcPy guard is installed dies without a Pro licence."""
    monkeypatch.delitem(sys.modules, "arcgis", raising=False)
    monkeypatch.setattr(common.importlib.metadata, "version", lambda name: "2.4.3")
    common.arcgis_version_warning()
    assert "arcgis" not in sys.modules


def test_preflight_check_warns(monkeypatch):
    monkeypatch.setattr(common.importlib.metadata, "version", lambda name: "3.0.1")
    status, message = preflight.check_arcgis_version(None)
    assert status == preflight.WARN and "3.0.1" in message
    monkeypatch.setattr(common.importlib.metadata, "version", lambda name: "2.4.3")
    assert preflight.check_arcgis_version(None)[0] == preflight.OK


def test_ssl_error_on_a_rest_call_names_the_fix():
    def query():
        raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

    with pytest.raises(SystemExit) as info:
        common.with_retry(query, label="TaskRuns query", attempts=2, sleep=lambda s: None)
    text = str(info.value.code)
    assert "SSLError" in text and "REQUESTS_CA_BUNDLE" in text and "root certificate" in text
    assert "TaskRuns query" in text


def test_ssl_error_is_named(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

    fake_gis = types.ModuleType("arcgis.gis")
    fake_gis.GIS = boom
    monkeypatch.setitem(sys.modules, "arcgis.gis", fake_gis)
    with pytest.raises(SystemExit) as info:
        common._open_gis("https://example.invalid")
    text = str(info.value.code)
    assert "SSLError" in text and "REQUESTS_CA_BUNDLE" in text and "root certificate" in text
    assert "TaskDash cannot reach ArcGIS" not in text or "TLS" in text

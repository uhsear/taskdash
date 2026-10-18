"""Shared pytest setup for the TaskDash suite.

Every test runs on a frozen clock, except in taskdash_metrics. Tests build their rows around
fixed dates, and the code judges 24-hour, 7-day and 30-day windows against utc_now(). On the
real clock those windows drift past the fixed dates, and a test that passed last week fails today.

taskdash_metrics keeps the real clock on purpose. Its tests write real log files, so file
mtimes are real, and they age files with os.utime relative to now. A frozen clock there would
drift away from those mtimes and give negative log ages.
"""

from __future__ import annotations

import ctypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import taskdash_collect
import taskdash_common
import taskdash_metrics
import taskdash_publish

# Matches test_tables_hardening.NOW, the date most window tests are built around. A test file
# with its own NOW still patches utc_now itself; that patch runs after this one and wins.
FROZEN_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_clock: run this test on the real clock, not on conftest.FROZEN_NOW")


def _reload_zone():
    # datetime.astimezone() reads the C runtime zone, not time.timezone, so a monkeypatch of the
    # time module cannot move it. Windows has no time.tzset; the CRT's _tzset re-reads TZ.
    if hasattr(time, "tzset"):
        time.tzset()
    else:
        ctypes.CDLL("ucrtbase")._tzset()


@pytest.fixture
def local_zone():
    """Call local_zone("EST5") to make naive datetimes and astimezone() use that POSIX TZ string
    (UTC0, CET-1 = UTC+1, EST5 = UTC-5), so a zone-sensitive test runs the same on any machine."""
    saved = os.environ.get("TZ")

    def pin(tz):
        os.environ["TZ"] = tz
        _reload_zone()
    yield pin
    # Restore TZ before reloading, so later tests see the machine zone again.
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    _reload_zone()


@pytest.fixture(autouse=True)
def frozen_clock(request, monkeypatch):
    if request.node.get_closest_marker("real_clock"):
        return
    # publish and collect import utc_now by name, so each module's own binding is patched.
    # taskdash_common's functions look it up in their module globals, so one patch covers them.
    for module in (taskdash_common, taskdash_publish, taskdash_collect):
        monkeypatch.setattr(module, "utc_now", lambda: FROZEN_NOW)


# The suite characterises the reference install: rules/default.json plus its profile. The profile
# here is a synthetic copy of the reference install's own one, rule for rule, with every real name
# replaced, so the public suite holds no county data. Applied before every test, because load_config
# (and a test of the generic rules) switches the module tables to whatever its config selects.
REFERENCE_PROFILES = ("tests/fixtures/reference_profile.json",)
assert (Path(__file__).parent / REFERENCE_PROFILES[0]).is_file(), f"missing {REFERENCE_PROFILES[0]}"


@pytest.fixture(autouse=True)
def reference_rules():
    taskdash_metrics.configure_rules({"rules": {"profiles": list(REFERENCE_PROFILES)}})

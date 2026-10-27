"""H4-6: the rules setting decimal_separator. Nothing touches the network or real logs."""

from __future__ import annotations

import json

import pytest

import taskdash_metrics as metrics


def _load(tmp_path, value):
    profile = tmp_path / "decimal.json"
    profile.write_text(json.dumps({"decimal_separator": value}), encoding="utf-8")
    metrics.configure_rules({"rules": {"profiles": [str(profile)]}})


@pytest.fixture(autouse=True)
def _reset_rules():
    yield
    metrics.configure_rules({})


def _values(line):
    return dict(metrics.extract_metrics_from_line(line))


def test_default_reads_comma_as_thousands():
    assert _values("Rows loaded: 1,234.5")["rows_loaded"] == 1234.5
    assert _values("Rows loaded: 1,5")["rows_loaded"] == 15.0


@pytest.mark.parametrize("text, expected", [
    ("1,5", 1.5), ("1.234", 1234.0), ("1.234,5", 1234.5), ("12", 12.0), ("1.234.567", 1234567.0),
])
def test_comma_profile_reads_comma_as_decimal(tmp_path, text, expected):
    _load(tmp_path, ",")
    assert _values(f"Rows loaded: {text}")["rows_loaded"] == expected


@pytest.mark.parametrize("text, expected", [
    ("1.5", 1.5), ("0.512", 0.512), ("12.75", 12.75), ("1.2345", 1.2345),
])
def test_comma_profile_does_not_read_a_dotted_decimal_as_thousands(tmp_path, text, expected):
    # A "." groups thousands only when every group after it has exactly three digits and the
    # number does not start with 0. Anything else is a plain dot decimal, not a factor-of-ten error.
    _load(tmp_path, ",")
    assert _values(f"Rows loaded: {text}").get("rows_loaded") == expected


def test_comma_profile_keeps_a_trailing_comma(tmp_path):
    _load(tmp_path, ",")
    assert _values("Rows loaded: 5, done")["rows_loaded"] == 5.0


def test_taskdash_lines_keep_their_documented_dot_decimal_format_under_any_profile(tmp_path):
    line = "TASKDASH" + " rows_updated=3.5 skipped=1,234 ratio=0.512"
    expected = [("rows_updated", 3.5), ("skipped", 1234.0), ("ratio", 0.512)]
    assert metrics.extract_taskdash_line(line) == expected
    _load(tmp_path, ",")
    assert metrics.extract_taskdash_line(line) == expected


def test_comma_profile_reads_a_comma_duration(tmp_path):
    _load(tmp_path, ",")
    assert _values("Process completed in 71,6s")["duration_seconds"] == pytest.approx(71.6)


def test_dot_profile_is_the_default(tmp_path):
    _load(tmp_path, ".")
    assert _values("Rows loaded: 1,234.5")["rows_loaded"] == 1234.5


@pytest.mark.parametrize("bad", [";", "", "comma", None, 1])
def test_other_values_are_a_rules_error(tmp_path, bad):
    with pytest.raises(SystemExit, match="decimal_separator"):
        _load(tmp_path, bad)

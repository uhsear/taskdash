"""H4-7: a corrupt alert_state.json is kept aside and warned about; read_last_lines caps at 8 MB."""
import json
import logging

import pytest

import taskdash_metrics as metrics
import taskdash_publish as pub


def test_corrupt_state_is_renamed_and_warned(tmp_path, caplog):
    state = tmp_path / "alert_state.json"
    state.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert pub._read_alert_state(state) == {}
    assert not state.exists()
    kept = list(tmp_path.glob("alert_state.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{not json"
    text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "alert_state.json" in text and "rebuilds" in text and "repeat" in text


def test_wrong_shape_state_is_renamed(tmp_path):
    state = tmp_path / "alert_state.json"
    state.write_text("[1, 2]", encoding="utf-8")
    assert pub._read_alert_state(state) == {}
    assert len(list(tmp_path.glob("alert_state.json.corrupt-*"))) == 1


@pytest.mark.parametrize("text", [
    '{"problem_first_seen_utc": "x"}',
    '{"problem_last_alert_utc": ["a"]}',
    '{"signature": "FAILED: a"}',
    '{"digest_pending": {"a": 1}}',
    '{"signature": [1, 2]}',
    '{"incident_causes": 3}',
])
def test_a_field_of_the_wrong_type_is_renamed_and_cannot_crash_alerting(tmp_path, text):
    state = tmp_path / "alert_state.json"
    state.write_text(text, encoding="utf-8")
    assert pub._read_alert_state(state) == {}
    assert not state.exists()
    assert len(list(tmp_path.glob("alert_state.json.corrupt-*"))) == 1


def test_good_and_missing_state_are_untouched(tmp_path):
    state = tmp_path / "alert_state.json"
    assert pub._read_alert_state(state) == {}
    good = {"signature": ["FAILED: a"], "digest_pending": [], "problem_first_seen_utc": {"FAILED: a": "t"},
            "problem_last_alert_utc": {}, "last_heartbeat_utc": "t"}
    state.write_text(json.dumps(good), encoding="utf-8")
    assert pub._read_alert_state(state) == good
    assert state.exists() and not list(tmp_path.glob("*.corrupt-*"))


def test_preview_read_does_not_rename(tmp_path):
    state = tmp_path / "alert_state.json"
    state.write_text("{bad", encoding="utf-8")
    assert pub._read_alert_state(state, quarantine=False) == {}
    assert state.exists()


def test_read_last_lines_default_cap_is_8_mb():
    assert metrics.MAX_TAIL_BYTES == 8 * 1024 * 1024
    assert metrics.read_last_lines.__defaults__[-1] == 8 * 1024 * 1024


def test_read_last_lines_default_cap_limits_huge_file(tmp_path):
    log = tmp_path / "big.log"
    log.write_bytes(b"x" * (9 * 1024 * 1024) + b"\nlast\n")
    assert metrics.read_last_lines(log, max_lines=0) == ["last"]

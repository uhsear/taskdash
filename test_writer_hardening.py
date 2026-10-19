"""Regression tests for the taskdash_common.py AGOL-writer hardening pass (plan items 2.4-2.8,
ops-11).

Each test targets one plan item and fails against the pre-hardening implementation. Uses fakes
in place of real arcgis objects -- no network writes here; the lead verifies live after this
passes. Matches test_taskdash.py / test_metrics_hardening.py style.
"""

from __future__ import annotations

import json

import pytest

import taskdash_common as common
import taskdash_publish as publish
from conftest import REFERENCE_PROFILES


# ---------------------------------------------------------------- 2.4 with_retry

class _FlakyCall:
    """Raises the given exceptions in order, then returns a fixed value."""

    def __init__(self, *exceptions, result="ok"):
        self.exceptions = list(exceptions)
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.exceptions:
            raise self.exceptions.pop(0)
        return self.result


def _no_sleep(seconds):
    pass


def test_with_retry_retries_a_503_and_succeeds():
    flaky = _FlakyCall(Exception("Http Error: 503 Server Error: Service Unavailable "
                                  "for url: https://x (Error Code: 503)"))
    result = common.with_retry(flaky, label="t", sleep=_no_sleep)
    assert result == "ok"
    assert flaky.calls == 2


def test_with_retry_does_not_retry_a_400():
    flaky = _FlakyCall(Exception("Invalid field 'foo' (Error Code: 400)"))
    with pytest.raises(Exception, match="400"):
        common.with_retry(flaky, label="t", sleep=_no_sleep, attempts=4)
    assert flaky.calls == 1, "a non-retryable 400 must fail on the first attempt"


def test_with_retry_raises_systemexit_after_exhausting_attempts():
    flaky = _FlakyCall(*(Exception("(Error Code: 500)") for _ in range(5)))
    with pytest.raises(SystemExit):
        common.with_retry(flaky, label="t", sleep=_no_sleep, attempts=3)
    assert flaky.calls == 3


def test_with_retry_honours_retry_after_header():
    class _Resp:
        status_code = 429
        headers = {"Retry-After": "7"}

    class _RateLimited(Exception):
        response = _Resp()

    flaky = _FlakyCall(_RateLimited("rate limited"))
    delays = []
    result = common.with_retry(flaky, label="t", sleep=delays.append)
    assert result == "ok"
    assert delays[0] == 7.0


def test_with_retry_calls_before_retry_hook_once_per_failure():
    flaky = _FlakyCall(Exception("(Error Code: 502)"), Exception("(Error Code: 502)"))
    hook_calls = []
    common.with_retry(flaky, label="t", sleep=_no_sleep, before_retry=lambda: hook_calls.append(1))
    assert len(hook_calls) == 2


def test_with_retry_retries_a_connection_error():
    flaky = _FlakyCall(ConnectionError("reset"))
    result = common.with_retry(flaky, label="t", sleep=_no_sleep)
    assert result == "ok"


# ---------------------------------------------------------------- 2.4 adds dedupe-before-retry hook

class _AmbiguousAddsTable:
    """First edit_features(adds=...) call raises a transient error; the second must be called
    with whatever dedupe_adds returned, not the original chunk."""

    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.calls = []

    def edit_features(self, **kw):
        self.calls.append(kw)
        if len(self.calls) <= self.fail_times:
            raise Exception("(Error Code: 503)")
        adds = kw.get("adds") or []
        return {"addResults": [{"success": True}] * len(adds)}


def test_apply_edits_chunked_dedupes_adds_before_a_retry(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    table = _AmbiguousAddsTable(fail_times=1)
    rows = [{"run_key": "a"}, {"run_key": "b"}]

    def dedupe(rows):
        # Simulate a re-query finding that "a" already landed from the ambiguous first attempt.
        return [r for r in rows if r["run_key"] != "a"]

    counts = common.apply_edits_chunked(table, adds=rows, label="T", dedupe_adds=dedupe)
    assert counts["added"] == 2
    second_call_keys = [f["attributes"]["run_key"] for f in table.calls[1]["adds"]]
    assert second_call_keys == ["b"], "the retry must not resend the row dedupe_adds dropped"


def test_apply_edits_chunked_retries_updates_without_calling_dedupe_adds(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    class _Table:
        def __init__(self):
            self.calls = []

        def edit_features(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                raise Exception("(Error Code: 503)")
            updates = kw.get("updates") or []
            return {"updateResults": [{"success": True}] * len(updates)}

    table = _Table()
    hook_called = []
    counts = common.apply_edits_chunked(
        table, updates=[{"OBJECTID": 1, "x": 2}], label="T",
        dedupe_adds=lambda rows: hook_called.append(rows) or rows)
    assert counts["updated"] == 1
    assert hook_called == [], "dedupe_adds must only run for adds retries, never updates"


# ---------------------------------------------------------------- 2.7a paging + object_id_field

class _Props(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


class _FakeTable:
    """A table whose server enforces maxRecordCount, independent of what a caller requests."""

    def __init__(self, rows, max_record_count=2, object_id_field="OBJECTID"):
        self.rows = rows
        self.properties = _Props(maxRecordCount=max_record_count, objectIdField=object_id_field)
        self.offsets_seen = []

    def query(self, *, where="1=1", out_fields="*", return_geometry=False,
              result_offset=0, result_record_count=None, **kw):
        self.offsets_seen.append(result_offset)
        page = self.rows[result_offset:result_offset + self.properties["maxRecordCount"]]

        class _Feature:
            def __init__(self, attrs):
                self.attributes = attrs

        class _Result:
            features = [_Feature(r) for r in page]

        return _Result()


def test_query_all_rows_pages_at_the_servers_max_record_count_not_a_hardcoded_guess():
    """Server maxRecordCount (2) is below the old hardcoded page (2000): the old code would
    request result_record_count=2000, get back only 2 rows on the first call, see
    len(features) < 2000, and stop -- silently dropping every row past the second."""
    rows = [{"OBJECTID": i, "run_key": f"k{i}"} for i in range(1, 6)]
    table = _FakeTable(rows, max_record_count=2)
    result = common.query_all_rows(table)
    assert len(result) == 5
    assert table.offsets_seen == [0, 2, 4]


def test_query_all_rows_stops_on_a_short_final_page():
    rows = [{"OBJECTID": 1}]
    table = _FakeTable(rows, max_record_count=2000)
    assert common.query_all_rows(table) == rows


def test_query_all_rows_orders_every_page_by_the_oid():
    """Without order_by_fields, arcgis 2.4 fetches a short last page on 5 threads, which
    trips the truststore 0.10.0 race that turns certificate checks off."""
    table = _FakeTable([{"OID": i} for i in range(1, 6)], max_record_count=2, object_id_field="OID")
    seen = []
    original = table.query
    table.query = lambda **kw: (seen.append(kw.get("order_by_fields")), original(**kw))[1]
    assert len(common.query_all_rows(table)) == 5
    assert seen == ["OID", "OID", "OID"]


def test_object_id_field_reads_the_tables_property():
    table = _FakeTable([], object_id_field="OID")
    assert common.object_id_field(table) == "OID"


def test_object_id_field_falls_back_when_missing():
    assert common.object_id_field(object()) == "OBJECTID"


# ---------------------------------------------------------------- 2.8 config validation

def test_load_config_accepts_a_utf8_bom(tmp_path):
    path = tmp_path / "c.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"host_name_override": "H", "rules": {"profiles": list(REFERENCE_PROFILES)}}).encode("utf-8"))
    assert common.load_config(path)["host_name_override"] == "H"


def test_load_config_rejects_a_string_where_a_list_is_required(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"alerting": {"to": "someone@example.com"}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="alerting.to"):
        common.load_config(path)


def test_load_config_collects_every_problem_in_one_pass(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"alerting": {"to": "x", "enabled": "yes"},
                                "powershell_timeout_seconds": "180"}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        common.load_config(path)
    message = str(exc.value)
    assert "alerting.to" in message
    assert "alerting.enabled" in message
    assert "powershell_timeout_seconds" in message


def test_load_config_warns_but_does_not_fail_on_an_unknown_top_level_key(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"totally_new_section": {"x": 1},
                                "rules": {"profiles": list(REFERENCE_PROFILES)}}), encoding="utf-8")
    warnings = []
    monkeypatch.setattr(common, "emit", lambda msg: warnings.append(msg))
    config = common.load_config(path)
    assert config["totally_new_section"] == {"x": 1}
    assert any("totally_new_section" in w for w in warnings)


def test_load_config_redacts_a_bad_password_value(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"arcgis": {"password": 12345}}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        common.load_config(path)
    assert "12345" not in str(exc.value)
    assert "redacted" in str(exc.value)


# ---------------------------------------------------------------- 2.5 run lock

def test_run_lock_blocks_a_second_instance(tmp_path):
    with common.run_lock(tmp_path):
        with pytest.raises(common.RunLockHeld):
            with common.run_lock(tmp_path):
                pass


def test_run_lock_releases_on_exception(tmp_path):
    with pytest.raises(ValueError):
        with common.run_lock(tmp_path):
            raise ValueError("boom")
    with common.run_lock(tmp_path):
        pass  # lock must be free again


# ---------------------------------------------------------------- 2.6 atomic JSON write

def test_atomic_write_json_leaves_no_temp_file_on_success(tmp_path):
    path = tmp_path / "alert_state.json"
    common.atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.glob(".*")) == []


def test_atomic_write_json_never_leaves_a_truncated_file(tmp_path, monkeypatch):
    path = tmp_path / "alert_state.json"
    path.write_text(json.dumps({"prior": True}), encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(common.os, "replace", _boom)
    with pytest.raises(OSError):
        common.atomic_write_json(path, {"new": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"prior": True}
    assert list(tmp_path.glob(".*tmp*")) == []


def test_atomic_write_json_retries_a_briefly_locked_target(tmp_path, monkeypatch):
    path = tmp_path / "alert_state.json"
    real_replace, calls = common.os.replace, []

    def _locked_twice(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(common.os, "replace", _locked_twice)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    common.atomic_write_json(path, {"new": True})
    assert len(calls) == 3 and json.loads(path.read_text(encoding="utf-8")) == {"new": True}


def test_atomic_write_json_gives_up_on_a_target_that_stays_locked(tmp_path, monkeypatch):
    path = tmp_path / "alert_state.json"

    def _locked(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(common.os, "replace", _locked)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        common.atomic_write_json(path, {"new": True})
    assert list(tmp_path.glob(".*tmp*")) == []


# ---------------------------------------------------------------- ops-11 cleanup_old_logs scope

def test_cleanup_old_logs_only_touches_taskdash_log_names(tmp_path):
    old = tmp_path / "taskdash_20200101_000000.log"
    old.write_text("x", encoding="utf-8")
    boot = tmp_path / "bootstrap_FAIL_20200101_000000.log"
    boot.write_text("x", encoding="utf-8")
    other = tmp_path / "some_other_service.log"
    other.write_text("x", encoding="utf-8")
    import os
    import time
    old_time = time.time() - 100 * 86400
    os.utime(old, (old_time, old_time))
    os.utime(boot, (old_time, old_time))
    os.utime(other, (old_time, old_time))

    common.cleanup_old_logs(tmp_path, max_age_days=30)

    assert not old.exists()
    assert not boot.exists()
    assert other.exists(), "a non-TaskDash *.log in a shared log_dir must survive cleanup"


# ================================================================== taskdash_publish.py
# Regression tests for the writer-hardening pass on taskdash_publish.py (plan items
# 2.1-2.3, 2.5-2.7b, and the read_run_events budget-setting message). test_taskdash.py
# keeps the sync_inventory (2.1) guard tests inline with its other publish.py coverage;
# everything else new lives here.

class _FieldProp:
    def __init__(self, name, type_):
        self.name = name
        self.type = type_


class _FakeManager:
    def __init__(self, calls):
        self.calls = calls

    def add_to_definition(self, definition):
        self.calls.append(definition)


class _SchemaTable:
    """A live table stand-in for schema-drift / migration tests: fields + indexes on
    .properties (attribute access, like the real arcgis PropertyMap), edits recorded on
    .manager.calls."""

    def __init__(self, field_names, indexes=(), object_id_field="OBJECTID"):
        self.properties = _Props(
            fields=[_FieldProp(n, "esriFieldTypeString") for n in field_names],
            # Listed indexes are unique, as --migrate creates them; a unique index also
            # satisfies a non-unique spec on the same fields.
            indexes=[{"fields": ",".join(f), "isUnique": True} for f in indexes],
            objectIdField=object_id_field,
        )
        self.manager = _FakeManager([])


# ---------------------------------------------------------------- 2.2 schema drift

def test_diff_schema_finds_a_missing_field_case_insensitively():
    table = _SchemaTable(["OBJECTID", "HOST_NAME", "task_full_name"])
    expected = [
        common.make_field("OBJECTID", "esriFieldTypeOID", "OBJECTID"),
        common.make_field("host_name", "esriFieldTypeString", "Host"),
        common.make_field("needs_attention", "esriFieldTypeInteger", "Needs Attention"),
    ]
    missing = publish.diff_schema(table, expected)
    assert [f["name"] for f in missing] == ["needs_attention"]


def test_check_schema_drift_raises_naming_the_missing_fields_and_migrate(monkeypatch):
    table = _SchemaTable(["OBJECTID", "task_full_name"])
    monkeypatch.setattr(publish, "TABLE_SPECS",
                        {publish.TABLE_RUNS: publish.TABLE_SPECS[publish.TABLE_RUNS]})
    with pytest.raises(SystemExit) as exc:
        publish.check_schema_drift({publish.TABLE_RUNS: table})
    message = str(exc.value)
    assert publish.TABLE_RUNS in message
    assert "run_key" in message
    assert "--migrate" in message


def test_check_schema_drift_passes_when_nothing_is_missing():
    field_names = [f["name"] for f in publish.runs_fields()]
    table = _SchemaTable(field_names, indexes=[("run_key",)])
    publish.check_schema_drift({publish.TABLE_RUNS: table})  # must not raise


def test_check_schema_drift_raises_when_the_unique_index_is_missing():
    """Without the unique index, a resent write after a transient failure (a retried POST that
    already committed) duplicates a row instead of failing loudly."""
    field_names = [f["name"] for f in publish.runs_fields()]
    table = _SchemaTable(field_names, indexes=[])
    with pytest.raises(SystemExit) as exc:
        publish.check_schema_drift({publish.TABLE_RUNS: table})
    message = str(exc.value)
    assert "run_key" in message
    assert "--migrate" in message


def test_check_schema_drift_warns_on_a_type_mismatch_but_does_not_raise(monkeypatch):
    field_names = [f["name"] for f in publish.runs_fields()]
    table = _SchemaTable(field_names, indexes=[("run_key",), ("start_time_utc",), ("is_error",)])
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    publish.check_schema_drift({publish.TABLE_RUNS: table})  # must not raise
    assert any("start_time_utc" in m and "esriFieldTypeDate" in m for m in messages)


# ---------------------------------------------------------------- 2.3 indexes

def test_missing_indexes_skips_an_index_already_covering_the_same_field_set():
    table = _SchemaTable([], indexes=[("run_key",)])
    expected = [{"fields": ["run_key"], "unique": True},
               {"fields": ["is_error"], "unique": False}]
    missing = publish.missing_indexes(table, expected)
    assert [spec["fields"] for spec in missing] == [["is_error"]]


def test_find_duplicate_keys_reports_repeated_key_combinations():
    class _Feature:
        def __init__(self, attrs):
            self.attributes = attrs

    class _Result:
        features = [_Feature({"run_key": "H|a", "n": 3}), _Feature({"run_key": "H|b", "n": 1})]

    class _Table:
        def query(self, **kw):
            assert kw["group_by_fields_for_statistics"] == "run_key"
            return _Result()

    assert publish.find_duplicate_keys(_Table(), ["run_key"]) == [("H|a",)]


def test_describe_migration_reports_without_writing(monkeypatch):
    table = _SchemaTable(["OBJECTID"])
    tables = {name: table for name in
             (publish.TABLE_INVENTORY, publish.TABLE_RUNS, publish.TABLE_METRICS)}
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    changed = publish.describe_migration(tables)
    assert changed is True
    assert table.manager.calls == []
    assert any("would add" in m for m in messages)


def test_apply_migration_adds_missing_fields_then_indexes(monkeypatch):
    table = _SchemaTable(["OBJECTID", "run_key"], indexes=[])
    monkeypatch.setattr(publish, "find_duplicate_keys", lambda *a, **k: [])
    publish.apply_migration({publish.TABLE_RUNS: table})
    calls = table.manager.calls
    assert "fields" in calls[0], "fields must be added before any index"
    index_calls = [c["indexes"][0] for c in calls if "indexes" in c]
    assert {"run_key", "start_time_utc", "is_error"} <= {c["fields"] for c in index_calls}
    unique_call = next(c for c in index_calls if c["isUnique"])
    assert unique_call["fields"] == "run_key"


def test_apply_migration_refuses_a_unique_index_over_existing_duplicates(monkeypatch):
    field_names = [f["name"] for f in publish.runs_fields()]
    table = _SchemaTable(field_names, indexes=[])
    monkeypatch.setattr(publish, "find_duplicate_keys", lambda *a, **k: [("H|dup",)])
    with pytest.raises(SystemExit, match="duplicate"):
        publish.apply_migration({publish.TABLE_RUNS: table})
    assert not any("indexes" in c for c in table.manager.calls), \
        "must refuse before calling add_to_definition for that index"


# ---------------------------------------------------------------- 2.7b upsert_by_key

def test_upsert_by_key_queries_only_the_incoming_keys_not_the_whole_table(monkeypatch):
    queries = []

    class _Table:
        properties = _Props(objectIdField="OID")

        def edit_features(self, **kw):
            return {"addResults": [{"success": True}] * len(kw.get("adds") or []),
                   "updateResults": [{"success": True}] * len(kw.get("updates") or [])}

    def fake_query_all_rows(table, *, where="1=1", out_fields="*", page=None):
        queries.append(where)
        return []

    monkeypatch.setattr(publish, "query_all_rows", fake_query_all_rows)
    publish.upsert_by_key(_Table(), [{"run_key": "a"}, {"run_key": "b"}], ["run_key"],
                          chunk_size=200, label="TaskRuns")
    assert queries == ["run_key IN ('a','b')"]


def test_upsert_by_key_uses_the_tables_real_oid_field(monkeypatch):
    class _Table:
        properties = _Props(objectIdField="OID")

        def __init__(self):
            self.calls = []

        def edit_features(self, **kw):
            self.calls.append(kw)
            return {"updateResults": [{"success": True}] * len(kw.get("updates") or [])}

    monkeypatch.setattr(publish, "query_all_rows",
                        lambda table, *, where="1=1", out_fields="*", page=None:
                        [{"OID": 5, "run_key": "a"}])
    table = _Table()
    publish.upsert_by_key(table, [{"run_key": "a"}], ["run_key"], chunk_size=200, label="TaskRuns")
    assert table.calls[0]["updates"][0]["attributes"]["OID"] == 5


def test_upsert_by_key_requeries_before_resending_an_ambiguous_add(monkeypatch):
    monkeypatch.setattr(common.time, "sleep", lambda s: None)

    class _Table:
        properties = _Props(objectIdField="OBJECTID")

        def __init__(self):
            self.calls = []

        def edit_features(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                raise Exception("(Error Code: 503)")
            adds = kw.get("adds") or []
            return {"addResults": [{"success": True}] * len(adds)}

    query_calls = []

    def fake_query_all_rows(table, *, where="1=1", out_fields="*", page=None):
        query_calls.append(where)
        if len(query_calls) == 1:
            return []  # nothing stored yet when the chunk's index is first built
        return [{"OBJECTID": 9, "run_key": "a"}]  # re-query after the failure: "a" landed

    monkeypatch.setattr(publish, "query_all_rows", fake_query_all_rows)
    table = _Table()
    counts = publish.upsert_by_key(table, [{"run_key": "a"}, {"run_key": "b"}], ["run_key"],
                                   chunk_size=200, label="TaskRuns")
    assert counts["added"] == 2
    second_call_keys = [f["attributes"]["run_key"] for f in table.calls[1]["adds"]]
    assert second_call_keys == ["b"], "must not resend the row the re-query found already landed"


# ---------------------------------------------------------------- 2.5 run lock in main()

def test_main_apply_returns_0_without_publishing_when_the_lock_is_held(tmp_path, monkeypatch):
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)},
                                       "rules": {"profiles": list(REFERENCE_PROFILES)}}), encoding="utf-8")
    monkeypatch.setattr(publish, "setup_logging", lambda log_dir, prefix: tmp_path / "x.log")
    monkeypatch.setattr(publish, "cleanup_old_logs", lambda *a: None)
    ran = []
    monkeypatch.setattr(publish, "run_publish", lambda *a, **k: ran.append(1) or 0)
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))

    with common.run_lock(tmp_path):
        result = publish.main(["--apply", "--config", str(config_path)])

    assert result == 0
    assert ran == [], "run_publish must never run while another instance holds the lock"
    assert any("progress" in m.lower() for m in messages)


def test_run_publish_apply_refuses_a_table_missing_a_field(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service",
                        lambda gis, cfg, tgt: type("I", (), {"title": "x", "id": tgt})())
    monkeypatch.setattr(publish, "get_tables", lambda item: {"TaskRuns": object()})
    monkeypatch.setattr(publish, "check_schema_drift",
                        lambda tables: (_ for _ in ()).throw(SystemExit("Schema drift -- TaskRuns: run_key")))
    # The drift refusal is deferred, so the run reaches collection; keep it off the real host.
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: {"host_name": "H", "inventory": [], "runs": [],
                                         "metrics": [], "log_summary": {}})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: None)
    monkeypatch.setattr(publish, "publish",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))
    config = {"host_name_override": socket.gethostname()}
    with pytest.raises(SystemExit, match="Schema drift"):
        publish.run_publish(publish.parse_args(["--apply"]), config, tmp_path)


# ---------------------------------------------------------------- 2.6 atomic alert-state write

def test_maybe_send_alert_persists_state_through_atomic_write_json(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "load_mail_settings",
                        lambda cfg: {"smtp server": "x", "from address": "a@b"})
    monkeypatch.setattr(common, "send_email", lambda *a, **k: True)
    atomic_calls = []
    monkeypatch.setattr(publish, "atomic_write_json",
                        lambda path, data: atomic_calls.append((path, data)))
    config = {"alerting": {"enabled": True, "to": ["x@example.com"]}}
    collected = {"inventory": [{"last_run_failed": 1, "task_name": "T", "last_result_text": "boom"}],
                "host_name": "H"}

    publish.maybe_send_alert(collected, config, tmp_path)

    assert atomic_calls, "alert state must be persisted through atomic_write_json"
    assert atomic_calls[0][0] == tmp_path / "alert_state.json"


# ---------------------------------------------------------------- read_run_events budget message

def test_read_run_events_names_max_events_per_run_when_that_is_the_budget_used(monkeypatch):
    monkeypatch.setattr(publish.collect, "collect_run_events",
                        lambda days, budget, timeout, min_record_id=0: [{}] * budget)
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    publish.read_run_events({"max_events_per_run": 3}, 60, 0, 0)  # max_events=0: a dry run
    warning = next(m for m in messages if "WARNING" in m)
    assert "history.max_events_per_run" in warning
    assert "history.first_run_max_events" not in warning


def test_read_run_events_names_first_run_max_events_when_that_is_the_budget_used(monkeypatch):
    monkeypatch.setattr(publish.collect, "collect_run_events",
                        lambda days, budget, timeout, min_record_id=0: [{}] * budget)
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    publish.read_run_events({"max_events_per_run": 3}, 60, 0, 30000)
    warning = next(m for m in messages if "WARNING" in m)
    assert "history.first_run_max_events" in warning


# ================================================================== review-round fixes

# ---------------------------------------------------------------- with_retry reads a JSON-body error code

def test_with_retry_retries_a_query_valueerror_with_a_dict_payload():
    """Query._process_query_result raises ValueError(result) with the raw error dict, not a
    formatted "(Error Code: N)" string -- neither regex the retry classifier used to have
    matches that, so a query failure (query_all_rows, max_field_value, fetch_keys...) was never
    retried."""
    flaky = _FlakyCall(ValueError({"error": {"code": 503, "message": "Service unavailable"}}))
    result = common.with_retry(flaky, label="t", sleep=_no_sleep)
    assert result == "ok"
    assert flaky.calls == 2


# ---------------------------------------------------------------- sync_inventory retry safety + dup healing

def _sync_inventory_table(existing, object_id_field="OBJECTID", fail_first=False):
    """existing: list of (task_full_name, OBJECTID) -- a duplicate is seeded by repeating a
    task_full_name under two OBJECTIDs. fail_first simulates an ambiguous failure: the add
    actually lands (mutating .rows) before the exception is raised on the first call."""
    import types

    class FakeTable:
        def __init__(self):
            self.oid_field = object_id_field
            self.properties = types.SimpleNamespace(objectIdField=object_id_field, maxRecordCount=2000)
            self.rows = [{"OBJECTID": oid, "task_full_name": name} for name, oid in existing]
            self.edit_calls = []

        def query(self, *, where="1=1", out_fields="*", return_geometry=False,
                  result_offset=0, result_record_count=None, **kw):
            class _Feature:
                def __init__(self, attrs): self.attributes = attrs
            class _Result:
                pass
            result = _Result()
            result.features = [_Feature(dict(row)) for row in self.rows] if result_offset == 0 else []
            return result

        def edit_features(self, **kw):
            self.edit_calls.append(kw)
            if fail_first and len(self.edit_calls) == 1:
                next_oid = max((r["OBJECTID"] for r in self.rows), default=0) + 1
                for item in kw.get("adds") or []:
                    row = dict(item["attributes"])
                    row[self.oid_field] = next_oid
                    self.rows.append(row)
                    next_oid += 1
                raise Exception("(Error Code: 504)")
            result = {}
            for kind, key in (("adds", "addResults"), ("updates", "updateResults")):
                items = kw.get(kind) or []
                result[key] = [{"success": True}] * len(items)
            deletes = kw.get("deletes")
            result["deleteResults"] = [{"success": True}] * (len(deletes.split(",")) if deletes else 0)
            return result

    return FakeTable()


def test_sync_inventory_heals_a_duplicate_row_left_by_an_earlier_resent_add():
    """Two stored rows share task_full_name 't0' (what an earlier resend leaves behind before
    the unique index exists). The extra OID must be deleted even though its key is still
    present in this pass's rows."""
    table = _sync_inventory_table([("t0", 1), ("t0", 2)])
    counts = publish.sync_inventory(table, [{"task_full_name": "t0"}], "H",
                                    chunk_size=200, label="TaskInventory")
    assert counts["deleted"] == 1
    assert table.edit_calls[0]["deletes"] == "2"


def test_sync_inventory_rebuilds_the_payload_before_a_retry_after_a_504(monkeypatch):
    """A 504 on the sync request may have committed. The retry must re-query and turn the
    landed add into an update rather than resend it -- otherwise, without the unique index
    applied yet, every retried add duplicates a row."""
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    table = _sync_inventory_table([], fail_first=True)

    publish.sync_inventory(table, [{"task_full_name": "t0"}], "H", chunk_size=200, label="TaskInventory")

    assert len(table.edit_calls) == 2, "must retry once after the simulated 504"
    second = table.edit_calls[1]
    assert not (second.get("adds") or []), "the add that landed on the first attempt must not be resent"
    assert (second.get("updates") or [])[0]["attributes"]["task_full_name"] == "t0", \
        "the landed row must be sent as an update instead"


# ---------------------------------------------------------------- TaskMetrics key includes host_name

def test_taskmetrics_unique_index_and_upsert_key_include_host_name():
    """Two hosts can legitimately publish a task of the same full name/metric/log file name;
    without host_name in both the index spec and the upsert key, the second host's scoped
    fetch never sees the first host's row and sends a colliding add every pass."""
    spec = next(s for s in publish.TABLE_INDEXES[publish.TABLE_METRICS] if s["unique"])
    assert "host_name" in spec["fields"]


def test_publish_upserts_taskmetrics_keyed_including_host_name(monkeypatch):
    calls = {}

    def fake_upsert_by_key(table, rows, key_fields, *, chunk_size, label, order_field=""):
        if label == publish.TABLE_METRICS:
            calls["key_fields"] = key_fields
        return {"added": 0, "updated": 0, "deleted": 0}

    monkeypatch.setattr(publish, "upsert_by_key", fake_upsert_by_key)
    monkeypatch.setattr(publish, "sync_inventory", lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    monkeypatch.setattr(publish, "summarize_runs_from_agol", lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "keep_stored_start", lambda *a, **k: 0)

    class _Table:
        properties = _Props(objectIdField="OBJECTID")

    tables = {publish.TABLE_INVENTORY: _Table(), publish.TABLE_RUNS: _Table(), publish.TABLE_METRICS: _Table()}
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    collected = {"runs": [], "inventory": [],
                "metrics": [{"host_name": "H", "task_full_name": "t", "metric_key": "k",
                            "log_file_name": "f", "metric_value": 1}],
                "host_name": "H", "log_summary": {}}
    publish.publish(gis=object(), item=object(), collected=collected, config={}, force=False)
    assert calls["key_fields"] == ["host_name", "task_full_name", "metric_key", "log_file_name"]


# ---------------------------------------------------------------- upsert_by_key multi-field key caching

def test_upsert_by_key_builds_a_multi_field_index_once_not_per_chunk(monkeypatch):
    queries = []

    class _Table:
        properties = _Props(objectIdField="OID")

        def edit_features(self, **kw):
            return {"addResults": [{"success": True}] * len(kw.get("adds") or []),
                   "updateResults": [{"success": True}] * len(kw.get("updates") or [])}

    def fake_query_all_rows(table, *, where="1=1", out_fields="*", page=None):
        queries.append(where)
        return []

    monkeypatch.setattr(publish, "query_all_rows", fake_query_all_rows)
    rows = [{"host_name": "H", "task_full_name": f"t{i}", "metric_key": "k", "log_file_name": "f"}
            for i in range(5)]
    publish.upsert_by_key(_Table(), rows, ["host_name", "task_full_name", "metric_key", "log_file_name"],
                          chunk_size=2, label="TaskMetrics")
    assert len(queries) == 1, "a multi-field key must be fetched once for the whole pass, not per chunk"


# ---------------------------------------------------------------- find_duplicate_keys having clause

def test_find_duplicate_keys_passes_a_having_clause_counting_the_oid_field():
    class _Result:
        features = []

    class _Table:
        properties = _Props(objectIdField="OID")

        def query(self, **kw):
            assert kw["having"] == "COUNT(OID) > 1"
            assert kw["out_statistics"][0]["onStatisticField"] == "OID"
            return _Result()

    assert publish.find_duplicate_keys(_Table(), ["run_key"]) == []


# ---------------------------------------------------------------- apply_migration checks add_to_definition success

def test_apply_migration_raises_when_add_to_definition_reports_failure():
    class _FailingManager:
        def add_to_definition(self, definition):
            return {"success": False, "error": {"message": "boom"}}

    field_names = [f["name"] for f in publish.runs_fields() if f["name"] != "run_key"]
    table = _SchemaTable(field_names, indexes=[])
    table.manager = _FailingManager()
    with pytest.raises(SystemExit, match="run_key"):
        publish.apply_migration({publish.TABLE_RUNS: table})


# ---------------------------------------------------------------- run_lock stale-lock detection

def test_run_lock_held_carries_the_holders_start_time(tmp_path):
    with common.run_lock(tmp_path):
        try:
            with common.run_lock(tmp_path):
                pass
        except common.RunLockHeld as exc:
            assert exc.held_since is not None
            assert (common.utc_now() - exc.held_since).total_seconds() < 60
        else:
            pytest.fail("expected RunLockHeld")


def test_run_under_lock_exits_nonzero_when_the_lock_is_stale(tmp_path, monkeypatch):
    import datetime as _dt
    stale_time = common.utc_now() - _dt.timedelta(hours=common.STALE_LOCK_HOURS + 1)

    def fake_run_lock(log_dir, name="taskdash.lock"):
        raise common.RunLockHeld("held", held_since=stale_time)

    monkeypatch.setattr(publish, "run_lock", fake_run_lock)
    messages = []
    monkeypatch.setattr(publish, "emit", lambda msg: messages.append(msg))
    result = publish._run_under_lock(tmp_path, lambda: 0)
    assert result == 1
    assert any("stuck" in m.lower() for m in messages)


def test_run_under_lock_exits_zero_for_a_fresh_lock(tmp_path, monkeypatch):
    def fake_run_lock(log_dir, name="taskdash.lock"):
        raise common.RunLockHeld("held", held_since=common.utc_now())

    monkeypatch.setattr(publish, "run_lock", fake_run_lock)
    monkeypatch.setattr(publish, "emit", lambda msg: None)
    assert publish._run_under_lock(tmp_path, lambda: 0) == 0


def test_main_migrate_apply_runs_inside_the_run_lock(tmp_path, monkeypatch):
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)},
                                       "rules": {"profiles": list(REFERENCE_PROFILES)}}), encoding="utf-8")
    monkeypatch.setattr(publish, "setup_logging", lambda log_dir, prefix: tmp_path / "x.log")
    monkeypatch.setattr(publish, "cleanup_old_logs", lambda *a: None)
    ran = []
    monkeypatch.setattr(publish, "run_migration", lambda *a, **k: ran.append(1) or 0)
    monkeypatch.setattr(publish, "emit", lambda msg: None)

    with common.run_lock(tmp_path):
        result = publish.main(["--migrate", "--apply", "--config", str(config_path)])

    assert result == 0
    assert ran == [], "run_migration must never run while another instance holds the lock"


def test_main_migrate_without_apply_ignores_the_lock(tmp_path, monkeypatch):
    """A dry-run --migrate report is read-only and must not be blocked by a held lock."""
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({"logging": {"log_dir": str(tmp_path)},
                                       "rules": {"profiles": list(REFERENCE_PROFILES)}}), encoding="utf-8")
    monkeypatch.setattr(publish, "setup_logging", lambda log_dir, prefix: tmp_path / "x.log")
    monkeypatch.setattr(publish, "cleanup_old_logs", lambda *a: None)
    ran = []
    monkeypatch.setattr(publish, "run_migration", lambda *a, **k: ran.append(1) or 0)
    monkeypatch.setattr(publish, "emit", lambda msg: None)

    with common.run_lock(tmp_path):
        result = publish.main(["--migrate", "--config", str(config_path)])

    assert result == 0
    assert ran == [1], "a dry-run --migrate must run even while the apply lock is held"


# ---------------------------------------------------------------- deferred config-key warnings

def test_load_config_defer_warnings_returns_a_tuple_without_emitting(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"totally_new_section": {"x": 1},
                                "rules": {"profiles": list(REFERENCE_PROFILES)}}), encoding="utf-8")
    emitted = []
    monkeypatch.setattr(common, "emit", lambda msg: emitted.append(msg))
    config, warnings = common.load_config(path, defer_warnings=True)
    assert config["totally_new_section"] == {"x": 1}
    assert any("totally_new_section" in w for w in warnings)
    assert emitted == [], "deferred warnings must not be emitted by load_config itself"


def test_main_logs_a_deferred_config_warning_to_the_log_file(tmp_path, monkeypatch):
    """Before this fix, load_config emitted the warning before setup_logging ran, so
    logging.info() was silently dropped by the unconfigured root logger and it never reached
    the log file -- the only record under pythonw/Task Scheduler."""
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps({"totally_new_section": {"x": 1},
                                       "rules": {"profiles": list(REFERENCE_PROFILES)},
                                       "logging": {"log_dir": str(tmp_path)}}), encoding="utf-8")
    monkeypatch.setattr(publish, "run_publish", lambda *a, **k: 0)
    publish.main(["--config", str(config_path)])
    logs = list(tmp_path.glob("taskdash_*.log"))
    assert logs, "setup_logging must have created a log file"
    content = logs[0].read_text(encoding="utf-8")
    assert "totally_new_section" in content


def test_underscore_config_keys_are_documentation_not_typos(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"_notes": "why these settings", "history": {},
                           "rules": {"profiles": list(REFERENCE_PROFILES)}}), encoding="utf-8")
    config, warnings = common.load_config(path, defer_warnings=True)
    assert config["_notes"] and warnings == []



def test_upsert_merges_rows_that_repeat_a_key_in_one_pass(monkeypatch):
    """Same log name in the script folder and its Logs folder: two adds hit the unique index."""
    sent = []

    class _Table:
        properties = type("P", (), {"objectIdField": "OBJECTID", "maxRecordCount": 2000})()

        def query(self, **kw):
            return type("R", (), {"features": []})()

        def edit_features(self, **kw):
            sent.append(kw)
            return {"addResults": [{"success": True}] * len(kw.get("adds") or []),
                    "updateResults": []}

    key = ["host_name", "task_full_name", "metric_key", "log_file_name"]
    rows = [{"host_name": "H", "task_full_name": "\\T", "metric_key": "k", "log_file_name": "Sync.log",
             "metric_value": 1},
            {"host_name": "H", "task_full_name": "\\T", "metric_key": "k", "log_file_name": "sync.log",
             "metric_value": 2}]
    counts = publish.upsert_by_key(_Table(), rows, key, chunk_size=200, label="TaskMetrics")
    assert counts["added"] == 1
    assert [a["attributes"]["metric_value"] for a in sent[0]["adds"]] == [2]


def test_a_non_unique_index_does_not_satisfy_a_unique_spec():
    table = type("T", (), {"properties": type("P", (), {
        "indexes": [{"fields": "run_key", "isUnique": False}]})()})()
    spec = [{"fields": ["run_key"], "unique": True}]
    assert publish.missing_indexes(table, spec) == spec
    table.properties.indexes = [{"fields": "run_key", "isUnique": True}]
    assert publish.missing_indexes(table, spec) == []


def test_a_duplicate_key_rejection_says_so():
    result = {"addResults": [{"success": False, "error": {
        "description": "Cannot insert duplicate key row in object 'x' with unique index 'UQ'."}}]}
    with pytest.raises(SystemExit, match="unique index"):
        common.raise_on_edit_failure(result, "adds", "TaskRuns")


def test_a_failed_purge_delete_is_not_reported_as_done(monkeypatch):
    class _Table:
        def query(self, **kw):
            return 3

        def delete_features(self, **kw):
            return {"deleteResults": [{"success": False, "error": {"description": "locked"}}]}

    with pytest.raises(SystemExit, match="locked"):
        common.purge_older_than(_Table(), "start_time_utc", 90, "TaskRuns")

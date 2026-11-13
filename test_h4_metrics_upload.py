"""H4-2: the hourly publish sends only the metric rows of log files that changed.

Before, every pass re-sent every mined row (about 40 files per task) and read all of the host's
stored metric keys. The state file `metrics_state.json` in the log folder holds one digest per
(task, log file) from the last clean publish. No network: AGOL tables are fakes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from types import SimpleNamespace

import pytest

import taskdash_publish as publish

NOW = dt.datetime(2026, 9, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
TASK = "\\Jobs\\Sync"
OTHER = "\\Jobs\\Export"
SIGNATURE = "sig-1"
KEYS = ["host_name", "task_full_name", "metric_key", "log_file_name"]


def row(task=TASK, key="rows_added", log="sync_1.log", value=5.0, observed="2026-09-25T10:00:00+00:00",
        display="Rows Added"):
    return {"host_name": "HOST-A", "task_name": task.split("\\")[-1], "task_full_name": task,
            "metric_key": key, "display_name": display, "metric_value": value,
            "observed_at_utc": observed, "log_file_name": log, "series_key": f"{task}|{key}",
            "sample_line": f"{key} {value}"}


def plan(rows, state=None, signature=SIGNATURE, keep_days=90):
    return publish.plan_metric_upload(rows, state, signature, NOW, keep_days)


# ---------------------------------------------------------------- the plan

def test_no_state_sends_every_row_and_reads_the_whole_host():
    rows = [row(), row(log="sync_2.log"), row(task=OTHER, log="export_1.log")]
    result = plan(rows)
    assert result["rows"] == rows
    assert result["where"] == ""
    assert set(result["state"]["files"]) == {TASK, OTHER}


def test_an_unchanged_file_sends_nothing_on_the_second_run():
    rows = [row(), row(key="rows_failed", value=0.0), row(log="sync_2.log")]
    state = plan(rows)["state"]
    second = plan(list(rows), state)
    assert second["rows"] == []
    assert second["state"] == state


def test_a_changed_file_sends_only_its_rows_and_bounds_the_key_read():
    first = [row(), row(log="sync_2.log", observed="2026-09-25T11:00:00+00:00")]
    state = plan(first)["state"]
    changed = [row(), row(log="sync_2.log", value=9.0, observed="2026-09-25T11:30:00+00:00")]
    result = plan(changed, state)
    assert [r["log_file_name"] for r in result["rows"]] == ["sync_2.log"]
    assert result["rows"][0]["metric_value"] == 9.0
    assert result["where"] == "(log_file_name IN ('sync_2.log'))"


def test_a_file_new_to_the_state_is_matched_by_name():
    # A stored row for it can pre-date the state file.
    state = plan([row()])["state"]
    result = plan([row(), row(log="o'brien.log", observed="2026-09-25T11:45:00+00:00")], state)
    assert [r["log_file_name"] for r in result["rows"]] == ["o'brien.log"]
    assert result["where"] == "(log_file_name IN ('o''brien.log'))"


def test_a_key_that_leaves_a_file_and_comes_back_is_still_found(monkeypatch):
    """Pass 2 drops key B and moves the file's observed time on; B's stored row keeps the old
    time. The bound must still return it, or pass 3 adds it again and the unique index rejects it."""
    stored = [dict(row(key="b_key", observed="2026-09-25T08:00:00+00:00"), OBJECTID=11)]
    pass_one = [row(key="a_key", observed="2026-09-25T08:00:00+00:00"), stored[0]]
    state = plan(pass_one)["state"]
    state = plan([row(key="a_key", value=6.0, observed="2026-09-25T09:00:00+00:00")], state)["state"]
    pass_three = [row(key="a_key", value=7.0, observed="2026-09-25T10:00:00+00:00"),
                  row(key="b_key", value=2.0, observed="2026-09-25T10:00:00+00:00")]
    result = plan(pass_three, state)

    def query(table, *, where="1=1", out_fields="*", page=None):
        names = re.findall(r"'((?:[^']|'')*)'", where.split("log_file_name IN", 1)[1])
        return [r for r in stored if r["log_file_name"] in names]

    monkeypatch.setattr(publish, "query_all_rows", query)
    index = publish.fetch_keys(object(), result["rows"], KEYS, "OBJECTID", extra_where=result["where"])
    assert publish.folded_key(pass_three[1], KEYS) in index


def test_too_many_changed_files_fall_back_to_the_unbounded_read():
    state = plan([row()])["state"]
    fresh = [row(log=f"n{i}.log") for i in range(publish.METRICS_BOUND_NAMES_LIMIT + 1)]
    result = plan(fresh, state)
    assert len(result["rows"]) == len(fresh)
    assert result["where"] == ""


def test_a_changed_signature_ignores_the_state_and_sends_everything():
    rows = [row(), row(log="sync_2.log")]
    state = plan(rows)["state"]
    result = plan(rows, state, signature="sig-2")
    assert result["rows"] == rows and result["where"] == ""
    assert result["state"]["signature"] == "sig-2"


def test_a_new_display_name_or_value_changes_the_digest_and_resends():
    state = plan([row()])["state"]
    assert plan([row(display="Rows Written")], state)["rows"] != []
    assert plan([row(value=6.0)], state)["rows"] != []


def test_state_keeps_a_file_that_was_not_mined_this_pass_until_its_rows_expire():
    old = row(log="old.log", observed="2026-07-01T00:00:00+00:00")       # 86 days old
    ancient = row(log="ancient.log", observed="2026-06-01T00:00:00+00:00")  # 116 days old
    state = plan([old, ancient, row()])["state"]
    assert set(state["files"][TASK]) == {"old.log", "sync_1.log"}
    # Not mined now (aged out of the window), but a later touch must still find its stored row.
    later = plan([row()], state)["state"]
    assert set(later["files"][TASK]) == {"old.log", "sync_1.log"}
    touched = plan([row(), row(log="old.log", value=7.0, observed="2026-09-25T09:00:00+00:00")], later)
    assert touched["where"] == "(log_file_name IN ('old.log'))"


# ---------------------------------------------------------------- the state file

def test_a_missing_or_damaged_state_file_reads_as_no_state(tmp_path, capsys):
    path = tmp_path / publish.METRICS_STATE_FILE
    assert publish.load_metrics_state(path) is None
    path.write_text("{not json", encoding="utf-8")
    assert publish.load_metrics_state(path) is None
    assert "ignored metrics_state.json" in capsys.readouterr().out
    path.write_text('{"signature": "s", "files": []}', encoding="utf-8")
    assert publish.load_metrics_state(path) is None


def test_the_state_file_name_is_per_target():
    assert publish.target_file_name(publish.METRICS_STATE_FILE, "prod") == "metrics_state.json"
    assert publish.target_file_name(publish.METRICS_STATE_FILE, "test") == "metrics_state_test.json"


def test_the_signature_follows_the_service_the_settings_and_the_version():
    base = publish.metrics_state_signature({"metrics": {"days_back": 30}}, "svc-1")
    assert base == publish.metrics_state_signature({"metrics": {"days_back": 30}}, "svc-1")
    assert base != publish.metrics_state_signature({"metrics": {"days_back": 30}}, "svc-2")
    assert base != publish.metrics_state_signature({"metrics": {"days_back": 14}}, "svc-1")
    assert base != publish.metrics_state_signature({"metrics": {"days_back": 30}, "privacy": {}}, "svc-1")


# ---------------------------------------------------------------- the bounded key read

def test_fetch_keys_adds_the_bound_to_a_multi_field_read(monkeypatch):
    seen = []
    monkeypatch.setattr(publish, "query_all_rows",
                        lambda table, *, where="1=1", out_fields="*", page=None: seen.append(where) or [])
    rows = [row()]
    publish.fetch_keys(object(), rows, KEYS, "OBJECTID")
    publish.fetch_keys(object(), rows, KEYS, "OBJECTID", extra_where="(observed_at_utc >= TIMESTAMP 'x')")
    assert seen == ["host_name = 'HOST-A'",
                    "host_name = 'HOST-A' AND (observed_at_utc >= TIMESTAMP 'x')"]


class _Table:
    properties = SimpleNamespace(objectIdField="OBJECTID")


def _publish_with_fakes(monkeypatch, collected, metrics_plan=None):
    calls = []

    def fake_upsert(table, rows, key_fields, *, chunk_size, label, order_field="", **kw):
        if label == publish.TABLE_METRICS:
            calls.append({"rows": rows, **kw})
        return {"added": 0, "updated": 0, "deleted": 0}

    monkeypatch.setattr(publish, "upsert_by_key", fake_upsert)
    monkeypatch.setattr(publish, "sync_inventory", lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    monkeypatch.setattr(publish, "summarize_runs_from_agol", lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    tables = {publish.TABLE_INVENTORY: _Table(), publish.TABLE_RUNS: _Table(), publish.TABLE_METRICS: _Table()}
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    kwargs = {} if metrics_plan is None else {"metrics_plan": metrics_plan}
    counts = publish.publish(gis=object(), item=object(), collected=collected, config={}, **kwargs)
    return calls, counts


def _collected(rows):
    return {"runs": [], "inventory": [], "metrics": rows, "host_name": "HOST-A", "log_summary": {}}


def test_publish_without_a_plan_upserts_every_row_as_before(monkeypatch):
    calls, _ = _publish_with_fakes(monkeypatch, _collected([row(), row(log="b.log")]))
    assert len(calls) == 1 and len(calls[0]["rows"]) == 2 and "existing_where" not in calls[0]


def test_publish_sends_nothing_when_the_plan_has_no_changed_rows(monkeypatch):
    rows = [row()]
    unchanged = plan(rows, plan(rows)["state"])
    calls, counts = _publish_with_fakes(monkeypatch, _collected(rows), unchanged)
    assert calls == []
    assert counts["metrics_added"] == 0 and counts["metrics_updated"] == 0


def test_publish_sends_the_changed_rows_with_the_bound(monkeypatch):
    rows = [row(), row(log="b.log", observed="2026-09-25T11:00:00+00:00")]
    state = plan(rows)["state"]
    rows[1] = row(log="b.log", value=8.0, observed="2026-09-25T11:30:00+00:00")
    calls, _ = _publish_with_fakes(monkeypatch, _collected(rows), plan(rows, state))
    (call,) = calls
    assert [r["log_file_name"] for r in call["rows"]] == ["b.log"]
    assert call["existing_where"] == "(log_file_name IN ('b.log'))"


def test_upsert_passes_the_bound_to_the_key_read_and_the_retry_read(monkeypatch):
    seen = []

    def fetch(table, rows, key_fields, oid_field, extra_where=""):
        seen.append(extra_where)
        return {}

    class Table(_Table):
        edits = 0

        def edit_features(self, **kw):
            self.edits += 1
            if self.edits == 1:
                raise Exception("(Error Code: 504)")
            return {"addResults": [{"success": True}] * len(kw.get("adds") or [])}

    monkeypatch.setattr(publish, "fetch_keys", fetch)
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    publish.upsert_by_key(Table(), [row()], KEYS, chunk_size=10, label="TaskMetrics", existing_where="W")
    assert seen == ["W", "W"]


# ---------------------------------------------------------------- _run_publish writes the state

@pytest.fixture
def run_world(monkeypatch, tmp_path):
    item = SimpleNamespace(title="x", id="svc-1", access="private")
    sent = []
    monkeypatch.setattr(publish, "connect_agol", lambda cfg: object())
    monkeypatch.setattr(publish, "ensure_service", lambda gis, cfg, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables, *a: None)
    monkeypatch.setattr(publish, "check_source_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "check_release_sharing", lambda *a: None)
    monkeypatch.setattr(publish, "collect_everything", lambda *a, **k: {
        "runs": [], "inventory": [], "metrics": [row(), row(log="b.log")]})
    monkeypatch.setattr(publish, "report", lambda collected: None)
    monkeypatch.setattr(publish, "maybe_send_alert", lambda *a: False)
    monkeypatch.setattr(publish, "_write_collector_run_row", lambda *a, **k: None)

    def fake_publish(gis, item, collected, config, force=False, metrics_plan=None, target="prod"):
        sent.append(metrics_plan)
        return {}

    monkeypatch.setattr(publish, "publish", fake_publish)

    def run(*flags, target="prod"):
        args = publish.parse_args([*flags, "--target", target])
        config = {"logging": {"log_dir": str(tmp_path)}}
        return publish._run_publish(args, config, tmp_path, publish.utc_now(),
                                    {"armed": False, "recorded": False})

    run.sent = sent
    return run


def test_a_clean_apply_saves_the_state_and_the_next_run_sends_nothing(run_world, tmp_path):
    assert run_world("--apply") == 0
    path = tmp_path / "metrics_state.json"
    assert len(run_world.sent[0]["rows"]) == 2
    assert set(json.loads(path.read_text(encoding="utf-8"))["files"][TASK]) == {"sync_1.log", "b.log"}

    assert run_world("--apply") == 0
    assert run_world.sent[1]["rows"] == []


def test_check_mode_writes_no_state_and_plans_nothing(run_world, tmp_path):
    assert run_world() == 0
    assert run_world.sent == []
    assert list(tmp_path.glob("metrics_state*.json")) == []


def test_a_test_target_writes_its_own_file_and_never_prods(run_world, tmp_path):
    prod = tmp_path / "metrics_state.json"
    prod.write_text('{"signature": "keep", "files": {}}', encoding="utf-8")
    assert run_world("--apply", target="test") == 0
    assert (tmp_path / "metrics_state_test.json").exists()
    assert json.loads(prod.read_text(encoding="utf-8")) == {"signature": "keep", "files": {}}
    # The prod file belongs to another signature, so the test run must not have trusted it either.
    assert len(run_world.sent[0]["rows"]) == 2


def test_a_failed_publish_keeps_the_old_state(run_world, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("AGOL refused")

    monkeypatch.setattr(publish, "publish", boom)
    with pytest.raises(RuntimeError):
        run_world("--apply")
    assert list(tmp_path.glob("metrics_state*.json")) == []


def test_a_failed_metric_change_write_keeps_the_old_state_so_the_next_run_resends(
        run_world, monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "publish", lambda *a, **k: {"metric_change_failed": 1})
    assert run_world("--apply") == 0
    assert list(tmp_path.glob("metrics_state*.json")) == []


# ---------------------------------------------------------------- --refresh-metrics invalidates it

def test_refresh_metrics_apply_removes_the_state_so_the_next_publish_resends(monkeypatch, tmp_path):
    from test_round2_review import _MetricsTable, _fresh, _stored

    table = _MetricsTable([_stored(1, "features_max_oid", "new.log")])
    monkeypatch.setattr(publish, "connect_agol", lambda config: None)
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: None)
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables, *a: None)
    monkeypatch.setattr(publish, "get_tables", lambda item: {publish.TABLE_METRICS: table,
                                                             publish.TABLE_INVENTORY: _MetricsTable([])})
    monkeypatch.setattr(publish, "mine_metrics",
                        lambda config, inventory, host, examined=None: ([_fresh("exported_record_count", "new.log")], {}))
    config = {"host_name_override": "H", "privacy": {"hide_paths": False}, "logging": {"log_dir": str(tmp_path)}}
    prod, test = tmp_path / "metrics_state.json", tmp_path / "metrics_state_test.json"
    prod.write_text("{}", encoding="utf-8")
    test.write_text("{}", encoding="utf-8")

    publish.run_refresh_metrics(config, publish.parse_args(["--refresh-metrics"]))
    assert prod.exists(), "a dry run must not touch the state"

    publish.run_refresh_metrics(config, publish.parse_args(["--refresh-metrics", "--apply"]))
    assert not prod.exists() and test.exists()

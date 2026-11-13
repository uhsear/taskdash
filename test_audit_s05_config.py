"""Audit lane s05: config, description and rules checks (TD-029, TD-030, TD-105, TD-106, TD-110, TD-114, TD-116,
TD-117, TD-119, TD-120, TD-121, TD-122, TD-123, TD-124).

No network, no ArcGIS Online: fakes and temporary files throughout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402
import taskdash_metrics as tm  # noqa: E402
import taskdash_preflight as pf  # noqa: E402
from test_taskdash import NOW, _task  # noqa: E402


@pytest.fixture
def messages(monkeypatch):
    seen = []
    monkeypatch.setattr(collect, "emit", seen.append)
    return seen


def _write(tmp_path, payload):
    path = tmp_path / "workflow_descriptions.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


# ------------------------------------------------------------------ TD-029 file checks

def test_unknown_entry_key_is_named_with_a_suggestion(tmp_path):
    path = _write(tmp_path, {"tasks": {"Job": {"description": "d", "overdue_grace_minute": 30}}})
    warnings = []
    result = collect.load_descriptions(path, warnings=warnings)
    assert result["job"]["description"] == "d" and result["job"]["overdue_grace_minutes"] is None
    assert len(warnings) == 1
    assert "'overdue_grace_minute'" in warnings[0] and "did you mean 'overdue_grace_minutes'" in warnings[0]


def test_missing_tasks_wrapper_is_reported(tmp_path):
    warnings = []
    assert collect.load_descriptions(_write(tmp_path, {"Job": "d"}), warnings=warnings) == {}
    assert len(warnings) == 1 and '"tasks"' in warnings[0]


def test_mistyped_tasks_key_is_reported_with_a_suggestion(tmp_path):
    warnings = []
    collect.load_descriptions(_write(tmp_path, {"taks": {"Job": "d"}}), warnings=warnings)
    assert any("'taks'" in w and "close to 'tasks'" in w for w in warnings)


@pytest.mark.parametrize("grace", ["90 min", -5, True])
def test_a_bad_grace_is_reported_and_left_out(tmp_path, grace):
    warnings = []
    result = collect.load_descriptions(
        _write(tmp_path, {"tasks": {"Job": {"overdue_grace_minutes": grace, "description": "d"}}}),
        warnings=warnings)
    assert result["job"]["overdue_grace_minutes"] is None
    assert len(warnings) == 1 and "overdue_grace_minutes" in warnings[0]


def test_a_clean_file_gives_no_warning_and_underscore_keys_are_ignored(tmp_path):
    warnings = []
    path = _write(tmp_path, {"_notes": "x", "tasks": {"Job": {"_why": "x", "description": "d"}}})
    assert collect.load_descriptions(path, warnings=warnings)["job"]["description"] == "d"
    assert warnings == []


def test_load_without_a_sink_logs_the_warning(tmp_path, messages):
    collect.load_descriptions(_write(tmp_path, {"tasks": {"Job": {"descripton": "d"}}}))
    assert len(messages) == 1 and "did you mean 'description'" in messages[0]


def test_preflight_check_reports_file_problems(tmp_path, monkeypatch):
    _write(tmp_path, {"tasks": {"Parcel Refresh": {"overdue_grace_minute": 30}, "Gone": "d"}})
    monkeypatch.setattr(pf, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(collect, "runtime_dir", lambda: tmp_path)
    ctx = pf.Context(None, config={}, tasks=[_task()])
    status, message = pf.check_descriptions(ctx)
    assert status == pf.WARN
    assert "overdue_grace_minute" in message and "'gone' matches no monitored task" in message


def test_preflight_check_skips_when_there_is_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "runtime_dir", lambda: tmp_path)
    assert pf.check_descriptions(pf.Context(None, config={}))[0] == pf.SKIP


def test_preflight_check_passes_a_clean_file(tmp_path, monkeypatch):
    _write(tmp_path, {"tasks": {"Parcel Refresh": "d", "ParcelRefresh.py": "d2"}})
    monkeypatch.setattr(pf, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(collect, "runtime_dir", lambda: tmp_path)
    assert pf.check_descriptions(pf.Context(None, config={}, tasks=[_task()]))[0] == pf.OK


# ------------------------------------------------------------------ TD-030 unmatched keys

def test_a_key_that_names_no_task_gets_one_warning_with_a_suggestion():
    descriptions = {"parcel refreh": collect.parse_description_entry("d"),
                    "parcel refresh": collect.parse_description_entry("d"),
                    "parcelrefresh.py": collect.parse_description_entry("d")}
    warnings = collect.description_key_warnings(descriptions, [_task()])
    assert len(warnings) == 1
    assert "'parcel refreh'" in warnings[0] and "Did you mean 'Parcel Refresh'" in warnings[0]
    assert "rename" in warnings[0]


# ------------------------------------------------------------------ TD-121 / TD-120 acknowledgements

def _ack(fragments, reason="On purpose."):
    return {"parcel refresh": {"description": "", "acknowledge_config_warnings": fragments,
                               "acknowledge_reason": reason}}


def _settings_detail(descriptions):
    return collect.build_inventory_rows([_task(start_when_available=False)], host_name="H",
                                        descriptions=descriptions, audit_dt=NOW)[0]["settings_detail"]


def test_accepted_shows_when_a_fragment_hides_a_note(messages):
    assert _settings_detail(_ack(["StartWhenAvailable"])).endswith("| Accepted: On purpose.")
    assert messages == []


def test_accepted_is_left_out_and_warned_when_no_fragment_matches(messages):
    assert "Accepted" not in _settings_detail(_ack(["StartWhenAvailble"]))
    assert len(messages) == 1 and "'StartWhenAvailble'" in messages[0] and "matches no setting note" in messages[0]


def test_accepted_is_left_out_for_a_reason_with_no_fragment(messages):
    assert "Accepted" not in _settings_detail(_ack([]))


def test_one_matching_fragment_keeps_accepted_and_the_other_is_warned(messages):
    assert "Accepted" in _settings_detail(_ack(["StartWhenAvailable", "nothing like it"]))
    assert len(messages) == 1 and "'nothing like it'" in messages[0]


def test_a_long_reason_ends_with_an_ellipsis_and_a_warning(messages):
    detail = _settings_detail(_ack(["StartWhenAvailable"], reason="word " * 80))
    assert len(detail) == collect.SETTINGS_DETAIL_LENGTH and detail.endswith("…")
    assert len(messages) == 1 and "acknowledge_reason is too long" in messages[0]


# ------------------------------------------------------------------ TD-106 log_directory_overrides

def _inventory(tmp_path):
    (tmp_path / "job.py").write_text("", encoding="utf-8")
    logs = tmp_path / "Logs"
    logs.mkdir(exist_ok=True)
    (logs / "job.log").write_text("Rows loaded: 12\n", encoding="utf-8")
    return [{"task_name": "Job", "task_full_name": "\\Folder\\Job", "command_target_path": str(tmp_path / "job.py")}]


def test_override_key_in_another_case_still_applies(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "other.log").write_text("Rows loaded: 99\n", encoding="utf-8")
    rows, summary = tm.build_metric_rows(_inventory(tmp_path), host_name="H",
                                         log_directory_overrides={"\\FOLDER\\job": str(elsewhere)})
    assert summary["\\Folder\\Job"]["latest_log_name"] == "other.log"
    assert [r["metric_value"] for r in rows] == [99.0]


def test_unmatched_override_key_warns(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(tm, "emit", seen.append)
    tm.build_metric_rows(_inventory(tmp_path), host_name="H",
                         log_directory_overrides={"\\Folder\\Jobb": str(tmp_path), "\\folder\\JOB": str(tmp_path)})
    hits = [m for m in seen if "log_directory_overrides key" in m]
    assert len(hits) == 1 and "'\\\\Folder\\\\Jobb'" in hits[0]


def test_preflight_log_folders_warns_on_an_unmatched_key(tmp_path):
    config = {"metrics": {"log_directory_overrides": {"\\Nope": str(tmp_path), "\\PARCEL REFRESH": str(tmp_path)}}}
    status, message = pf.check_log_folders(pf.Context(None, config=config, tasks=[_task()]))
    assert status == pf.WARN and "'\\\\Nope'" in message and "PARCEL" not in message


# ------------------------------------------------------------------ TD-110 smtp.ssl follows the port

@pytest.mark.parametrize("smtp, expected", [
    ({"port": 465, "ssl": None}, True),
    ({"port": 465}, True),
    ({"port": 587, "ssl": None}, False),
    ({"port": 465, "ssl": False}, False),
    ({"port": 587, "ssl": True}, True),
])
def test_smtp_ssl_null_lets_the_port_decide(smtp, expected):
    mail = common._smtp_block_settings({"host": "h", "from": "f", **smtp})
    assert mail["use ssl"] is expected


def test_the_example_config_leaves_smtp_ssl_to_the_port():
    example = json.loads((Path(__file__).parent / "TaskDash.config.example.json").read_text(encoding="utf-8"))
    assert example["smtp"]["ssl"] is None


# ------------------------------------------------------------------ TD-116 / TD-117 maintenance windows

def test_a_maintenance_time_without_an_offset_gets_a_warning():
    windows = [{"task": "x", "start": "2026-11-01T22:00:00", "end": "2026-11-02T06:00:00-05:00"},
               {"task": "y", "start": "2026-11-01T22:00:00Z", "end": "2026-11-02T06:00:00+00:00"}]
    warnings = common._maintenance_window_warnings({"alerting": {"maintenance_windows": windows}})
    assert len(warnings) == 1 and "maintenance_windows[0].start has no offset" in warnings[0]


def test_a_bad_maintenance_regex_warns_once_however_many_tasks(monkeypatch):
    import taskdash_publish as publish
    seen = []
    monkeypatch.setattr(publish, "emit", seen.append)
    windows = [{"task": "(unclosed", "start": None, "end": None, "reason": "r"}]
    active = publish._active_maintenance_windows(windows, NOW)
    inventory = [{"task_name": f"T{i}", "last_run_failed": 1, "last_result_text": ""} for i in range(6)]
    alerted, suppressed = publish.partition_maintenance(inventory, active)
    assert len(alerted) == 6 and suppressed == []
    assert len([m for m in seen if "not a valid regex" in m]) == 1


# ------------------------------------------------------------------ TD-105 --show-metrics

def test_show_metrics_lists_the_newest_value_of_each_key(monkeypatch):
    import taskdash_publish as publish
    seen = []
    monkeypatch.setattr(publish, "emit", seen.append)
    row = {"task_name": "Job", "metric_key": "rows_count", "log_file_name": "job.log", "series_key": "s1"}
    publish.report_metrics({"metrics": [dict(row, metric_value=1.0, observed_at_utc="2026-01-01T00:00:00+00:00"),
                                        dict(row, metric_value=7.0, observed_at_utc="2026-01-02T00:00:00+00:00")]})
    assert seen[-1] == "  Job | rows_count | 7 | job.log"


def test_show_metrics_says_when_nothing_was_mined(monkeypatch):
    import taskdash_publish as publish
    seen = []
    monkeypatch.setattr(publish, "emit", seen.append)
    publish.report_metrics({"metrics": []})
    assert seen[-1].startswith("  none")


def test_show_metrics_refuses_another_action_flag():
    import taskdash_publish as publish
    with pytest.raises(SystemExit):
        publish.parse_args(["--show-metrics", "--migrate"])
    assert publish.parse_args(["--show-metrics"]).show_metrics is True


# ------------------------------------------------------------------ TD-114 log_dir

def test_a_relative_log_dir_starts_in_the_taskdash_folder(tmp_path, monkeypatch):
    import taskdash_publish as publish
    monkeypatch.setattr(publish, "runtime_dir", lambda: tmp_path)
    assert publish.resolve_log_dir({"logging": {"log_dir": "state"}}) == tmp_path / "state"
    assert publish.resolve_log_dir({}) == tmp_path / "Logs"
    assert publish.resolve_log_dir({"logging": {"log_dir": str(tmp_path / "abs")}}) == tmp_path / "abs"


# ------------------------------------------------------------------ TD-119 item type

class _Item:
    def __init__(self, item_type, keywords):
        self.type, self.typeKeywords = item_type, keywords


class _Gis:
    def __init__(self, item):
        self.content = type("C", (), {"get": staticmethod(lambda item_id: item)})()


@pytest.mark.parametrize("item, shown", [
    (_Item("Feature Service", ["Hosted Service", "View Service"]), "a hosted view"),
    (_Item("Dashboard", ["Dashboard"]), "an item of type 'Dashboard'"),
])
def test_a_view_or_dashboard_id_in_item_id_is_refused_by_key(item, shown):
    import taskdash_publish as publish
    with pytest.raises(SystemExit) as caught:
        publish.ensure_service(_Gis(item), {"arcgis": {"item_id": "abc"}}, "prod")
    assert "arcgis.item_id is abc, which is " + shown in str(caught.value)


def test_a_table_service_id_in_item_id_is_accepted():
    import taskdash_publish as publish
    item = _Item("Feature Service", ["Hosted Service", "Feature Service"])
    assert publish.ensure_service(_Gis(item), {"arcgis": {"item_id": "abc"}}, "prod") is item


# ------------------------------------------------------------------ TD-122 --describe

def test_undescribed_tasks_skip_described_ones_and_windows_descriptions():
    tasks = [_task(task_name="Plain", description=""), _task(task_name="Has Windows Text", description="x"),
             _task(task_name="Named In File", description=""),
             _task(task_name="Script Task", description="", primary_arguments="C:/Jobs/nightly.py")]
    descriptions = {"named in file": collect.parse_description_entry("d"),
                    "nightly.py": collect.parse_description_entry("d")}
    assert pf.undescribed_tasks(tasks, {}, descriptions) == ["Plain"]


def test_describe_prints_a_starter_file(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(pf, "emit", seen.append)
    monkeypatch.setattr(collect, "collect_tasks", lambda *a: [_task(task_name="Plain", description="")])
    monkeypatch.setattr(collect, "runtime_dir", lambda: tmp_path)
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"rules": {"profiles": []}}), encoding="utf-8")
    assert pf.main(["--describe", "--config", str(config)]) == 0
    assert json.loads(seen[-1]) == {"tasks": {"Plain": ""}}


def test_describe_cannot_combine_with_other_flags():
    with pytest.raises(SystemExit):
        pf.parse_args(["--describe", "--offline"])


# ------------------------------------------------------------------ TD-123 / TD-124 rules

def test_lower_is_better_tokens_are_lower_cased_on_load(tmp_path):
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"metric_change_lower_is_better": [{"order": 1000, "token": "ERROR"}]}),
                       encoding="utf-8")
    tokens = tm.load_rules((str(profile),))["metric_change_lower_is_better"]
    assert "error" in tokens and "ERROR" not in tokens


def _rules_message(tmp_path, data):
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        tm.load_rules((str(profile),))
    return str(caught.value)


def test_a_rules_error_names_only_the_parts_that_apply(tmp_path):
    message = _rules_message(tmp_path, {"canonical_metric_rules": [{"order": 7, "pattern": "^x$"}]})
    assert "missing or wrong-type field(s): key" in message
    assert "unknown field" not in message and "[]" not in message and "none" not in message


def test_a_rules_error_shows_the_pattern_as_written_in_the_file(tmp_path):
    message = _rules_message(tmp_path, {"prose_count_rules": [{"order": 7, "pattern": "Loaded (\\d+ rows",
                                                                "key": "r_count"}]})
    assert 'bad regex "Loaded (\\\\d+ rows"' in message

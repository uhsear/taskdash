"""Phase 2.5 publish/collect hardening: source sharing, the secret scrubber, privacy defaults,
hide_paths, view field migration, --scrub-history, the anonymous check and the CLI flags.

Fakes only: nothing here reaches ArcGIS Online, a network or Task Scheduler.
"""

from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_collect as collect  # noqa: E402
import taskdash_publish as publish  # noqa: E402

DUMMY = "Dummy0Pw9"  # not a real secret
STAMP = "2026-10-16T12:00:00+00:00"
SHOWN = {"hide_command_lines": False, "hide_sample_lines": False, "hide_accounts": False,
         "hide_paths": False}
HERE = Path(__file__).parent


class _Item:
    def __init__(self, access="private", shared=None, item_id="svc1"):
        self.id, self.title, self.access = item_id, "TaskDash", access
        self.shared_with = shared or {"everyone": access == "public", "org": access == "org", "groups": []}


# ------------------------------------------------------------------ 1. source sharing

@pytest.mark.parametrize("access", ["org", "shared"])
def test_a_shared_source_warns_once_when_a_view_is_configured(access, capsys):
    publish.check_source_sharing(_Item(access), {"arcgis": {"view_item_id": "v1"}}, "prod")
    out = capsys.readouterr().out
    assert out.count("WARNING") == 1 and "Owner" in out


def test_a_public_source_stops_the_publish_when_a_view_is_configured():
    with pytest.raises(SystemExit, match="Set its sharing to Owner"):
        publish.check_source_sharing(_Item("public"), {"arcgis": {"test_view_item_id": "v1"}}, "test")


def test_source_sharing_is_not_judged_without_a_view_or_when_private(capsys):
    publish.check_source_sharing(_Item("public"), {"arcgis": {}}, "prod")
    publish.check_source_sharing(_Item("private"), {"arcgis": {"view_item_id": "v1"}}, "prod")
    assert "WARNING" not in capsys.readouterr().out


def test_the_apply_publish_runs_the_sharing_check_after_the_schema_check(monkeypatch, tmp_path):
    calls = []
    item = _Item("public")
    monkeypatch.setattr(publish, "connect_agol", lambda config: "gis")
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {})
    monkeypatch.setattr(publish, "check_schema_drift", lambda tables: calls.append("schema"))
    monkeypatch.setattr(publish, "collect_everything",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must stop first")))
    args = SimpleNamespace(apply=True, target="prod", allow_host_mismatch=False, full_history=False,
                           fixture="")
    with pytest.raises(SystemExit, match="shared with everyone"):
        publish._run_publish(args, {"arcgis": {"view_item_id": "v1"}}, tmp_path,
                             publish.utc_now(), {"armed": False, "recorded": False})
    assert calls == ["schema"]


# ------------------------------------------------------------------ 2. secret scrubber

SECRET_CASES = [
    (f"sqlcmd -S s -U sa -P {DUMMY}", "-P <redacted>"),
    (f"bcp t out f -U u -P {DUMMY}", "-P <redacted>"),
    (f"plink -pw {DUMMY} host", "-pw <redacted> host"),
    (f"pscp -pw {DUMMY} f host:", "-pw <redacted> f host:"),
    (f"mysql -uroot -p{DUMMY}", "mysql -uroot -p<redacted>"),
    (f"curl -u admin:{DUMMY} https://h", "-u admin:<redacted> https://h"),
    (rf"net use Z: \\srv\share {DUMMY} /user:DOM\u", r"\\srv\share <redacted> /user:DOM\u"),
    (f"tool /p:{DUMMY}", "/p:<redacted>"),
    (f"tool /password:{DUMMY}", "/password:<redacted>"),
    (f"Install -AdminPassword {DUMMY}", "-AdminPassword <redacted>"),
    (f"x --db-password {DUMMY}", "--db-password <redacted>"),
    (f"x --passwd {DUMMY}", "--passwd <redacted>"),
    (f"passwd={DUMMY}", "passwd=<redacted>"),
    (f"AccountKey={DUMMY};", "AccountKey=<redacted>"),
    (f"SharedAccessKey={DUMMY}", "SharedAccessKey=<redacted>"),
    (f"https://a.blob.core.windows.net/c?sv=1&sig={DUMMY}", "?sv=1&sig=<redacted>"),
    (f"Authorization: Basic {DUMMY}", "Authorization: Basic <redacted>"),
    (f'ConvertTo-SecureString "{DUMMY}" -AsPlainText -Force', "ConvertTo-SecureString <redacted> -AsPlainText"),
    (f"-SqlPassword {DUMMY}", "-SqlPassword <redacted>"),
]


@pytest.mark.parametrize("text,kept", SECRET_CASES, ids=[c[0].split()[0] + str(i) for i, c in enumerate(SECRET_CASES)])
def test_the_scrubber_redacts_the_value_and_keeps_the_flag(text, kept):
    scrubbed = publish._scrub_secrets(text)
    assert DUMMY not in scrubbed
    assert kept in scrubbed


NOT_SECRETS = [
    r"C:\Python\python.exe -u C:\Jobs\script.py --apply",
    "python.exe -u script.py",
    "ssh -p 8080 host",
    "scp -P 2222 f host:",
    "sqlcmd -S server -U user -i job.sql",
    r"msiexec /i x.msi /quiet",
    r"net use Z: \\srv\share /user:DOM\u /persistent:no",
    r'"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" C:\Jobs\x.py',
    "msbuild app.sln /p:Configuration=Release",
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\Jobs\\x.ps1 -Path C:\\Out -Passthru",
    collect.check_logon_type_drift(collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN + "\\python.exe", "Password")[0],
]


@pytest.mark.parametrize("text", NOT_SECRETS)
def test_ordinary_command_lines_pass_unchanged(text):
    assert publish._scrub_secrets(text) == text


def test_the_scrubber_stays_fast_on_long_text():
    import time
    for text in ("a" * 20000, '"' * 20000, "-Password " * 3000, "x " * 10000 + '-P "' + "a" * 10000):
        started = time.perf_counter()
        publish._scrub_secrets(text)
        assert time.perf_counter() - started < 1.0, text[:20]


def test_an_unclosed_quote_redacts_to_the_end():
    assert publish._scrub_secrets(f'-Password "{DUMMY} and more') == "-Password <redacted>"
    assert publish._scrub_secrets(f"pwd='{DUMMY}") == "pwd=<redacted>"
    assert DUMMY not in publish._scrub_secrets(f'ConvertTo-SecureString "{DUMMY}')


def test_collect_keeps_the_whole_command_line_for_the_scrubber():
    padding = "--flag " * 80  # over 500 characters before the password
    arguments = f"C:\\Jobs\\sync.py {padding}-Password {DUMMY}"
    assert len(arguments) > 520
    assert collect.script_arguments("python.exe", arguments, "C:\\Jobs\\sync.py").endswith(DUMMY)


def test_prepare_rows_scrubs_a_quoted_value_that_crosses_the_cut():
    value = "a" * 480 + ' -Password "' + f"{DUMMY} " * 5 + '"'
    (row,) = publish.prepare_rows([{"command_target_arguments": value}], publish.TABLE_INVENTORY, STAMP, SHOWN)
    assert "Dummy" not in row["command_target_arguments"] and len(row["command_target_arguments"]) <= 500


def test_the_mail_log_tail_is_scrubbed_before_it_is_cut(tmp_path):
    log = tmp_path / "job.log"
    # The 2048-character cut falls exactly between the flag and its value.
    text = "x" * 100 + "\n" + "y" * 10 + " -Password " + DUMMY + "z" * 2039
    log.write_text(text + "\n", encoding="utf-8")
    cut_first = publish._scrub_secrets(text[-2048:])
    assert cut_first.startswith(DUMMY), "precondition: cutting first leaves the bare value"
    assert DUMMY not in publish.tail_log_for_email(str(log))


def test_a_mid_line_read_drops_its_partial_first_line(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("-Password " + DUMMY + "q" * 20000 + "\nlast line\n", encoding="utf-8")
    tail = publish.tail_log_for_email(str(log), max_bytes=100)
    assert "Dummy" not in tail and tail.endswith("last line")


# ------------------------------------------------------------------ 3. privacy defaults

def test_a_missing_switch_hides_and_only_false_shows():
    assert publish.privacy_hides(None, "hide_accounts") is True
    assert publish.privacy_hides({}, "hide_accounts") is True
    assert publish.privacy_hides({"hide_accounts": None}, "hide_accounts") is True
    assert publish.privacy_hides({"hide_accounts": False}, "hide_accounts") is False
    assert publish.privacy_hides({"hide_accounts": True}, "hide_accounts") is True


def test_the_view_hides_every_switch_field_by_default():
    hidden = publish.view_hidden_fields({})
    assert set(hidden[publish.TABLE_INVENTORY]) >= {"actions", "command_target_arguments", "run_as_user",
                                                    "command_target_path", "latest_log_path"}
    assert hidden[publish.TABLE_RUNS] == ["user_context"] and hidden[publish.TABLE_METRICS] == ["sample_line"]


def test_task_changes_hide_commands_and_accounts_by_default(monkeypatch):
    seen = {}
    monkeypatch.setattr(publish, "detect_task_changes",
                        lambda *a: seen.update(commands=a[5], accounts=a[6]) or [])
    item = SimpleNamespace()
    tables = {name: object() for name in publish.TABLE_SPECS}
    monkeypatch.setattr(publish, "get_tables", lambda i: tables)
    for name in ("upsert_by_key", "sync_inventory"):
        monkeypatch.setattr(publish, name, lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    for name in ("summarize_runs_from_agol", "_fetch_prior_inventory_for_changes"):
        monkeypatch.setattr(publish, name, lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "enrich_inventory", lambda *a, **k: None)
    monkeypatch.setattr(publish, "stamp_success_rate", lambda *a, **k: None)
    collected = {"runs": [], "inventory": [], "metrics": [], "host_name": "H1"}
    publish.publish(None, item, collected, {})
    assert seen == {"commands": True, "accounts": True}


# ------------------------------------------------------------------ 4. hide_paths

def _raw(**overrides):
    raw = {"task_name": "Sync", "task_path": "\\", "task_full_name": "\\Sync", "state": "Ready",
           "enabled": True, "run_as_user": "svc", "logon_type": "Password", "run_level": "Limited",
           "trigger_types": "Daily", "trigger_details": [],
           "primary_executable": r"C:\Tools\python.exe",
           "primary_arguments": r"C:\Users\jdoe\Jobs\sync.py", "primary_working_directory": r"C:\Users\jdoe\Jobs",
           "last_task_result": 0, "number_of_missed_runs": 0,
           "description": f"Runs sync.py -Password {DUMMY}"}
    raw.update(overrides)
    return raw


def test_hide_paths_keeps_paths_out_of_the_config_notes():
    raw = _raw()
    (hidden,) = collect.build_inventory_rows([raw], host_name="H1", descriptions={}, exists=lambda p: False,
                                             hide_paths=True)
    assert "jdoe" not in hidden["config_warnings"]
    assert "script not found: (path hidden)" in hidden["config_warnings"]
    assert "working directory not found: (path hidden)" in hidden["config_warnings"]
    (shown,) = collect.build_inventory_rows([raw], host_name="H1", descriptions={}, exists=lambda p: False)
    assert r"script not found: C:\Users\jdoe\Jobs\sync.py" in shown["config_warnings"]


def test_hide_paths_blanks_the_path_fields_and_keeps_the_log_name():
    row = {"command_target_path": r"C:\Users\jdoe\x.py", "command_target_working_directory": r"C:\Users\jdoe",
           "latest_log_path": r"\\srv\logs\x.log", "latest_log_name": "x.log"}
    (prepared,) = publish.prepare_rows([row], publish.TABLE_INVENTORY, STAMP, {})
    assert prepared["command_target_path"] == prepared["command_target_working_directory"] == ""
    assert prepared["latest_log_path"] == "" and prepared["latest_log_name"] == "x.log"
    (kept,) = publish.prepare_rows([row], publish.TABLE_INVENTORY, STAMP, SHOWN)
    assert kept["latest_log_path"] == row["latest_log_path"]


def test_description_and_config_warnings_are_always_scrubbed():
    row = {"description": f"Runs with -Password {DUMMY}", "config_warnings": f"note pwd={DUMMY}"}
    (prepared,) = publish.prepare_rows([row], publish.TABLE_INVENTORY, STAMP, SHOWN)
    assert DUMMY not in json.dumps(prepared)


def test_collect_everything_reads_hide_paths_from_the_privacy_config(monkeypatch):
    seen = {}

    def fake_build(*a, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(collect, "build_inventory_rows", fake_build)
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout: [])
    monkeypatch.setattr(publish, "read_run_events", lambda *a: ([], False))
    monkeypatch.setattr(collect, "load_descriptions", lambda: {})
    for privacy, expected in (({}, True), ({"hide_paths": False}, False)):
        with pytest.raises(RuntimeError):
            publish.collect_everything({"privacy": privacy}, None)
        assert seen["hide_paths"] is expected


# ------------------------------------------------------------------ 5 / 6. view fields

class _Manager:
    def __init__(self, log, label, fail=False):
        self.log, self.label, self.fail = log, label, fail

    def update_definition(self, payload):
        self.log.append((self.label, json.loads(json.dumps(payload))))
        if self.fail and "fields" in payload:
            raise RuntimeError("update failed")
        return {"success": True}


def _view_tables(log, drop=(), fail=False):
    return {name: SimpleNamespace(manager=_Manager(log, name, fail),
                                  properties=SimpleNamespace(fields=[
                                      SimpleNamespace(name=f["name"]) for f in publish.TABLE_SPECS[name][0]()
                                      if f["name"] not in drop]))
            for name in publish.TABLE_SPECS}


@pytest.fixture
def flc(monkeypatch):
    log = []

    class _Flc:
        def __init__(self, item):
            self.manager = _Manager(log, "service")

        @classmethod
        def fromitem(cls, item):
            return cls(item)

    fake = types.ModuleType("arcgis.features")
    fake.FeatureLayerCollection = _Flc
    monkeypatch.setitem(sys.modules, "arcgis.features", fake)
    return log


def test_migrate_hides_fields_the_switches_hide_that_the_view_still_shows(flc, monkeypatch):
    tables = _view_tables(flc)
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    publish.migrate_view(object(), {"privacy": dict(SHOWN, hide_accounts=True)}, apply=True)
    fields = {label: payload["fields"] for label, payload in flc if "fields" in payload}
    assert fields[publish.TABLE_INVENTORY] == [{"name": "run_as_user", "visible": False},
                                               {"name": "definition_hash", "visible": False}]
    assert fields[publish.TABLE_RUNS] == [{"name": "user_context", "visible": False}]
    last_fields = max(i for i, (_, p) in enumerate(flc) if "fields" in p)
    assert any(p == {"capabilities": "Query"} for _, p in flc[last_fields:])


def test_migrate_check_mode_reports_the_fields_it_would_hide(flc, monkeypatch, capsys):
    monkeypatch.setattr(publish, "get_tables", lambda item: _view_tables(flc))
    publish.migrate_view(object(), {"privacy": SHOWN}, apply=False)
    assert flc == [] and "would hide field(s) definition_hash" in capsys.readouterr().out


@pytest.mark.parametrize("which", ["migrate", "create"])
def test_capabilities_are_resent_even_when_a_field_update_fails(flc, monkeypatch, which):
    tables = _view_tables(flc, fail=True)
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    with pytest.raises(RuntimeError, match="update failed"):
        if which == "migrate":
            publish.migrate_view(object(), {"privacy": SHOWN}, apply=True)
        else:
            view = _Item(item_id="view1")
            gis = SimpleNamespace(content=SimpleNamespace(search=lambda *a, **k: [], get=lambda i: view),
                                  users=SimpleNamespace(me=SimpleNamespace(username="u")))
            flc_cls = sys.modules["arcgis.features"].FeatureLayerCollection
            monkeypatch.setattr(flc_cls, "__init__", lambda self, item: setattr(
                self, "manager", SimpleNamespace(create_view=lambda **kw: view,
                                                 update_definition=_Manager(flc, "service").update_definition)))
            publish.create_view(gis, _Item(), {"privacy": SHOWN}, apply=True)
    assert ("service", {"capabilities": "Query"}) in flc


def test_create_view_stops_before_any_call_when_the_source_is_public(flc):
    gis = SimpleNamespace(content=SimpleNamespace(search=lambda *a, **k: pytest.fail("no search")))
    with pytest.raises(SystemExit, match="shared with everyone"):
        publish.create_view(gis, _Item("public"), {}, apply=True)
    assert flc == []


def test_create_view_warns_when_the_source_stays_shared(flc, monkeypatch, capsys):
    tables = _view_tables(flc)
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)
    view = _Item(item_id="view1")
    view.share = lambda **kw: {}
    gis = SimpleNamespace(content=SimpleNamespace(search=lambda *a, **k: [], get=lambda i: view),
                          users=SimpleNamespace(me=SimpleNamespace(username="u")))
    flc_cls = sys.modules["arcgis.features"].FeatureLayerCollection
    monkeypatch.setattr(flc_cls, "__init__", lambda self, item: setattr(
        self, "manager", SimpleNamespace(create_view=lambda **kw: view,
                                         update_definition=lambda p: {"success": True})))
    publish.create_view(gis, _Item("org"), {}, apply=True)
    assert "WARNING: the editable service svc1 is shared ('org')" in capsys.readouterr().out


def test_definition_hash_is_always_hidden_and_no_widget_reads_it():
    for config in ({}, {"privacy": SHOWN}):
        assert "definition_hash" in publish.view_hidden_fields(config)[publish.TABLE_INVENTORY]
    for name in ("taskdash_dashboard.py", "dashboard_templates.json"):
        assert "definition_hash" not in (HERE / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 5. --scrub-history

class _Table:
    def __init__(self, rows):
        self.rows = rows
        self.properties = SimpleNamespace(objectIdField="OBJECTID", maxRecordCount=1000)
        self.edits = []

    def query(self, *, where="1=1", out_fields="*", result_offset=0, result_record_count=None, **kw):
        names = out_fields.split(",")
        page = self.rows[result_offset:result_offset + self.properties.maxRecordCount]
        return SimpleNamespace(features=[SimpleNamespace(attributes={k: r.get(k) for k in names})
                                         for r in page])

    def edit_features(self, **kw):
        self.edits.append(kw)
        return {"updateResults": [{"success": True} for _ in kw.get("updates") or []]}


def _history_tables():
    return {
        publish.TABLE_INVENTORY: _Table([
            {"OBJECTID": 1, "actions": f"x.py -Password {DUMMY}", "run_as_user": "svc",
             "command_target_arguments": "--apply", "description": "", "config_warnings": None},
            {"OBJECTID": 2, "actions": "x.py --apply", "run_as_user": "", "command_target_arguments": "--apply",
             "description": "plain", "config_warnings": ""}]),
        publish.TABLE_RUNS: _Table([{"OBJECTID": 7, "user_context": "svc"}, {"OBJECTID": 8, "user_context": None}]),
        publish.TABLE_METRICS: _Table([{"OBJECTID": 3, "sample_line": f"pwd={DUMMY}"}]),
        publish.TABLE_COLLECTOR_RUNS: _Table([{"OBJECTID": 4, "error_text": "timed out"}]),
        publish.TABLE_TASK_CHANGES: _Table([{"OBJECTID": 5, "old_summary": f"-pw {DUMMY}", "new_summary": ""}]),
    }


def test_scrub_history_check_mode_counts_and_never_prints_values(capsys):
    tables = _history_tables()
    privacy = dict(SHOWN, hide_accounts=True)
    counts = publish.scrub_history(tables, privacy, apply=False)
    assert counts == {publish.TABLE_INVENTORY: 1, publish.TABLE_RUNS: 1, publish.TABLE_METRICS: 1,
                      publish.TABLE_COLLECTOR_RUNS: 0, publish.TABLE_TASK_CHANGES: 1}
    assert all(t.edits == [] for t in tables.values())
    out = capsys.readouterr().out
    assert DUMMY not in out and "svc" not in out and "would change" in out


def test_scrub_history_apply_updates_only_changed_rows_and_fields():
    tables = _history_tables()
    publish.scrub_history(tables, dict(SHOWN, hide_accounts=True), apply=True)
    (inventory_edit,) = tables[publish.TABLE_INVENTORY].edits
    (update,) = [u["attributes"] for u in inventory_edit["updates"]]
    assert update == {"OBJECTID": 1, "actions": "x.py -Password <redacted>", "run_as_user": ""}
    (runs_edit,) = tables[publish.TABLE_RUNS].edits
    assert [u["attributes"] for u in runs_edit["updates"]] == [{"OBJECTID": 7, "user_context": ""}]
    assert tables[publish.TABLE_COLLECTOR_RUNS].edits == []


def test_scrub_history_runs_alone_and_locks_with_apply(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        publish.parse_args(["--scrub-history", "--migrate"])
    seen = []
    monkeypatch.setattr(publish, "run_scrub_history", lambda config, args: seen.append(args.apply) or 0)
    monkeypatch.setattr(publish, "_run_under_lock", lambda log_dir, action: seen.append("lock") or action())
    for argv in (["--scrub-history"], ["--scrub-history", "--apply"]):
        assert publish._main_dispatch(publish.parse_args(argv), {}, tmp_path) == 0
    assert seen == [False, "lock", True]


# ------------------------------------------------------------------ 7. anonymous access

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(answers, seen):
    def fake(url, timeout=None):
        seen.append(url)
        answer = answers[url.split("/query?")[0]]
        if isinstance(answer, BaseException):
            raise answer
        return _Response(json.dumps(answer).encode("utf-8"))
    return fake


def _http_error(code, body=b""):
    return urllib.error.HTTPError("https://h/q", code, "denied", {}, io.BytesIO(body))


def test_anonymous_check_passes_only_on_refusals(monkeypatch):
    answers = {
        "https://h/svc/0": {"error": {"code": 499, "message": "Token Required"}},
        "https://h/svc/1": {"error": {"code": 403}},
        "https://h/view/0": _http_error(401, b'{"error": {"code": 499, "message": "Token Required"}}'),
        "https://h/view/1": _http_error(403, b'{"error": {"code": 403}}'),
    }
    seen = []
    monkeypatch.setattr("urllib.request.urlopen", _urlopen(answers, seen))
    assert publish.check_anonymous_access(list(answers)) == []
    assert all("token" not in u.lower() and "where=1%3D1&resultRecordCount=1&f=json" in u for u in seen)


def test_anonymous_check_fails_on_rows_other_errors_and_no_answer(monkeypatch):
    answers = {
        "https://h/view/0": {"features": [], "fields": []},
        "https://h/view/1": _http_error(500),
        "https://h/view/2": urllib.error.URLError("unreachable"),
        "https://h/view/3": {"error": {"code": 400}},
    }
    monkeypatch.setattr("urllib.request.urlopen", _urlopen(answers, []))
    problems = publish.check_anonymous_access(list(answers))
    assert len(problems) == 4 and "rows" in problems[0] and "HTTP 500" in problems[1]
    with pytest.raises(SystemExit, match="Owner"):
        publish.require_no_anonymous_access(list(answers))


@pytest.mark.parametrize("error", [
    _http_error(403),
    _http_error(401),
    _http_error(403, b"<html>Request blocked by firewall</html>"),
    _http_error(403, b'{"error": {"code": 400}}'),
], ids=["403-empty", "401-empty", "403-html", "403-other-code"])
def test_a_bare_http_refusal_is_inconclusive(monkeypatch, error):
    # A proxy or WAF can 403 the urllib user agent: only an ArcGIS error body proves a refusal.
    monkeypatch.setattr("urllib.request.urlopen", _urlopen({"https://h/view/0": error}, []))
    (problem,) = publish.check_anonymous_access(["https://h/view/0"])
    assert "inconclusive" in problem


def test_check_anonymous_flag_exits_0_or_1(monkeypatch):
    item, view = _Item(), _Item(item_id="view1")
    monkeypatch.setattr(publish, "connect_agol", lambda config: SimpleNamespace(
        content=SimpleNamespace(get=lambda i: view)))
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {"T": SimpleNamespace(url=f"https://h/{i.id}/0")})
    args = publish.parse_args(["--check-anonymous"])
    config = {"arcgis": {"view_item_id": "view1"}}
    refused = {"error": {"code": 499}}
    monkeypatch.setattr("urllib.request.urlopen", _urlopen({"https://h/svc1/0": refused,
                                                            "https://h/view1/0": refused}, []))
    assert publish.run_check_anonymous(config, args) == 0
    monkeypatch.setattr("urllib.request.urlopen", _urlopen({"https://h/svc1/0": refused,
                                                            "https://h/view1/0": {"features": []}}, []))
    assert publish.run_check_anonymous(config, args) == 1
    with pytest.raises(SystemExit):
        publish.parse_args(["--check-anonymous", "--apply"])


def test_migrate_apply_ends_with_the_anonymous_check(monkeypatch):
    item = _Item()
    monkeypatch.setattr(publish, "connect_agol", lambda config: "gis")
    monkeypatch.setattr(publish, "ensure_service", lambda gis, config, target: item)
    monkeypatch.setattr(publish, "get_tables", lambda i: {"T": SimpleNamespace(url="https://h/svc1/0")})
    monkeypatch.setattr(publish, "apply_migration", lambda tables, item=None: None)
    monkeypatch.setattr("urllib.request.urlopen", _urlopen({"https://h/svc1/0": {"features": []}}, []))
    with pytest.raises(SystemExit, match="Anonymous users may read"):
        publish.run_migration({"arcgis": {}}, SimpleNamespace(target="prod", apply=True))


# ------------------------------------------------------------------ 8. literals and CLI

def test_create_service_is_check_by_default(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(publish, "connect_agol", lambda config: pytest.fail("a plan must not connect"))
    assert publish._main_dispatch(publish.parse_args(["--create-service"]), {}, tmp_path) == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_create_service_snippet_is_generic(monkeypatch):
    seen = {}
    item = SimpleNamespace(id="new1")
    gis = SimpleNamespace(users=SimpleNamespace(me=SimpleNamespace(username="u")),
                          content=SimpleNamespace(search=lambda *a, **k: [],
                                                  create_service=lambda **kw: seen.update(kw) or item))
    fake = types.ModuleType("arcgis.features")
    fake.FeatureLayerCollection = SimpleNamespace(fromitem=lambda i: SimpleNamespace(
        manager=SimpleNamespace(add_to_definition=lambda d: {"success": True})))
    monkeypatch.setitem(sys.modules, "arcgis.features", fake)
    publish.create_service(gis, "TaskDash")
    assert seen["snippet"] == "Scheduled task health, run history and log metrics."


def test_version_flag_prints_the_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        publish.parse_args(["--version"])
    assert exit_info.value.code == 0
    assert taskdash_common.TASKDASH_VERSION in capsys.readouterr().out


# ------------------------------------------------------------------ 9. Interactive rule switch

def test_interactive_rule_default_and_off():
    assert publish.interactive_pattern({}) == collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN
    assert publish.interactive_pattern({"interactive_required_pattern": "\\Custom\\Py"}) == "\\Custom\\Py"
    pattern = publish.interactive_pattern({"interactive_required_pattern": False})
    assert pattern == ""
    assert collect.check_logon_type_drift(
        collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN + "\\python.exe", "Password", pattern) == []


@pytest.mark.parametrize("value", [None, "", "  "])
def test_interactive_rule_stays_on_unless_the_value_is_false(value):
    assert (publish.interactive_pattern({"interactive_required_pattern": value})
            == collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN)


def test_the_logon_note_names_the_single_use_licence():
    (note,) = collect.check_logon_type_drift(collect.DEFAULT_INTERACTIVE_REQUIRED_PATTERN + "\\python.exe",
                                             "Password")
    assert "With a Single Use licence, arcpy is licensed only in the Interactive session." in note


# ------------------------------------------------------------------ 10. capture warning

def test_capture_fixture_warns_that_the_file_is_private(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(collect, "collect_tasks", lambda timeout: [{"actions": f"x -Password {DUMMY}"}])
    monkeypatch.setattr(collect, "collect_run_events", lambda *a, **k: [])
    out = tmp_path / "f.json"
    assert publish._main_dispatch(publish.parse_args(["--capture-fixture", str(out)]), {}, tmp_path) == 0
    assert "WARNING: the fixture holds raw command lines and account names" in capsys.readouterr().out
    assert DUMMY in out.read_text(encoding="utf-8"), "raw values stay: the golden replay needs them"


# ------------------------------------------------------------------ 11. descriptions with a BOM

def test_workflow_descriptions_with_a_bom_load(tmp_path):
    path = tmp_path / "workflow_descriptions.json"
    path.write_text(json.dumps({"tasks": {"Sync": "Copies data."}}), encoding="utf-8-sig")
    assert collect.load_descriptions(path)["sync"]["description"] == "Copies data."


# ------------------------------------------------------------------ 12. review fixes (deploy gate)

class _NoSharedWith(_Item):
    @property
    def shared_with(self):
        raise AssertionError("shared_with makes portal requests; the hourly check must not read it")

    @shared_with.setter
    def shared_with(self, value):
        pass


def test_the_hourly_sharing_check_reads_only_access(capsys):
    config = {"arcgis": {"view_item_id": "v1"}}
    for access in ("private", "org", "shared", None):
        publish.check_source_sharing(_NoSharedWith(access), config, "prod")
    with pytest.raises(SystemExit, match="Set its sharing to Owner"):
        publish.check_source_sharing(_NoSharedWith("public"), config, "prod")
    assert capsys.readouterr().out.count("WARNING") == 2


def test_a_missing_access_says_nothing(capsys):
    item = _Item()
    item.access = None
    publish.check_source_sharing(item, {"arcgis": {"view_item_id": "v1"}}, "prod")
    assert capsys.readouterr().out == ""


def test_create_view_still_refuses_a_source_shared_with_everyone(flc):
    gis = SimpleNamespace(content=SimpleNamespace(search=lambda *a, **k: pytest.fail("no search")))
    with pytest.raises(SystemExit, match="shared with everyone"):
        publish.create_view(gis, _Item("org", shared={"everyone": True, "org": True, "groups": []}), {},
                            apply=True)


def test_a_failed_resend_does_not_mask_the_update_error(flc, monkeypatch, capsys):
    tables = _view_tables(flc, fail=True)
    monkeypatch.setattr(publish, "get_tables", lambda item: tables)

    def resend(view_item, view_tables):
        raise ConnectionError("portal down")

    monkeypatch.setattr(publish, "resend_view_capabilities", resend)
    with pytest.raises(RuntimeError, match="update failed"):
        publish.migrate_view(object(), {"privacy": SHOWN}, apply=True)
    out = capsys.readouterr().out
    assert "WARNING: re-sending the view capabilities" in out and "portal down" in out


def test_a_failed_resend_after_a_good_update_still_raises(flc, monkeypatch):
    monkeypatch.setattr(publish, "get_tables", lambda item: _view_tables(flc))
    monkeypatch.setattr(publish, "resend_view_capabilities",
                        lambda *a: (_ for _ in ()).throw(ConnectionError("portal down")))
    with pytest.raises(ConnectionError):
        publish.migrate_view(object(), {"privacy": SHOWN}, apply=True)


SUMMARY = (r"Actions: C:\Tools\python.exe C:\Jobs\sync.py --apply | D:\x.exe (Working folder: C:\Jobs) "
           r"| Runs as: CORP\svc_sync (Password, Limited) | Enabled: yes | Time limit: 3 days")


def test_summary_masking_is_a_no_op_with_every_switch_false():
    for text in (SUMMARY, r"script not found: \\srv\share\x.py", "", None):
        assert publish.mask_summary(text, SHOWN) == text


def test_summary_masking_follows_each_switch():
    accounts = publish.mask_summary(SUMMARY, dict(SHOWN, hide_accounts=True))
    assert r"CORP\svc_sync" not in accounts and "Runs as: (hidden) (Password, Limited)" in accounts
    commands = publish.mask_summary(SUMMARY, dict(SHOWN, hide_command_lines=True))
    assert commands.startswith("Actions: (hidden) | Runs as: CORP")
    paths = publish.mask_summary(SUMMARY + r" | not found: \\srv\share\Some Folder\x.py",
                                 dict(SHOWN, hide_paths=True))
    assert "Jobs" not in paths and "srv" not in paths and "Some Folder" not in paths
    assert r"(path hidden) --apply" in paths and "(Working folder: (path hidden))" in paths
    assert publish.mask_summary(paths, dict(SHOWN, hide_paths=True)) == paths


def test_task_change_rows_are_masked_when_written_and_by_scrub_history():
    row = {"old_summary": SUMMARY, "new_summary": rf"script not found: C:\Users\jdoe\x.py pwd={DUMMY}"}
    (shown,) = publish.prepare_rows([row], publish.TABLE_TASK_CHANGES, STAMP, SHOWN)
    assert shown["old_summary"] == SUMMARY
    (masked,) = publish.prepare_rows([row], publish.TABLE_TASK_CHANGES, STAMP, {})
    assert "svc_sync" not in json.dumps(masked) and "jdoe" not in json.dumps(masked)
    assert DUMMY not in json.dumps(masked)
    assert publish.plan_scrub_history([dict(row, OBJECTID=1)], publish.TABLE_TASK_CHANGES,
                                      {}, "OBJECTID") == [dict(masked, OBJECTID=1)]
    (unchanged,) = publish.plan_scrub_history([dict(shown, OBJECTID=1)], publish.TABLE_TASK_CHANGES, {},
                                              "OBJECTID")
    assert publish.plan_scrub_history([dict(unchanged, OBJECTID=1)], publish.TABLE_TASK_CHANGES, {},
                                      "OBJECTID") == []


def test_publish_passes_hide_paths_to_change_detection(monkeypatch):
    seen = {}
    monkeypatch.setattr(publish, "detect_task_changes", lambda *a: seen.update(paths=a[7]) or [])
    tables = {name: object() for name in publish.TABLE_SPECS}
    monkeypatch.setattr(publish, "get_tables", lambda i: tables)
    for name in ("upsert_by_key", "sync_inventory"):
        monkeypatch.setattr(publish, name, lambda *a, **k: {"added": 0, "updated": 0, "deleted": 0})
    for name in ("summarize_runs_from_agol", "_fetch_prior_inventory_for_changes"):
        monkeypatch.setattr(publish, name, lambda *a, **k: {})
    monkeypatch.setattr(publish, "fetch_other_host_counts", lambda *a, **k: [])
    monkeypatch.setattr(publish, "purge_older_than", lambda *a, **k: 0)
    monkeypatch.setattr(publish, "enrich_inventory", lambda *a, **k: None)
    monkeypatch.setattr(publish, "stamp_success_rate", lambda *a, **k: None)
    collected = {"runs": [], "inventory": [], "metrics": [], "host_name": "H1"}
    publish.publish(None, SimpleNamespace(), collected, {"privacy": SHOWN})
    assert seen == {"paths": False}


def _change_row(notes):
    return {"task_full_name": "\\T", "task_name": "T", "config_warnings": notes,
            "definition_hash": publish.DEFINITION_HASH_PREFIX + "a" * 62}


def test_a_path_that_became_path_hidden_is_not_a_note_change():
    old = _change_row(r"script not found: C:\Users\jdoe\x.py")
    new = _change_row("script not found: (path hidden)")
    assert publish.detect_task_changes({"\\T": old}, [new], "H", STAMP, hide_paths=True) == []
    changed = publish.detect_task_changes({"\\T": old}, [new], "H", STAMP)
    assert {c["change_kind"] for c in changed} == {"config_warning_added", "config_warning_cleared"}


@pytest.mark.parametrize("notes", [
    f"note with pwd={DUMMY} in it",
    "; ".join(f"note {i} " + "x" * 60 for i in range(20)),
], ids=["secret", "over-1000"])
def test_an_identical_second_pass_logs_no_note_change(notes):
    assert len(notes) > 1000 or DUMMY in notes
    new = _change_row(notes)
    (stored,) = publish.prepare_rows([new], publish.TABLE_INVENTORY, STAMP, SHOWN)
    assert stored["config_warnings"] != notes, "precondition: the stored copy differs from the raw"
    assert publish.detect_task_changes({"\\T": stored}, [dict(new)], "H", STAMP) == []


BYPASS_CASES = [
    (rf"psexec \\h -u u -p {DUMMY} cmd", "-p <redacted> cmd"),
    (rf"PaExec.exe \\h -p {DUMMY}", "-p <redacted>"),
    (f"schtasks /Create /RU u /RP {DUMMY} /TN t", "/RP <redacted> /TN t"),
    (f"schtasks /Create /S h /U u /P {DUMMY} /TN t", "/P <redacted> /TN t"),
    (f"sqlcmd -S s -U sa -P{DUMMY}", "-P<redacted>"),
    (f"bcp db.t out f.csv -S s -U u -P{DUMMY}", "-P<redacted>"),
    (f"7z a -p{DUMMY} x.7z", "7z a -p<redacted> x.7z"),
    (f"7za.exe x -p{DUMMY} x.7z", "-p<redacted> x.7z"),
    (f"curl --user=u:{DUMMY} https://h", "--user=u:<redacted> https://h"),
    (f"curl -uu:{DUMMY} https://h", "-uu:<redacted> https://h"),
    (rf"net use Z: \\s\sh /user:D\u {DUMMY}", r"/user:D\u <redacted>"),
    (rf'net use Z: "\\s\sh" {DUMMY} /persistent:no', r'"\\s\sh" <redacted> /persistent:no'),
    (f"powershell.exe -EncodedCommand {DUMMY}QQBCAA== -NoProfile", "-EncodedCommand <redacted> -NoProfile"),
    (f"powershell -enc {DUMMY}QQBCAA==", "-enc <redacted>"),
    (f"SECRET_KEY={DUMMY}", "SECRET_KEY=<redacted>"),
    (f"aws_secret_access_key={DUMMY}", "aws_secret_access_key=<redacted>"),
    (f"passphrase={DUMMY}", "passphrase=<redacted>"),
    (f"$pw = '{DUMMY}'", "$pw = <redacted>"),
    (f'$password = "{DUMMY} two"', "$password = <redacted>"),
    (f"$ApiToken='{DUMMY}'; Invoke-X", "$ApiToken=<redacted>; Invoke-X"),
]


@pytest.mark.parametrize("text,kept", BYPASS_CASES, ids=[str(i) for i in range(len(BYPASS_CASES))])
def test_the_remaining_scrubber_bypasses_are_closed(text, kept):
    scrubbed = publish._scrub_secrets(text)
    assert DUMMY not in scrubbed
    assert kept in scrubbed
    # Idempotent: notes comparison and --scrub-history re-scrub stored text, so a second pass that
    # changed it would log a change, or rewrite the row, every time.
    assert publish._scrub_secrets(scrubbed) == scrubbed


BYPASS_NOT_SECRETS = [
    "ssh -p 8080 host",
    "scp -P 22 f host:",
    "sqlcmd -S server -U user -i job.sql",
    r"net use Z: \\srv\share /user:DOM\u",
    "msbuild app.sln /p:Prop=Val",
    "python -u script.py",
    r"C:\Jobs\x.py -path C:\Out",
    r"mysql -h db -e q; tool -path C:\Out -profile p",
    r"schtasks /Create /RU u /RP /TN t",
    "psexec \\\\h cmd /c dir",
    "Out-File -Encoding utf8 x.txt",
    "$name = 'report'",
    "$path = 'C:\\Jobs'",
]


@pytest.mark.parametrize("text", BYPASS_NOT_SECRETS)
def test_the_new_scrubber_forms_leave_ordinary_text_alone(text):
    assert publish._scrub_secrets(text) == text


def test_the_scrubber_stays_linear_on_many_mysql_tokens():
    import time
    # 16 KB as specified; the 64 KB line is the one the old lazy-bridge pattern fails (seconds).
    for text in ("mysql " * 2731, "mysql " * 10923, "mysql -p" * 2048, "psexec -p " * 1640, "$" + "a" * 16000 + " = 1"):
        started = time.perf_counter()
        publish._scrub_secrets(text)
        assert time.perf_counter() - started < 0.5, text[:20]

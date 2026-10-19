"""Phase 3: taskdash_preflight, the read-only install checker.

Every check runs against fakes: no network, no sign-in, no real wevtutil, no Task Scheduler.
"""

from __future__ import annotations

import json
import smtplib
import ssl
from types import SimpleNamespace

import pytest

import taskdash_collect
import taskdash_preflight as pf
import taskdash_publish as publish
from taskdash_common import SCHEMA_VERSION

GOOD_LOG = (f"name: {pf.EVENT_LOG_NAME}\nenabled: true\nlogging:\n  retention: false\n"
            f"  maxSize: {pf.RECOMMENDED_LOG_MB * 1024 * 1024}\n")
ALL_PRIVILEGES = [key for key, _ in pf.REQUIRED_PRIVILEGES]


def make_ctx(config=None, **extra):
    args = SimpleNamespace(config="", offline=False)
    return pf.Context(args, config={"rules": {"profiles": []}, **(config or {})}, **extra)


def write_config(tmp_path, body=None):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"rules": {"profiles": []}, "arcgis": {"item_id": "svc1", "profile": "p1"},
                                **(body or {})}), encoding="utf-8")
    return str(path)


def fake_table(fields, indexes, url="https://example.invalid/0", capabilities=None):
    return SimpleNamespace(url=url, properties=SimpleNamespace(
        fields=fields, indexes=indexes, capabilities=capabilities))


def service_tables():
    return {name: fake_table(
        spec[0](), [{"fields": ",".join(i["fields"]), "isUnique": i["unique"]} for i in publish.TABLE_INDEXES[name]],
        url=f"https://example.invalid/svc/{name}")
        for name, spec in publish.TABLE_SPECS.items()}


def view_tables(config):
    hidden = publish.view_hidden_fields(config)
    return {name: fake_table([f for f in spec[0]() if f["name"] not in hidden.get(name, ())], [],
                             url=f"https://example.invalid/view/{name}", capabilities="Query")
            for name, spec in publish.TABLE_SPECS.items()}


class FakeItem:
    def __init__(self, item_id="svc1", access="private", stamp=SCHEMA_VERSION):
        self.id, self.access = item_id, access
        self.properties = {} if stamp is None else {"taskdash_schema_version": stamp}

    def update(self, *a, **k):
        raise AssertionError("preflight wrote to an item")

    @property
    def sharing(self):
        raise AssertionError("preflight read or changed an item's sharing")

    def delete(self, *a, **k):
        raise AssertionError("preflight deleted an item")


class FakeGis:
    def __init__(self, privileges=None, items=None, user_type="creatorUT", role="org_publisher"):
        self.users = SimpleNamespace(me=SimpleNamespace(
            username="svc_account", userType=user_type, role=role,
            privileges=ALL_PRIVILEGES if privileges is None else privileges))
        self.content = SimpleNamespace(get=lambda item_id: (items or {}).get(item_id))

    def __getattr__(self, name):
        raise AssertionError(f"preflight touched gis.{name}")


def fake_wevtutil(monkeypatch, stdout=GOOD_LOG, returncode=0, stderr="", calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(list(cmd))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(pf.subprocess, "run", run)


# --------------------------------------------------------------------------- 1 config

def test_config_ok_and_warning_listed(tmp_path):
    ctx = pf.Context(SimpleNamespace(config=write_config(tmp_path)))
    assert pf.check_config(ctx)[0] == pf.OK
    ctx = pf.Context(SimpleNamespace(config=write_config(tmp_path, {"mystery": 1, "arcgis": {"item_id": "x", "typo_key": 1}})))
    status, message = pf.check_config(ctx)
    assert status == pf.WARN and "mystery" in message and "typo_key" in message


def test_config_failure_stops_the_run_with_one_line(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert pf.main(["--config", str(path)]) == 1
    out = capsys.readouterr().out
    assert out.count("[!!] config: SystemExit:") == 1 and "not valid JSON" in out


# --------------------------------------------------------------------------- 2 credentials

def test_credentials_ok_reports_user_type_and_role(monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(pf, "connect_agol", lambda config: FakeGis())
    status, message = pf.check_credentials(ctx)
    assert status == pf.OK and "svc_account" in message and "creatorUT" in message and "org_publisher" in message


def test_credentials_failure_is_isolated(monkeypatch):
    def refuse(config):
        raise SystemExit("No ArcGIS credentials resolved. Set arcgis.profile.")
    monkeypatch.setattr(pf, "connect_agol", refuse)
    status, message = pf.run_check(pf.check_credentials, make_ctx())
    assert status == pf.FAIL and message.startswith("SystemExit: No ArcGIS credentials")


# --------------------------------------------------------------------------- 3 privileges

def test_privileges_ok():
    assert pf.check_privileges(make_ctx(gis=FakeGis()))[0] == pf.OK


def test_privileges_missing_names_the_privilege():
    gis = FakeGis(privileges=["portal:user:createItem", "features:user:edit"])
    status, message = pf.check_privileges(make_ctx(gis=gis))
    assert status == pf.FAIL and "portal:user:createGroup" in message and "portal:user:shareToGroup" in message
    assert "portal:user:createItem" not in message


@pytest.mark.parametrize("value", [None, [], "all"])
def test_privileges_unreadable(value):
    gis = FakeGis()
    gis.users.me.privileges = value
    assert pf.check_privileges(make_ctx(gis=gis)) == (pf.FAIL, "could not read privileges; confirm in ArcGIS Online")


def test_network_checks_fail_without_a_sign_in():
    for check in (pf.check_privileges, pf.check_service, pf.check_view, pf.check_sharing):
        assert pf.run_check(check, make_ctx())[0] == pf.FAIL


# --------------------------------------------------------------------------- 4 event log

def test_event_log_ok(monkeypatch):
    fake_wevtutil(monkeypatch)
    assert pf.check_event_log(make_ctx())[0] == pf.OK


def test_event_log_small_and_disabled_name_the_fix(monkeypatch):
    fake_wevtutil(monkeypatch, "enabled: false\n  maxSize: 1048576\n")
    status, message = pf.check_event_log(make_ctx())
    assert status == pf.FAIL and "disabled" in message and "1 MB" in message
    assert "--enable-event-log --apply" in message


def test_event_log_unreadable_is_a_crash_line(monkeypatch):
    fake_wevtutil(monkeypatch, returncode=5, stderr="Access is denied.")
    status, message = pf.run_check(pf.check_event_log, make_ctx())
    assert status == pf.FAIL and message.startswith("RuntimeError: wevtutil gl exited 5")


# --------------------------------------------------------------------------- 5 tasks

def test_tasks_counted_and_empty_fails(monkeypatch):
    monkeypatch.setattr(taskdash_collect, "collect_tasks", lambda timeout: [{"task_name": "A"}, {"task_name": "B"}])
    ctx = make_ctx()
    status, message = pf.check_tasks(ctx)
    assert status == pf.OK and "2 task" in message and len(ctx.tasks) == 2
    monkeypatch.setattr(taskdash_collect, "collect_tasks", lambda timeout: [])
    assert pf.check_tasks(make_ctx())[0] == pf.FAIL


# --------------------------------------------------------------------------- 6 log folders

def test_log_folders(tmp_path):
    assert pf.check_log_folders(make_ctx())[0] == pf.SKIP
    good = make_ctx({"metrics": {"log_directory_overrides": {"\\Nightly": str(tmp_path)}}})
    assert pf.check_log_folders(good)[0] == pf.OK
    bad = make_ctx({"metrics": {"log_directory_overrides": {"\\Nightly": str(tmp_path / "gone")}}})
    status, message = pf.check_log_folders(bad)
    assert status == pf.FAIL and "\\Nightly" in message and "gone" in message


# --------------------------------------------------------------------------- 7 smtp

class FakeSmtp:
    log: list = []

    def __init__(self, host, port, timeout=None, context=None):
        FakeSmtp.log.append(("connect", host, port, context))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        FakeSmtp.log.append(("quit",))

    def ehlo(self):
        FakeSmtp.log.append(("ehlo",))

    def starttls(self, context=None):
        FakeSmtp.log.append(("starttls", context))

    def login(self, *a):
        raise AssertionError("preflight logged in to the relay")

    def send_message(self, *a):
        raise AssertionError("preflight sent mail")

    sendmail = send_message


def smtp_config(**block):
    return {"alerting": {"enabled": True}, "smtp": {"host": "relay.example.invalid", "from": "a@example.invalid", **block}}


def test_smtp_skipped_when_alerting_off():
    assert pf.check_smtp(make_ctx())[0] == pf.SKIP


def test_smtp_block_verifies_certificate_and_never_logs_in(monkeypatch):
    FakeSmtp.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    status, _ = pf.check_smtp(make_ctx(smtp_config()))
    assert status == pf.OK
    steps = [entry[0] for entry in FakeSmtp.log]
    assert steps == ["connect", "ehlo", "starttls", "ehlo", "quit"]
    context = FakeSmtp.log[2][1]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_smtp_legacy_mail_file_keeps_its_unverified_tls(monkeypatch, tmp_path):
    FakeSmtp.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    mail_file = tmp_path / "mail.json"
    mail_file.write_text(json.dumps({"email settings": {"smtp server": "relay.example.invalid",
                                                        "from address": "a@example.invalid"}}), encoding="utf-8")
    ctx = make_ctx({"alerting": {"enabled": True, "mail_config_path": str(mail_file)}})
    assert pf.check_smtp(ctx)[0] == pf.OK
    assert FakeSmtp.log[2] == ("starttls", None)


def test_smtp_unreachable_and_unusable(monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(smtplib, "SMTP", refuse)
    status, message = pf.check_smtp(make_ctx(smtp_config()))
    assert status == pf.FAIL and "ConnectionRefusedError" in message and "smtp.host" in message
    status, message = pf.check_smtp(make_ctx({"alerting": {"enabled": True}}))
    assert status == pf.FAIL and "unusable" in message


# --------------------------------------------------------------------------- 8 service

def service_ctx(monkeypatch, tables=None, item=None):
    item = item or FakeItem()
    monkeypatch.setattr(pf, "ensure_service", lambda gis, config, target: item)
    monkeypatch.setattr(pf, "get_tables", lambda i: tables if tables is not None else service_tables())
    return make_ctx(gis=FakeGis())


def test_service_ok_and_unstamped_note(monkeypatch):
    status, message = pf.check_service(service_ctx(monkeypatch))
    assert status == pf.OK and f"schema version {SCHEMA_VERSION}" in message
    status, message = pf.check_service(service_ctx(monkeypatch, item=FakeItem(stamp=None)))
    assert status == pf.OK and "next --migrate --apply stamps it" in message


def test_service_newer_stamp_fails(monkeypatch):
    status, message = pf.check_service(service_ctx(monkeypatch, item=FakeItem(stamp=SCHEMA_VERSION + 1)))
    assert status == pf.FAIL and "Upgrade TaskDash" in message


def test_service_drift_missing_table_field_and_unique_index(monkeypatch):
    tables = service_tables()
    del tables[publish.TABLE_COLLECTOR_RUNS]
    tables[publish.TABLE_RUNS].properties.fields = tables[publish.TABLE_RUNS].properties.fields[:-1]
    tables[publish.TABLE_INVENTORY].properties.indexes = []
    status, message = pf.check_service(service_ctx(monkeypatch, tables))
    assert status == pf.FAIL
    assert publish.TABLE_COLLECTOR_RUNS in message and "TaskRuns missing field" in message
    assert "TaskInventory missing unique index: host_name+task_full_name" in message
    assert "--migrate --apply" in message


def test_service_missing_item_id_is_a_crash_line():
    ctx = make_ctx(gis=FakeGis())
    status, message = pf.run_check(pf.check_service, ctx)
    assert status == pf.FAIL and "No arcgis.item_id" in message


# --------------------------------------------------------------------------- 9 view and sharing

def view_ctx(monkeypatch, config=None, view_caps="Query", tables=None, view=None):
    config = {"arcgis": {"view_item_id": "view1"}, **(config or {})}
    ctx = make_ctx(config, gis=FakeGis(items={"view1": view or FakeItem("view1", "org")}))
    ctx.item, ctx.tables = FakeItem(), service_tables()
    vt = tables if tables is not None else view_tables(config)
    monkeypatch.setattr(pf, "get_tables", lambda i: vt)
    monkeypatch.setattr(pf, "_view_capabilities", lambda v: view_caps)
    return ctx


def test_view_skipped_without_view_id():
    assert pf.check_view(make_ctx(gis=FakeGis()))[0] == pf.SKIP


def test_view_ok(monkeypatch):
    assert pf.check_view(view_ctx(monkeypatch))[0] == pf.OK


def test_view_still_showing_hidden_field_fails(monkeypatch):
    config = {"arcgis": {"view_item_id": "view1"}}
    tables = {name: fake_table(spec[0](), [], capabilities="Query") for name, spec in publish.TABLE_SPECS.items()}
    status, message = pf.check_view(view_ctx(monkeypatch, tables=tables))
    assert status == pf.FAIL and "definition_hash" in message
    assert publish.view_shown_hidden_fields(tables, config)


def test_view_capabilities_must_be_query_only(monkeypatch):
    status, message = pf.check_view(view_ctx(monkeypatch, view_caps="Query,Update"))
    assert status == pf.FAIL and "view=Query,Update" in message
    ctx = view_ctx(monkeypatch)
    tables = view_tables(ctx.config)
    tables[publish.TABLE_RUNS].properties.capabilities = "Query,Editing"
    monkeypatch.setattr(pf, "get_tables", lambda i: tables)
    assert "TaskRuns=Query,Editing" in pf.check_view(ctx)[1]


def test_view_not_visible_is_a_crash_line(monkeypatch):
    ctx = view_ctx(monkeypatch)
    ctx.gis.content = SimpleNamespace(get=lambda item_id: None)
    status, message = pf.run_check(pf.check_view, ctx)
    assert status == pf.FAIL and "LookupError" in message and "view1" in message


def test_sharing_ok_checks_every_table_url(monkeypatch):
    ctx = view_ctx(monkeypatch)
    seen = []
    monkeypatch.setattr(pf, "check_anonymous_access", lambda urls: seen.extend(urls) or [])
    assert pf.check_sharing(ctx)[0] == pf.OK
    assert len(seen) == 2 * len(publish.TABLE_SPECS)


def test_sharing_shared_source_and_anonymous_answer_fail(monkeypatch):
    ctx = view_ctx(monkeypatch)
    ctx.item.access = "org"
    monkeypatch.setattr(pf, "check_anonymous_access", lambda urls: ["https://x/0: answered"])
    status, message = pf.check_sharing(ctx)
    assert status == pf.FAIL and "shared ('org')" in message and "answered" in message


def test_sharing_without_view_ignores_source_access_but_still_tests_anonymous(monkeypatch):
    ctx = make_ctx(gis=FakeGis())
    ctx.item, ctx.tables = FakeItem(access="org"), service_tables()
    seen = []
    monkeypatch.setattr(pf, "check_anonymous_access", lambda urls: seen.extend(urls) or [])
    assert pf.check_sharing(ctx)[0] == pf.OK and len(seen) == len(publish.TABLE_SPECS)


# --------------------------------------------------------------------------- 10 logon type

def task(name, logon, **extra):
    return {"task_name": name, "logon_type": logon, "trigger_details": [], **extra}


def test_logon_s4u_with_stored_credentials_fails():
    ctx = make_ctx({"arcgis": {"profile": "p1"}}, tasks=[task("TaskDash Collector", "S4U")])
    status, message = pf.check_logon_type(ctx)
    assert status == pf.FAIL and "S4U logon cannot read stored credentials" in message
    assert "Password logon type" in message and "arcgis.profile" in message


def test_logon_s4u_with_keyring_smtp_user_fails_but_plain_config_is_ok():
    keyring = {"alerting": {"enabled": True}, "smtp": {"user": "u1"}}
    assert pf.check_logon_type(make_ctx(keyring, tasks=[task("TaskDash Collector", "S4U")]))[0] == pf.FAIL
    assert pf.check_logon_type(make_ctx({"arcgis": {"username_env": "A"}}, tasks=[task("TaskDash Collector", "S4U")]))[0] == pf.OK


@pytest.mark.parametrize("logon", ["Password", "Interactive"])
def test_logon_password_and_interactive_are_ok(logon):
    ctx = make_ctx({"arcgis": {"profile": "p1"}}, tasks=[task("TaskDash Collector", logon)])
    assert pf.check_logon_type(ctx)[0] == pf.OK


def test_logon_task_not_found_names_the_installer_and_honours_the_configured_name():
    ctx = make_ctx(tasks=[task("Other", "Password")])
    status, message = pf.check_logon_type(ctx)
    assert status == pf.FAIL and "install_task.ps1" in message and "'TaskDash Collector'" in message
    ctx = make_ctx({"scheduler": {"collector_task_name": "Custom Collector"}},
                   tasks=[task("custom collector", "Password")])
    assert pf.check_logon_type(ctx)[0] == pf.OK


def test_logon_without_task_list_fails():
    assert pf.check_logon_type(make_ctx())[0] == pf.FAIL


# --------------------------------------------------------------------------- 10b collector account

def admin_ctx(user):
    return make_ctx(tasks=[task("TaskDash Collector", "Password", run_as_user=user)])


@pytest.mark.parametrize("user", ["DOMAIN\\svc-td", "svc-td", "SVC-TD"])
def test_admin_direct_member_is_ok_by_full_or_short_name(monkeypatch, user):
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("DOMAIN\\svc-td", "User"), ("HOST\\Administrator", "User")])
    assert pf.check_admin_account(admin_ctx(user))[0] == pf.OK


def test_admin_short_name_match_needs_an_account_without_a_domain(monkeypatch):
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\svc-td", "User")])
    assert pf.check_admin_account(admin_ctx("CORP\\svc-td"))[0] != pf.OK
    assert pf.check_admin_account(admin_ctx("svc-td"))[0] == pf.OK


def test_admin_standard_account_fails_with_the_net_localgroup_fix(monkeypatch):
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\Administrator", "User")])
    status, message = pf.check_admin_account(admin_ctx("HOST\\td_probe"))
    assert status == pf.FAIL and "misses other accounts' tasks" in message
    assert "net localgroup Administrators HOST\\td_probe /add" in message


def test_admin_possible_group_membership_only_warns(monkeypatch):
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("DOMAIN\\Domain Admins", "Group")])
    status, message = pf.check_admin_account(admin_ctx("DOMAIN\\svc-td"))
    assert status == pf.WARN and message.startswith("WARNING:") and "DOMAIN\\Domain Admins" in message


def test_admin_unreadable_group_warns_instead_of_failing(monkeypatch):
    def broken():
        raise SystemExit("PowerShell collection failed (exit 1): Failed to compare two elements")
    monkeypatch.setattr(pf, "read_admin_members", broken)
    status, message = pf.check_admin_account(admin_ctx("DOMAIN\\svc-td"))
    assert status == pf.WARN and "net localgroup Administrators" in message


def test_admin_system_task_and_unregistered_task_need_no_group_read(monkeypatch):
    monkeypatch.setattr(pf, "read_admin_members", lambda: pytest.fail("must not read the group"))
    assert pf.check_admin_account(admin_ctx("SYSTEM"))[0] == pf.OK
    assert pf.check_admin_account(admin_ctx(""))[0] == pf.WARN
    assert pf.check_admin_account(make_ctx(tasks=[task("Other", "Password")]))[0] == pf.SKIP
    assert pf.check_admin_account(make_ctx())[0] == pf.FAIL


# --------------------------------------------------------------------------- 11 scale

def test_scale_within_tested_ok():
    ctx = make_ctx(tasks=[task(f"T{i}", "Password") for i in range(pf.TESTED_TASK_COUNT)])
    assert pf.check_scale(ctx)[0] == pf.OK


def test_scale_too_many_tasks_warns():
    ctx = make_ctx(tasks=[task(f"T{i}", "Password") for i in range(pf.TESTED_TASK_COUNT + 1)])
    status, message = pf.check_scale(ctx)
    assert status == pf.WARN and message.startswith("WARNING") and "may work" in message


def test_scale_counts_only_monitored_tasks_and_flags_fast_repetition():
    microsoft = [task(f"M{i}", "Password", task_path="\\Microsoft\\Windows\\") for i in range(40)]
    assert pf.check_scale(make_ctx(tasks=microsoft))[0] == pf.OK
    fast = task("Fast", "Password", trigger_details=[{"enabled": True, "repetition_interval": "PT5M"}])
    assert pf.check_scale(make_ctx(tasks=[fast]))[0] == pf.WARN


# --------------------------------------------------------------------------- 12 health check

def test_health_check_blank_warns_without_failing():
    for config in ({}, {"alerting": {"healthcheck_url": "  "}}):
        status, message = pf.check_health_check(make_ctx(config))
        assert status == pf.WARN and message.startswith("WARNING")
        assert "not set" in message and "alerting.healthcheck_url" in message and "never starts" in message


def test_health_check_set_passes_and_hides_the_url():
    url = "https://hc.example/secret-ping-id"
    status, message = pf.check_health_check(make_ctx({"alerting": {"healthcheck_url": url}}))
    assert status == pf.OK and "set" in message and "secret-ping-id" not in message and url not in message


def test_health_check_warning_keeps_preflight_exit_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "CHECKS", (("health check", False, pf.check_health_check),))
    args = SimpleNamespace(config=write_config(tmp_path), offline=True)
    assert pf.run_preflight(args) == 0


# --------------------------------------------------------------------------- the runner

def test_crash_in_one_check_does_not_stop_the_next(tmp_path, monkeypatch, capsys):
    def boom(ctx):
        raise KeyError("missing")
    monkeypatch.setattr(pf, "CHECKS", (("boom", False, boom), ("fine", False, lambda ctx: (pf.OK, "fine"))))
    assert pf.main(["--config", write_config(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "[!!] boom: KeyError: 'missing'" in out and "[ok] fine: fine" in out


def test_warnings_do_not_set_exit_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pf, "CHECKS", (("scale", False, lambda ctx: (pf.WARN, "WARNING: big")),))
    assert pf.main(["--config", write_config(tmp_path, {"mystery": 1})]) == 0
    assert "Preflight: 0 problem(s), 2 warning(s)." in capsys.readouterr().out


def local_fakes(monkeypatch, tasks=None):
    fake_wevtutil(monkeypatch)
    monkeypatch.setattr(taskdash_collect, "collect_tasks",
                        lambda timeout: tasks if tasks is not None
                        else [task("TaskDash Collector", "Password", run_as_user="HOST\\svc-td")])
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\svc-td", "User")])


def test_offline_makes_no_network_call(tmp_path, monkeypatch, capsys):
    def forbidden(*a, **k):
        raise AssertionError("network call with --offline")
    for name in ("connect_agol", "get_tables", "ensure_service", "check_anonymous_access", "probe_smtp"):
        monkeypatch.setattr(pf, name, forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    local_fakes(monkeypatch)
    config = write_config(tmp_path, {"alerting": {"enabled": True}, "smtp": {"host": "h", "from": "f"}})
    assert pf.main(["--config", config, "--offline"]) == 0
    lines = capsys.readouterr().out.splitlines()
    skipped = [line for line in lines if line.startswith("[--]")]
    assert [line.split(":")[0] for line in skipped] == [
        "[--] credentials", "[--] privileges", "[--] log folders",
        "[--] smtp", "[--] service", "[--] view", "[--] sharing"]
    assert any(line.startswith("[ok] event log") for line in lines)


def test_full_run_with_fakes_makes_no_write(tmp_path, monkeypatch, capsys):
    config_path = write_config(tmp_path, {"arcgis": {"item_id": "svc1", "view_item_id": "view1", "profile": "p1"},
                                          "alerting": {"healthcheck_url": "https://hc.example/ping-id"}})
    config = json.loads(open(config_path, encoding="utf-8").read())
    gis = FakeGis(items={"svc1": FakeItem("svc1"), "view1": FakeItem("view1", "org")})
    monkeypatch.setattr(pf, "connect_agol", lambda c: gis)
    monkeypatch.setattr(pf, "get_tables",
                        lambda item: service_tables() if item.id == "svc1" else view_tables(config))
    monkeypatch.setattr(pf, "_view_capabilities", lambda v: "Query")
    monkeypatch.setattr(pf, "check_anonymous_access", lambda urls: [])
    local_fakes(monkeypatch)
    assert pf.main(["--config", config_path]) == 0
    out = capsys.readouterr().out
    assert "[!!]" not in out and out.count("[ok]") == 12 and "[--] smtp" in out and "ping-id" not in out
    assert "Preflight: 0 problem(s), 0 warning(s)." in out


# --------------------------------------------------------------------------- --enable-event-log

def test_enable_event_log_check_mode_never_calls_set_log(monkeypatch, capsys):
    calls = []
    fake_wevtutil(monkeypatch, "enabled: false\n  maxSize: 1048576\n", calls=calls)
    assert pf.main(["--enable-event-log"]) == 0
    assert [c[1] for c in calls] == ["gl"]
    out = capsys.readouterr().out
    expected = f"wevtutil sl {pf.EVENT_LOG_NAME} /e:true /ms:{pf.RECOMMENDED_LOG_MB * 1024 * 1024}"
    assert expected in out and "nothing changed" in out


def test_enable_event_log_apply_runs_set_log_with_the_right_args_and_rereads(monkeypatch, capsys):
    calls, states = [], iter(["enabled: false\n  maxSize: 1048576\n", "enabled: true\n  maxSize: 33554432\n"])

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=next(states) if cmd[1] == "gl" else "", stderr="")
    monkeypatch.setattr(pf.subprocess, "run", run)
    assert pf.main(["--enable-event-log", "--apply", "--max-size-mb", "32"]) == 0
    assert [c[1] for c in calls] == ["gl", "sl", "gl"]
    assert calls[1] == ["wevtutil", "sl", pf.EVENT_LOG_NAME, "/e:true", f"/ms:{32 * 1024 * 1024}"]
    assert "enabled=true, max size 33554432" in capsys.readouterr().out


def test_enable_event_log_default_size_never_shrinks_a_larger_log(monkeypatch):
    calls = []
    fake_wevtutil(monkeypatch, "enabled: true\n  maxSize: 268435456\n", calls=calls)
    pf.main(["--enable-event-log"])
    assert calls == [["wevtutil", "gl", pf.EVENT_LOG_NAME]]
    calls.clear()
    pf.main(["--enable-event-log", "--apply"])
    assert calls[1][-1] == "/ms:268435456"


def test_enable_event_log_non_admin_failure_says_elevated(monkeypatch, capsys):
    def run(cmd, **kwargs):
        if cmd[1] == "gl":
            return SimpleNamespace(returncode=0, stdout="enabled: false\n  maxSize: 1048576\n", stderr="")
        return SimpleNamespace(returncode=5, stdout="", stderr="Access is denied.")
    monkeypatch.setattr(pf.subprocess, "run", run)
    assert pf.main(["--enable-event-log", "--apply"]) == 1
    assert "run from an elevated prompt" in capsys.readouterr().out


# --------------------------------------------------------------------------- arguments

def test_apply_alone_and_abbreviations_are_refused(capsys):
    for argv in (["--apply"], ["--max-size-mb", "8"], ["--enable-event-log", "--app"], ["--offl"]):
        with pytest.raises(SystemExit) as exc:
            pf.main(argv)
        assert exc.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        pf.main(["--version"])
    assert exc.value.code == 0 and "TaskDash" in capsys.readouterr().out

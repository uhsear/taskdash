"""Phase 3: taskdash_preflight, the read-only install checker.

Every check runs against fakes: no network, no sign-in, no real wevtutil, no Task Scheduler.
"""

from __future__ import annotations

import json
import os
import smtplib
import socket
import ssl
from types import SimpleNamespace

import pytest

import taskdash_collect
import taskdash_preflight as pf
import taskdash_publish as publish
from taskdash_common import SCHEMA_VERSION, system32_exe

GOOD_LOG = (f"name: {pf.EVENT_LOG_NAME}\nenabled: true\nlogging:\n  retention: false\n"
            f"  maxSize: {pf.RECOMMENDED_LOG_MB * 1024 * 1024}\n")
ALL_PRIVILEGES = [key for key, _ in pf.REQUIRED_PRIVILEGES]


def make_ctx(config=None, **extra):
    args = SimpleNamespace(config="", offline=False)
    config = dict(config or {})
    config["arcgis"] = {"item_id": "svc1", **config.get("arcgis", {})}  # a set-up install; "" means not yet
    return pf.Context(args, config={"rules": {"profiles": []}, **config}, **extra)


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
    def __init__(self, item_id="svc1", access="private", stamp=SCHEMA_VERSION, protected=True):
        self.id, self.access, self.protected = item_id, access, protected
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
    monkeypatch.setattr(taskdash_collect, "collect_tasks", lambda timeout, *_: [{"task_name": "A"}, {"task_name": "B"}])
    ctx = make_ctx()
    status, message = pf.check_tasks(ctx)
    assert status == pf.OK and "2 task" in message and len(ctx.tasks) == 2
    monkeypatch.setattr(taskdash_collect, "collect_tasks", lambda timeout, *_: [])
    assert pf.check_tasks(make_ctx())[0] == pf.FAIL


def test_a_host_with_only_microsoft_tasks_passes_the_task_check(monkeypatch):
    calls = []

    def fake(timeout, exclude=True):
        calls.append(exclude)
        return [] if exclude else [{"task_name": "\\Microsoft\\Windows\\X"}]

    monkeypatch.setattr(taskdash_collect, "collect_tasks", fake)
    status, message = pf.check_tasks(make_ctx())
    assert status == pf.OK and "no task outside" in message and calls == [True, False]


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
    return {"alerting": {"enabled": True, "to": ["ops@example.invalid"]}, "smtp": {"host": "relay.example.invalid", "from": "a@example.invalid", **block}}


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
    ctx = make_ctx({"alerting": {"enabled": True, "to": ["ops@example.invalid"], "mail_config_path": str(mail_file)}})
    status, message = pf.check_smtp(ctx)
    assert status == pf.WARN
    assert "deprecated" in message and "not verified" in message and "smtp block" in message
    assert str(mail_file) not in message
    assert FakeSmtp.log[2] == ("starttls", None)


def test_smtp_legacy_file_with_login_and_no_tls_fails(monkeypatch, tmp_path):
    FakeSmtp.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    mail_file = tmp_path / "mail.json"
    mail_file.write_text(json.dumps({"email settings": {
        "smtp server": "relay.example.invalid", "from address": "a@example.invalid",
        "use tls": False, "smtp username": "u", "smtp password": "secret-pw"}}), encoding="utf-8")
    status, message = pf.check_smtp(make_ctx({"alerting": {"enabled": True, "to": ["ops@example.invalid"], "mail_config_path": str(mail_file)}}))
    assert status == pf.FAIL and "plain text" in message and "secret-pw" not in message
    assert FakeSmtp.log == [], "the check must stop before it connects"


def test_smtp_unreachable_and_unusable(monkeypatch):
    def refuse(*a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(smtplib, "SMTP", refuse)
    status, message = pf.check_smtp(make_ctx(smtp_config()))
    assert status == pf.FAIL and "ConnectionRefusedError" in message and "smtp.host" in message
    status, message = pf.check_smtp(make_ctx({"alerting": {"enabled": True, "to": ["ops@example.invalid"]}}))
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


def test_service_newer_stamp_warns_with_the_advice_collect_gives(monkeypatch, capsys):
    # After a rollback collect keeps publishing, so preflight must not call it a failure.
    item = FakeItem(stamp=SCHEMA_VERSION + 1)
    status, message = pf.check_service(service_ctx(monkeypatch, item=item))
    assert status == pf.WARN and message.startswith("WARNING")
    publish.warn_if_service_schema_newer(item)
    assert message == capsys.readouterr().out.strip()
    assert "Install the newer TaskDash" in message


def test_service_newer_stamp_does_not_hide_schema_drift(monkeypatch):
    tables = service_tables()
    del tables[publish.TABLE_COLLECTOR_RUNS]
    status, _ = pf.check_service(service_ctx(monkeypatch, tables, FakeItem(stamp=SCHEMA_VERSION + 1)))
    assert status == pf.FAIL


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


def test_service_without_an_item_id_is_not_set_up_yet_not_a_failure():
    for ctx in (make_ctx({"arcgis": {"item_id": ""}}, gis=FakeGis()), make_ctx({"arcgis": {"item_id": ""}})):
        status, message = pf.run_check(pf.check_service, ctx)
        assert status == pf.SKIP and "arcgis.item_id" in message and "taskdash setup --apply" in message


@pytest.mark.parametrize("key", ["view_item_id", "dashboard_id"])
def test_other_install_keys_without_an_item_id_fail_as_a_damaged_install(key):
    status, message = pf.run_check(pf.check_service, make_ctx({"arcgis": {"item_id": "", key: "x1"}}))
    assert status == pf.FAIL and f"arcgis.{key}" in message and "arcgis.item_id" in message
    assert "The service is not in the config" in message


def test_sharing_without_an_item_id_is_not_set_up_yet_but_a_missing_read_still_fails():
    status, message = pf.check_sharing(make_ctx({"arcgis": {"item_id": ""}}, gis=FakeGis()))
    assert status == pf.SKIP and "arcgis.item_id" in message
    assert pf.check_sharing(make_ctx(gis=FakeGis()))[0] == pf.FAIL


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


def fake_dashboard(view_id="view1", access="org", item_id="dash1"):
    """A Dashboard item whose layer data sources name view_id; reading is all it supports."""
    dash = FakeItem(item_id, access)
    source = {"dataSource": {"type": "layerDataSource", "itemId": view_id, "layerId": 0}}
    data = {"desktopView": {"widgets": [{"datasets": [source]}]}, "mobileView": {"widgets": []}}
    dash.get_data = lambda: data
    return dash


def dashboard_ctx(dash, view_id="view1"):
    config = {"arcgis": {"dashboard_id": "dash1", "view_item_id": view_id}}
    return make_ctx(config, gis=FakeGis(items={"dash1": dash} if dash else {}))


def test_dashboard_skipped_without_dashboard_id():
    assert pf.check_dashboard(make_ctx(gis=FakeGis()))[0] == pf.SKIP


def test_dashboard_ok_when_it_reads_the_view_and_is_not_public():
    status, message = pf.check_dashboard(dashboard_ctx(fake_dashboard()))
    assert status == pf.OK and "dash1" in message


def test_dashboard_missing_warns_with_the_fix():
    status, message = pf.check_dashboard(dashboard_ctx(None))
    assert status == pf.WARN and message.startswith("WARNING") and "arcgis.dashboard_id" in message
    assert "taskdash dashboard --target prod --apply" in message


def test_dashboard_shared_with_everyone_warns():
    status, message = pf.check_dashboard(dashboard_ctx(fake_dashboard(access="public")))
    assert status == pf.WARN and "everyone" in message


def test_dashboard_reading_another_item_than_the_view_warns():
    status, message = pf.check_dashboard(dashboard_ctx(fake_dashboard(view_id="svc9")))
    assert status == pf.WARN and "do not read the view view1" in message


@pytest.mark.parametrize("widget", [{}, {"datasets": [{}]}, {"datasets": [{"dataSource": None}]},
                                    {"datasets": None}])
def test_a_hand_edited_widget_without_datasets_warns_instead_of_failing(widget):
    dash = fake_dashboard()
    data = {"desktopView": {"widgets": [widget]}}
    dash.get_data = lambda: data
    status, message = pf.check_dashboard(dashboard_ctx(dash))
    assert status == pf.WARN and "do not read the view view1" in message


def test_a_dashboard_desktop_view_without_widgets_warns():
    dash = fake_dashboard()
    dash.get_data = lambda: {"desktopView": {"sidebar": {}}}
    assert pf.check_dashboard(dashboard_ctx(dash))[0] == pf.WARN


def test_dashboard_without_a_view_does_not_judge_its_data_sources():
    config = {"arcgis": {"dashboard_id": "dash1"}}
    ctx = make_ctx(config, gis=FakeGis(items={"dash1": fake_dashboard(view_id="svc9")}))
    assert pf.check_dashboard(ctx)[0] == pf.OK


def protection_ctx(service_protected=True, view_protected=True):
    view = FakeItem("view1", "org", protected=view_protected)
    ctx = make_ctx({"arcgis": {"view_item_id": "view1"}}, gis=FakeGis(items={"view1": view}))
    ctx.item = FakeItem("svc1", protected=service_protected)
    return ctx


def test_delete_protection_ok_when_service_and_view_are_protected():
    assert pf.check_delete_protection(protection_ctx())[0] == pf.OK


def test_delete_protection_off_warns_naming_each_item_and_the_fix():
    status, message = pf.check_delete_protection(protection_ctx(False, False))
    assert status == pf.WARN and message.startswith("WARNING")
    assert "service svc1" in message and "view view1" in message
    assert "Item > Settings > Delete protection" in message and "taskdash setup" in message


def test_delete_protection_warns_for_the_one_item_that_is_off():
    message = pf.check_delete_protection(protection_ctx(view_protected=False))[1]
    assert "view view1" in message and "service svc1" not in message


def test_delete_protection_skipped_when_no_item_was_read():
    ctx = make_ctx(gis=FakeGis())
    assert pf.check_delete_protection(ctx)[0] == pf.SKIP


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


def test_logon_task_not_registered_yet_skips_names_the_installer_and_honours_the_configured_name():
    ctx = make_ctx({"arcgis": {"item_id": ""}}, tasks=[task("Other", "Password")])
    status, message = pf.check_logon_type(ctx)
    assert status == pf.SKIP and "install_task.ps1" in message and "'TaskDash Collector'" in message
    ctx = make_ctx({"scheduler": {"collector_task_name": "Custom Collector"}},
                   tasks=[task("custom collector", "Password")])
    assert pf.check_logon_type(ctx)[0] == pf.OK


def test_logon_without_task_list_fails():
    assert pf.check_logon_type(make_ctx())[0] == pf.FAIL


@pytest.mark.parametrize("check", [pf.check_logon_type, pf.check_admin_account])
def test_a_set_up_install_with_no_visible_collector_task_warns_instead_of_passing(check):
    # Once the service exists the task should too. A standard window cannot see another account's
    # task, and a wrong scheduler.collector_task_name looks the same, so [--] would hide both.
    status, message = check(make_ctx(tasks=[task("Other", "Password")]))
    assert status == pf.WARN and message.startswith("WARNING:")
    assert "'TaskDash Collector'" in message and "administrator" in message
    assert "scheduler.collector_task_name" in message


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
    assert pf.check_admin_account(make_ctx({"arcgis": {"item_id": ""}}, tasks=[task("Other", "Password")]))[0] == pf.SKIP
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


# --------------------------------------------------------------------------- host name override

def test_host_name_override_mismatch_warns_with_both_fixes(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "SYNTH-NEW-01")
    status, message = pf.check_host_name(make_ctx({"host_name_override": "SYNTH-OLD-01"}))
    assert status == pf.WARN and message.startswith("WARNING")
    assert "SYNTH-OLD-01" in message and "SYNTH-NEW-01" in message
    assert "host_previous_names" in message and "--allow-host-mismatch" in message


def test_host_name_override_blank_or_equal_is_silent(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "SYNTH-NEW-01")
    for config in ({}, {"host_name_override": "  "}, {"host_name_override": "synth-new-01"}):
        assert pf.check_host_name(make_ctx(config))[0] == pf.OK


def test_preflight_and_the_publish_refusal_share_one_mismatch_test(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "SYNTH-NEW-01")
    assert publish.host_mismatch({"host_name_override": " SYNTH-OLD-01 "}) == ("SYNTH-OLD-01", "SYNTH-NEW-01")
    assert publish.host_mismatch({"host_name_override": "synth-new-01"}) is None
    assert publish.host_mismatch({}) is None
    calls = []
    monkeypatch.setattr(pf, "host_mismatch", lambda config: calls.append(config) or None)
    assert pf.check_host_name(make_ctx({"host_name_override": "SYNTH-OLD-01"}))[0] == pf.OK
    assert len(calls) == 1


# --------------------------------------------------------------------------- base Python and install folder

USER_PYTHON = r"C:\Users\synth\AppData\Local\Programs\Python\Python311"
SHARED_PYTHON = r"C:\Program Files\Python311"


@pytest.mark.parametrize("prefix", [USER_PYTHON, USER_PYTHON.lower(), r"C:\USERS\synth\Python311"])
def test_base_python_under_users_warns_with_the_fix(monkeypatch, prefix):
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(pf.sys, "base_prefix", prefix)
    status, message = pf.check_base_python(make_ctx())
    assert status == pf.WARN and message.startswith("WARNING") and prefix in message
    assert "for all users" in message and "docs/INSTALL.md" in message


@pytest.mark.parametrize("prefix", [SHARED_PYTHON, r"C:\UsersShared\Python311", r"D:\Users\Python311"])
def test_base_python_outside_users_is_ok(monkeypatch, prefix):
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(pf.sys, "base_prefix", prefix)
    assert pf.check_base_python(make_ctx())[0] == pf.OK


HOME = r"C:\SynthTaskDash"
LOCKED_ACL = (f"{HOME} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)\n"
              "           BUILTIN\\Administrators:(OI)(CI)(F)\n"
              "           HOST\\svc-td:(OI)(CI)(M)\n\n"
              "Successfully processed 1 files; Failed processing 0 files\n")


def fake_icacls(monkeypatch, stdout=LOCKED_ACL, returncode=0, stderr="", calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append(list(cmd))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(pf.subprocess, "run", run)
    monkeypatch.setattr(pf, "home_dir", lambda: HOME)
    monkeypatch.setattr(pf, "read_owner", lambda path: ("S-1-5-32-544", "BUILTIN\\Administrators"))
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\svc-td", "User")])


@pytest.mark.parametrize("sid", list(pf.TRUSTED_OWNER_SIDS))
def test_install_folder_owned_by_system_administrators_or_trustedinstaller_is_ok(monkeypatch, sid):
    fake_icacls(monkeypatch)
    monkeypatch.setattr(pf, "read_owner", lambda path: (sid, ""))
    assert pf.check_install_folder_acl(make_ctx())[0] == pf.OK


def test_install_folder_owned_by_a_direct_administrator_is_ok(monkeypatch):
    fake_icacls(monkeypatch)
    monkeypatch.setattr(pf, "read_owner", lambda path: ("S-1-5-21-1-2-3-1001", "HOST\\Admin"))
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\admin", "User"), ("HOST\\svc-td", "User")])
    assert pf.check_install_folder_acl(make_ctx())[0] == pf.OK


@pytest.mark.parametrize("members", [[("HOST\\other", "User")]])
def test_install_folder_owned_by_a_non_administrator_fails_with_the_setowner_fix(monkeypatch, members):
    # A locked folder still belongs to its owner, who can grant themselves write again at any time.
    fake_icacls(monkeypatch)
    monkeypatch.setattr(pf, "read_owner", lambda path: ("S-1-5-21-1-2-3-1001", "HOST\\Admin"))
    monkeypatch.setattr(pf, "read_admin_members", lambda: members + [("HOST\\svc-td", "User")])
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.FAIL and "HOST\\Admin" in message
    assert f'icacls "{HOME}" /setowner *S-1-5-32-544' in message


def test_install_folder_owner_with_an_unreadable_administrators_group_only_warns(monkeypatch):
    fake_icacls(monkeypatch)
    monkeypatch.setattr(pf, "read_owner", lambda path: ("S-1-5-21-1-2-3-1001", "HOST\\Admin"))

    def unreadable():
        raise OSError("no group")
    monkeypatch.setattr(pf, "read_admin_members", unreadable)
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.WARN and message.startswith("WARNING") and "net localgroup Administrators" in message


def test_install_folder_owner_unreadable_or_unresolved_still_warns(monkeypatch):
    fake_icacls(monkeypatch)

    def denied(path):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(pf, "read_owner", denied)
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.WARN and "could not read the owner" in message
    monkeypatch.setattr(pf, "read_owner", lambda path: ("S-1-5-21-9-9-9-1234", ""))
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\svc-td", "User")])
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.FAIL and "S-1-5-21-9-9-9-1234" in message


def test_read_owner_reads_a_real_folder_through_the_win32_api():
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    sid, name = pf.read_owner(windir)
    assert sid == pf.TRUSTED_OWNER_SIDS[2] and name.lower().endswith("trustedinstaller")
    with pytest.raises(OSError):
        pf.read_owner(os.path.join(windir, "no-such-folder-taskdash"))


def test_install_folder_acl_locked_is_ok_and_icacls_runs_by_full_path(monkeypatch):
    calls = []
    fake_icacls(monkeypatch, calls=calls)
    assert pf.check_install_folder_acl(make_ctx())[0] == pf.OK
    assert calls == [[system32_exe("icacls.exe"), HOME]]


@pytest.mark.parametrize("who", ["BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users", "Everyone", "*S-1-5-11"])
@pytest.mark.parametrize("rights", ["(OI)(CI)(M)", "(F)", "(I)(W)", "(OI)(CI)(IO)(M)", "(CI)(DE,WD,AD)", "(GW)", "(OI)(CI)(DE)"])
def test_install_folder_acl_with_a_write_right_for_everyone_fails_with_the_icacls_fix(monkeypatch, who, rights):
    fake_icacls(monkeypatch, LOCKED_ACL.replace("\n\n", f"\n           {who}:{rights}\n\n"))
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.FAIL and who in message and HOME in message
    assert "icacls" in message and "/inheritance:r" in message and "*S-1-5-32-545" in message
    assert "administrator" in message


@pytest.mark.parametrize("rights", ["(OI)(CI)(F)", "(OI)(CI)(M)", "(W)"])
def test_install_folder_acl_with_a_write_right_for_a_non_administrator_fails(monkeypatch, rights):
    # The daily account gets Read & Execute only: a write right is control of the administrator task account.
    fake_icacls(monkeypatch, LOCKED_ACL.replace("\n\n", f"\n           HOST\\daily:{rights}\n\n"))
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.FAIL and "HOST\\daily" in message and "not administrators" in message
    assert f'icacls "{HOME}" /grant:r "HOST\\daily:(OI)(CI)RX"' in message


def test_install_folder_acl_with_a_read_only_daily_account_and_creator_owner_is_ok(monkeypatch):
    acl = LOCKED_ACL.replace("\n\n", "\n           HOST\\daily:(OI)(CI)(RX)\n           CREATOR OWNER:(OI)(CI)(IO)(F)\n\n")
    fake_icacls(monkeypatch, acl)
    assert pf.check_install_folder_acl(make_ctx())[0] == pf.OK


def test_install_folder_acl_with_an_unreadable_administrators_group_warns_about_other_writers(monkeypatch):
    fake_icacls(monkeypatch)

    def unreadable():
        raise OSError("no group")
    monkeypatch.setattr(pf, "read_admin_members", unreadable)
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.WARN and "HOST\\svc-td" in message


def test_install_folder_acl_accepts_a_task_account_that_is_an_administrator_through_a_group(monkeypatch):
    # The usual domain setup: the task account holds Modify and is in no direct-member list.
    fake_icacls(monkeypatch)
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\Domain Admins", "Group"), ("HOST\\Admin", "User")])
    assert pf.check_install_folder_acl(admin_ctx("HOST\\svc-td")) == (
        pf.OK, f"{HOME} is writable only by SYSTEM, Administrators and administrators")
    assert pf.check_install_folder_acl(admin_ctx("svc-td"))[0] == pf.OK


def test_install_folder_acl_warns_for_another_group_admin_writer_and_never_asks_to_lower_the_task_account(monkeypatch):
    acl = LOCKED_ACL.replace("\n\n", "\n           HOST\\daily:(OI)(CI)(M)\n\n")
    fake_icacls(monkeypatch, acl)
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\Domain Admins", "Group")])
    status, message = pf.check_install_folder_acl(admin_ctx("HOST\\svc-td"))
    assert status == pf.WARN and "HOST\\daily" in message and "Domain Admins" in message
    assert "svc-td" not in message and "/grant:r" not in message
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\Admin", "User")])
    status, message = pf.check_install_folder_acl(admin_ctx("HOST\\svc-td"))
    assert status == pf.FAIL and "HOST\\daily" in message and "svc-td" not in message


def test_install_folder_owner_that_may_be_an_administrator_through_a_group_only_warns(monkeypatch):
    fake_icacls(monkeypatch)
    monkeypatch.setattr(pf, "read_owner", lambda path: ("S-1-5-21-1-2-3-1001", "HOST\\Admin"))
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\Domain Admins", "Group"), ("HOST\\svc-td", "User")])
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.WARN and message.startswith("WARNING") and "Domain Admins" in message
    assert f'icacls "{HOME}" /setowner *S-1-5-32-544' in message


def test_install_folder_acl_names_a_loose_entry_on_the_first_line_too(monkeypatch):
    fake_icacls(monkeypatch, LOCKED_ACL.replace(f"{HOME} NT AUTHORITY\\SYSTEM:(OI)(CI)(F)", f"{HOME} Everyone:(F)"))
    assert "Everyone" in pf.check_install_folder_acl(make_ctx())[1]


@pytest.mark.parametrize("rights", ["(OI)(CI)(RX)", "(R)", "(I)(RX)", "(OI)(CI)(DENY)(W)", "(N)"])
def test_install_folder_acl_read_only_or_denied_entries_pass(monkeypatch, rights):
    fake_icacls(monkeypatch, LOCKED_ACL.replace("\n\n", f"\n           BUILTIN\\Users:{rights}\n\n"))
    assert pf.check_install_folder_acl(make_ctx())[0] == pf.OK


GERMAN_NAMES = {"s-1-5-32-545": "benutzer", "s-1-5-11": "authentifizierte benutzer", "s-1-1-0": "jeder"}
GERMAN_ACL = (f"{HOME} NT-AUTORITAET\\SYSTEM:(OI)(CI)(F)\n"
              "           VORDEFINIERT\\Administratoren:(OI)(CI)(F)\n"
              "           HOST\\svc-td:(OI)(CI)(M)\n\n"
              "Successfully processed 1 files; Failed processing 0 files\n")


def fake_local_names(monkeypatch, names):
    monkeypatch.setattr(pf, "local_names", lambda sids: {names[sid.lower()] for sid in sids if sid.lower() in names})


@pytest.mark.parametrize("who", ["VORDEFINIERT\\Benutzer", "NT-AUTORITAET\\Authentifizierte Benutzer", "Jeder"])
def test_install_folder_acl_finds_a_loose_writer_by_its_local_language_name(monkeypatch, who):
    fake_icacls(monkeypatch, GERMAN_ACL.replace("\n\n", f"\n           {who}:(OI)(CI)(M)\n\n"))
    fake_local_names(monkeypatch, {**GERMAN_NAMES, "s-1-5-18": "system", "s-1-5-32-544": "administratoren"})
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.FAIL and who in message


def test_install_folder_acl_never_says_ok_when_it_recognises_no_trusted_principal(monkeypatch):
    # Neither SYSTEM nor Administrators is listed under a name this check knows: the language is not
    # understood, so a clean result would be a guess.
    fake_icacls(monkeypatch, GERMAN_ACL.replace("NT-AUTORITAET\\SYSTEM", "AUTORITE NT\\Systeme")
                .replace("Administratoren", "Administrateurs"))
    fake_local_names(monkeypatch, {})
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.WARN and message.startswith("WARNING") and f'icacls "{HOME}"' in message


def test_install_folder_acl_in_another_language_is_ok_once_the_local_names_resolve(monkeypatch):
    fake_icacls(monkeypatch, GERMAN_ACL)
    fake_local_names(monkeypatch, {**GERMAN_NAMES, "s-1-5-18": "system", "s-1-5-32-544": "administratoren"})
    assert pf.check_install_folder_acl(make_ctx())[0] == pf.OK


def test_install_folder_acl_unreadable_warns_and_a_missing_icacls_too(monkeypatch):
    fake_icacls(monkeypatch, "", returncode=5, stderr="Access is denied.")
    status, message = pf.check_install_folder_acl(make_ctx())
    assert status == pf.WARN and message.startswith("WARNING") and f'icacls "{HOME}"' in message

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(pf.subprocess, "run", missing)
    assert pf.check_install_folder_acl(make_ctx())[0] == pf.WARN


# --------------------------------------------------------------------------- the [--] marker

def test_unset_ids_and_no_task_give_skip_lines_without_a_problem(tmp_path, monkeypatch, capsys):
    gis = FakeGis()
    monkeypatch.setattr(pf, "connect_agol", lambda config: gis)
    monkeypatch.setattr(pf, "CHECKS", (("credentials", True, pf.check_credentials),
                                       ("task definitions", False, pf.check_tasks),
                                       ("service", True, pf.check_service), ("sharing", True, pf.check_sharing),
                                       ("collector logon type", False, pf.check_logon_type)))
    monkeypatch.setattr(taskdash_collect, "collect_tasks", lambda timeout, *_: [task("Other", "Password")])
    config = write_config(tmp_path, {"arcgis": {"profile": "p1"}})
    assert pf.main(["--config", config]) == 0
    out = capsys.readouterr().out
    assert "[--] service:" in out and "[--] sharing:" in out and "[--] collector logon type:" in out
    assert "[!!]" not in out and "Preflight: 0 problem(s), 0 warning(s)." in out


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
    monkeypatch.setattr(pf, "handoff_waiting", lambda: None)
    monkeypatch.setattr(pf, "home_dir", lambda: HOME)
    monkeypatch.setattr(taskdash_collect, "collect_tasks",
                        lambda timeout, *_: tasks if tasks is not None
                        else [task("TaskDash Collector", "Password", run_as_user="HOST\\svc-td",
                                   primary_working_directory=HOME, primary_executable=HOME + "\\venv\\Scripts\\taskdash.exe")])
    monkeypatch.setattr(pf, "read_admin_members", lambda: [("HOST\\svc-td", "User")])
    monkeypatch.setattr(pf, "powershell_language_mode", lambda: "FullLanguage")
    monkeypatch.setattr(pf.sys, "base_prefix", SHARED_PYTHON)
    monkeypatch.setattr(pf, "read_acl", lambda path: LOCKED_ACL)
    monkeypatch.setattr(pf, "read_owner", lambda path: ("S-1-5-32-544", "BUILTIN\\Administrators"))
    # The descriptions check reads the repo's own workflow_descriptions.json otherwise.
    monkeypatch.setattr(pf, "runtime_dir", lambda: pf.Path("no-such-folder"))


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
        "[--] credentials", "[--] privileges", "[--] log folders", "[--] descriptions",
        "[--] smtp", "[--] service", "[--] view", "[--] sharing", "[--] dashboard", "[--] delete protection",
        "[--] hand-off"]
    assert any(line.startswith("[ok] event log") for line in lines)


def test_full_run_with_fakes_makes_no_write(tmp_path, monkeypatch, capsys):
    config_path = write_config(tmp_path, {"arcgis": {"item_id": "svc1", "view_item_id": "view1",
                                                      "dashboard_id": "dash1", "profile": "p1"},
                                          "alerting": {"healthcheck_url": "https://hc.example/ping-id"},
                                          "metrics": {"log_roots": ["D:/Jobs"]}})
    config = json.loads(open(config_path, encoding="utf-8").read())
    gis = FakeGis(items={"svc1": FakeItem("svc1"), "view1": FakeItem("view1", "org"),
                         "dash1": fake_dashboard()})
    monkeypatch.setattr(pf, "connect_agol", lambda c: gis)
    monkeypatch.setattr(pf, "get_tables",
                        lambda item: service_tables() if item.id == "svc1" else view_tables(config))
    monkeypatch.setattr(pf, "_view_capabilities", lambda v: "Query")
    monkeypatch.setattr(pf, "check_anonymous_access", lambda urls: [])
    local_fakes(monkeypatch)
    assert pf.main(["--config", config_path]) == 0
    out = capsys.readouterr().out
    assert "[!!]" not in out and out.count("[ok]") == 21 and "[--] smtp" in out and "ping-id" not in out
    assert "[ok] dashboard" in out and "[ok] delete protection" in out
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
    assert calls[1] == [system32_exe("wevtutil.exe"), "sl", pf.EVENT_LOG_NAME, "/e:true", f"/ms:{32 * 1024 * 1024}"]
    assert "enabled=true, max size 33554432" in capsys.readouterr().out


def test_enable_event_log_default_size_never_shrinks_a_larger_log(monkeypatch):
    calls = []
    fake_wevtutil(monkeypatch, "enabled: true\n  maxSize: 268435456\n", calls=calls)
    pf.main(["--enable-event-log"])
    assert calls == [[system32_exe("wevtutil.exe"), "gl", pf.EVENT_LOG_NAME]]
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

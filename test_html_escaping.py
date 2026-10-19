"""Task-derived text must not reach Dashboards list HTML (or the e-mail) as markup or control bytes."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import taskdash_dashboard as dash  # noqa: E402
import taskdash_publish as pub  # noqa: E402

HOSTILE = "<img src=x onerror=alert(1)> & done"


def _esc_in_python() -> str:
    """Run the esc() chain from FMT_HELPERS: its Replace pairs, applied in order."""
    body = re.search(r"function esc\(t\) \{ return (.*); \}", dash.FMT_HELPERS).group(1)
    text = HOSTILE
    for find, repl in (re.findall(r'"([^"]*)", "([^"]*)"\)', body)):
        text = text.replace(find, repl)
    return text


def test_esc_helper_escapes_ampersand_first_and_angle_brackets():
    assert _esc_in_python() == "&lt;img src=x onerror=alert(1)&gt; &amp; done"
    assert dash.FMT_HELPERS.index('"&", "&amp;"') < dash.FMT_HELPERS.index('"<", "&lt;"')


# attribute -> text that must appear in the expression, proving the value is escaped
ESCAPED = {
    "detail": ["descText: esc(", "whyText: IIf(kind == \"\", \"\", \"Why: \" + esc(",
               "lastText: ", "scheduleText: ", "esc(DefaultValue($datapoint[\"command_target_path\"]",
               "esc(DefaultValue($datapoint[\"run_as_user\"]", "esc(DefaultValue($datapoint[\"settings_detail\"]",
               "logPathText: logPath", "cfgText: IIf(cw == \"\", \"\", \"• \" + esc("],
    "failures": ["resultText: esc("],
    "changes": ["var oldS = esc(Replace(", "var newS = esc(Replace("],
    "notes": ["noteText: IIf(cw == \"\", \"No setting note.\", \"• \" + esc("],
    "metric": ["metricText: esc(", "logText: esc("],
    "mobile": ["reasonText = esc(shortText("],
}


@pytest.mark.parametrize("which", sorted(ESCAPED))
def test_every_task_derived_list_value_is_escaped(which):
    exprs = {
        "detail": dash.detail_list_expression(),
        "failures": dash.failure_list_expression(),
        "changes": dash.changes_list_expression(),
        "notes": dash.config_notes_expression(),
        "metric": dash.METRIC_LIST_EXPRESSION,
        "mobile": dash.mobile_attention_expression(),
    }
    for needle in ESCAPED[which]:
        assert needle in exprs[which], needle
    assert "function esc(t)" in exprs[which]


def test_detail_free_text_variables_are_escaped_where_they_are_read():
    for var in ("cmdPath", "args", "workDir", "logPath", "runAs", "detail", "settingsDetail"):
        assert re.search(rf"var {var} = esc\(", dash.DETAIL_VARS), var


def test_hidden_fields_still_blank_out_inside_esc():
    expression = dash.detail_list_expression(hidden=["latest_log_path"])
    assert 'var logPath = esc("");' in expression


def _built_dashboard():
    import json

    template = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in template["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return dash.build("TESTITEM", templates)


def test_built_dashboard_escapes_every_field_a_list_routes_through_arcade():
    """{task_name} and {host_name} become {expression/f_*} in the generated JSON; the Arcade that
    feeds them must escape < > & because the list text is HTML."""
    routed = 0
    for widget in dash.all_items(_built_dashboard()):
        if not (widget.get("arcadeEnabled") and "text" in widget and "expression" in widget):
            continue
        assert not re.search(r"\{[A-Za-z_]\w*\}", widget["text"]), widget["name"]
        for line in re.findall(r"^\s+f_\w+: .*$", widget["expression"], re.MULTILINE):
            routed += 1
            assert line.count("Replace(") == 3, f"{widget['name']}: {line}"
            assert '"&", "&amp;"' in line and '"<", "&lt;"' in line and '">", "&gt;"' in line
    assert routed >= 2, "the build routes no field through Arcade, so this test proves nothing"


def test_route_fields_through_arcade_escapes_ampersand_before_angle_brackets():
    expression, text = dash.route_fields_through_arcade(
        "return {\n" + dash.ARCADE_ATTRIBUTES + "  }\n}", "<p>{task_name}</p>")
    line = next(l for l in expression.splitlines() if "f_task_name" in l)
    assert line.index('"&", "&amp;"') < line.index('"<", "&lt;"')
    assert text == "<p>{expression/f_task_name}</p>"


def test_email_body_drops_control_characters_but_keeps_tab_and_newline():
    cleaned = pub.strip_control_chars("ok\x00\x1b[31m\tred\x07\nnext\r\x7fend")
    assert cleaned == "ok[31m\tred\nnextend"


def test_email_body_and_subject_carry_no_control_characters():
    collected = {"host_name": "HOST\x1b1", "inventory": [{
        "task_name": "Job\x07A", "attention_kind": "FAILED", "needs_attention": 1,
        "attention_reason": "bad\x00 <b>x</b>\x1b[0m", "last_run_failed": 1}]}
    subject, body = pub._alert_message(collected, {}, pub.utc_now(), "changed", ["FAILED: Job\x07A"], [])
    for text in (subject, body):
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text)
    assert "JobA" in body and "&lt;" not in body  # plain text: no HTML escaping in the e-mail

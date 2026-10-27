"""H3-11: a refusal that docs/TROUBLESHOOTING.md documents prints the section to open.

The helper builds the line. A second test keeps every heading named in the code in step with the
docs, so renaming a heading fails here instead of leaving a dead pointer in a message."""

import ast
import re
from pathlib import Path

import pytest

import taskdash_common as common
import taskdash_publish as publish

ROOT = Path(__file__).resolve().parent
TROUBLESHOOTING = ROOT / "docs" / "TROUBLESHOOTING.md"
DEV_MODULES = {"taskdash_synthetic.py"}


def doc_headings():
    found = set()
    for line in TROUBLESHOOTING.read_text(encoding="utf-8").splitlines():
        match = re.match(r"#{2,4} (.+?)\s*$", line)
        if match:
            found.add(match.group(1))
    return found


def referenced_headings():
    """{heading: [file:line]} for every see_troubleshooting(...) call in the shipped modules."""
    found = {}
    for path in sorted(ROOT.glob("taskdash_*.py")):
        if path.name in DEV_MODULES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None))
                    == "see_troubleshooting"):
                arg = node.args[0] if node.args else None
                assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                    f"{path.name}:{node.lineno}: pass the heading as a string literal, so the test can read it")
                found.setdefault(arg.value, []).append(f"{path.name}:{node.lineno}")
    return found


def test_the_helper_names_the_file_and_the_section():
    assert common.see_troubleshooting("The config file is missing") == (
        "See TROUBLESHOOTING.md, section The config file is missing.")


def test_every_heading_named_in_the_code_exists_in_the_docs():
    referenced = referenced_headings()
    assert len(referenced) >= 30, "the covered sites were removed or renamed"
    missing = {h: sites for h, sites in referenced.items() if h not in doc_headings()}
    assert not missing, (f"headings in the code but not in docs/TROUBLESHOOTING.md: {missing}. "
                         f"Rename the heading in the code, or restore it in the docs.")


@pytest.mark.parametrize("text, heading", [
    ("not json {", "The config is not valid JSON"),
    ("[1, 2]", "The config is not a JSON object"),
    ('{"scheduler": 5}', "The config has problems"),
])
def test_a_bad_config_prints_its_section(tmp_path, text, heading):
    path = tmp_path / "TaskDash.config.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SystemExit) as raised:
        common.load_config(path)
    assert str(raised.value).splitlines()[-1] == common.see_troubleshooting(heading)


def test_a_missing_config_and_a_non_utf8_config_print_their_sections(tmp_path):
    path = tmp_path / "TaskDash.config.json"
    with pytest.raises(SystemExit) as raised:
        common.load_config(path)
    assert str(raised.value).endswith(common.see_troubleshooting("The config file is missing"))
    path.write_bytes(b"\xff\xfe{\x00}\x00")
    with pytest.raises(SystemExit) as raised:
        common.load_config(path)
    assert str(raised.value).endswith(common.see_troubleshooting("The config is not UTF-8 text"))


def test_both_hand_off_failures_print_their_sections(tmp_path, monkeypatch):
    import taskdash_handoff as handoff

    monkeypatch.setattr(handoff, "runtime_dir", lambda: tmp_path)
    (tmp_path / handoff.HANDOFF_FILE).write_bytes(b"x")
    monkeypatch.setattr(handoff, "unseal", lambda blob: (_ for _ in ()).throw(OSError("wrong machine")))
    with pytest.raises(SystemExit) as raised:
        handoff.consume_handoff()
    assert str(raised.value).endswith(common.see_troubleshooting("The hand-off file could not be opened"))
    monkeypatch.setattr(handoff, "unseal", lambda blob: {"account": "svc-demo"})
    monkeypatch.setattr(handoff.getpass, "getuser", lambda: "svc-demo")
    monkeypatch.setattr(handoff, "_store", lambda payload: (_ for _ in ()).throw(RuntimeError("no")))
    with pytest.raises(SystemExit) as raised:
        handoff.consume_handoff()
    assert str(raised.value).endswith(common.see_troubleshooting("The hand-off sign-in was kept"))


def test_the_refusals_in_the_sharing_and_retire_host_paths_print_their_sections():
    with pytest.raises(SystemExit) as raised:
        publish.refuse_public_sharing(True)
    assert str(raised.value).endswith(common.see_troubleshooting("Public sharing is refused"))
    with pytest.raises(SystemExit) as raised:
        publish.retire_host_name("*", "THIS-PC")
    assert str(raised.value).endswith(common.see_troubleshooting("`--retire-host` refuses the name"))
    with pytest.raises(SystemExit) as raised:
        publish.retire_host_name("this-pc", "THIS-PC")
    assert str(raised.value).endswith(common.see_troubleshooting("`--retire-host` refuses the name"))

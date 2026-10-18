"""List what the docs must cover: every config key, every CLI flag and every SystemExit message.

    python _tools/doc_inventory.py                  # print what the docs do not cover yet
    python _tools/doc_inventory.py --write-pending  # rewrite tests/docs_pending.json from that

test_docs_coverage.py fails when an item is neither covered nor listed in tests/docs_pending.json,
and when the pending file lists an item that is covered or gone. So the pending list can only
shrink as the docs grow; the Phase 4 definition of done is an empty pending list.

Covered means:
- a config key appears in docs/CONFIGURE.md as `dotted.key` (in backticks);
- a CLI flag appears in docs/CONFIGURE.md as `--flag`;
- a SystemExit message's key text appears in docs/TROUBLESHOOTING.md. The key text is the
  longest literal run of the message (f-string placeholders split it), with whitespace collapsed.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIGURE = ROOT / "docs" / "CONFIGURE.md"
TROUBLESHOOTING = ROOT / "docs" / "TROUBLESHOOTING.md"
PENDING = ROOT / "tests" / "docs_pending.json"
# The sets in taskdash_common.py that validate leaf config keys by type.
KEY_SETS = ("_CONFIG_STRING_KEYS", "_CONFIG_NUMBER_KEYS", "_CONFIG_BOOL_KEYS",
            "_CONFIG_STRING_LIST_KEYS", "_CONFIG_DICT_KEYS", "_CONFIG_LIST_OF_DICT_KEYS")


# taskdash_synthetic.py ships only so `taskdash demo` can build data; its own command line is a
# developer tool for the test fixtures, not a user command.
DEV_MODULES = {"taskdash_synthetic.py"}


def _modules():
    return sorted(p for p in ROOT.glob("taskdash_*.py") if p.name not in DEV_MODULES)


def _string_set(node):
    """The string literals of a set display, following `A | {...}` unions."""
    if isinstance(node, ast.Set):
        return {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _string_set(node.left) | _string_set(node.right)
    return set()


def config_keys():
    tree = ast.parse((ROOT / "taskdash_common.py").read_text(encoding="utf-8"))
    keys = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id in KEY_SETS for t in targets):
            keys |= _string_set(node.value)
    return keys


def cli_flags():
    flags = set()
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                flags |= {a.value for a in node.args
                          if isinstance(a, ast.Constant) and str(a.value).startswith("--")}
    return flags


def _literal_runs(node):
    """Literal text runs of a str or f-string expression; a placeholder ends a run."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        runs, current = [], ""
        for part in node.values:
            if isinstance(part, ast.Constant):
                current += part.value
            else:
                runs.append(current)
                current = ""
        return runs + [current]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_runs(node.left), _literal_runs(node.right)
        if left and right:
            return left[:-1] + [left[-1] + right[0]] + right[1:]
        return left + right
    return []


def message_key(node):
    runs = [" ".join(r.split()) for r in _literal_runs(node)]
    return max(runs, key=len, default="").strip(" .:;,'\"")


def exit_messages(short=None):
    """{key text: 'file:line'} for every SystemExit raised with a message. Sites whose key text is
    too short to find in prose go to the `short` list instead, so none is dropped silently."""
    found = {}
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "SystemExit" and node.args):
                arg = node.args[0]
                if isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "main":
                    continue  # `raise SystemExit(main())` passes an exit code, not a message
                key = message_key(node.args[0])
                site = f"{path.name}:{node.lineno}"
                if len(key) >= 12:  # shorter runs cannot be found reliably in prose
                    found.setdefault(key, site)
                elif short is not None:
                    short.append(site)
    return found


def _text(path):
    return " ".join(path.read_text(encoding="utf-8").split()) if path.is_file() else ""


def uncovered():
    configure, troubleshooting = _text(CONFIGURE), _text(TROUBLESHOOTING)
    return {
        "config_keys": sorted(k for k in config_keys() if f"`{k}`" not in configure),
        "cli_flags": sorted(f for f in cli_flags() if f"`{f}`" not in configure),
        "exit_messages": sorted(m for m in exit_messages() if m not in troubleshooting),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--write-pending", action="store_true", help="Rewrite tests/docs_pending.json.")
    args = parser.parse_args(argv)
    missing = uncovered()
    if args.write_pending:
        PENDING.write_text(json.dumps(missing, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {PENDING.relative_to(ROOT)}.")
    for kind, items in missing.items():
        print(f"{kind}: {len(items)} not covered")
    short = []
    exit_messages(short)
    if short:
        print("SystemExit sites with no usable key text (document them by hand):", ", ".join(short))
    return 0


if __name__ == "__main__":
    sys.exit(main())

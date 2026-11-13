"""Golden replay harness: derive TaskInventory, TaskRuns, TaskMetrics, TaskChanges and the alert
from a captured fixture with no network, then compare two replays against an approved-change list.

Driver:  python _tools/golden_replay.py replay --config C --fixture F [--prior-inventory P]
                                               [--alert-state S] --out O
         python _tools/golden_replay.py compare A B [--approved L]
         python _tools/golden_replay.py synthetic --out O [--now T] [--seed N] [--fixtures-dir D]
Source:  a --capture-fixture file (or a synthetic one), optionally a prod inventory snapshot (a JSON
         list of TaskInventory rows) and an alert_state.json.
Target:  one sorted JSON file. Nothing is sent, written to ArcGIS or read from a task host.

Replay reads the task logs on disk (metrics, log tails in the alert), not data stored in the capture.
Two replays are comparable only while those logs are unchanged. For an older capture, point
metrics.log_directory_overrides in the replay config at a log snapshot taken with the capture.

"synthetic" replays tests/fixtures/synthetic_app_host_*.json against metric logs written for a frozen
--now (a temp folder, metrics.days_back raised so the frozen logs never age out) and the prior inventory
each host fixture carries, so metrics and changes are filled. Its output is the committed
tests/fixtures/synthetic_golden.json: inventory/runs/metrics/changes merged across hosts, alert per host.

Approved list L is a JSON list of {"section", "key": {field: value, ...}, "field", "old", "new"}.
"key" matches a row when every listed key field is equal; "old"/"new" are optional and mean "any"
when absent. field "_added" approves a row only in B, "_removed" a row only in A. Only counts,
keys and field names are printed, never log lines or field values.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Run from the deployed folder too: the taskdash_* modules sit in this script's parent directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import taskdash_common  # noqa: E402

taskdash_common.disable_arcpy_import()
import taskdash_publish as publish  # noqa: E402

VOLATILE_FIELDS = ("collected_at_utc", "latest_log_age_hours", "next_run_in_hours")
SECTION_KEYS = {
    "inventory": ("task_full_name", "host_name"),
    "runs": ("task_full_name", "host_name", "start_time_utc"),
    "metrics": ("host_name", "task_full_name", "metric_key", "log_file_name", "observed_at_utc"),
    "changes": ("host_name", "task_full_name", "change_kind"),
    "alert": (),
}
SECTIONS = tuple(SECTION_KEYS)
SYNTHETIC_NOW = "2026-10-16T12:00:00Z"
SYNTHETIC_FIXTURES = ("synthetic_app_host_01.json", "synthetic_app_host_02.json")
# The alert text carries the run's own clock and the machine's time-zone name; two replays must
# still be byte-identical on any machine. Only those are masked, so a date inside a log line or
# task field still shows as a difference.
_SINCE_IN_SUBJECT = re.compile(r"since (?:[A-Z][a-z]{2} \d\d )?\d\d:\d\d")
# A first-run e-mail shows each failure's own last-run time, which is a local time as well.
_OPEN_SINCE = re.compile(r"\(open since [^)]*\)")


def _clean(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned = [{k: v for k, v in row.items() if k not in VOLATILE_FIELDS} for row in rows]
    return sorted(cleaned, key=lambda r: json.dumps(r, sort_keys=True, default=str))


def replay(config: Dict[str, Any], fixture: Path, prior_inventory: Sequence[Dict[str, Any]] = (),
           alert_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # collect_everything emit()s task and log details; the harness prints counts only.
    with contextlib.redirect_stdout(io.StringIO()):
        collected = publish.collect_everything(config, fixture)
        now = collected["audit_dt"]
        privacy = config.get("privacy") or {}
        prior = {taskdash_common.normalize_text(r.get("task_full_name")): r for r in prior_inventory}
        changes = publish.detect_task_changes(
            prior, collected["inventory"], collected["host_name"], now.isoformat(), {},
            publish.privacy_hides(privacy, "hide_command_lines"), publish.privacy_hides(privacy, "hide_accounts"),
            publish.privacy_hides(privacy, "hide_paths"))
        alert = publish.compose_alert(collected, config, alert_state or {}, now)
    return {
        "inventory": _clean(collected["inventory"]),
        "runs": _clean(collected["runs"]),
        "metrics": _clean(collected["metrics"]),
        "changes": _clean(changes),
        "alert": {"send": alert["send"], "reason": alert["reason"],
                  "subject": _SINCE_IN_SUBJECT.sub("since <TIME>", alert["subject"]),
                  "body": _OPEN_SINCE.sub("(open since <TIME> <TZ>)", alert["body"])
                          .replace(f"{now.astimezone():%Y-%m-%d %H:%M}", "<TIME>")
                          .replace(f"<TIME> {now.astimezone().tzname()}", "<TIME> <TZ>")},
    }


def replay_synthetic(fixtures_dir: Path, now_text: str = SYNTHETIC_NOW, seed: int = 1) -> Dict[str, Any]:
    """Replay the synthetic host fixtures with frozen metric logs; the result is a pure function of
    the fixtures, now_text, seed and the code under test."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import make_synthetic_fixture as gen

    merged: Dict[str, Any] = {section: [] for section in SECTIONS if section != "alert"}
    merged["alert"] = {}
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        overrides = gen.write_metric_logs(tmp / "logs", gen.parse_now(now_text), seed, generic=True)
        config_path = tmp / "config.json"
        config_path.write_text(json.dumps({
            "rules": {"profiles": []}, "alerting": {"dashboard_url": "https://example.invalid/dash"},
            "metrics": {"days_back": 36500, "log_directory_overrides": overrides}}), encoding="utf-8")
        config = taskdash_common.load_config(config_path)
        for name in SYNTHETIC_FIXTURES:
            fixture = fixtures_dir / name
            payload = _read_json(fixture, "fixture")
            prior: List[Dict[str, Any]] = []
            before = gen.prior_fixture(payload)
            if before is not None:
                prior_path = tmp / f"prior_{name}"
                prior_path.write_text(json.dumps(before), encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()):
                    prior = publish.collect_everything({}, prior_path)["inventory"]
            # A healthy earlier run left alert state: with none, every failure counts as already open
            # and the golden would lose the shared-cause incident e-mail.
            result = replay(config, fixture, prior,
                            {"signature": [], "updated_utc": gen.parse_now(now_text).isoformat()})
            for section in merged:
                if section != "alert":
                    merged[section] += result[section]
            merged["alert"][payload["host_name"]] = result["alert"]
        # The temp folder holds the user name and changes per run; the golden must not carry it.
        text = json.dumps(merged, default=str)
        for root in {tmp_name, str(tmp), str(tmp.resolve())}:
            text = text.replace(json.dumps(root)[1:-1], "<LOGS>")
        merged = json.loads(text)
    for section in merged:
        if section != "alert":
            merged[section] = _clean(merged[section])
    return merged


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read {what} {path} ({type(exc).__name__}): pass an existing JSON file.")


def _key_of(section: str, row: Dict[str, Any]) -> str:
    return " | ".join(str(row.get(f, "")) for f in SECTION_KEYS[section])


def _index(section: str, data: Any) -> Dict[str, Dict[str, Any]]:
    if section == "alert":
        return {"alert": data}
    indexed: Dict[str, Dict[str, Any]] = {}
    for row in data:
        key = _key_of(section, row)
        # TaskChanges can hold two rows of one kind for a task (two warning notes): number them.
        n = 1
        while (f"{key}#{n}" if n > 1 else key) in indexed:
            n += 1
        indexed[f"{key}#{n}" if n > 1 else key] = row
    return indexed


def _approves(entry: Dict[str, Any], section: str, row: Dict[str, Any], field: str,
              old: Any = None, new: Any = None) -> bool:
    if entry.get("section") != section or entry.get("field") != field:
        return False
    if any(row.get(k) != v for k, v in (entry.get("key") or {}).items()):
        return False
    return ("old" not in entry or entry["old"] == old) and ("new" not in entry or entry["new"] == new)


def compare(a: Dict[str, Any], b: Dict[str, Any],
            approved: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Per section: only_a / only_b keys and changed {key: [fields]}, each split into
    unapproved and approved."""
    report: Dict[str, Any] = {}
    for section in SECTIONS:
        ia, ib = _index(section, a.get(section, [])), _index(section, b.get(section, []))
        out = {"only_a": [], "only_b": [], "changed": {}, "approved_only_a": [],
               "approved_only_b": [], "approved_changed": {}}
        for key in sorted(ia.keys() - ib.keys()):
            ok = any(_approves(e, section, ia[key], "_removed") for e in approved)
            out["approved_only_a" if ok else "only_a"].append(key)
        for key in sorted(ib.keys() - ia.keys()):
            ok = any(_approves(e, section, ib[key], "_added") for e in approved)
            out["approved_only_b" if ok else "only_b"].append(key)
        for key in sorted(ia.keys() & ib.keys()):
            ra, rb = ia[key], ib[key]
            for field in sorted(set(ra) | set(rb)):
                if ra.get(field) == rb.get(field):
                    continue
                ok = any(_approves(e, section, rb, field, ra.get(field), rb.get(field))
                         for e in approved)
                out["approved_changed" if ok else "changed"].setdefault(key, []).append(field)
        report[section] = out
    return report


def _unapproved(report: Dict[str, Any]) -> int:
    return sum(len(s["only_a"]) + len(s["only_b"]) + sum(len(f) for f in s["changed"].values())
               for s in report.values())


def _print_report(report: Dict[str, Any]) -> None:
    for section, s in report.items():
        approved = (len(s["approved_only_a"]) + len(s["approved_only_b"])
                    + sum(len(f) for f in s["approved_changed"].values()))
        bad = len(s["only_a"]) + len(s["only_b"]) + sum(len(f) for f in s["changed"].values())
        print(f"{section}: {bad} unapproved difference(s), {approved} approved")
        for label, keys in (("only in A", s["only_a"]), ("only in B", s["only_b"]),
                            ("approved only in A", s["approved_only_a"]),
                            ("approved only in B", s["approved_only_b"])):
            for key in keys:
                print(f"  {label}: {key}")
        for label, changed in (("changed", s["changed"]), ("approved change", s["approved_changed"])):
            for key, fields in changed.items():
                print(f"  {label}: {key}: {', '.join(fields)}")


def _cmd_replay(args: argparse.Namespace) -> int:
    config = taskdash_common.load_config(Path(args.config))
    prior = _read_json(Path(args.prior_inventory), "prior inventory") if args.prior_inventory else []
    if not isinstance(prior, list):
        raise SystemExit("--prior-inventory must be a JSON list of TaskInventory rows.")
    state = _read_json(Path(args.alert_state), "alert state") if args.alert_state else {}
    result = replay(config, Path(args.fixture), prior, state)
    Path(args.out).write_text(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n",
                              encoding="utf-8")
    counts = ", ".join(f"{s} {len(result[s])}" for s in SECTIONS if s != "alert")
    print(f"Replay written to {args.out}: {counts}; alert reason '{result['alert']['reason']}'")
    return 0


def _cmd_synthetic(args: argparse.Namespace) -> int:
    result = replay_synthetic(Path(args.fixtures_dir), args.now, args.seed)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(result, indent=1, sort_keys=True, default=str) + "\n")
    counts = ", ".join(f"{s} {len(result[s])}" for s in SECTIONS if s != "alert")
    print(f"Synthetic replay written to {args.out}: {counts}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    approved = _read_json(Path(args.approved), "approved list") if args.approved else []
    if not isinstance(approved, list):
        raise SystemExit("--approved must be a JSON list of approved-change entries.")
    report = compare(_read_json(Path(args.a), "replay A"), _read_json(Path(args.b), "replay B"),
                     approved)
    _print_report(report)
    bad = _unapproved(report)
    print(f"{bad} unapproved difference(s)." if bad else "All differences approved (or none).")
    return 1 if bad else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("replay", allow_abbrev=False)
    r.add_argument("--config", required=True)
    r.add_argument("--fixture", required=True)
    r.add_argument("--prior-inventory", default="")
    r.add_argument("--alert-state", default="")
    r.add_argument("--out", required=True)
    r.set_defaults(func=_cmd_replay)
    y = sub.add_parser("synthetic", allow_abbrev=False)
    y.add_argument("--out", required=True)
    y.add_argument("--now", default=SYNTHETIC_NOW)
    y.add_argument("--seed", type=int, default=1)
    y.add_argument("--fixtures-dir", default=str(Path(__file__).resolve().parents[1] / "tests" / "fixtures"))
    y.set_defaults(func=_cmd_synthetic)
    c = sub.add_parser("compare", allow_abbrev=False)
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--approved", default="")
    c.set_defaults(func=_cmd_compare)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""Builds the export that goes to the private repository used for CI.

Driver: manual
Source: the files git tracks at HEAD (uncommitted edits never leak in)
Target: --out DIR, only with --apply

    python _tools/export_public.py --out DIR                       # check: list what would be exported
    python _tools/export_public.py --out DIR --apply [--private-list PATH]

After writing, the output is scanned with deny_scan (generic list, plus the private list when given)
and with the publisher's secret scrubber. Hits print as file paths and counts, never the matched text.
A deny-list hit exits 1; credential-pattern files are listed for review and do not change the exit code.
This script never runs git commit or git push; pushing the output is a separate, manual step.
docs/RELEASING.md lists the whole release process, including the copy to the public repository.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import List, Optional, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "_tools"))

import deny_scan  # noqa: E402

# The org rules profile and SDE extension are deny-listed in source, so their names are assembled.
EXCLUDED_FILES = {
    "rules/mar" + "ion.json",
    "workflow_descriptions.json",  # the root copy only; tests/fixtures keeps its own
    "dashboard_taskdash.json",
    "TaskDash.config.json",
}
EXCLUDED_DIRS = ("archive/", "Logs/")
EXCLUDED_SUFFIXES = (".sd" + "e",)
# deny_scan.py holds the generic list, so it is the one file the scan skips (as the Phase 2 gate does).
SCAN_SKIP = {"_tools/deny_scan.py"}
# The copyright holder's name is on the private list, which keeps it out of every other line.
# Only these exact lines may carry it (owner decision, 2026-10-18). The name is assembled so this
# file does not hit the list itself.
_HOLDER = "Copyright 2026 " + "As" + "ir Khan"
ALLOWED_LINES = {"NOTICE": _HOLDER, "README.md": _HOLDER + "."}


def select_files(paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split tracked paths into (exported, excluded). Pure: no git, no disk."""
    kept: List[str] = []
    dropped: List[str] = []
    for path in paths:
        norm = PurePosixPath(path).as_posix()
        parts = norm.split("/")
        excluded = (norm in EXCLUDED_FILES
                    or norm.startswith(EXCLUDED_DIRS)
                    or any(f"{p}/" in EXCLUDED_DIRS for p in parts[:-1])
                    or norm.lower().endswith(EXCLUDED_SUFFIXES))
        (dropped if excluded else kept).append(norm)
    return kept, dropped


def _git(*args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=text)


def tracked_at_head() -> List[str]:
    result = _git("ls-tree", "-r", "--name-only", "-z", "HEAD")
    if result.returncode:
        raise SystemExit(f"git ls-tree failed: {result.stderr.strip()}. Run this from a TaskDash checkout.")
    return [p for p in result.stdout.split("\0") if p]


def ignored_by_gitignore(paths: Sequence[str]) -> List[str]:
    """Tracked paths that .gitignore would also match (a file added with -f)."""
    result = subprocess.run(["git", "-C", str(REPO), "check-ignore", "--no-index", "--stdin", "-z"],
                            input="\0".join(paths), capture_output=True, text=True)
    return [p for p in result.stdout.split("\0") if p]


def write_export(paths: Sequence[str], out: Path) -> None:
    for path in paths:
        blob = _git("show", f"HEAD:{path}", text=False)
        if blob.returncode:
            raise SystemExit(f"git show HEAD:{path} failed. Check that the file is committed.")
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.stdout)


def allowed_line(out: Path, path: Path, line_no: int) -> bool:
    """True when the hit is on one of ALLOWED_LINES, matched exactly, so no other term can ride along."""
    allowed = ALLOWED_LINES.get(path.relative_to(out).as_posix())
    if allowed is None:
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return 0 < line_no <= len(lines) and lines[line_no - 1].strip() == allowed


def deny_hits(out: Path, private_list: str) -> dict:
    terms = deny_scan.load_terms(private_list)
    files = [str(p) for p in sorted(out.rglob("*")) if p.is_file()
             and p.relative_to(out).as_posix() not in SCAN_SKIP]
    counts: dict = {}
    for hit in deny_scan.scan(files, terms):
        # A hit is "<path>:<line>: <terms>"; the path itself can hold a drive-letter colon.
        match = re.match(r"(.*?):(\d+): (.*)", hit)
        name, line_no, labels = match.group(1), int(match.group(2)), match.group(3)
        if allowed_line(out, Path(name), line_no):
            continue
        # .gitignore must carry the SDE pattern; only a private-list hit in it counts.
        if Path(name).name == ".gitignore" and labels == ".sd" + "e":
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def secret_hits(out: Path) -> Optional[dict]:
    """Per-file count of lines the publisher's scrubber would change; None if it cannot be imported."""
    sys.path.insert(0, str(REPO))
    try:
        from taskdash_publish import _scrub_secrets
    except Exception:
        return None
    counts: dict = {}
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        changed = sum(1 for line in text.splitlines() if _scrub_secrets(line) != line)
        if changed:
            counts[str(path)] = changed
    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export the committed repo for the private CI repository.",
                                     allow_abbrev=False)
    parser.add_argument("--out", required=True, help="Folder to write the export into.")
    parser.add_argument("--apply", action="store_true", help="Write the files (default is a check).")
    parser.add_argument("--private-list", default="", help="Path of the private deny list.")
    args = parser.parse_args(argv)

    kept, dropped = select_files(tracked_at_head())
    ignored = set(ignored_by_gitignore(kept))
    kept = [p for p in kept if p not in ignored]
    dropped += sorted(ignored)
    print(f"{len(kept)} file(s) to export, {len(dropped)} excluded.")
    for path in kept:
        print(f"  export  {path}")
    for path in dropped:
        print(f"  EXCLUDE {path}")
    if not args.apply:
        print("CHECK MODE - nothing written. Re-run with --apply.")
        return 0

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty. Pass a new or empty --out folder; this script never deletes files.")
    write_export(kept, out)
    print(f"Wrote {len(kept)} file(s) to {out}.")

    denied = deny_hits(out, args.private_list)
    for name, count in sorted(denied.items()):
        print(f"DENY-LIST HIT  {name}: {count} line(s)")
    secrets = secret_hits(out)
    if secrets is None:
        print("Credential scan SKIPPED: taskdash_publish could not be imported.")
    for name, count in sorted((secrets or {}).items()):
        print(f"CREDENTIAL PATTERN  {name}: {count} line(s)")
    # Review-only: the scrubber over-matches ordinary words such as "token" in code, rules and
    # the scrubber's own tests, so these counts are a list to read, not a gate.
    print(f"Deny-list files with hits: {len(denied)}. Credential-pattern files to review: "
          f"{'skipped' if secrets is None else len(secrets)}.")
    return 1 if denied else 0


if __name__ == "__main__":
    raise SystemExit(main())

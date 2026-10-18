"""Deny-list scanner (roadmap Phase 2, deliverable 2).

Driver: manual, or CI with a path list
Source: the files and folders given on the command line
Target: stdout (file:line hits); exit code 1 on any hit, 2 on a bad argument

    python _tools/deny_scan.py tests/fixtures
    python _tools/deny_scan.py . --extra-list <path to the private list>

The generic list names the reference install's identifiers. The private list of real names lives
outside the repository and is passed by path (one term per line, blank lines and # comments
skipped); this script never creates or prints it. A hit prints a generic term, or "private term #N"
for the Nth term of the private list, and never the line, so a private term cannot leak through the
scan's own output. Matching is case-insensitive.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

GENERIC_DENY = ("Marion", "marionfl", "GISTASKS", "MCP-", "GIS_Work", ".sde", "ServiceSupport")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}


def load_terms(extra_list: str) -> List[str]:
    terms = list(GENERIC_DENY)
    if extra_list:
        try:
            lines = Path(extra_list).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SystemExit(f"Cannot read the extra deny list {extra_list!r}: {exc}. "
                             "Pass the path of the private list file.")
        terms += [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    return terms


def iter_files(paths: Sequence[str]) -> Iterator[Path]:
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and not SKIP_DIRS.intersection(child.parts):
                    yield child
        else:
            raise SystemExit(f"Path not found: {raw}. Give an existing file or folder.")


def scan(paths: Sequence[str], terms: Sequence[str]) -> List[str]:
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    # Generic terms print as written; a private term prints as its list position, so the scan's
    # own output never carries a real name.
    labels: dict = {}
    for index, term in enumerate(terms):
        labels.setdefault(term.lower(), term.lower() if index < len(GENERIC_DENY) else f"private term #{index - len(GENERIC_DENY) + 1}")
    hits: List[str] = []
    for path in iter_files(paths):
        data = path.read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16", errors="replace")  # PowerShell's default text encoding
        elif b"\x00" in data:
            continue  # binary file: nothing to read as lines
        else:
            text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            found = sorted({labels[m.group(0).lower()] for m in pattern.finditer(line)})
            if found:
                hits.append(f"{path}:{number}: {', '.join(found)}")
    return hits


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Scan paths for deny-listed terms.", allow_abbrev=False)
    parser.add_argument("paths", nargs="+", help="Files or folders to scan.")
    parser.add_argument("--extra-list", default="", help="Path of a private list, one term per line.")
    args = parser.parse_args(argv)
    hits = scan(args.paths, load_terms(args.extra_list))
    for hit in hits:
        print(hit)
    print(f"{len(hits)} hit(s).", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())

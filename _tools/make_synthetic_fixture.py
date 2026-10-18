"""Command-line entry for the synthetic fixture generator, which lives in taskdash_synthetic.py.

    python _tools/make_synthetic_fixture.py --out-dir tests/fixtures --now 2026-10-16T12:00:00Z
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from taskdash_synthetic import *  # noqa: F401,F403 -- golden_replay and the tests load this file
from taskdash_synthetic import main

if __name__ == "__main__":
    raise SystemExit(main())

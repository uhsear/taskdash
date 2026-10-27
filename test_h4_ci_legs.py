"""H4-10: a CI leg that names a language mode or a culture must really run in it.

A leg that silently stayed in FullLanguage or en-US would pass every test and prove nothing. Only
the environment legs of .github/workflows/matrix.yml set these variables, so on any other machine
the tests skip. This file starts PowerShell and must not parse a script with the Language class,
because it also runs in the ConstrainedLanguage leg; the static workflow checks live in
test_h4_ci_matrix.py. The time-zone variable is checked by test_phase3_install.py.
"""

from __future__ import annotations

import os
import subprocess

import pytest

import taskdash_common as common


def _expected(name):
    value = os.environ.get(name)
    if value is None:
        pytest.skip("not a CI environment leg")
    return value


def test_the_ci_leg_runs_in_the_language_mode_it_names():
    assert common.powershell_language_mode() == _expected("TASKDASH_EXPECT_LANGUAGE_MODE")


def test_the_ci_leg_runs_in_the_culture_it_names():
    expected = _expected("TASKDASH_EXPECT_CULTURE")
    done = subprocess.run(common._powershell_argv("(Get-Culture).Name"), capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr.decode("utf-8", errors="replace")
    assert done.stdout.decode("utf-8", errors="replace").strip() == expected

"""H3-3: home_dir() falls back to the folder that holds the venv when the current folder has no config."""
from __future__ import annotations

import sys

import pytest

import taskdash_common as common


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A wheel install: install/ holds the config and the venv, elsewhere/ is the current folder."""
    install = tmp_path / "install"
    venv = install / ".venv"
    elsewhere = tmp_path / "elsewhere"
    for folder in (venv, elsewhere):
        folder.mkdir(parents=True)
    monkeypatch.setattr(common, "_folder_deploy", lambda: False)
    monkeypatch.delenv("TASKDASH_HOME", raising=False)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.chdir(elsewhere)
    return install, elsewhere


def _config(folder):
    (folder / common.DEFAULT_CONFIG_FILE).write_text("{}", encoding="utf-8")


def test_the_venv_parent_wins_when_the_current_folder_has_no_config(world):
    install, _ = world
    _config(install)
    assert common.home_dir() == install.resolve()


def test_the_current_folder_wins_when_it_has_a_config(world):
    install, elsewhere = world
    _config(install)
    _config(elsewhere)
    assert common.home_dir() == elsewhere.resolve()


def test_the_env_var_wins_over_the_venv_parent(world, monkeypatch, tmp_path):
    install, _ = world
    _config(install)
    monkeypatch.setenv("TASKDASH_HOME", str(tmp_path))
    assert common.home_dir() == tmp_path.resolve()


def test_the_current_folder_is_kept_when_the_venv_parent_has_no_config(world):
    _, elsewhere = world
    assert common.home_dir() == elsewhere.resolve()


def test_no_venv_means_no_fallback(world, monkeypatch):
    install, elsewhere = world
    _config(install)
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    assert common.home_dir() == elsewhere.resolve()

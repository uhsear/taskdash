"""H1-7: a wheel install keeps a config or import failure in Logs/bootstrap_FAIL_*.log.

No test touches the network; the bad config and the failing import are both local.
"""

from __future__ import annotations

import pytest

import taskdash_cli as cli
import taskdash_common as common
import taskdash_publish as publish


@pytest.fixture
def wheel_home(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "_folder_deploy", lambda: False)
    monkeypatch.setenv("TASKDASH_HOME", str(tmp_path))
    return tmp_path


def _fail_logs(home):
    return sorted((home / "Logs").glob("bootstrap_FAIL_*.log"))


def test_bad_config_leaves_one_bootstrap_fail_log(wheel_home):
    bad = wheel_home / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        publish.main(["--config", str(bad)])
    assert exc.value.code
    logs = _fail_logs(wheel_home)
    assert len(logs) == 1
    assert "is not valid JSON" in logs[0].read_text(encoding="utf-8")


def test_utf16_config_leaves_one_bootstrap_fail_log_that_says_to_save_as_utf8(wheel_home):
    # PowerShell 5.1 Out-File writes UTF-16 by default.
    bad = wheel_home / "utf16.json"
    bad.write_text('{"item_id": "x"}', encoding="utf-16")
    with pytest.raises(SystemExit) as exc:
        publish.main(["--config", str(bad)])
    assert "UTF-8" in str(exc.value.code)
    (log,) = _fail_logs(wheel_home)
    assert "UTF-8" in log.read_text(encoding="utf-8")


def test_unreadable_config_leaves_one_bootstrap_fail_log(wheel_home, monkeypatch):
    unreadable = wheel_home / "denied.json"
    unreadable.write_text("{}", encoding="utf-8")
    real_read_text = type(unreadable).read_text

    def denied(self, *a, **k):
        if self.name == "denied.json":
            raise PermissionError("access denied")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(type(unreadable), "read_text", denied)
    with pytest.raises(SystemExit) as exc:
        publish.main(["--config", str(unreadable)])
    assert "read access" in str(exc.value.code)
    assert len(_fail_logs(wheel_home)) == 1


def test_failing_import_leaves_one_bootstrap_fail_log(wheel_home, monkeypatch):
    def boom(name):
        raise ImportError("synthetic import failure")

    monkeypatch.setattr(cli.importlib, "import_module", boom)
    with pytest.raises(ImportError):
        cli.main(["setup"])
    logs = _fail_logs(wheel_home)
    assert len(logs) == 1
    text = logs[0].read_text(encoding="utf-8")
    assert "synthetic import failure" in text and "Traceback" in text


def test_missing_subcommand_module_leaves_one_bootstrap_fail_log(wheel_home, monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(f"No module named '{name}'", name=name)

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup"])
    assert "Reinstall TaskDash" in str(exc.value.code)
    (log,) = _fail_logs(wheel_home)
    assert "taskdash_setup.py is not part of this install" in log.read_text(encoding="utf-8")


def test_folder_install_writes_no_fallback_file(monkeypatch, tmp_path):
    monkeypatch.setattr(common, "home_dir", lambda: tmp_path)
    common.write_bootstrap_fail("message")
    assert not (tmp_path / "Logs").exists()


def test_writer_never_raises(wheel_home, monkeypatch, capsys):
    def broken_home():
        raise OSError("disk gone")

    monkeypatch.setattr(common, "home_dir", broken_home)
    common.write_bootstrap_fail("message")
    assert "could not write bootstrap_FAIL log" in capsys.readouterr().err

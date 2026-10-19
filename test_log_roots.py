"""metrics.log_roots (H5-5): confine log reads to listed folders. Unset keeps today's discovery."""

import os
import stat
import subprocess
import sys

import pytest

import taskdash_common
import taskdash_metrics as metrics
import taskdash_publish as publish


def _task(name, script):
    return {"task_name": name, "task_full_name": "\\" + name, "command_target_path": str(script)}


def _project(base, name, text="Records updated: 5\n"):
    """base/<name>/run.py with base/<name>/Logs/run.log; returns the script path."""
    (base / name / "Logs").mkdir(parents=True)
    (base / name / "Logs" / "run.log").write_text(text, encoding="utf-8")
    script = base / name / "run.py"
    script.write_text("", encoding="utf-8")
    return script


def _link_dir(link, target):
    """A directory symlink, or a junction when symlinks need a privilege this account lacks."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform != "win32":
        return False
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
    return result.returncode == 0


@pytest.fixture
def roots(monkeypatch):
    def set_roots(*folders):
        monkeypatch.setattr(metrics, "LOG_ROOTS", [metrics._resolve_log_root(str(f)) for f in folders])
    monkeypatch.setattr(metrics, "LOG_ROOTS", [])
    return set_roots


@pytest.fixture
def warnings_seen(monkeypatch):
    seen = []
    monkeypatch.setattr(metrics, "emit", seen.append)
    return seen


def _mined(tasks):
    rows, summary = metrics.build_metric_rows(tasks, host_name="H")
    return {row["task_name"] for row in rows}, summary


def test_unset_roots_mine_every_discovered_folder(tmp_path, roots):
    script = _project(tmp_path, "alpha")
    names, _ = _mined([_task("alpha", script)])
    assert names == {"alpha"}


def test_folder_inside_a_root_is_mined_and_a_shared_prefix_sibling_is_skipped(tmp_path, roots, warnings_seen):
    inside = _project(tmp_path / "Logs", "alpha")
    sibling = _project(tmp_path / "Logs2", "beta")
    roots(tmp_path / "Logs")
    names, summary = _mined([_task("alpha", inside), _task("beta", sibling)])
    assert names == {"alpha"}
    assert "\\beta" not in summary
    skipped = [line for line in warnings_seen if "log_roots" in line]
    assert len(skipped) == 1 and "skipped 2 log folder(s)" in skipped[0]
    assert "beta" not in skipped[0]


def test_link_inside_a_root_that_points_outside_is_skipped(tmp_path, roots, warnings_seen):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (outside).mkdir()
    (outside / "run.log").write_text("Records updated: 9\n", encoding="utf-8")
    root.mkdir()
    script = root / "run.py"
    script.write_text("", encoding="utf-8")
    if not _link_dir(root / "Logs", outside):
        pytest.skip("this account cannot create a symlink or junction")
    roots(root)
    names, _ = _mined([_task("alpha", script)])
    assert names == set()
    assert any("log_roots" in line for line in warnings_seen)
    if sys.platform == "win32":
        assert metrics._is_reparse_point(root / "Logs") is True


def test_link_is_skipped_even_when_realpath_cannot_see_it(tmp_path, roots, monkeypatch):
    """Stand-in for a platform where realpath reports the link as inside the root."""
    script = _project(tmp_path / "root", "alpha")
    roots(tmp_path / "root")
    monkeypatch.setattr(metrics, "_is_reparse_point", lambda path: path.name == "Logs")
    names, _ = _mined([_task("alpha", script)])
    assert names == set()


@pytest.mark.parametrize("tag, is_link", [(stat.IO_REPARSE_TAG_SYMLINK, True),
                                           (stat.IO_REPARSE_TAG_MOUNT_POINT, True),
                                           (0x80000013, False)])  # deduplicated file: a log, not a link
def test_only_symlink_and_junction_reparse_tags_count_as_links(tmp_path, monkeypatch, tag, is_link):
    plain = tmp_path / "a.log"
    plain.write_text("x", encoding="utf-8")
    assert metrics._is_reparse_point(plain) is False
    flagged = type("S", (), {"st_file_attributes": stat.FILE_ATTRIBUTE_REPARSE_POINT, "st_reparse_tag": tag})()
    monkeypatch.setattr(metrics.os, "lstat", lambda path: flagged)
    assert metrics._is_reparse_point(plain) is is_link


def test_root_check_is_case_insensitive_on_windows_and_component_aware(tmp_path, roots):
    (tmp_path / "Logs" / "x").mkdir(parents=True)
    (tmp_path / "Logs2").mkdir()
    roots(tmp_path / "Logs")
    assert metrics.log_path_allowed(tmp_path / "Logs" / "x")
    assert metrics.log_path_allowed(tmp_path / "Logs")
    assert not metrics.log_path_allowed(tmp_path / "Logs2")
    if os.name == "nt":
        assert metrics.log_path_allowed(str(tmp_path / "LOGS" / "x"))


def test_email_tail_is_refused_for_a_file_outside_the_roots(tmp_path, roots):
    inside = tmp_path / "Logs"
    inside.mkdir()
    (inside / "ok.log").write_text("ok line\n", encoding="utf-8")
    outside = tmp_path / "Other"
    outside.mkdir()
    (outside / "no.log").write_text("secret-ish line\n", encoding="utf-8")
    roots(inside)
    assert "ok line" in publish.tail_log_for_email(str(inside / "ok.log"))
    assert publish.tail_log_for_email(str(outside / "no.log")) == ""


def test_email_tail_reads_any_file_when_roots_are_unset(tmp_path, roots):
    log = tmp_path / "a.log"
    log.write_text("line one\n", encoding="utf-8")
    assert "line one" in publish.tail_log_for_email(str(log))


def test_configure_rules_reads_log_roots_and_empty_means_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "LOG_ROOTS", [])
    metrics.configure_rules({"metrics": {"log_roots": [str(tmp_path)]}})
    assert len(metrics.LOG_ROOTS) == 1
    metrics.configure_rules({"metrics": {"log_roots": []}})
    assert metrics.LOG_ROOTS == []


@pytest.mark.parametrize("value", ["C:\\Logs", [1], {"a": "b"}])
def test_config_validation_rejects_a_bad_log_roots_type(value):
    errors = taskdash_common._validate_config({"metrics": {"log_roots": value}})
    assert any("metrics.log_roots must be a list of strings" in e for e in errors)
    assert taskdash_common._validate_config({"metrics": {"log_roots": ["C:\\Logs"]}}) == []


def test_a_file_swapped_after_the_roots_check_is_not_read(tmp_path, roots, monkeypatch):
    inside = tmp_path / "root" / "run.log"
    inside.parent.mkdir()
    inside.write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("outside\n", encoding="utf-8")
    roots(tmp_path / "root")
    assert metrics.read_last_lines(inside, 5) == ["inside"]
    real_stat = os.stat
    # The opened handle is another file than the one the path names now: a swap happened.
    monkeypatch.setattr(metrics.os, "fstat", lambda fd: real_stat(outside))
    with pytest.raises(PermissionError, match="log_roots"):
        metrics.read_last_lines(inside, 5)

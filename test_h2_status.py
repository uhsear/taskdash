"""H2-9: `taskdash status` exit codes, the --host filter, and `status --prune`.

Every service call is a fake and every file lives under tmp_path. Nothing signs in.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import taskdash_cli as cli
import taskdash_common as common

NOW = datetime.now(timezone.utc)


def _ms(minutes_ago: float) -> int:
    return int((NOW - timedelta(minutes=minutes_ago)).timestamp() * 1000)


def _run(host: str, minutes_ago: float, status: str = "success") -> dict:
    return {"host_name": host, "started_utc": _ms(minutes_ago), "exit_status": status}


class FakeTable:
    """Answers the two status queries: optional host_name = '...' and exit_status = 'success'."""

    def __init__(self, rows):
        self.rows, self.wheres = rows, []

    def query(self, **kw):
        where = kw["where"]
        self.wheres.append(where)
        rows = [r for r in self.rows
                if ("host_name" not in where or f"host_name = '{r['host_name']}'" in where)
                and ("exit_status = 'success'" not in where or r["exit_status"] == "success")]
        rows.sort(key=lambda r: r["started_utc"], reverse=True)
        return SimpleNamespace(features=[SimpleNamespace(attributes=dict(r)) for r in rows[:1]])


@pytest.fixture
def service(monkeypatch, tmp_path):
    """Returns serve(rows, **config) -> FakeTable; status then reads those rows as host HOST-A."""
    monkeypatch.setattr(cli, "runtime_dir", lambda: tmp_path)
    (tmp_path / "TaskDash.config.json").write_text("{}", encoding="utf-8")

    def serve(rows, **scheduler):
        config = {"arcgis": {"item_id": "ITEM"}, "host_name_override": "HOST-A",
                  "logging": {"log_dir": str(tmp_path / "Logs")}}
        if scheduler:
            config["scheduler"] = scheduler
        table = FakeTable(rows)
        gis = SimpleNamespace(content=SimpleNamespace(get=lambda item_id: SimpleNamespace(id=item_id)))
        monkeypatch.setattr(common, "load_config", lambda path=None: config)
        monkeypatch.setattr(common, "connect_agol", lambda cfg: gis)
        monkeypatch.setattr(common, "get_tables", lambda item: {common.TABLE_COLLECTOR_RUNS: table})
        return table

    return serve


# ------------------------------------------------------------------ exit codes

def test_fresh_success_exits_0_and_shows_the_last_success_age(service, capsys):
    service([_run("HOST-A", 47)])
    assert cli.main(["status"]) == 0
    assert "last success 47 min ago" in capsys.readouterr().out


def test_a_failed_newest_run_exits_1_and_still_shows_the_older_success(service, capsys):
    service([_run("HOST-A", 10, "failed"), _run("HOST-A", 70)])
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "exit_status failed" in out and "last success 70 min ago" in out


def test_a_stale_newest_success_exits_2(service, capsys):
    service([_run("HOST-A", 180)])
    assert cli.main(["status"]) == 2
    assert "older than 120 min" in capsys.readouterr().out


def test_the_stale_limit_follows_scheduler_collector_interval_minutes(service):
    service([_run("HOST-A", 180)], collector_interval_minutes=180)
    assert cli.main(["status"]) == 0


def test_no_run_at_all_exits_2(service, capsys):
    service([])
    assert cli.main(["status"]) == 2
    assert "no rows" in capsys.readouterr().out


def test_a_failed_run_with_no_success_ever_exits_1_and_says_so(service, capsys):
    service([_run("HOST-A", 10, "failed")])
    assert cli.main(["status"]) == 1
    assert "last success: none" in capsys.readouterr().out


def test_an_unreadable_service_exits_3_with_the_message(service, monkeypatch, capsys):
    service([])

    def refuse(config):
        raise SystemExit("arcgis.profile 'p' did not sign in. Store it once.")

    monkeypatch.setattr(common, "connect_agol", refuse)
    assert cli.main(["status"]) == 3
    assert "did not sign in" in capsys.readouterr().out


def test_a_network_error_exits_3(service, monkeypatch, capsys):
    service([])

    def refuse(config):
        raise ConnectionError("no route")

    monkeypatch.setattr(common, "connect_agol", refuse)
    assert cli.main(["status"]) == 3
    assert "no route" in capsys.readouterr().out


def test_a_missing_service_id_exits_3(service, monkeypatch):
    service([])
    monkeypatch.setattr(common, "load_config", lambda path=None: {"arcgis": {}})
    assert cli.main(["status"]) == 3


# ------------------------------------------------------------------ --host

ROWS = [_run("HOST-A", 20, "failed"), _run("HOST-A", 80), _run("HOST-B", 5)]


def test_default_host_is_this_installs_effective_host(service):
    table = service(ROWS)
    assert cli.main(["status"]) == 1  # HOST-A's newest run failed; HOST-B's success is ignored
    assert all("host_name = 'HOST-A'" in w for w in table.wheres)


def test_host_names_another_host(service):
    table = service(ROWS)
    assert cli.main(["status", "--host", "HOST-B"]) == 0
    assert all("host_name = 'HOST-B'" in w for w in table.wheres)


def test_host_all_reads_every_host(service):
    table = service(ROWS)
    assert cli.main(["status", "--host", "all"]) == 0
    assert all("host_name" not in w for w in table.wheres)


def test_host_with_a_quote_cannot_break_the_filter(service):
    table = service([])
    cli.main(["status", "--host", "x' OR '1'='1"])
    assert table.wheres[0] == "host_name = 'x'' OR ''1''=''1'"


# ------------------------------------------------------------------ --prune

OLD = time.time() - 10 * 86400
BACKUP = "dashboard_backup_{item}_202601{day:02d}_000000.json"


@pytest.fixture
def folders(monkeypatch, tmp_path):
    """tmp/NewLogs is the configured log folder; tmp/Logs is the old default folder."""
    new, old = tmp_path / "NewLogs", tmp_path / "Logs"
    new.mkdir()
    old.mkdir()
    config = tmp_path / "td.config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(common, "load_config", lambda path=None: {"logging": {"log_dir": str(new)}})
    return SimpleNamespace(root=tmp_path, new=new, old=old, config=config)


def _touch(path, aged=False, text="x"):
    path.write_text(text, encoding="utf-8")
    if aged:
        os.utime(path, (OLD, OLD))
    return path


def _populate(f):
    for day in range(1, 13):
        _touch(f.new / BACKUP.format(item="ITEMA", day=day))
    for day in (1, 2, 3):
        _touch(f.new / BACKUP.format(item="ITEMB", day=day))
    for day in (1, 2, 3):
        _touch(f.root / f"td.config.json.bak_setup_202601{day:02d}_000000")
    _touch(f.new / "collector_runs_pending_prod.json", aged=True, text="[]")
    _touch(f.new / "collector_runs_pending_test.json", aged=True, text='[{"run_id": "r1"}]')
    _touch(f.new / "collector_runs_pending_dev.json", aged=True, text="[]")
    _touch(f.old / "bootstrap.log", aged=True)
    _touch(f.old / "bootstrap_FAIL_20260101_000000_1.log", aged=True)
    _touch(f.old / "bootstrap_FAIL_fresh.log")
    # Not TaskDash's own names: must never be touched.
    for name in ("notes.json", "dashboard_backup_ITEMA.json", "dashboard_backup_ITEMA_20260101_000000.json.old"):
        _touch(f.new / name, aged=True)
    _touch(f.old / "taskdash_20260101_000000.log", aged=True)
    _touch(f.old / "other_bootstrap.log", aged=True)
    _touch(f.root / "other.config.json.bak_setup_20260101_000000", aged=True)


def _names(folder):
    return {p.name for p in folder.iterdir()}


def test_prune_without_apply_lists_and_removes_nothing(folders, capsys):
    _populate(folders)
    before = (_names(folders.new), _names(folders.old), _names(folders.root))
    assert cli.main(["status", "--prune", "--config", str(folders.config)]) == 0
    out = capsys.readouterr().out
    assert BACKUP.format(item="ITEMA", day=1) in out and "collector_runs_pending_prod.json" in out
    assert "Nothing removed" in out
    assert before == (_names(folders.new), _names(folders.old), _names(folders.root))


def test_prune_apply_removes_only_old_matching_files_and_keeps_the_newest_n(folders, capsys):
    _populate(folders)
    assert cli.main(["status", "--prune", "--apply", "--config", str(folders.config)]) == 0
    out = capsys.readouterr().out
    assert "Removed 3 file(s)" in out
    new = _names(folders.new)
    assert BACKUP.format(item="ITEMA", day=1) not in new and BACKUP.format(item="ITEMA", day=2) not in new
    assert BACKUP.format(item="ITEMA", day=3) in new and BACKUP.format(item="ITEMA", day=12) in new
    assert {BACKUP.format(item="ITEMB", day=d) for d in (1, 2, 3)} <= new
    assert "collector_runs_pending_prod.json" not in new  # empty and old
    assert {"collector_runs_pending_test.json", "collector_runs_pending_dev.json"} <= new  # rows kept; not ours
    assert {"notes.json", "dashboard_backup_ITEMA.json",
            "dashboard_backup_ITEMA_20260101_000000.json.old"} <= new
    # The newest bootstrap logs stay, and so does a crash record: bootstrap_FAIL_* is the only record
    # of a failure before logging starts.
    assert _names(folders.old) == {"bootstrap.log", "bootstrap_FAIL_20260101_000000_1.log",
                                   "bootstrap_FAIL_fresh.log", "taskdash_20260101_000000.log",
                                   "other_bootstrap.log"}
    assert {f"td.config.json.bak_setup_202601{d:02d}_000000" for d in (1, 2, 3)} <= _names(folders.root)


def test_prune_keep_changes_how_many_backups_stay(folders):
    _populate(folders)
    assert cli.main(["status", "--prune", "--apply", "--keep", "2", "--config", str(folders.config)]) == 0
    kept = sorted(n for n in _names(folders.new) if n.startswith("dashboard_backup_ITEMA_") and n.endswith(".json"))
    assert kept == [BACKUP.format(item="ITEMA", day=11), BACKUP.format(item="ITEMA", day=12)]
    root = _names(folders.root)
    assert "td.config.json.bak_setup_20260101_000000" not in root  # only the oldest of 3 goes
    assert {"td.config.json.bak_setup_20260102_000000", "td.config.json.bak_setup_20260103_000000",
            "other.config.json.bak_setup_20260101_000000", "td.config.json"} <= root


def _aged_at(path, days_ago):
    _touch(path)
    stamp = time.time() - days_ago * 86400
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("log_dir_moved", [True, False])
def test_prune_removes_bootstrap_logs_older_than_7_days_whatever_log_dir_says(folders, monkeypatch, capsys, log_dir_moved):
    # run_taskdash.cmd writes <install>\Logs on every run, so that folder is live: age decides, not log_dir.
    if not log_dir_moved:
        monkeypatch.setattr(common, "load_config", lambda path=None: {"logging": {"log_dir": str(folders.old)}})
    oldest = _aged_at(folders.old / "bootstrap_20260101_000000_a.log", 30)
    older = _aged_at(folders.old / "bootstrap_20260102_000000_b.log", 20)
    stale = _aged_at(folders.old / "bootstrap.log", 10)
    fresh = _aged_at(folders.old / "bootstrap_20260301_000000_c.log", 1)
    assert cli.main(["status", "--prune", "--apply", "--keep", "2", "--config", str(folders.config)]) == 0
    out = capsys.readouterr().out
    assert not oldest.exists() and not older.exists()
    assert stale.exists() and fresh.exists()  # the newest 2 stay; one is also younger than 7 days
    assert "bootstrap log older than 7 days" in out and "outside the log folder" not in out


def test_prune_keeps_a_young_bootstrap_log_even_beyond_keep(folders):
    young = [_aged_at(folders.old / f"bootstrap_2026030{i}_000000_x.log", i) for i in range(1, 5)]
    assert cli.main(["status", "--prune", "--apply", "--keep", "1", "--config", str(folders.config)]) == 0
    assert all(p.exists() for p in young)


def test_prune_without_a_config_stops_with_the_fix_instead_of_guessing_a_folder(folders, tmp_path, capsys):
    stranger = _touch(tmp_path / "Logs" / "bootstrap.log", aged=True)
    missing = tmp_path / "nope.config.json"
    with pytest.raises(SystemExit, match="Config not found") as exc:
        cli.main(["status", "--prune", "--apply", "--config", str(missing)])
    assert str(missing) in str(exc.value) and "--config" in str(exc.value)
    assert stranger.exists()


def test_prune_default_config_missing_names_the_default_path(folders, tmp_path):
    folders.config.unlink()
    with pytest.raises(SystemExit, match="Config not found") as exc:
        cli.main(["status", "--prune"])
    assert common.DEFAULT_CONFIG_FILE in str(exc.value)


def test_prune_skips_pending_files_while_a_collector_run_holds_the_lock(folders, capsys):
    pending = _touch(folders.new / "collector_runs_pending_prod.json", aged=True, text="[]")
    with common.run_lock(folders.new):
        assert cli.main(["status", "--prune", "--apply", "--config", str(folders.config)]) == 0
    out = capsys.readouterr().out
    assert pending.exists()
    assert "Pending-row files skipped: a collector run holds its lock" in out


def test_prune_skips_pending_files_when_the_lock_cannot_be_checked(folders, monkeypatch, capsys):
    pending = _touch(folders.new / "collector_runs_pending_prod.json", aged=True, text="[]")

    def broken(log_dir):
        raise OSError("denied")

    monkeypatch.setattr(cli, "run_lock", broken)
    assert cli.main(["status", "--prune", "--apply", "--config", str(folders.config)]) == 0
    assert pending.exists() and "could not be checked (OSError)" in capsys.readouterr().out


def test_prune_takes_the_lock_for_the_whole_apply_and_releases_it(folders):
    _touch(folders.new / "collector_runs_pending_prod.json", aged=True, text="[]")
    assert cli.main(["status", "--prune", "--apply", "--config", str(folders.config)]) == 0
    assert not (folders.new / "collector_runs_pending_prod.json").exists()
    with common.run_lock(folders.new):  # free again: prune did not leave it held
        pass


def test_prune_keeps_the_newest_crash_records_and_removes_older_ones(folders):
    for day in (1, 2, 3):
        _touch(folders.old / f"bootstrap_FAIL_2026010{day}_000000_1.log", aged=True)
    assert cli.main(["status", "--prune", "--apply", "--keep", "2", "--config", str(folders.config)]) == 0
    assert _names(folders.old) == {"bootstrap_FAIL_20260102_000000_1.log", "bootstrap_FAIL_20260103_000000_1.log"}


def test_prune_reads_the_logs_folder_of_the_config_not_the_working_directory(folders, tmp_path, monkeypatch):
    # run_taskdash.cmd and write_bootstrap_fail write next to the install, so that is the folder
    # to prune. A folder named Logs under the directory the command runs in is not TaskDash's.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "Logs").mkdir(parents=True)
    stranger = _touch(elsewhere / "Logs" / "bootstrap.log", aged=True)
    monkeypatch.setattr(cli, "runtime_dir", lambda: elsewhere)
    ours = _aged_at(folders.old / "bootstrap_20260101_000000_a.log", 30)
    _aged_at(folders.old / "bootstrap.log", 10)
    assert cli.main(["status", "--prune", "--apply", "--keep", "1", "--config", str(folders.config)]) == 0
    assert stranger.exists() and not ours.exists()


def test_prune_never_touches_a_link(folders, tmp_path):
    outside = _touch(tmp_path / "outside.json", aged=True, text="keep me")
    link = folders.new / BACKUP.format(item="ITEMA", day=1)
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this account may not create symlinks")
    for day in range(2, 13):
        _touch(folders.new / BACKUP.format(item="ITEMA", day=day))
    assert cli.main(["status", "--prune", "--apply", "--config", str(folders.config)]) == 0
    assert link.is_symlink() and outside.read_text(encoding="utf-8") == "keep me"


def test_a_reparse_point_is_not_a_plain_file(tmp_path, monkeypatch):
    path = _touch(tmp_path / "a.json")
    assert cli._is_plain_file(path) is True
    real = path.lstat()
    fake = SimpleNamespace(st_mode=real.st_mode, st_file_attributes=0x400)
    monkeypatch.setattr(type(path), "lstat", lambda self: fake)
    assert cli._is_plain_file(path) is False


def test_apply_without_prune_is_refused(folders, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["status", "--apply", "--offline"])
    assert exc.value.code == 2


def test_keep_below_one_is_refused(folders):
    with pytest.raises(SystemExit) as exc:
        cli.main(["status", "--prune", "--keep", "0", "--config", str(folders.config)])
    assert exc.value.code == 2

"""H4-1: the hourly runs summary reads 30 days of stored runs, not the whole 90-day table.

A task whose newest real run is older than the window is found with one grouped MAX query
(outStatistics) and one small follow-up, so it keeps its last-run facts on the dashboard.
No network: a fake runs table that understands only the where-clauses the summary builds.
"""

from __future__ import annotations

import datetime as dt
import re

import taskdash_common as common

common.disable_arcpy_import()

import taskdash_publish as publish  # noqa: E402


NOW = dt.datetime(2026, 9, 29, 12, 0, 0, tzinfo=dt.timezone.utc)
HOST = "H"


def _ms(days_ago: float) -> int:
    return int((NOW - dt.timedelta(days=days_ago)).timestamp() * 1000)


def _row(task, days_ago, status="Success", duration=10.0, error=0, host=HOST):
    return {"host_name": host, "task_full_name": task, "start_time_utc": _ms(days_ago),
            "run_status": status, "duration_seconds": duration, "is_error": error,
            "result_text": "ok", "result_code": 0, "instance_id": f"{task}{days_ago}"}


def _epoch(text: str) -> int:
    parsed = dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp() * 1000)


class _Feature:
    def __init__(self, attributes):
        self.attributes = attributes


class _Result:
    def __init__(self, rows):
        self.features = [_Feature(r) for r in rows]


class _RunsTable:
    """Evaluates the three where shapes the summary builds, and the grouped-MAX statistics."""

    properties = {"maxRecordCount": 2000, "objectIdField": "OBJECTID"}

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def _matches(self, row, where):
        if row["host_name"] != re.search(r"host_name = '([^']*)'", where).group(1):
            return False
        if "NOT IN" in where:
            if row["run_status"] in re.findall(r"'([^']+)'", where.split("NOT IN")[1]):
                return False
        pairs = re.findall(r"task_full_name = '([^']*)' AND start_time_utc >= TIMESTAMP '([^']*)'"
                           r"(?: AND start_time_utc < TIMESTAMP '([^']*)')?", where)
        if pairs:
            return any(row["task_full_name"] == task and row["start_time_utc"] >= _epoch(ts)
                       and (not upper or row["start_time_utc"] < _epoch(upper))
                       for task, ts, upper in pairs)
        bounds = re.findall(r"start_time_utc >= TIMESTAMP '([^']*)'", where)
        return all(row["start_time_utc"] >= _epoch(ts) for ts in bounds)

    def query(self, *, where="1=1", out_fields="*", result_offset=0, **kw):
        self.queries.append({"where": where, **kw})
        matched = [r for r in self.rows if self._matches(r, where)]
        if kw.get("out_statistics"):
            stat = kw["out_statistics"][0]
            assert (stat["statisticType"], stat["onStatisticField"]) == ("max", "start_time_utc")
            assert kw["group_by_fields_for_statistics"] == "task_full_name"
            newest = {}
            for row in matched:
                key = row["task_full_name"]
                newest[key] = max(newest.get(key, 0), row["start_time_utc"])
            return _Result([{"task_full_name": k, stat["outStatisticFieldName"]: v}
                            for k, v in newest.items()])
        return _Result(matched[result_offset:result_offset + 2000])


def _summary(rows):
    table = _RunsTable(rows)
    return publish.summarize_runs_from_agol(table, HOST, NOW), table


def test_only_the_last_30_days_of_run_rows_are_downloaded():
    rows = [_row("\\Daily", d) for d in (1, 2, 29, 31, 45, 80)]
    summary, table = _summary(rows)
    downloaded = [q for q in table.queries if not q.get("out_statistics")]
    assert len(downloaded) == 1 and "start_time_utc >=" in downloaded[0]["where"]
    assert summary["\\Daily"]["completed_run_count"] == 3  # 1, 2 and 29 days ago


def test_a_task_with_a_run_in_the_window_summarizes_as_before():
    rows = [_row("\\Daily", d, duration=10.0 + d) for d in (0.5, 1, 2, 3, 4, 5, 6, 8)]
    rows.append(_row("\\Daily", 0.1, duration=None, status="Running"))
    summary, _ = _summary(rows)
    expected = publish.summarize_runs(
        [dict(r, start_time_utc=dt.datetime.fromtimestamp(
            r["start_time_utc"] / 1000, tz=dt.timezone.utc).isoformat()) for r in rows], NOW)
    assert summary == expected


def test_a_task_whose_newest_run_is_older_than_the_window_keeps_its_last_run_facts():
    rows = [_row("\\Retired", 45, duration=42.0, error=1, status="Error"),
            _row("\\Retired", 60), _row("\\Retired", 70),
            _row("\\Daily", 1)]
    summary, table = _summary(rows)
    stale = summary["\\Retired"]
    assert stale["last_run_status"] == "Error"
    assert stale["last_run_is_error"] == 1
    assert stale["last_duration_seconds"] == 42.0
    assert stale["runs_last_7_days"] == 0
    assert stale["completed_run_count"] == 1, "only the newest run is fetched, not the 60/70 day rows"
    follow_up = table.queries[-1]
    assert "'\\Retired'" in follow_up["where"] and "'\\Daily'" not in follow_up["where"]


def test_a_missed_row_in_the_window_does_not_hide_an_older_real_run():
    rows = [_row("\\Weekly", 2, status="Missed", duration=None), _row("\\Weekly", 40, duration=7.0)]
    summary, _ = _summary(rows)
    assert summary["\\Weekly"]["last_duration_seconds"] == 7.0
    assert summary["\\Weekly"]["last_run_status"] == "Success"


def test_in_window_non_run_rows_are_not_counted_twice_by_the_follow_up():
    rows = [_row("\\Weekly", 2, status="Missed", duration=None),
            _row("\\Weekly", 0.5, status="Skipped-AlreadyRunning", duration=None),
            _row("\\Weekly", 40, duration=7.0)]
    summary, _ = _summary(rows)
    assert summary["\\Weekly"]["skipped_last_7_days"] == 2  # the Missed row and the Skipped row, once each
    assert summary["\\Weekly"]["skipped_already_running_24h"] == 1


def test_a_running_row_in_the_window_does_not_hide_an_older_finished_run():
    rows = [_row("\\Slow", 0.1, duration=None, status="Running"),
            _row("\\Slow", 40, duration=7.0, error=1, status="Error"), _row("\\Slow", 55)]
    summary, table = _summary(rows)
    slow = summary["\\Slow"]
    assert slow["last_run_status"] == "Running" and slow["running_since"]
    assert slow["last_duration_seconds"] == 7.0 and slow["last_completed_is_error"] == 1
    assert slow["last_completed_start_utc"]
    assert slow["completed_run_count"] == 1, "only the newest finished run is fetched"
    assert "'Running'" in table.queries[-1]["where"]  # the follow-up asks for finished rows only


def test_a_stuck_running_row_outside_the_window_does_not_hide_the_last_finished_run():
    rows = [_row("\\Stuck", 35, duration=None, status="Running"), _row("\\Stuck", 40, duration=7.0),
            _row("\\Stuck", 55, duration=9.0)]
    summary, _ = _summary(rows)
    stuck = summary["\\Stuck"]
    assert stuck["last_run_status"] == "Running"
    assert stuck["last_duration_seconds"] == 7.0 and stuck["last_completed_start_utc"]
    assert stuck["completed_run_count"] == 1


def test_a_task_with_a_finished_run_in_the_window_needs_no_finished_run_query():
    rows = [_row("\\Slow", 0.1, duration=None, status="Running"), _row("\\Slow", 3)]
    _, table = _summary(rows)
    assert len(table.queries) == 2


def test_the_newest_run_query_is_one_grouped_max_on_the_task_key():
    _, table = _summary([_row("\\Daily", 1), _row("\\Other", 50)])
    stats = [q for q in table.queries if q.get("out_statistics")]
    assert len(stats) == 1
    assert stats[0]["out_statistics"] == [{"statisticType": "max", "onStatisticField": "start_time_utc",
                                           "outStatisticFieldName": "newest_start"}]
    assert stats[0]["group_by_fields_for_statistics"] == "task_full_name"
    assert "host_name = 'H'" in stats[0]["where"] and "NOT IN" in stats[0]["where"]


def test_no_follow_up_query_when_every_task_ran_inside_the_window():
    _, table = _summary([_row("\\Daily", 1), _row("\\Other", 20)])
    assert len(table.queries) == 2  # the window download and the grouped MAX, nothing else


def test_another_hosts_rows_are_never_read():
    rows = [_row("\\Daily", 1), _row("\\Daily", 50, host="Other")]
    summary, _ = _summary(rows)
    assert summary["\\Daily"]["completed_run_count"] == 1

"""Round 1 audit, metrics findings M1-M15, X5 and D2.

Every log line below is copied from a real TASKHOST-1 log (trimmed to the lines each test
needs), so a test fails if the parser stops reading the format the scripts actually write.
"""

from __future__ import annotations

import pytest

import taskdash_metrics as metrics


def _scan(tmp_path, text, name="run.log"):
    log = tmp_path / name
    log.write_text(text, encoding="utf-8")
    return metrics.scan_log_file(log, max_lines=0)


def _values(result):
    return {key: value for key, (value, _) in result["metrics"].items()}


# ---------------------------------------------------------------- M1 per-layer keys

STORMWATER = """\
2026-09-25 00:25:18,797 - INFO - Processing: Acme_StormBasins
2026-09-25 00:25:25,821 - INFO -   Exported 3,404 features to temp FGDB
2026-09-25 00:25:36,213 - INFO -   Deleting 3,404 features (max OID: 279,128)...
2026-09-25 00:26:02,567 - INFO -   Batch OID 274001-276000: deleted 276
2026-09-25 00:26:05,955 - INFO -   Batch OID 278001-280000: deleted 1,128
2026-09-25 00:26:05,955 - INFO -   Total deleted: 3,404
2026-09-25 00:26:54,188 - INFO -   Verification: 3,404 features in hosted layer
2026-09-25 00:28:02,377 - INFO - Processing: Acme_StormPipe
2026-09-25 00:28:10,050 - INFO -   Exported 15,562 features to temp FGDB
2026-09-25 00:28:16,827 - INFO -   Deleting 15,562 features (max OID: 824,786)...
2026-09-25 00:29:21,533 - INFO -   Batch OID 824001-826000: deleted 786
2026-09-25 00:29:21,533 - INFO -   Total deleted: 15,562
2026-09-25 00:31:20,582 - INFO -   Verification: 15,562 features in hosted layer
2026-09-25 00:31:21,124 - INFO -   Acme_StormBasins              SUCCESS                        (3,404 features)
"""


def test_m1_each_stormwater_layer_keeps_its_own_counts(tmp_path):
    values = _values(_scan(tmp_path, STORMWATER))
    assert values == {
        "acme_storm_basins_exported_record_count": 3404.0,
        "acme_storm_basins_deleted_record_count": 3404.0,
        "acme_storm_basins_hosted_record_count": 3404.0,
        "acme_storm_pipe_exported_record_count": 15562.0,
        "acme_storm_pipe_deleted_record_count": 15562.0,
        "acme_storm_pipe_hosted_record_count": 15562.0,
    }


def test_m1_one_section_with_one_value_keeps_the_plain_key(tmp_path):
    values = _values(_scan(tmp_path, "2026-09-25 00:28:02,377 - INFO - Processing: Acme_StormPipe\n"
                                     "2026-09-25 00:31:20,582 - INFO -   Verification: 15,562 features in hosted layer\n"))
    assert values == {"hosted_record_count": 15562.0}


# ---------------------------------------------------------------- M2 key=value (Streets OpenData)

STREETS_DIFF = """\
2026-09-25 02:30:24,179 INFO source rows=47342  target rows=47342
2026-09-25 02:30:24,179 INFO diff: inserts=0 updates=1 deletes=0 unchanged=47341
2026-09-25 02:30:24,179 INFO       of which attr_changed=0 geom_changed=0 (a row can be both)
2026-09-25 02:30:24,179 INFO geometry candidates=4, tolerance-suppressed=4 (TOL=0.01 ft)
2026-09-25 02:30:24,179 INFO       suppressed SEGMENT_IDs: 54840, 54842, 54953, 54991
2026-09-25 02:30:24,179 INFO estimated delta rows to be written = inserts + 2*updates + deletes = 2
2026-09-25 02:30:24,179 INFO target rows after=47342 (expected 47342)
"""


def test_m2_streets_differential_counts_are_mined(tmp_path):
    values = _values(_scan(tmp_path, STREETS_DIFF))
    assert values == {
        "source_record_count": 47342.0,
        "target_record_count_before": 47342.0,
        "inserted_record_count": 0.0,
        "updated_record_count": 1.0,
        # the formula's "deletes = 2" is the estimated total, not the delete count
        "deleted_record_count": 0.0,
        "unchanged_record_count": 47341.0,
        "target_record_count_after": 47342.0,
    }


def test_m2_full_refresh_insert_line_is_a_reload_count_not_an_insert_count(tmp_path):
    # metrics_tab-05: a full refresh's "inserted N rows in ..." is every row in the table, not
    # a differential's real insert count -- keep it out of inserted_record_count, or a full
    # refresh reads as a -100% drop against the differential runs' usual insert count.
    values = _values(_scan(tmp_path,
                           "2026-09-18 02:32:19,495 INFO inserted 47342 rows in 0:01:20.620008\n"
                           "2026-09-18 02:32:52,088 INFO row count verified: 47342\n"))
    assert values == {"reloaded_record_count": 47342.0, "target_record_count_after": 47342.0}


# ---------------------------------------------------------------- M3 separators and log choice

@pytest.mark.parametrize("line,level", [
    ("2026-09-24 07:54:02 -- ERROR: Failed to query feature count: HTTPSConnectionPool(host='services1.arcgis.com', port=443)", "ERROR"),
    ("2026-09-05 01:01:41,611 [ERROR]   FAILED after 0:01:20.538222: ERROR 999999", "ERROR"),
    ("2026-09-24 05:00:00,922:INFO:Server restart script initiated.", "INFO"),
])
def test_m3_level_is_read_across_every_separator(line, level):
    assert metrics.LOG_LEVEL_RE.match(line).group("level").upper() == level


def test_m3_preamble_is_stripped_for_colon_and_bracket_levels():
    assert metrics.LOG_PREAMBLE_RE.sub("", "2026-09-24 05:00:00,923:INFO:Estimated total runtime: 64 minutes") \
        == "Estimated total runtime: 64 minutes"
    assert metrics.LOG_PREAMBLE_RE.sub("", "2026-09-25 01:04:41,713 [INFO] All locators rebuilt successfully.") \
        == "All locators rebuilt successfully."


def test_m3_a_debug_file_is_not_the_latest_log_when_a_run_log_exists(tmp_path):
    import os
    logs = tmp_path / "Logs"
    logs.mkdir()
    run_log = logs / "csc_20260925.log"
    run_log.write_text("2026-09-24 23:50:13,313 - INFO - Total records in feature class: 356908\n",
                       encoding="utf-8")
    debug_log = logs / "deskmailer_debug.log"
    debug_log.write_text("2026-09-22 07:00:51 -- ERROR: SMTP connection/setup failed, e-mail section "
                         "aborted: _ssl.c:1015: The handshake operation timed out\n", encoding="utf-8")
    os.utime(run_log, (run_log.stat().st_mtime - 60,) * 2)  # the debug file is the newest
    (tmp_path / "csc.py").write_text("", encoding="utf-8")

    _, summary = metrics.build_metric_rows(
        [{"task_name": "Service Desk Mailer", "task_full_name": "\\Service Desk Mailer",
          "command_target_path": str(tmp_path / "csc.py")}], host_name="H")
    assert summary["\\Service Desk Mailer"]["latest_log_name"] == "csc_20260925.log"


# ---------------------------------------------------------------- M4 junk series

@pytest.mark.parametrize("line", [
    "2026-09-25 00:28:16,827 - INFO -   Deleting 15,562 features (max OID: 824,786)...",
    "2026-09-25 02:30:24,179 INFO       suppressed SEGMENT_IDs: 54840, 54842, 54953, 54991",
    "2026-09-19 07:00:06,413 - INFO - Service max record count: 20000",
    "2026-09-19 20:00:23,600 - INFO -   Processed 40,000 records...",
    "2026-09-23 11:34:44,722 -   skip 120777  Growth_Area            idle=4 ack=0",
    "2026-09-25 07:05:01,672 - INFO -   CRITICAL LOUDLY  ObjectID=56675 ReplicaID=56675 gap=443",
    "[2026-09-24 23:26:21] OK    Process completed in 71.6s - Features: 1",
])
def test_m4_ids_settings_and_progress_ticks_are_not_metrics(tmp_path, line):
    keys = set(_scan(tmp_path, line + "\n")["metrics"])
    # metrics_tab-02/wording-r3regress-noise-metrics-in-picker: output_feature_count is now
    # NOISE_METRIC_KEY_PATTERNS too (a structural constant, not workload), so only
    # duration_seconds can survive from these lines.
    assert not keys - {"duration_seconds"}, keys
    assert "s_features" not in keys


# ---------------------------------------------------------------- M5 Parcel Refresh loaded rows

def test_m5_loaded_row_counts_are_mined(tmp_path):
    values = _values(_scan(tmp_path, """\
2026-09-25 06:21:08,744 - INFO - Staged data validation PASSED (288,148 rows)
2026-09-25 06:21:18,380 - INFO - SDE Parcels before truncate: 288,148
2026-09-25 06:27:29,481 - INFO - Parcels row count validation PASSED (288,148 rows)
2026-09-25 06:35:14,608 - INFO - SDE Parcels_Permit publish validated (288,148 rows)
2026-09-25 06:35:17,957 - INFO - SDE Parcels_Permit_Crosswalk publish validated (3,773 rows)
"""))
    assert values == {
        "staged_row_count": 288148.0,
        "sde_parcels_before_truncate": 288148.0,
        "parcels_row_count": 288148.0,
        "sde_parcels_permit_row_count": 288148.0,
        "sde_parcels_permit_crosswalk_row_count": 3773.0,
    }


# ---------------------------------------------------------------- M6 Customer Base Parcels

def test_m6_join_counts_are_kept(tmp_path):
    values = _values(_scan(tmp_path, """\
2026-09-24 23:35:19,546 INFO billing parcels    : 51888
2026-09-24 23:35:19,547 INFO matched (distinct) : 51846
2026-09-24 23:35:19,548 INFO unmatched          : 39
2026-09-24 23:38:08,625 INFO output polygons    : 51847  -> C:\\Jobs\\x.gdb\\GISDB.GISOWNER.Utility_Account_Parcels
2026-09-24 23:38:08,626 INFO output points      : 51847  -> C:\\Jobs\\x.gdb\\GISDB.GISOWNER.Customer_Service_Points
2026-09-24 23:38:08,629 INFO exceptions written : 39  -> C:\\Jobs\\UtilityAccountParcels\\unmatched_parcels.csv
2026-09-24 23:38:08,629 INFO collapsed spellings: 3
"""))
    # metrics_tab-09: exceptions_written always equals unmatched_parcel_count (the log line
    # itself is "exceptions written : 39 -> unmatched_parcels.csv") -- it's noise now, not a
    # second series under a different name.
    assert values == {
        "billing_parcels": 51888.0, "matched_parcel_count": 51846.0, "unmatched_parcel_count": 39.0,
        "output_polygon_count": 51847.0, "output_point_count": 51847.0,
    }


# ---------------------------------------------------------------- M7 Animal Services join sections

def test_m7_each_join_keeps_its_own_counts(tmp_path):
    values = _values(_scan(tmp_path, """\
2026-09-10 08:01:18  INFO       AP exported: 356817 records
2026-09-10 08:01:19  INFO       Joining AP x Geo (Direction -> Geo)...
2026-09-10 08:01:43  INFO         26 unmatched points, running CLOSEST fallback...
2026-09-10 08:01:51  INFO         Updated 53939 records
2026-09-10 08:01:51  INFO       Joining AP x Zip (PO_NAME -> Jurisdicti)...
2026-09-10 08:02:16  INFO         14 unmatched points, running CLOSEST fallback...
2026-09-10 08:02:33  INFO         Updated 356817 records
"""))
    assert values == {
        "ap_exported": 356817.0,
        "ap_x_geo_unmatched_point_count": 26.0, "ap_x_geo_updated_record_count": 53939.0,
        "ap_x_zip_unmatched_point_count": 14.0, "ap_x_zip_updated_record_count": 356817.0,
    }


# ---------------------------------------------------------------- M8 one error/warning rule

@pytest.mark.parametrize("line,expected", [
    ("2026-09-25 07:05:01,672 - INFO -   CRITICAL LOUDLY  ObjectID=56675 ReplicaID=56675 gap=443", (True, False)),
    ("2026-09-25 07:05:01,676 - INFO -   WARNING LOUDLY  ObjectID=100196 ReplicaID=100196 gap=9", (False, True)),
    ("2026-09-25 07:05:01,671 - INFO - CHECK 2  watermark gap: 312 live replica items parsed, "
     "27 over the warning threshold (gap > 3)", (False, False)),
    ("2026-09-07 06:05:21 | WARNING  | find_duplicates |   45751: 2 occurrences", (False, True)),
    ("2026-09-24 07:54:02 -- ERROR: Failed to query feature count: HTTPSConnectionPool(host='services1.arcgis.com', port=443)", (True, False)),
    ("Failed to execute (RebuildAddressLocator).", (True, False)),
    ("2026-09-25 02:30:24,179 INFO no changes to apply; edit session not opened (0 delta rows written)", (False, False)),
])
def test_m8_errors_and_warnings_use_one_rule(line, expected):
    assert metrics.classify_line(line) == expected


# ---------------------------------------------------------------- M10 durations

@pytest.mark.parametrize("line,seconds", [
    ("2026-09-24 23:51:11,474 - INFO - Completed: Processed 356908 records, updated 26 in 31.8 seconds", 31.8),
    ("2026-09-25 02:06:28,851 - INFO - SUCCESS: Copied 288148 records in 383.79s", 383.79),
    ("[2026-09-24 23:26:21] OK    Process completed in 71.6s - Features: 1", 71.6),
    ("2026-09-19 07:00:39,221 - INFO - Process completed in 0:00:34.516054", 34.516054),
])
def test_m10_completion_line_durations_are_mined(tmp_path, line, seconds):
    assert _values(_scan(tmp_path, line + "\n"))["duration_seconds"] == pytest.approx(seconds)


@pytest.mark.parametrize("line", [
    "2026-09-25 01:01:38,155 [INFO]   Rebuilt successfully in 0:01:17.975144.",
    "2026-09-25 02:30:11,855 INFO preflight OK (83 data columns, shape=Shape) in 0:00:09.948037",
    "2026-09-07 06:15:31 | INFO     | update_adds | Added 56 new segments in 13.41 seconds",
])
def test_m10_step_timings_are_not_the_run_duration(tmp_path, line):
    assert "duration_seconds" not in _scan(tmp_path, line + "\n")["metrics"]


# ---------------------------------------------------------------- M11 Parcel Diff clauses

def test_m11_removed_and_common_parcels_are_mined(tmp_path):
    values = _values(_scan(tmp_path, "2026-09-24 06:45:27 | INFO     | Found 0 new parcels, "
                                     "1 removed parcels, 287,969 common parcels\n"))
    assert values == {"new_parcel_count": 0.0, "removed_parcel_count": 1.0, "common_parcel_count": 287969.0}


# ---------------------------------------------------------------- M12 duplicate phrasings

def test_m12_processed_records_is_the_feature_count(tmp_path):
    values = _values(_scan(tmp_path, """\
2026-09-24 23:50:13,313 - INFO - Total records in feature class: 356908
2026-09-24 23:51:11,474 - INFO - Completed: Processed 356908 records, updated 26 in 31.8 seconds
"""))
    assert set(values) == {"feature_record_count", "updated_record_count", "duration_seconds"}


def test_m12_hydrant_download_count_is_the_source_count(tmp_path):
    values = _values(_scan(tmp_path, """\
2026-09-19 07:00:06,490 - INFO - Total records to download: 4610
2026-09-19 07:00:13,476 - INFO - Download complete: 4610 features saved to C:\\Jobs\\HydrantUpdates\\OutputData.gdb
2026-09-19 07:00:18,687 - INFO - Source record count: 4610
"""))
    assert values == {"source_record_count": 4610.0}


# ---------------------------------------------------------------- M13 prose counts

def test_m13_prose_counts_are_mined(tmp_path):
    values = _values(_scan(tmp_path, """\
2026-09-25 07:05:01,671 - INFO - CHECK 2  watermark gap: 312 live replica items parsed, 27 over the warning threshold (gap > 3)
2026-09-25 07:05:01,690 - INFO - CHECK 3  SYNC chain length: 15 replica families, 0 over the critical threshold (chain > 20)
2026-09-23 11:35:18,874 - RESULT: would touch 5 idle replica(s)
2026-09-05 01:04:49,741 [WARNING] Completed WITH FAILURES (4/5):
2026-09-07 06:04:44 | INFO     | extract_feature_classes | Extracted ADDS_PTS to \\\\contoso\\dfs\\2026\\0907 (356813 features)
2026-09-24 22:07:33,006 - INFO - Total datasets to analyze: 860
"""))
    # metrics_tab-10: "would touch 5 idle replica(s)" is a dry-run probe, not the scheduled
    # --apply run -- it gives no idle_replica_count now (see test_round2_metrics.py).
    assert values == {
        "replica_item_count": 312.0, "replica_over_warning_count": 27.0,
        "replica_family_count": 15.0, "replica_family_over_critical_count": 0.0,
        "failure_count": 4.0,
        "adds_pts_feature_count": 356813.0, "total_datasets_to_analyze": 860.0,
    }


# ---------------------------------------------------------------- M14 never-run task

def test_m14_logs_of_a_task_that_never_ran_are_ignored(tmp_path):
    logs = tmp_path / "Logs"
    logs.mkdir()
    (logs / "keepalive_20260923_113444.log").write_text(
        "2026-09-23 11:35:18,874 - RESULT: would touch 5 idle replica(s)\n", encoding="utf-8")
    (tmp_path / "keepalive.py").write_text("", encoding="utf-8")

    rows, summary = metrics.build_metric_rows(
        [{"task_name": "Replica Keep-Alive Job", "task_full_name": "\\Keep-Alive",
          "command_target_path": str(tmp_path / "keepalive.py"), "last_run_time": None}], host_name="H")
    assert rows == []
    # explicit None, so the hosted row loses a stale latest log instead of keeping it
    assert summary["\\Keep-Alive"]["latest_log_name"] is None
    assert summary["\\Keep-Alive"]["log_files_scanned"] == 0


# ---------------------------------------------------------------- M15 total wins over a step

def test_m15_total_execution_time_wins_whatever_the_line_order(tmp_path):
    values = _values(_scan(tmp_path, """\
2026-09-07 06:15:43 | INFO     | main | Total execution time: 929.82 seconds (15.50 minutes)
2026-09-07 06:15:43 | INFO     | update_updates | Processing time: 6.01 seconds (0.1 minutes)
"""))
    assert values["duration_seconds"] == pytest.approx(929.82)


# ---------------------------------------------------------------- X5 / D2 display names

@pytest.mark.parametrize("key,name", [
    # metrics_tab-07/wording-07: ap_exported and cf_change_count are now DISPLAY_NAMES
    # overrides, not the automatic sentence-case rule -- see taskdash_metrics.py DISPLAY_NAMES.
    ("ap_exported", "Address points exported"),
    ("cf_change_count", "Confidentiality changes"),
    # metrics_tab-03/wording-metric-names-mixed-patterns: "before truncate" is now "before
    # reload" -- an override, not the auto sentence-case rule.
    ("sde_parcels_permit_crosswalk_before_truncate", "SDE parcels EPL crosswalk before reload"),
    # metrics_tab-04/wording-08: duration_seconds is also a DISPLAY_NAMES override now, so a
    # native table without fmtDur (Metric range) shows its unit instead of a bare number.
    # metrics_tab-01: "Script-reported" because on AP/Street Updates it times only the
    # field-update phase, not the whole run.
    ("duration_seconds", "Script-reported time (seconds)"),
    # metrics_tab-02: an override now too, so the three Acme Stormwater layers read "rows in
    # hosted layer" instead of the raw canonical key in sentence case.
    ("acme_storm_basins_hosted_record_count", "Acme storm basins rows in hosted layer"),
    # repeat-metrics_tab-03-names-hide-meaning: the auto sentence-case name dropped the only
    # informative part of the log line ("ZIP values swapped (99999 replaced): 45"); now an
    # override that keeps it.
    ("zip_values_swapped_count", "ZIP 99999 values replaced"),
])
def test_x5_display_names_keep_acronyms_in_sentence_case(key, name):
    assert metrics.metric_display_name(key) == name


def test_x5_rows_carry_the_new_display_name(tmp_path):
    logs = tmp_path / "Logs"
    logs.mkdir()
    (logs / "run.log").write_text("2026-09-24 22:39:25,090 - INFO - Duration: 0:39:18.669188\n", encoding="utf-8")
    (tmp_path / "run.py").write_text("", encoding="utf-8")
    rows, _ = metrics.build_metric_rows(
        [{"task_name": "Geodatabase Upkeep", "task_full_name": "\\DB",
          "command_target_path": str(tmp_path / "run.py")}], host_name="H")
    assert [(r["metric_key"], r["display_name"], round(r["metric_value"], 2)) for r in rows] == [
        ("duration_seconds", "Script-reported time (seconds)", 2358.67)]

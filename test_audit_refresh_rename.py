"""A DISPLAY_NAMES rename splits a metric series in two until --refresh-metrics renames old rows.

The hourly upsert only renames rows from logs it mines again, so older rows keep the old name.
plan_metric_refresh is the only path that renames them. This pins that it does so for the four
keys renamed in the wording audit, so the deploy step in the owner notes does what it says.
"""
import taskdash_metrics as metrics
import taskdash_publish as publish

RENAMED = {
    "exported_hosted_has": "Exported Hosted Has",
    "found_record": "Found Record",
    "history_rows_to_update_with_an_earlier_qa_decision": "History Rows To Update With An Earlier Qa Decision",
    "review_fields_to_the_qacycle_row": "Review Fields To The Qacycle Row",
}


def test_refresh_renames_old_rows_of_the_wording_audit_keys():
    task = "\\S"
    stored = [{"OBJECTID": i, "host_name": "H", "task_full_name": task, "metric_key": key,
               "log_file_name": "old.log", "display_name": old,
               "series_key": metrics.metric_series_key("H", task, key)}
              for i, (key, old) in enumerate(RENAMED.items(), start=1)]
    deletes, updates = publish.plan_metric_refresh(stored, [], "OBJECTID")
    assert deletes == []
    assert updates == [{"OBJECTID": i, "display_name": metrics.metric_display_name(key)}
                       for i, key in enumerate(RENAMED, start=1)]
    assert all(u["display_name"] != old for u, old in zip(updates, RENAMED.values()))

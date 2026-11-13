"""Final-audit wording fixes: filter labels, About text, plain help, metric names, rise flag.
Arcade cannot run here, so Arcade checks read the generated source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import taskdash_common

taskdash_common.disable_arcpy_import()

import taskdash_dashboard as dash  # noqa: E402
import taskdash_metrics as metrics  # noqa: E402


def _build():
    tpl = json.loads((Path(dash.__file__).parent / dash.TEMPLATE_FILE).read_text(encoding="utf-8"))
    by_name = {w["name"]: w for w in tpl["desktopView"]["widgets"]}
    templates = {"table": by_name["Task Inventory"], "list": by_name["Recent Runs"],
                 "serial_config": by_name["Schedule Mix"]["chartConfig"]}
    return dash.build("TESTITEM", templates)


def _help(name):
    return " ".join(x for x in dash.HELP[name] if x)


def test_status_and_schedule_labels_name_what_they_drive():
    # They reach HEALTH_SCHEDULE_TARGETS: the Overview widgets and the Setting notes list only.
    assert dash.HEALTH_SCHEDULE_TARGETS == dash.INVENTORY_TARGETS + ("Setting Notes",)
    assert "Collector" not in dash.selector_labels("Health")["label"]
    assert dash.selector_labels("Health")["label"] == "Status (Overview and Setting notes)"
    assert dash.selector_labels("Schedule")["label"] == "Schedule (Overview and Setting notes)"


def test_scheduled_bucket_gets_a_clearer_selector_label():
    assert dash.selector_labels("Schedule")["label_overrides"] == {"Scheduled": "Scheduled (frequency not read)"}


def test_about_text_lists_recent_changes():
    about = next(w for w in dash.all_items(_build()) if w["name"] == dash.ABOUT_NAME)
    assert "recent changes" in about["text"]


def test_help_has_no_config_jargon_and_one_term_per_concept():
    everything = " ".join(_help(n) for n in dash.HELP)
    for jargon in ("workflow_descriptions", "after their next run time", "the config sets"):
        assert jargon not in everything
    assert "log error note" not in everything


def test_metric_observations_prompt_says_how_to_open_filters():
    assert "Open the Filters panel at the left edge" in dash.PROMPTS["Metric Observations"]


def test_recent_failures_help_explains_the_pink_tint():
    assert "pink" in _help("Recent Failures") and "7 days" in _help("Recent Failures")


def test_new_help_sentences_stay_short():
    for name in ("Overdue", "Recent Failures", "Metric Change", "Setting Notes", "Collector Runs", "Task Health"):
        for sentence in re.split(r"(?<=[.!?])\s+", _help(name)):
            assert len(sentence.split()) <= 25, sentence


def test_odd_metric_names_are_fixed_and_keys_do_not_change():
    odd = {"exported_hosted_has": "Exported hosted has", "found_record": "Found record",
           "review_fields_to_the_qacycle_row": "Review fields to the qacycle row",
           "history_rows_to_update_with_an_earlier_qa_decision":
               "History rows to update with an earlier qa decision"}
    for key, old in odd.items():
        assert metrics.metric_display_name(key) != old
    # Names only: a key is the series identity, so the same key text must still map to the same series.
    assert metrics.metric_series_key("H", "\\T", "found_record") == "H|\\T|found_record"
    assert metrics.metric_display_name("checked_record_count") == "Checked record count"


def test_rise_flag_for_a_lower_is_better_count():
    script = dash.metric_change_source_script("https://example.maps.arcgis.com", "TESTITEM", 2)
    assert dash.METRIC_CHANGE_FLAGS[2] == "Far over usual"
    assert f"change >= {dash.METRIC_CHANGE_RISE_PCT}" in script
    assert "latest > highest && lowerIsBetter" in script
    assert "highest = sp[n - 1]" in script
    # flagged rows still sort first, and the flag text reaches the table and its help
    assert '"flag_rank ASC, task_name ASC, display_name ASC"' in script
    assert 'flag_rank: IIf(flag != "", 0,' in script
    assert f'"{dash.METRIC_CHANGE_FLAGS[2]}"' in _help("Metric Change")
    assert dash.METRIC_CHANGE_FLAGS[2] in script

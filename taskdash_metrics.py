"""Mine each workflow's own log files into a metric time series.

One row per (task, log file, metric) rather than one last/min/max row per task, so the
dashboard can chart a metric over time and an overnight collapse in a record count is visible.
Each log file is treated as one run; its modification time is the observation timestamp.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from taskdash_common import data_dir, emit, home_dir, normalize_text, parse_iso_datetime, utc_now

DEFAULT_MAX_LOG_FILES_PER_TASK = 40
DEFAULT_MAX_LINES_PER_FILE = 5000
# task_detail-01/metrics_tab-10: a scheduled run creates its log when it starts, so a log file
# created more than this long after last_run_time is a manual/other run, not the scheduled one.
MANUAL_RUN_SLACK_MINUTES = 15
LOG_SUFFIXES = (".log", ".txt")
# metrics-08: a directory named Logs/logs/Log/log is unambiguously a log folder, so any .txt
# in it is fair game. A bare script folder is not -- README.txt, data.txt etc. sit right next
# to the script, and a "recently edited" .txt there used to get treated as a log and could even
# win latest_log. .log is trusted everywhere; .txt only inside a directory named like the above.
LOG_DIR_NAMES = {"logs", "log"}

# Strip the logging preamble before mining a line. Without this the label matcher sees the
# level word as part of the metric name: "... - INFO - Duration: 0:00:07" yielded BOTH the real
# duration_seconds=7.13 and a junk info_duration=0.0 (the "0" of "0:00:07"), and since
# canonicalize_metric_key maps info_duration onto duration_seconds too, the zero overwrote the
# real value -- every such task charted a flat 0. Same cause made "Start time: 2026-.." mine
# a metric of 2026. Handles every shape on the task server: "ts - LEVEL -", "ts | LEVEL |",
# "ts LEVEL -", "ts [LEVEL]", "ts:LEVEL:", "ts -- LEVEL:", "[ts]", and a bare timestamp.
# M3: the separator is a RUN of -|:[ characters, not one -- "ts -- ERROR:" used to keep its
# level, so nearly all of one task's error lines went uncounted.
# The timestamp, separator and level pieces live in the rules files (log_preamble); see
# load_rules. metrics-07: LOG_LEVEL_RE captures the level word across every preamble shape,
# so error-line detection isn't stuck reading only the dash-delimited shape.
LOG_PREAMBLE_RE: re.Pattern
LOG_LEVEL_RE: re.Pattern

# M8: a level word can also lead the message itself -- Replica Lag Monitor logs its
# findings as "- INFO -   CRITICAL LOUDLY ..." / "- INFO -   WARNING LOUDLY ...". Both count
# the same way; the old rule counted any "warning" substring but never a leading CRITICAL.
MESSAGE_LEVEL_RE = re.compile(r"^[\W_]*(?P<level>ERRORS?|CRITICAL|FATAL|WARNINGS?|WARN)\b", re.IGNORECASE)
ERROR_LEVELS = {"ERROR", "ERRORS", "CRITICAL", "FATAL"}
WARNING_LEVELS = {"WARNING", "WARNINGS", "WARN"}
FAILED_WORD_RE = re.compile(r"\bfailed\b")
WARNING_KEYWORD_RE = re.compile(r"\bwarnings?:")

# metrics-07: "0 failed" / "errors: 0" / "failed: 0" report SUCCESS, not a failure -- without
# this a clean run with a zero-failure summary line counted as an error line.
ZERO_COUNT_RE = re.compile(
    r"(?i)\b(?:0|no|zero)\s+(?:failed|failures?|errors?|warnings?)\b"
    r"|\b(?:failed|failures?|errors?|warnings?)\s*:?\s*0\b"
)

HMS_DURATION_RE = re.compile(
    r"(?i)\b(?P<label>duration|elapsed time|processing time|total execution time|total time)\s*:\s*"
    r"(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2}(?:\.\d+)?)"
)

# metrics-06: an optional duration unit right after the number, so "45000 ms" and "3 minutes"
# can be normalised to seconds instead of stored as literal 45000/3 in duration_seconds.
# M2: "=" separates a label too (Streets OpenData: "source rows=47342  diff: inserts=0"). A label
# never starts mid-word ("71.6s - Features: 1" used to mine key s_features), and the value never
# ends on a comma, so the list check in extract_metrics_from_line can see "54840, 54842".
LABEL_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<label>[A-Za-z][A-Za-z0-9 /()_-]{2,80}?)\s*[:=]\s*"
    r"(?P<value>-?\d(?:[\d,]*\d)?(?:\.\d+)?)"
    r"(?:\s*(?P<unit>milliseconds?|ms|seconds?|secs?|sec|s|minutes?|mins?|min|hours?|hrs?|hr|h))?\b"
)
# "54840, 54842, 54953" is a list of IDs, not a count; "= inserts + 2*updates + deletes = 2" is a
# formula, and its trailing "deletes = 2" is the formula's total, not the delete count.
_LIST_TAIL_RE = re.compile(r"^\s*,\s*\d")
_OPERATOR_BEFORE_RE = re.compile(r"[+*/]\s*$")

# M10: a completion line's own duration -- "Process completed in 71.6s", "SUCCESS: Copied N
# records in 383.79s", "Process completed in 0:00:34.5". Only on a line that says completed or
# success, so a step timing ("inserted N rows in 0:01:20", "preflight OK ... in 0:00:09") and a
# per-locator "Rebuilt successfully in" are never read as the run's duration.
COMPLETION_LINE_RE = re.compile(r"(?i)\b(?:completed|success)\b")
IN_DURATION_RE = re.compile(
    r"(?i)\bin\s+(?:(?:(?P<hours>\d+):)?(?P<minutes>\d{1,2}):(?P<seconds>\d{2}(?:\.\d+)?)"
    r"|(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|secs?|seconds?|mins?|minutes?|hours?|h))\b"
)

# M5: "Parcels row count validation PASSED (288,148 rows)" / "SDE Parcels_Permit publish
# validated (288,148 rows)" carry the rows actually loaded; the "before truncate" lines only
# carry the previous day's count.
VALIDATED_ROWS_RE = re.compile(
    r"(?i)^(?P<subject>.+?)\s+(?:row count validation passed|data validation passed|"
    r"publish validated|validation passed|validated)\s*\((?P<value>\d[\d,]*)\s+rows\)"
)

# M13: per-dataset extract counts -- "Extracted ADDS_PTS to <path> (356813 features)". The
# optional leading "func |" is the Address & ERP Script's logger-function column.
EXTRACTED_FEATURES_RE = re.compile(
    r"^(?:[\w.]+\s*\|\s*)?(?:Extracted|Exported)\s+(?P<name>[A-Za-z][\w.]*)\b[^()]*"
    r"\((?P<value>\d[\d,]*)\s+features\)"
)

# critic-02 / metrics_tab-08: a script logs some counts only when something happened (updates on
# a changed night, failure_count on a failing run). A quiet or clean run left no point, so Metric
# range averaged only the busy nights and a stale nonzero failure_count stayed the latest value
# for 23 days. The run's own "nothing to do" or "all good" line is an explicit signal, so
# fixed_value_rules (rules data: pattern, keys, value) records a real 0 for it instead.
FIXED_VALUE_RULES: List[Tuple[re.Pattern, Tuple[str, ...], float]] = []

# M7/M13: counts that only appear in prose. An explicit list, because a generic "<N> <noun>"
# rule mines junk such as "Merging 1 temporary feature classes" or "copying 83 data columns".
# ponytail: one entry per phrase a monitored script uses; a script that prints a
# "SUMMARY key: value" block instead needs no entry here.
# A script that wants a guaranteed metric with no rules at all writes a TASKDASH line instead:
#   TASKDASH rows_updated=1234 skipped=3
# The line may follow a timestamped log preamble, or one of these level-only prefixes (upper-case
# level DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL, as logging.basicConfig prints them):
#   INFO:root:TASKDASH ...   INFO:my.module:TASKDASH ...   WARNING: TASKDASH ...
#   [INFO] TASKDASH ...      INFO - TASKDASH ...            INFO TASKDASH ...
# Any other text before TASKDASH (including a glued "xINFO:root:") makes it prose, not a metric
# line. Each key=value pair becomes a metric under its own
# lower-cased key, bypassing the synonym, noise and meaningful-word filters, and beats a value
# the rules mined for the same key. Keys are letters, digits and underscores starting with a
# letter; values are plain numbers (-12, 3.5, 1,234). A token that is not a pair is ignored.
_LEVEL = r"(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)"
# Python logging's default format has no timestamp, so LOG_PREAMBLE_RE (which requires one) leaves
# the level prefix in place; only this extraction accepts it, so prose rules are unaffected.
TASKDASH_LINE_RE = re.compile(
    rf"^(?:\[{_LEVEL}\]\s*|{_LEVEL}(?::[\w.]+:\s*|:\s*|\s+-\s+|\s+))?TASKDASH\s+(?P<pairs>.+)$"
)
TASKDASH_PAIR_RE = re.compile(
    r"(?<![\w=])(?P<key>[A-Za-z][A-Za-z0-9_]*)=(?P<value>-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\w.=]|,\d)"
)
# The phrases themselves are rules data (prose_count_rules); group 1 of each pattern is the count.
PROSE_COUNT_RULES: List[Tuple[re.Pattern, str]] = []

# M1/M7: a line that opens a per-item block ("Processing: <name>"). When one key takes different
# values in different blocks, each block keeps its own key instead of the last block silently
# overwriting the others. Rules data (section_pattern); its "name" group names the block.
SECTION_RE: re.Pattern
# M15: when one key repeats with different values, a "Total ..." line is the run's figure
# ("Total execution time" beats "Processing time"; "Total deleted" beats each batch).
TOTAL_WORD_RE = re.compile(r"(?i)\btotal\b")

# metrics-05: the object phrase used to allow digits (records in 5.4 seconds -> "records_in_5"),
# so every run with a different duration/count minted a brand-new raw key. Numbers belong in the
# VALUE, never the key -- letters/spaces/separators only here.
VERB_VALUE_RE = re.compile(
    r"(?i)\b(?P<lemma>added|removed|updated|processed|exported|loaded|copied|retired|deleted|inserted|"
    r"appended|downloaded|checked|found|created|committed|reconciled)\b"
    r"(?:\s+(?P<value>\d[\d,]*(?:\.\d+)?))?"
    r"(?:\s+(?P<object>[A-Za-z][A-Za-z _/-]{0,60}))?"
)
# M11: further ", <N> <noun>" clauses of one verb -- "Found 0 new parcels, 1 removed parcels,
# 287,969 common parcels" keys as found_removed_parcels and found_common_parcels.
VERB_CLAUSE_RE = re.compile(r",\s*(?P<value>\d[\d,]*)\s+(?P<object>[A-Za-z][A-Za-z _/-]{0,60})")
# M4: "Processed 40,000 records..." is a progress tick; the run's total comes on a later line.
PROGRESS_TICK_RE = re.compile(r"\.\.\.\s*$")

# The tables below are rules data, filled from rules/default.json and the configured profiles by
# configure_rules(). They are mutated in place, never rebound, so taskdash_dashboard and
# taskdash_publish can alias them at import and still see the configured rules.
NOISE_METRIC_KEY_PATTERNS: List[re.Pattern] = []

# Collapse synonyms onto one canonical key so a metric charts as a single series. First match wins.
CANONICAL_METRIC_RULES: List[Tuple[re.Pattern, str]] = []

# A canonical key is kept only when one of its words starts with one of these tokens.
MEANINGFUL_METRIC_TOKENS: List[str] = []

# X5: display names keep these upper case ("Ap Exported" read as a word, not Address Points).
DISPLAY_ACRONYMS: set = set()

# Substrings of a metric_key that make it lower-is-better (taskdash_dashboard's Metric change).
METRIC_CHANGE_LOWER_IS_BETTER: List[str] = []

# (cause, pattern) groups checked in order for a shared-cause incident, and each cause's
# (what, todo) hint text (taskdash_publish._failure_cause and the incident alert).
CAUSE_PATTERNS: List[Tuple[str, re.Pattern]] = []
CAUSE_TEXT: Dict[str, Tuple[str, str]] = {}


# --------------------------------------------------------------------------- metric parsing

def parse_number(text: str) -> Optional[float]:
    clean = str(text).replace(",", "").strip()
    if not clean:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def normalize_metric_key(label: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return clean[:80] if clean else ""


_MS_UNITS = {"ms", "millisecond", "milliseconds"}
_MIN_UNITS = {"min", "mins", "minute", "minutes"}
_HOUR_UNITS = {"h", "hr", "hrs", "hour", "hours"}
# s/sec/secs/second/seconds fall through unconverted -- already the target unit.

# metrics-12: US-format dates (09/25/2026) split on "/" instead of "-" -- without the "/"
# branch here "Last updated: 09/25/2026" mined a metric of 9.0 (the "09" is a legitimate
# LABEL_VALUE_RE match, and "last_updated" passes the MEANINGFUL token filter on its own).
_DATE_TAIL_RE = re.compile(r"^[-/]\d{1,2}(?:[-/]\d{2,4})?\b")
_CLOCK_TAIL_RE = re.compile(r"^:\d{2}(?::\d{2})?\b")


def _normalize_duration_value(value: float, unit: str) -> float:
    """metrics-06: fold a labelled duration to seconds so ms/min/h don't get stored as-is."""
    unit = unit.lower()
    if unit in _MS_UNITS:
        return value / 1000.0
    if unit in _HOUR_UNITS:
        return value * 3600.0
    if unit in _MIN_UNITS:
        return value * 60.0
    return value


def _is_date_or_clock_tail(line: str, value_end: int) -> bool:
    """True when the digits right after the matched value continue into a date (-09-25) or a
    clock (:32:07) -- metrics-06: 'Start time: 2026-09-25' used to mine a metric of 2026."""
    tail = line[value_end:value_end + 6]
    return bool(_DATE_TAIL_RE.match(tail) or _CLOCK_TAIL_RE.match(tail))


def canonicalize_metric_key(metric_key: str, sample_line: str = "") -> Optional[str]:
    """Map a raw label to a canonical metric key, or None if it is noise."""
    key = normalize_metric_key(metric_key)
    if not key:
        return None
    if key.startswith("info_"):
        key = key[5:]

    for pattern, replacement in CANONICAL_METRIC_RULES:
        if pattern.match(key):
            key = replacement
            break

    for pattern in NOISE_METRIC_KEY_PATTERNS:
        if pattern.match(key):
            return None

    if "traceback" in sample_line.lower() and key in {"failed", "runtimeerror", "cpp"}:
        return None
    # A token must start a word of the key: as a bare substring "row" matched "growth" and
    # "point" would match "checkpoint", so Keep-Alive's "Growth_Area idle=4" became a metric.
    words = key.split("_")
    if not any(word.startswith(token) for word in words for token in MEANINGFUL_METRIC_TOKENS):
        return None
    return key


def _hms_seconds(match: re.Match) -> float:
    return (float(match.group("hours") or 0) * 3600.0
            + float(match.group("minutes") or 0) * 60.0
            + float(match.group("seconds") or 0))


def _line_duration(line: str) -> Tuple[Optional[float], Optional[str]]:
    """(seconds, label it came from) for a labelled H:MM:SS duration ("Duration: 0:00:07"),
    else for a completion line's trailing "in <duration>"; (None, None) when neither."""
    match = HMS_DURATION_RE.search(line)
    if match:
        return _hms_seconds(match), normalize_metric_key(match.group("label"))
    if not COMPLETION_LINE_RE.search(line):
        return None, None
    matches = list(IN_DURATION_RE.finditer(line))
    if not matches:
        return None, None
    last = matches[-1]
    if last.group("number") is None:
        return _hms_seconds(last), None
    return _normalize_duration_value(float(last.group("number")), last.group("unit")), None


def _label_pairs(line: str) -> Iterable[Tuple[str, float]]:
    for match in LABEL_VALUE_RE.finditer(line):
        label = normalize_metric_key(match.group("label"))
        value = parse_number(match.group("value"))
        if not label or value is None:
            continue
        if _is_date_or_clock_tail(line, match.end("value")):
            continue
        if _LIST_TAIL_RE.match(line[match.end("value"):]) or _OPERATOR_BEFORE_RE.search(line[:match.start()]):
            continue
        unit = match.group("unit")
        yield label, (_normalize_duration_value(value, unit) if unit else value)


def _verb_pairs(line: str) -> Iterable[Tuple[str, float]]:
    if PROGRESS_TICK_RE.search(line):
        return
    for match in VERB_VALUE_RE.finditer(line):
        value = parse_number(match.group("value") or "")
        if value is None:
            continue
        lemma = match.group("lemma").lower()
        yield normalize_metric_key(f"{lemma}_{match.group('object') or ''}"), value
        pos = match.end()
        clause = VERB_CLAUSE_RE.match(line, pos)
        while clause:
            yield normalize_metric_key(f"{lemma}_{clause.group('object')}"), parse_number(clause.group("value"))
            clause = VERB_CLAUSE_RE.match(line, clause.end())


def _phrase_pairs(line: str, extra_rules: Sequence[Tuple[re.Pattern, str]] = ()) -> Iterable[Tuple[str, float]]:
    """Counts written as prose rather than as a "label: value" pair. extra_rules are one task's
    own prose_count_rules (config metrics.tasks), checked after the shared ones."""
    for pattern, keys, value in FIXED_VALUE_RULES:
        if pattern.search(line):
            for key in keys:
                yield key, value
    match = VALIDATED_ROWS_RE.search(line)
    if match:
        yield normalize_metric_key(f"{match.group('subject')}_row_count"), parse_number(match.group("value"))
    match = EXTRACTED_FEATURES_RE.search(line)
    if match:
        yield normalize_metric_key(f"{match.group('name')}_feature_count"), parse_number(match.group("value"))
    for pattern, key in (*PROSE_COUNT_RULES, *extra_rules):
        match = pattern.search(line)
        if match:
            yield key, parse_number(match.group(1))


def extract_metrics_from_line(line: str, extra_rules: Sequence[Tuple[re.Pattern, str]] = ()) -> List[Tuple[str, float]]:
    """Return (raw label, value) pairs from one log line; the first pair per label wins."""
    line = LOG_PREAMBLE_RE.sub("", line, count=1)
    results: List[Tuple[str, float]] = []
    seen: set[str] = set()
    duration, duration_label = _line_duration(line)
    if duration is not None:
        # Reserve the source label too, or the label matcher mines "Duration: 0:00:07" again
        # as a second duration of 0 (the "0" before the first colon).
        seen.update({"duration_seconds", duration_label or ""})
        results.append(("duration_seconds", duration))
    for label, value in (*_label_pairs(line), *_verb_pairs(line), *_phrase_pairs(line, extra_rules)):
        if label and value is not None and label not in seen:
            seen.add(label)
            results.append((label, value))
    return results


def extract_taskdash_line(line: str) -> List[Tuple[str, float]]:
    """(key, value) pairs of a "TASKDASH key=value ..." line, [] when the line is not one."""
    match = TASKDASH_LINE_RE.match(LOG_PREAMBLE_RE.sub("", line, count=1))
    if not match:
        return []
    pairs = []
    for pair in TASKDASH_PAIR_RE.finditer(match.group("pairs")):
        key = normalize_metric_key(pair.group("key"))
        if key:
            pairs.append((key, parse_number(pair.group("value"))))
    return pairs


def title_case_metric(metric_key: str) -> str:
    return " ".join(part.capitalize() for part in metric_key.split("_") if part)


# metrics_tab-07/wording-07/wording-08/metrics_tab-04: plain-English overrides for keys that read
# as jargon straight off the sentence-cased key, or that need a unit the generic rule can't add.
# Rules data (display_names); each profile notes the log line behind each of its names.
DISPLAY_NAMES: Dict[str, str] = {}


def metric_series_key(host_name: Any, task_full_name: Any, metric_key: Any) -> str:
    """One metric line of one task on one host, in one field. A Dashboards filter action applies
    only its first fieldMap entry (probed on TEST 2026-09-28: a task_name + metric_key map
    filtered on task_name alone), so a Metric change row click needs a single key to draw one
    line. The Metric change Arcade builds the same text: keep the two in step."""
    return f"{host_name}|{task_full_name}|{metric_key}"


def metric_display_name(metric_key: str) -> str:
    """X5: sentence case that keeps acronyms ("AP exported", "SDE rows before truncate").

    metrics_tab-07/wording-08: DISPLAY_NAMES is checked first for keys the automatic rule leaves
    as jargon or unitless. duration_seconds is one of them -- not every table shows it through
    fmtDur ("39 min 19 s"); Metric range (metric_summary_table) is a plain grouped table and
    showed the raw seconds with no unit at all.
    """
    if metric_key in DISPLAY_NAMES:
        return DISPLAY_NAMES[metric_key]
    words = [w for w in metric_key.split("_") if w]
    unit = ""
    if len(words) > 1 and words[-1] == "seconds":
        words, unit = words[:-1], " (seconds)"
    words = [w.upper() if w in DISPLAY_ACRONYMS else w for w in words]
    if words and not words[0].isupper():
        words[0] = words[0].capitalize()
    return " ".join(words) + unit


# --------------------------------------------------------------------------- rules as data

# The organization knowledge above (phrases, synonyms, noise keys, names, cause groups) is data.
# rules/default.json is generic and always loads; config rules.profiles adds profile files on top,
# in list order, paths relative to the TaskDash folder. A list entry carries an explicit "order":
# a profile interleaves its entries with the generic ones, so the merged order is still the order
# the rules had when they were code (first match wins in several tables). Orders must be unique
# across all loaded files, so a profile can never silently replace a generic rule. Object tables
# merge key by key, later files winning. A key starting with "_" is a note, never a rule.
DEFAULT_RULES_FILE = "rules/default.json"
# Fixed at import: tests move __file__ to fake an install root, and the rules must not move with it.
_RULES_BASE = data_dir()
_ORDERED_RULE_FIELDS = {
    "prose_count_rules": ("pattern", "key"),
    "fixed_value_rules": ("pattern", "keys", "value"),
    "canonical_metric_rules": ("pattern", "key"),
    "noise_metric_key_patterns": ("pattern",),
    "meaningful_metric_tokens": ("token",),
    "metric_change_lower_is_better": ("token",),
    "cause_patterns": ("cause", "pattern"),
}
# Prose and noise patterns always matched case-insensitively; the others carry inline flags.
_IGNORECASE_RULES = {"prose_count_rules", "fixed_value_rules", "noise_metric_key_patterns"}
# Fields that are not strings: the metric keys a fixed_value_rules entry sets, and their number.
_NON_STRING_FIELDS = {"keys": "a non-empty list of strings", "value": "a number"}
_DICT_RULE_SECTIONS = {"log_preamble", "display_names", "cause_text"}
_RULE_SECTIONS = {*_ORDERED_RULE_FIELDS, *_DICT_RULE_SECTIONS, "section_pattern", "display_acronyms"}
_PREAMBLE_KEYS = ("timestamp", "separator", "levels")

# Config metrics.tasks, keyed by task_full_name like metrics.log_directory_overrides:
#   {"enabled": false} stops metric rows for that task (its latest-log summary stays);
#   "prose_count_rules": [{"pattern", "key"}] adds rules after the shared ones -- the key still
#   passes the synonym, noise and meaningful-word filters, so name it with a word such as count;
#   "noise_metric_key_patterns": [regex] drops more canonical keys for that task only.
_TASK_SETTING_KEYS = {"enabled", "prose_count_rules", "noise_metric_key_patterns"}
TASK_SETTINGS: Dict[str, Dict[str, Any]] = {}


def _rules_error(where: str, problem: str, fix: str) -> SystemExit:
    return SystemExit(f"Rules problem in {where}: {problem}. Fix: {fix}, then re-run.")


def _compile_rule(where: str, pattern: Any, flags: int = 0, group: Any = None) -> re.Pattern:
    """Compile one rule pattern; group is a capture group the code reads (1, or "name")."""
    if not isinstance(pattern, str):
        raise _rules_error(where, f"the pattern must be a string, got {pattern!r}", "quote the pattern")
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise _rules_error(where, f"bad regex {pattern!r} ({exc})",
                           "correct the regex; in JSON one backslash is written as two")
    if group is not None and not (compiled.groups >= group if isinstance(group, int) else group in compiled.groupindex):
        raise _rules_error(where, f"the regex {pattern!r} has no capture group {group!r}",
                           f"add the capture group {group!r} that holds the value")
    return compiled


def _read_rules_file(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise _rules_error(str(path), f"the file cannot be read ({exc})",
                           "check the path in config rules.profiles (relative to the TaskDash folder)")
    except json.JSONDecodeError as exc:
        raise _rules_error(str(path), f"not valid JSON (line {exc.lineno}): {exc.msg}", "correct the JSON")
    if not isinstance(data, dict):
        raise _rules_error(str(path), "the file must hold a JSON object", "wrap the sections in { }")
    return data


def _merge_ordered(where: str, section: str, value: Any, target: Dict[int, Tuple[str, Dict[str, Any]]]) -> None:
    fields = _ORDERED_RULE_FIELDS[section]
    if not isinstance(value, list):
        raise _rules_error(where, "the section must be a list of objects", "use [ {...}, {...} ]")
    for index, entry in enumerate(value):
        at = f"{where} entry {index}"
        if not isinstance(entry, dict):
            raise _rules_error(at, f"the entry must be an object, got {entry!r}", f"give it the fields order, {', '.join(fields)}")
        order = entry.get("order")
        if not isinstance(order, int) or isinstance(order, bool):
            raise _rules_error(at, f"order must be a whole number, got {order!r}", 'give the entry a number "order"')
        at = f"{where} entry {index} (order {order})"
        unknown = sorted(k for k in entry if k != "order" and k not in fields and not k.startswith("_"))
        wrong = [f for f in fields if f not in _NON_STRING_FIELDS and not isinstance(entry.get(f), str)]
        keys, number = entry.get("keys"), entry.get("value")
        if "keys" in fields and not (isinstance(keys, list) and keys and all(isinstance(k, str) for k in keys)):
            wrong.append("keys")
        if "value" in fields and (isinstance(number, bool) or not isinstance(number, (int, float))):
            wrong.append("value")
        if unknown or wrong:
            raise _rules_error(at, f"unknown field(s) {unknown} or missing/non-string field(s) {wrong}",
                               f"use exactly the fields {', '.join(fields)} (strings, except "
                               f"{'; '.join(f'{n} is {d}' for n, d in _NON_STRING_FIELDS.items() if n in fields) or 'none'})")
        if order in target:
            raise _rules_error(at, f"order {order} is already used by {target[order][0]}",
                               "give each entry an order number used nowhere else in the loaded rules files")
        target[order] = (at, entry)


def _check_rule_dict(where: str, section: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise _rules_error(where, "the section must be an object", "use { \"name\": value }")
    for key, item in value.items():
        at = f"{where} key {key!r}"
        if section == "display_names":
            ok, shape = isinstance(item, str), "a string"
        elif section == "cause_text":
            ok, shape = isinstance(item, list) and len(item) == 2 and all(isinstance(s, str) for s in item), \
                "a list of two strings [what, what to check first]"
        else:
            ok = key in _PREAMBLE_KEYS and (isinstance(item, str) if key != "levels" else
                                             isinstance(item, list) and all(isinstance(s, str) for s in item))
            shape = "one of timestamp/separator (strings) or levels (a list of strings)"
        if not ok:
            raise _rules_error(at, f"the value {item!r} is not {shape}", f"make it {shape}")


def _merge_rule_files(paths: Sequence[Path]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {name: {} for name in (*_ORDERED_RULE_FIELDS, *_DICT_RULE_SECTIONS)}
    merged["display_acronyms"], merged["section_pattern"] = set(), None
    for path in paths:
        for section, value in _read_rules_file(path).items():
            if section.startswith("_"):
                continue
            where = f"{path} section {section!r}"
            if section not in _RULE_SECTIONS:
                raise _rules_error(where, "unknown section", f"use one of {', '.join(sorted(_RULE_SECTIONS))}, "
                                   "or start a note's name with _")
            if section in _ORDERED_RULE_FIELDS:
                _merge_ordered(where, section, value, merged[section])
            elif section in _DICT_RULE_SECTIONS:
                _check_rule_dict(where, section, value)
                merged[section].update(value)
            elif section == "display_acronyms":
                if not (isinstance(value, list) and all(isinstance(s, str) for s in value)):
                    raise _rules_error(where, f"expected a list of strings, got {value!r}", 'use ["db", "id"]')
                merged[section].update(value)
            else:
                merged[section] = (where, value)
    return merged


@functools.lru_cache(maxsize=8)
def load_rules(profiles: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """rules/default.json plus each profile, validated and compiled. Raises SystemExit naming the
    file, the entry and the fix on the first problem."""
    # A profile is the organization's own file, so it lives with the install, not with the shipped data.
    paths = [_RULES_BASE / DEFAULT_RULES_FILE, *(home_dir() / profile for profile in profiles)]
    merged = _merge_rule_files(paths)
    files = ", ".join(str(p) for p in paths)
    preamble = merged["log_preamble"]
    if any(key not in preamble for key in _PREAMBLE_KEYS) or merged["section_pattern"] is None:
        raise _rules_error(files, "log_preamble (timestamp, separator, levels) or section_pattern is missing",
                           f"restore them in {DEFAULT_RULES_FILE}")
    head, levels = preamble["timestamp"] + preamble["separator"], "|".join(preamble["levels"])
    rules: Dict[str, Any] = {
        "log_preamble_re": _compile_rule(f"{files} log_preamble", head + rf"(?:(?:{levels})\b)?\s*(?:[-|:\]]+\s*)?"),
        "log_level_re": _compile_rule(f"{files} log_preamble", head + rf"(?P<level>{levels})\b", re.IGNORECASE),
        "section_re": _compile_rule(*merged["section_pattern"], 0, "name"),
        "display_acronyms": set(merged["display_acronyms"]),
        "display_names": dict(merged["display_names"]),
        "cause_text": {cause: tuple(text) for cause, text in merged["cause_text"].items()},
    }
    for section, fields in _ORDERED_RULE_FIELDS.items():
        flags = re.IGNORECASE if section in _IGNORECASE_RULES else 0
        group = 1 if section == "prose_count_rules" else None
        compiled = []
        for _, (at, entry) in sorted(merged[section].items()):
            values = [_compile_rule(at, entry[f], flags, group) if f == "pattern"
                      else tuple(entry[f]) if f == "keys" else float(entry[f]) if f == "value" else entry[f]
                      for f in fields]
            compiled.append(tuple(values) if len(values) > 1 else values[0])
        rules[section] = compiled
    missing = sorted(({cause for cause, _ in rules["cause_patterns"]} | {""}) - set(rules["cause_text"]))
    if missing:
        raise _rules_error(f"{files} section 'cause_text'", f"no hint text for cause(s) {missing}",
                           "add [what, what to check first] for each cause, and for \"\" (no single cause)")
    return rules


def _compile_task_settings(tasks: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    settings: Dict[str, Dict[str, Any]] = {}
    for task_name, raw in tasks.items():
        where = f"config metrics.tasks[{task_name!r}]"
        if not isinstance(raw, dict) or set(raw) - _TASK_SETTING_KEYS:
            raise _rules_error(where, f"expected an object with only {sorted(_TASK_SETTING_KEYS)}, got {raw!r}",
                               "remove or rename the unknown setting")
        enabled = raw.get("enabled", True)
        extra_rules, extra_noise = raw.get("prose_count_rules", []), raw.get("noise_metric_key_patterns", [])
        if not isinstance(enabled, bool) or not isinstance(extra_rules, list) or not isinstance(extra_noise, list):
            raise _rules_error(where, "enabled must be true/false and the rule settings lists",
                               'use {"enabled": false, "prose_count_rules": [], "noise_metric_key_patterns": []}')
        rules = []
        for index, entry in enumerate(extra_rules):
            at = f"{where} prose_count_rules entry {index}"
            if not isinstance(entry, dict) or set(entry) != {"pattern", "key"} or not isinstance(entry["key"], str):
                raise _rules_error(at, f"expected {{\"pattern\": regex, \"key\": name}}, got {entry!r}",
                                   "give the entry exactly the string fields pattern and key")
            rules.append((_compile_rule(at, entry["pattern"], re.IGNORECASE, 1), entry["key"]))
        noise = [_compile_rule(f"{where} noise_metric_key_patterns entry {index}", pattern, re.IGNORECASE)
                 for index, pattern in enumerate(extra_noise)]
        settings[task_name] = {"enabled": enabled, "prose_count_rules": rules, "noise_metric_key_patterns": noise}
    return settings


def configure_rules(config: Dict[str, Any]) -> None:
    """Load the rules files that config rules.profiles selects, and config metrics.tasks, into
    this module's tables. load_config calls this, so a bad rule stops the run before any work."""
    rules = load_rules(tuple((config.get("rules") or {}).get("profiles") or ()))
    task_settings = _compile_task_settings((config.get("metrics") or {}).get("tasks") or {})
    global LOG_PREAMBLE_RE, LOG_LEVEL_RE, SECTION_RE
    LOG_PREAMBLE_RE, LOG_LEVEL_RE, SECTION_RE = rules["log_preamble_re"], rules["log_level_re"], rules["section_re"]
    for table in (PROSE_COUNT_RULES, FIXED_VALUE_RULES, CANONICAL_METRIC_RULES, NOISE_METRIC_KEY_PATTERNS,
                  MEANINGFUL_METRIC_TOKENS, METRIC_CHANGE_LOWER_IS_BETTER, CAUSE_PATTERNS):
        table.clear()
    PROSE_COUNT_RULES.extend(rules["prose_count_rules"])
    FIXED_VALUE_RULES.extend(rules["fixed_value_rules"])
    CANONICAL_METRIC_RULES.extend(rules["canonical_metric_rules"])
    NOISE_METRIC_KEY_PATTERNS.extend(rules["noise_metric_key_patterns"])
    MEANINGFUL_METRIC_TOKENS.extend(rules["meaningful_metric_tokens"])
    METRIC_CHANGE_LOWER_IS_BETTER.extend(rules["metric_change_lower_is_better"])
    CAUSE_PATTERNS.extend(rules["cause_patterns"])
    for table, source in ((DISPLAY_ACRONYMS, "display_acronyms"), (DISPLAY_NAMES, "display_names"),
                          (CAUSE_TEXT, "cause_text")):
        table.clear()
        table.update(rules[source])
    TASK_SETTINGS.clear()
    TASK_SETTINGS.update(task_settings)


# Import gives the generic rules only; load_config then applies the configured profiles.
configure_rules({})


# --------------------------------------------------------------------------- log discovery

def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.resolve().relative_to(parent)
        return True
    except (ValueError, OSError):
        return False


def _dir_key(path: Path) -> str:
    """Case-insensitive resolved-path identity, tolerant of a path that doesn't resolve."""
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def discover_log_directories(target_path: Any, working_directory: Any = None) -> List[Path]:
    """A workflow's logs live beside its script, in a Logs/ subdirectory of either the script's
    own folder, or -- when the scheduled task's action carries its own working directory --
    beside that folder too. Task Scheduler lets an action's WorkingDirectory differ from where
    the script file itself lives, and without seeding from both roots those logs are invisible.
    """
    roots: List[Path] = []
    script_root: Optional[Path] = None
    clean = normalize_text(target_path)
    if clean:
        # Keep script_root even when the directory is absent on this machine: the ancestor test
        # below must be lexical, or it silently stops guarding whenever the script path is not
        # local (off-host runs, a relocated script) -- exactly when bad data is most likely.
        script_root = Path(clean).parent
        if script_root.exists():
            roots.append(script_root)

    wd_clean = normalize_text(working_directory)
    if wd_clean:
        wd_root = Path(wd_clean)
        # Reject a working directory that is an ANCESTOR of the script directory. Measured on
        # Task server: 7 of 25 tasks declare the shared repo root as their working
        # directory, so accepting it would mine the same root-level log into 7 unrelated tasks
        # and give them all the same latest_log_name. An ancestor also never finds anything the
        # script's own folder would not.
        if wd_root.is_dir() and not (script_root and _is_within(script_root, wd_root)):
            roots.append(wd_root)

    if not roots:
        return []

    # Windows paths are case-insensitive, so Logs/logs/Log/log can all resolve to the same
    # directory. Dedupe on the resolved path or every log file gets scanned once per alias.
    candidates: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        for candidate in (root, root / "Logs", root / "logs", root / "Log", root / "log"):
            if not candidate.is_dir():
                continue
            key = str(candidate.resolve()).lower()
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
    return candidates


def list_recent_log_files(
    directories: Iterable[Path],
    days_back: int,
    limit: int,
    *,
    file_filter: Optional[Callable[[Path], bool]] = None,
    trusted_dirs: Optional[set] = None,
) -> List[Path]:
    cutoff = (utc_now() - timedelta(days=days_back)).timestamp()
    trusted = trusted_dirs or set()
    found: List[Tuple[float, Path]] = []
    for directory in directories:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        # metrics-08: .log is a log anywhere; .txt only counts inside a directory actually
        # named Logs/logs/Log/log -- a stray .txt beside the script itself is not a log.
        # metrics-09: an operator-supplied override directory is trusted the same way, even
        # when its name doesn't match -- it was pointed at deliberately, not discovered by
        # walking a shared folder.
        allowed_suffixes = (
            LOG_SUFFIXES
            if directory.name.lower() in LOG_DIR_NAMES or _dir_key(directory) in trusted
            else (".log",)
        )
        for path in entries:
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            if file_filter is not None and not file_filter(path):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                found.append((mtime, path))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in found[:limit]]


def _sniff_encoding(sample: bytes) -> Tuple[str, int]:
    """Detect a log's text encoding from its opening bytes; return (encoding, bytes-to-skip).

    metrics-04: PowerShell 5.1's Out-File/redirection defaults to UTF-16LE (BOM FF FE, or no
    BOM at all), and some tools write UTF-8-with-BOM. Both silently decode as plain UTF-8 --
    UTF-16 comes out as replacement-char junk full of NULs (zero metrics), UTF-8-BOM leaves a
    stray U+FEFF glued to the first label (also zero metrics, the label regex never matches).
    """
    if sample[:2] == b"\xff\xfe":
        return "utf-16-le", 2
    if sample[:2] == b"\xfe\xff":
        return "utf-16-be", 2
    if sample[:3] == b"\xef\xbb\xbf":
        return "utf-8", 3
    # BOM-less UTF-16LE: ASCII text encoded as 2-byte units is roughly half NUL bytes.
    if sample and sample.count(b"\x00") * 3 > len(sample):
        return "utf-16-le", 0
    return "utf-8", 0


def read_last_lines(path: Path, max_lines: int, chunk_size: int = 65536) -> List[str]:
    """Read up to the last max_lines lines of a log without loading a multi-GB file whole.

    metrics-02: max_lines used to cap a HEAD read, so a long-running or appended-to log showed
    stale metrics and a traceback near the end was never seen. Seeks backward from EOF in
    chunks instead. max_lines <= 0 means "no limit" and reads the whole (small) file, matching
    the previous unlimited mode.
    """
    with path.open("rb") as handle:
        sample = handle.read(64)
        encoding, bom_len = _sniff_encoding(sample)

        if max_lines <= 0:
            handle.seek(bom_len)
            return handle.read().decode(encoding, errors="replace").splitlines()

        # utf-16 line separators are 2 bytes, so every chunk boundary must land on a 2-byte
        # code-unit boundary or decoding the assembled buf shifts every character after it.
        # metrics-13: neither chunk_size (caller-settable) nor the file size (a truncated or
        # partially-written UTF-16 log can end on an odd byte) is guaranteed even, so alignment
        # is enforced here rather than assumed. is_utf16 forces every step to a 2-byte multiple,
        # and dropping one stray trailing byte up front keeps pos-bom_len even throughout.
        is_utf16 = encoding.startswith("utf-16")
        newline = {"utf-16-le": b"\n\x00", "utf-16-be": b"\x00\n"}.get(encoding, b"\n")
        pos = handle.seek(0, 2)
        if is_utf16:
            pos -= (pos - bom_len) % 2
        buf = b""
        while pos > bom_len:
            step = min(chunk_size, pos - bom_len)
            if is_utf16:
                step -= step % 2
                step = step or 2  # pos-bom_len is even and >0 here, so 2 bytes are available
            pos -= step
            handle.seek(pos)
            buf = handle.read(step) + buf
            if buf.count(newline) > max_lines:
                break

        lines = buf.decode(encoding, errors="replace").splitlines()
        return lines[-max_lines:] if len(lines) > max_lines else lines


def classify_line(line: str) -> Tuple[bool, bool]:
    """(is_error, is_warning) for one log line. M8: one rule for both -- the level decides
    first (the preamble level, or a level word leading the message), then a keyword fallback
    (a traceback or a nonzero "failed" for errors, "warning:" for warnings).

    metrics-11: a real ERROR/CRITICAL level is never hidden by a zero-count phrase elsewhere on
    the line ("ERROR | Append failed: 0 features written"). Only the zero-count phrases are
    scrubbed before the keyword fallback, so "12 succeeded, 3 failed, 0 errors" still counts.
    """
    level_match = LOG_LEVEL_RE.match(line)
    scrubbed = ZERO_COUNT_RE.sub("", LOG_PREAMBLE_RE.sub("", line, count=1).lower())
    head_match = MESSAGE_LEVEL_RE.match(scrubbed)
    levels = {m.group("level").upper() for m in (level_match, head_match) if m}
    is_error = bool(levels & ERROR_LEVELS) or "traceback" in scrubbed or bool(FAILED_WORD_RE.search(scrubbed))
    is_warning = bool(levels & WARNING_LEVELS) or bool(WARNING_KEYWORD_RE.search(scrubbed))
    return is_error, is_warning


def _section_key(line: str) -> Optional[str]:
    match = SECTION_RE.match(LOG_PREAMBLE_RE.sub("", line, count=1))
    if not match:
        return None
    # "Acme_StormBasins" -> acme_storm_basins, so the display name reads "Acme storm basins".
    return normalize_metric_key(re.sub(r"(?<=[a-z])(?=[A-Z])", "_", match.group("name"))) or None


def _pick_occurrence(hits: Sequence[Tuple[Optional[str], int, float, str]]) -> Tuple[float, str]:
    """The last hit among those with the highest priority (a "Total ..." line)."""
    _, best = max(enumerate(hits), key=lambda pair: (pair[1][1], pair[0]))
    return best[2], best[3]


def _resolve_metrics(
    occurrences: Dict[str, List[Tuple[Optional[str], int, float, str]]],
) -> Dict[str, Tuple[float, str]]:
    """One value per key. M1/M7: a key logged in more than one section gets one key per section
    ("acme_storm_basins_hosted_record_count"), not the last section's value only. The log's
    structure decides, not the values: a night when every layer reports the same count must not
    fold three per-layer series into a new bare one."""
    metrics: Dict[str, Tuple[float, str]] = {}
    for key, hits in occurrences.items():
        by_section: Dict[Optional[str], List[Tuple[Optional[str], int, float, str]]] = {}
        for hit in hits:
            by_section.setdefault(hit[0], []).append(hit)
        if len(by_section) == 1:
            metrics[key] = _pick_occurrence(hits)
            continue
        for section, section_hits in by_section.items():
            metrics[f"{section}_{key}" if section else key] = _pick_occurrence(section_hits)
    return metrics


def scan_log_file(path: Path, max_lines: int, task_settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return one value per metric in this file, plus line-level counters.

    metrics-10: any OSError while reading (open, seek, or decode of a truncated/locked file)
    is isolated to this file -- it must not abort the whole hourly publish. read_error=True
    tells the caller not to report a false "0 errors" for a file it never actually read.
    task_settings is the task's TASK_SETTINGS entry: its extra rules and extra noise keys.
    """
    # Sample lines are scrubbed before their 240-character cut: a cut first can drop a flag or a
    # URL scheme and leave its secret where prepare_rows' scrubber no longer recognizes it.
    # Imported here: taskdash_publish imports this module at load time.
    from taskdash_publish import _scrub_secrets

    extra_rules = (task_settings or {}).get("prose_count_rules", ())
    extra_noise = (task_settings or {}).get("noise_metric_key_patterns", ())
    counts = {"lines_scanned": 0, "error_lines": 0, "warning_lines": 0, "success_lines": 0}
    try:
        lines = read_last_lines(path, max_lines)
    except OSError as exc:
        emit(f"WARNING: could not read {path}: {exc}")
        return {"metrics": {}, **counts, "read_error": True}

    occurrences: Dict[str, List[Tuple[Optional[str], int, float, str]]] = {}
    section: Optional[str] = None
    explicit: Dict[str, Tuple[float, str]] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        counts["lines_scanned"] += 1
        taskdash_pairs = extract_taskdash_line(line)
        # A TASKDASH line is author-written data ('failed=0' must not count as an error line).
        is_error, is_warning = (False, False) if taskdash_pairs else classify_line(line)
        counts["error_lines"] += is_error
        counts["warning_lines"] += is_warning
        lower = line.lower()
        if "completed successfully" in lower or "success" in lower:
            counts["success_lines"] += 1

        if taskdash_pairs:
            # Last line wins, and no section prefix: the author named the key, so it stays as written.
            explicit.update({key: (value, _scrub_secrets(line)[:240]) for key, value in taskdash_pairs})
            continue

        section = _section_key(line) or section
        priority = 1 if TOTAL_WORD_RE.search(line) else 0
        for raw_key, value in extract_metrics_from_line(line, extra_rules):
            canonical = canonicalize_metric_key(raw_key, line)
            if canonical and not any(pattern.match(canonical) for pattern in extra_noise):
                occurrences.setdefault(canonical, []).append(
                    (section, priority, value, _scrub_secrets(line)[:240]))

    return {"metrics": {**_resolve_metrics(occurrences), **explicit}, **counts, "read_error": False}


# --------------------------------------------------------------------------- rows

_NAME_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _name_tokens(text: str, min_len: int = 3) -> List[str]:
    return [t.lower() for t in _NAME_TOKEN_RE.findall(text or "") if len(t) >= min_len]


_OWN_SCRIPT_NAME_RE = re.compile(r"^taskdash_\w+\.py$", re.IGNORECASE)


def is_taskdash_command(command_target_name: Any) -> bool:
    """metrics_tab-03/wording-03/interactions-taskdash-own-logs-mined: the Task Dash inventory
    row must never mine its own logs, whatever folder it or its Logs directory sit in -- that's
    what left junk keys like "Added updated" and doubled 08:00 values in the metric table. This
    check is on the command target itself, so it holds regardless of install location or log_dir.
    """
    name = normalize_text(command_target_name)
    if not name:
        return False
    return name.lower() == "run_taskdash.cmd" or bool(_OWN_SCRIPT_NAME_RE.match(name))


def _log_belongs_to_task(filename: str, script_stem: str, name_tokens: Sequence[str]) -> bool:
    """metrics-03: does this log file's name plausibly belong to this task?

    Used only when a directory is shared by more than one task (see build_metric_rows) --
    a folder with a single task keeps today's unfiltered behaviour.
    # ponytail: substring/token match, not a scoring model; misattributes if two tasks in one
    # shared folder have overlapping name tokens (e.g. both contain "Nightly"). Tighten to a
    # stricter match (e.g. require the full task name) if that turns out to happen on the task server.
    """
    lower = filename.lower()
    if script_stem and script_stem in lower:
        return True
    return any(token in lower for token in name_tokens)


def _plan_task_directories(
    inventory_rows: Sequence[Dict[str, Any]],
    overrides: Dict[str, str],
    own_log_keys: set,
) -> Tuple[List[Dict[str, Any]], Dict[str, set]]:
    """First pass: resolve each task's candidate directories (no file I/O yet) and count how
    many distinct tasks reference each directory. metrics-03: several tasks sharing one
    folder used to mine every log in it for every task, misattributing metrics and
    latest_log. A folder referenced by exactly one task keeps today's unfiltered behaviour.
    """
    task_plans: List[Dict[str, Any]] = []
    dir_task_owners: Dict[str, set] = {}

    for task in inventory_rows:
        task_full_name = task.get("task_full_name")
        override_dir = normalize_text(overrides.get(task_full_name)) if task_full_name else ""
        if is_taskdash_command(task.get("command_target_name")):
            # Never mine Task Dash's own logs -- independent of install folder, override or
            # log_dir. Keep the own_dir/own_logs_dir filter below too, as a second layer.
            directories = []
        elif override_dir:
            override_path = Path(override_dir)
            directories = [override_path] if override_path.is_dir() else []
        else:
            directories = discover_log_directories(
                task.get("command_target_path"), task.get("command_target_working_directory")
            )
        directories = [d for d in directories if _dir_key(d) not in own_log_keys]

        script_path = normalize_text(task.get("command_target_path"))
        task_plans.append({
            "task": task,
            "directories": directories,
            "script_stem": Path(script_path).stem.lower() if script_path else "",
            "name_tokens": _name_tokens(task.get("task_name") or ""),
            # metrics-09: an override directory was pointed at deliberately by an operator,
            # not discovered by walking a shared folder -- trust its .txt files too.
            "is_override": bool(override_dir),
        })
        for directory in directories:
            dir_task_owners.setdefault(_dir_key(directory), set()).add(task_full_name)

    return task_plans, dir_task_owners


_LATEST_LOG_FIELDS = (
    "latest_log_name", "latest_log_path", "latest_log_time_utc", "latest_log_age_hours",
    "latest_log_error_lines", "latest_log_warning_lines", "latest_log_lines_scanned",
)


def _file_created_dt(path: Path) -> Optional[datetime]:
    """Best-effort file creation time for the manual-run filter in _scan_task_logs.

    st_birthtime is the true creation time (Windows via Python 3.12+, and macOS/BSD always).
    Where the platform doesn't expose it, st_ctime is the fallback -- on Windows (unlike POSIX,
    where st_ctime is the last metadata change) st_ctime IS creation time too, so it still tells
    a scheduled run's log from a later manual one.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    ts = getattr(stat, "st_birthtime", None)
    if ts is None:
        ts = stat.st_ctime
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _summarize_task_logs(
    newest: Path, newest_dt: Any, newest_scan: Optional[Dict[str, Any]],
    files_scanned: int, now: Any,
) -> Dict[str, Any]:
    """Build the latest_log_* summary dict for one task from its newest file's scan result."""
    # newest_scan is only None when the newest file vanished mid-loop (stat() failed) -- that
    # is unread, not a clean run, so the fallback must carry read_error True too.
    scan = newest_scan or {"lines_scanned": 0, "error_lines": 0, "warning_lines": 0,
                            "success_lines": 0, "read_error": True}
    # metrics-10: a newest log that failed to read is UNKNOWN, not a clean 0 -- reporting 0
    # would hide a real problem (permissions, a locked/rotating file) behind a healthy-looking row.
    read_failed = scan.get("read_error", False)
    return {
        "latest_log_name": newest.name[:255],
        "latest_log_path": str(newest)[:1000],
        "latest_log_time_utc": newest_dt.isoformat(),
        "latest_log_age_hours": round((now - newest_dt).total_seconds() / 3600.0, 2),
        "latest_log_error_lines": None if read_failed else scan["error_lines"],
        "latest_log_warning_lines": None if read_failed else scan["warning_lines"],
        "latest_log_lines_scanned": None if read_failed else scan["lines_scanned"],
        "log_files_scanned": files_scanned,
    }


# repeat-operations-csc-email-failures-invisible: the leading timestamp of a log line, the same
# shape LOG_PREAMBLE_RE strips (comma or dot fraction, optional [ ] wrapper, space or T
# separator), kept as its own regex because callers here need the matched text, not a stripped
# line.
_LEADING_TIMESTAMP_RE = re.compile(r"^\s*\[?(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]?")


def _count_recent_debug_lines(
    path: Path, max_lines: int, now: Any, window_days: int = 7,
) -> Tuple[Optional[int], Optional[int]]:
    """(error, warning) line counts for a cumulative debug file, restricted to lines whose OWN
    timestamp falls in the last window_days.

    repeat-operations-csc-email-failures-invisible: the file's lifetime counts (hundreds of
    errors in a debug log whose task had 0 failed runs in 7 days) are meaningless
    as a per-run figure, but each line already carries its own timestamp ("2026-09-22 07:00:51
    -- ERROR: ..."), so a rolling window is countable without one-run-per-file. (None, None)
    only when not one line in the file carries a parsable leading timestamp -- that is unknown,
    not zero recent errors.
    """
    try:
        lines = read_last_lines(path, max_lines)
    except OSError:
        return None, None
    cutoff = now - timedelta(days=window_days)
    error_count = 0
    warning_count = 0
    any_parsed = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        match = _LEADING_TIMESTAMP_RE.match(line)
        if not match:
            continue
        try:
            ts = datetime.fromisoformat(match.group("ts").replace(",", "."))
        except ValueError:
            continue
        # A naive stamp is the server's local time: astimezone() reads it that way.
        ts = ts.astimezone(timezone.utc)
        any_parsed = True
        if ts < cutoff:
            continue
        is_error, is_warning = classify_line(line)
        error_count += is_error
        warning_count += is_warning
    if not any_parsed:
        return None, None
    return error_count, warning_count


_OWN_LOG_FILE_RE = re.compile(r"^taskdash_.*\.log$", re.IGNORECASE)


def _scan_own_task_logs(
    own_logs_dir: Path, days_back: int, max_lines_per_file: int, now: Any,
) -> Dict[str, Any]:
    """task_detail-taskdash-no-log-found-false: the Task Dash inventory row (see
    is_taskdash_command above) is never mined for metrics, but its Task Detail card still needs
    a real "Latest log" -- its own newest per-run log (taskdash_YYYYMMDD_HHMMSS.log), not
    bootstrap.log and not a monitored task's log."""
    # The pass that runs this is still writing its own log, so that file is skipped: the card
    # judges Task Dash by its previous run (apply_own_run_view), and so does its log line.
    writing = {Path(h.baseFilename).resolve() for h in logging.getLogger().handlers
               if getattr(h, "baseFilename", None)}
    files = list_recent_log_files(
        [own_logs_dir], days_back, 2,
        file_filter=lambda p: bool(_OWN_LOG_FILE_RE.match(p.name)) and p.resolve() not in writing,
    )
    if not files:
        return {**dict.fromkeys(_LATEST_LOG_FIELDS), "log_files_scanned": 0}
    newest = files[0]
    try:
        newest_dt = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return {**dict.fromkeys(_LATEST_LOG_FIELDS), "log_files_scanned": 0}
    return _summarize_task_logs(newest, newest_dt, scan_log_file(newest, max_lines_per_file), 1, now)


def _scan_task_logs(
    plan: Dict[str, Any],
    dir_task_owners: Dict[str, set],
    *,
    host_name: str,
    days_back: int,
    max_files_per_task: int,
    max_lines_per_file: int,
    now: Any,
    own_logs_dir: Path,
    examined: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Scan one task's candidate directories. Returns (its metric rows, its log summary entry
    -- None when the task had no files at all to record a summary for).

    examined, when given, receives (task_full_name, log file name) for every log this pass read
    or deliberately ignored, so --refresh-metrics can drop stored rows those logs no longer give.
    """
    task = plan["task"]
    task_full_name = task.get("task_full_name")
    if is_taskdash_command(task.get("command_target_name")):
        # task_detail-taskdash-no-log-found-false: never mined (no metric rows), but the Task
        # Detail card still needs a real "Latest log" -- its own newest per-run log
        # (taskdash_YYYYMMDD_HHMMSS.log), not bootstrap.log and not a monitored task's log.
        return [], _scan_own_task_logs(own_logs_dir, days_back, max_lines_per_file, now)
    settings = TASK_SETTINGS.get(task_full_name, {})
    if not settings.get("enabled", True):
        # A disabled task yields no fresh rows, so --refresh-metrics would read "examined but no
        # row" as "gone" and delete its stored history. Leave its files out of examined.
        examined = None
    directories = plan["directories"]
    script_stem = plan["script_stem"]
    name_tokens = plan["name_tokens"]

    # Shared-ness is a directory-level fact -- resolve it once per task's (few) directories
    # instead of once per candidate file, which used to re-resolve path.parent repeatedly.
    shared_dirs = {d for d in directories if len(dir_task_owners.get(_dir_key(d), ())) > 1}
    trusted_dirs = {_dir_key(d) for d in directories} if plan["is_override"] else set()

    def belongs_to_this_task(path: Path, _stem=script_stem, _tokens=name_tokens, _shared=shared_dirs) -> bool:
        if path.parent not in _shared:
            return True
        return _log_belongs_to_task(path.name, _stem, _tokens)

    files = list_recent_log_files(
        directories, days_back, max_files_per_task,
        file_filter=belongs_to_this_task, trusted_dirs=trusted_dirs,
    )
    if directories and not files:
        # Directories resolved but nothing recent was in them -- a real signal (wrong folder,
        # log rotation stopped, bad override), unlike a task whose script simply doesn't
        # resolve on this host. Record an explicit 0 rather than leaving the field NULL, or a
        # "no recent log" filter on = 0 would silently match nothing.
        checked = ", ".join(str(d) for d in directories)
        reason = (
            " (directory shared by other tasks -- files may have been excluded by name"
            " attribution)" if shared_dirs else ""
        )
        emit(f"WARNING: no recent log files for task {task_full_name!r}; checked: {checked}{reason}")
        return [], {"log_files_scanned": 0}
    if not files:
        return [], None
    if "last_run_time" in task and not task["last_run_time"]:
        # M14: the task has never run, so every log here came from a manual run (Keep-Alive's
        # only log is an apply=False dry run). Clear the latest-log fields explicitly: a missing
        # key would leave the previous pass's values in the hosted table.
        emit(f"Task {task_full_name!r} has not run yet; ignored {len(files)} log file(s) from manual runs.")
        if examined is not None:
            examined.update((task_full_name, f.name[:255]) for f in files)
        return [], {**dict.fromkeys(_LATEST_LOG_FIELDS), "log_files_scanned": 0}

    # task_detail-01/metrics_tab-10: a scheduled run creates its log when it starts, so a file
    # created well after last_run_time is a manual/other run (Keep-Alive's dry-run probe left a
    # log that outranked the real scheduled run by mtime). Only applies once the task HAS run --
    # the M14 guard above already handles "never run". Falls back to every file found when
    # nothing qualifies, so a task with only manual logs (or an unreadable creation time) keeps
    # today's behaviour instead of going silent.
    # Collected rows carry ISO text; --refresh-metrics reads the stored row, which is epoch millis.
    raw_last_run = task.get("last_run_time")
    last_run_dt = (datetime.fromtimestamp(raw_last_run / 1000, tz=timezone.utc)
                   if isinstance(raw_last_run, (int, float)) else parse_iso_datetime(raw_last_run))
    if last_run_dt is not None:
        # A script can open a later log after a long first step, so the run's own length counts.
        slack = max(MANUAL_RUN_SLACK_MINUTES * 60.0, float(task.get("last_duration_seconds") or 0))
        cutoff = last_run_dt + timedelta(seconds=slack)
        qualifying = [f for f in files if (created := _file_created_dt(f)) is not None and created <= cutoff]
        if qualifying:
            skipped = len(files) - len(qualifying)
            if skipped and examined is not None:
                examined.update((task_full_name, f.name[:255]) for f in files if f not in qualifying)
            if skipped:
                emit(
                    f"Task {task_full_name!r}: ignored {skipped} log file(s) created after the "
                    "last scheduled run (manual/other run)."
                )
            files = qualifying

    # M3: a cumulative debug file (one that collects every error for months) is not one
    # run's log. Prefer any other log as the latest one.
    # ponytail: the file name is the only signal here; a script-side per-run log is the real fix.
    if examined is not None:
        # Every file read below counts as examined; the manual-run files were added above.
        examined.update((task_full_name, f.name[:255]) for f in files)
    newest = next((f for f in files if "debug" not in f.name.lower()), files[0])
    try:
        # A log rotated away between listing and stat() must not kill the whole pass.
        newest_dt = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    except OSError:
        emit(f"WARNING: {newest} vanished during scanning; skipping {task['task_name']}.")
        return [], None

    metric_rows: List[Dict[str, Any]] = []
    newest_scan = None
    for path in files:
        try:
            observed_dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            # metrics-10: a file that vanished here (rotated away between listing and stat())
            # must not silently drop out -- log it, same as the newest-file case above.
            emit(f"WARNING: {path} vanished during scanning; skipping this file.")
            continue
        scan = scan_log_file(path, max_lines_per_file, settings)
        if path == newest:
            newest_scan = scan
        if not settings.get("enabled", True):
            # metrics.tasks enabled=false: no metric rows, but the latest-log summary still counts.
            continue

        for metric_key, (value, sample_line) in scan["metrics"].items():
            metric_rows.append({
                "host_name": host_name,
                "task_name": task["task_name"],
                "task_full_name": task_full_name,
                "metric_key": metric_key,
                "display_name": metric_display_name(metric_key),
                "metric_value": value,
                "observed_at_utc": observed_dt.isoformat(),
                "log_file_name": path.name[:255],
                "series_key": metric_series_key(host_name, task_full_name, metric_key),
                "sample_line": sample_line[:500],
            })

    summary = _summarize_task_logs(newest, newest_dt, newest_scan, len(files), now)
    if "debug" in newest.name.lower():
        # Only the cumulative debug file was found: its whole-file counts are a lifetime total
        # (hundreds for a task with 0 failed runs in 7 days), not one run's, so a real
        # failure the run status hides (repeat-operations-csc-email-failures-invisible) needs its
        # own window instead. Count only the error/warning lines whose own timestamp is within
        # the last 7 days; None only when no line in the file carries a parsable timestamp.
        error_7d, warning_7d = _count_recent_debug_lines(newest, max_lines_per_file, now)
        summary.update(latest_log_error_lines=error_7d, latest_log_warning_lines=warning_7d)
    return metric_rows, summary


def build_metric_rows(
    inventory_rows: Sequence[Dict[str, Any]],
    *,
    host_name: str,
    days_back: int = 30,
    max_files_per_task: int = DEFAULT_MAX_LOG_FILES_PER_TASK,
    max_lines_per_file: int = DEFAULT_MAX_LINES_PER_FILE,
    log_directory_overrides: Optional[Dict[str, str]] = None,
    own_log_dir: Any = None,
    examined: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Return (metric time-series rows, per-task log summary keyed by task_full_name).

    log_directory_overrides lets a caller point a straggler task at its real log folder
    (keyed by task_full_name) when discovery from the script path / working directory misses
    it. This function never reads config itself -- the caller resolves and passes the map.

    own_log_dir lets the caller pass its own resolved log directory (e.g. taskdash_publish's
    logging.log_dir config, which can differ from own_dir/Logs) so it stays excluded from
    mining even when relocated. Defaults to own_dir/Logs, matching the historical layout.
    """
    now = utc_now()
    overrides = log_directory_overrides or {}

    own_dir = Path(__file__).resolve().parent
    own_logs_dir = Path(normalize_text(own_log_dir)) if normalize_text(own_log_dir) else (own_dir / "Logs")
    # metrics-01: never mine our own logs (TaskDash's summary lines -- "TaskRuns: added 49,
    # updated 72" -- parse as metrics and land in the table as junk keys like added_updated_72).
    # Scoped to TaskDash's OWN log directory exactly, not "any descendant of TaskDash's folder":
    # the old ancestor check excluded every monitored task's logs whenever TaskDash was
    # installed at or above their folder (e.g. at the shared repo root).
    # wording-03: own_dir/"Logs" is added on top of own_logs_dir, not instead of it -- an
    # own_log_dir override (e.g. a relocated logging.log_dir) must not stop excluding the
    # historical default folder, which the collector can still write bootstrap.log into.
    # home_dir() is the same folder in a folder install; an installed wheel keeps its Logs there.
    own_log_keys = {_dir_key(own_dir), _dir_key(own_dir / "Logs"), _dir_key(own_logs_dir),
                    _dir_key(home_dir()), _dir_key(home_dir() / "Logs")}

    task_plans, dir_task_owners = _plan_task_directories(inventory_rows, overrides, own_log_keys)

    # A misspelt metrics.tasks key is otherwise ignored without a trace.
    known_tasks = {row.get("task_full_name") for row in inventory_rows}
    for key in TASK_SETTINGS:
        if key not in known_tasks:
            emit(f"WARNING: metrics.tasks key {key!r} matches no collected task; keys must be full "
                 "task paths like '\\Folder\\Task' (see task_full_name in the Tasks table).")

    metric_rows: List[Dict[str, Any]] = []
    log_summary: Dict[str, Dict[str, Any]] = {}
    for plan in task_plans:
        task_full_name = plan["task"].get("task_full_name")
        task_rows, summary_entry = _scan_task_logs(
            plan, dir_task_owners, host_name=host_name, days_back=days_back,
            max_files_per_task=max_files_per_task, max_lines_per_file=max_lines_per_file, now=now,
            own_logs_dir=own_logs_dir, examined=examined,
        )
        metric_rows.extend(task_rows)
        if summary_entry is not None:
            log_summary[task_full_name] = summary_entry

    metric_rows.sort(key=lambda r: (r["task_name"].lower(), r["metric_key"], r["observed_at_utc"]))
    return metric_rows, log_summary

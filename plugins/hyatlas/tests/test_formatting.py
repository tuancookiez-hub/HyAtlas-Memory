"""Tests for formatting.py: the prompt lines the agent reads for each search hit.

The package __init__ imports Hermes modules that are not installed locally, so
formatting.py is loaded by path instead of through the package.
"""

import importlib.util
import pathlib

_PATH = pathlib.Path(__file__).resolve().parent.parent / "formatting.py"
_spec = importlib.util.spec_from_file_location("hyatlas_formatting", _PATH)
fmt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fmt)

# 2026-10-12 00:00:00 UTC
SAVED = 1791763200


def test_old_format_without_event_or_history():
    m = {"content": "text", "layer": "l3_fact", "gmt_created": SAVED, "score": 0.81}
    assert fmt.format_hit_line(m, 300) == "- (l3_fact, 2026-10-12, score 0.81) text"


def test_event_date_differs_from_saved_date():
    m = {
        "content": "text",
        "layer": "l3_fact",
        "gmt_created": SAVED,
        "score": 0.81,
        "event_date": "2026-10-05",
    }
    assert (
        fmt.format_hit_line(m, 300)
        == "- (l3_fact, happened 2026-10-05, saved 2026-10-12, score 0.81) text"
    )


def test_event_date_matching_saved_date_shows_once():
    m = {
        "content": "text",
        "layer": "l3_fact",
        "gmt_created": SAVED,
        "score": 0.81,
        "event_date": "2026-10-12",
    }
    assert fmt.format_hit_line(m, 300) == "- (l3_fact, happened 2026-10-12, score 0.81) text"


def test_year_month_event_date_is_accepted():
    m = {"content": "text", "layer": "l3_fact", "score": 0.5, "event_date": "2026-10"}
    assert fmt.format_hit_line(m, 300) == "- (l3_fact, happened 2026-10, score 0.50) text"


def test_invalid_event_date_is_ignored():
    m = {
        "content": "text",
        "layer": "l3_fact",
        "gmt_created": SAVED,
        "score": 0.81,
        "event_date": "soon",
    }
    assert fmt.format_hit_line(m, 300) == "- (l3_fact, 2026-10-12, score 0.81) text"


def test_history_shows_two_entries_and_until_only_when_dated():
    m = {
        "content": "now",
        "layer": "l3_fact",
        "gmt_created": SAVED,
        "score": 0.9,
        "history": [
            {"content": "first old", "date": "2026-10-01", "how": "replaced"},
            {"content": "second old", "date": "", "how": "updated"},
            {"content": "third old", "date": "2026-09-01", "how": "replaced"},
        ],
    }
    assert fmt.format_hit_line(m, 300) == (
        "- (l3_fact, 2026-10-12, score 0.90) now\n"
        "    was: first old (until 2026-10-01)\n"
        "    was: second old"
    )


def test_history_skips_non_dict_and_empty_entries():
    m = {
        "content": "now",
        "layer": "l3_fact",
        "score": 0.1,
        "history": [
            "not a dict",
            {"content": "", "date": "2026-10-01"},
            {"content": "kept", "date": "2026-10-02"},
        ],
    }
    assert fmt.format_hit_line(m, 300) == (
        "- (l3_fact, score 0.10) now\n    was: kept (until 2026-10-02)"
    )


def test_history_content_is_cut_to_160_chars():
    m = {
        "content": "now",
        "layer": "l3_fact",
        "score": 0.1,
        "history": [{"content": "x" * 400, "date": ""}],
    }
    line = fmt.format_hit_line(m, 300).split("\n")[1]
    assert line == "    was: " + "x" * 160


def test_empty_content_returns_empty_string():
    assert fmt.format_hit_line({"content": "", "layer": "l3_fact", "score": 0.5}, 300) == ""
    assert fmt.format_hit_line({"layer": "l3_fact", "score": 0.5}, 300) == ""


def test_missing_score_and_layer_default_to_zero_and_empty():
    assert fmt.format_hit_line({"content": "text"}, 300) == "- (, score 0.00) text"


def test_snippet_is_cut_to_limit():
    m = {"content": "abcdef", "layer": "l3_fact", "score": 0.5}
    assert fmt.format_hit_line(m, 3) == "- (l3_fact, score 0.50) abc"


def test_iso_date_handles_zero_none_and_bad_values():
    assert fmt.iso_date(SAVED) == "2026-10-12"
    assert fmt.iso_date(0) == ""
    assert fmt.iso_date(None) == ""
    assert fmt.iso_date("not a number") == ""

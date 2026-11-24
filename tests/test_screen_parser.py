"""Contract tests for headless ScreenParser and firmware version / vector outcome matching."""

import pytest

from bitbox_e2e.runs import ScreenParser
from bitbox_e2e.units import parse_version, vector_outcome


def test_screen_parser_single_line_fields_and_ignores_log_noise():
    parser = ScreenParser()
    lines = [
        "Listening on port 15423",
        "noise: not inside a screen",
        "CONFIRM SCREEN START",
        "TITLE: Send Bitcoin",
        "BODY: 0.5 BTC",
        "CONFIRM SCREEN END",
        "Waiting for new clients",
    ]
    for line in lines:
        parser.feed(line, "sign")
    assert parser.screens == [
        {"step": "sign", "kind": "CONFIRM", "fields": {"TITLE": "Send Bitcoin", "BODY": "0.5 BTC"}}
    ]


def test_screen_parser_multiline_field_continuation():
    parser = ScreenParser()
    lines = [
        "CONFIRM SCREEN START",
        "line before any field is ignored",
        "TITLE: Address",
        "BODY: bc1qfirstline",
        "second line",
        "lowercase: stays part of body",
        "CONFIRM SCREEN END",
    ]
    for line in lines:
        parser.feed(line, "verify")
    assert parser.screens == [
        {
            "step": "verify",
            "kind": "CONFIRM",
            "fields": {
                "TITLE": "Address",
                "BODY": "bc1qfirstline\nsecond line\nlowercase: stays part of body",
            },
        }
    ]


def test_screen_parser_multiple_screens_across_steps():
    parser = ScreenParser()
    for step, line in [
        ("output", "CONFIRM SCREEN START"),
        ("output", "TITLE: Output 1"),
        ("output", "BODY: 0.1 BTC"),
        ("output", "CONFIRM SCREEN END"),
        ("fee", "TRANSACTION FEE SCREEN START"),
        ("fee", "TITLE: Fee"),
        ("fee", "BODY: 1200 sat"),
        ("fee", "TRANSACTION FEE SCREEN END"),
    ]:
        parser.feed(line, step)
    assert parser.screens == [
        {"step": "output", "kind": "CONFIRM", "fields": {"TITLE": "Output 1", "BODY": "0.1 BTC"}},
        {"step": "fee", "kind": "TRANSACTION FEE", "fields": {"TITLE": "Fee", "BODY": "1200 sat"}},
    ]


def test_screen_parser_incomplete_block_and_stray_end_are_not_emitted():
    parser = ScreenParser()
    for line in [
        "CONFIRM SCREEN END",
        "CONFIRM SCREEN START",
        "TITLE: Never closed",
        "BODY: still open",
    ]:
        parser.feed(line, "sign")
    assert parser.screens == []


# --- parse_version & vector_outcome ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bitbox02-multi-v9.28.0-simulator1.0.0-linux-amd64", (9, 28, 0)),
        ("9.30.0", (9, 30, 0)),
        ("  9.30.0\n", (9, 30, 0)),
    ],
)
def test_parse_version_extracts_semver_tuple(raw, expected):
    assert parse_version(raw) == expected


@pytest.mark.parametrize("bad", ["", "simulator", "9.28", "bitbox02-multi-dev"])
def test_parse_version_rejects_invalid_strings(bad):
    with pytest.raises(ValueError):
        parse_version(bad)


def test_vector_outcome_respects_inclusive_min_and_exclusive_max_bounds():
    unit = {
        "outcomes": [
            {"min_version": "9.20.0", "max_version_exclusive": "9.28.0", "outcome": "unsupported"},
            {"min_version": "9.28.0", "max_version_exclusive": "9.30.0", "outcome": "success"},
        ]
    }
    assert vector_outcome(unit, "9.19.9") is None
    assert vector_outcome(unit, "9.20.0") == "unsupported"
    assert vector_outcome(unit, "9.27.9") == "unsupported"
    assert vector_outcome(unit, "bitbox02-multi-v9.28.0-simulator1.0.0-linux-amd64") == "success"
    assert vector_outcome(unit, "9.30.0") is None


def test_vector_outcome_open_bounds_and_missing_outcomes():
    unit = {"outcomes": [{"min_version": None, "max_version_exclusive": "9.25.0", "outcome": "invalid_input"}]}
    assert vector_outcome(unit, "9.0.0") == "invalid_input"
    assert vector_outcome(unit, "9.25.0") is None
    assert vector_outcome({}, "9.28.0") is None
    assert vector_outcome({"outcomes": []}, "9.28.0") is None

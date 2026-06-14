"""Test normalizzazione numerica virgola decimale."""

from __future__ import annotations

import copy

import pytest

from idith.text_normalize_user_numbers import (
    format_leverage,
    normalize_decimal_commas_in_text,
    normalize_user_numeric_input,
    replace_spelled_numbers_in_text,
    parse_config_float,
    format_percent_config_string,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2,5", 2.5),
        ("1,5", 1.5),
        ("10,5", 10.5),
        ("3,5%", 3.5),
        ("2.5", 2.5),
        ("100", 100.0),
        ("abc", None),
        ("2,5,3", None),
    ],
)
def test_parse_config_float(raw, expected):
    assert parse_config_float(raw) == expected


def test_normalize_decimal_commas_in_text_phrase():
    text = "metti sl 1,5 tp 2,5 rischio 3,5 leva 2,5"
    assert normalize_decimal_commas_in_text(text) == "metti sl 1.5 tp 2.5 rischio 3.5 leva 2.5"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sl dieci", "sl 10"),
        ("stop loss dieci", "stop loss 10"),
        ("rischio due", "rischio 2"),
        ("leva cinque", "leva 5"),
        ("set leverage five", "set leverage 5"),
        ("set risk three", "set risk 3"),
        ("take profit four", "take profit 4"),
        ("metti sl dieci", "metti sl 10"),
        ("stop loss dieci", "stop loss 10"),
    ],
)
def test_replace_spelled_numbers_in_text(raw, expected):
    assert replace_spelled_numbers_in_text(raw) == expected


@pytest.mark.parametrize(
    "raw,field,expected",
    [
        ("sl dieci", "sl", 10.0),
        ("stop loss dieci", "sl", 10.0),
        ("rischio due", "risk_pct", 2.0),
        ("leva cinque", "leverage", 5.0),
        ("set leverage five", "leverage", 5.0),
        ("set risk three", "risk_pct", 3.0),
        ("take profit four", "tp", 4.0),
    ],
)
def test_extract_modification_spelled_numbers(raw, field, expected):
    from idith.orchestrator import _extract_modification_requests

    updates = _extract_modification_requests(raw, {})
    assert updates.get(field) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("set leverage five", 5.0),
        ("leverage seven", 7.0),
        ("leva cinque", 5.0),
        ("metti leva sette", 7.0),
    ],
)
def test_extract_direct_leverage_spelled_numbers(raw, expected):
    from idith.orchestrator import _extract_direct_leverage_value

    assert _extract_direct_leverage_value(raw) == expected


def test_extract_modification_multi_command_spelled_numbers():
    from idith.orchestrator import _extract_modification_requests

    updates = _extract_modification_requests(
        "insert tp five, leverage seven, five minutes",
        {},
    )
    assert updates.get("tp") == 5.0
    assert updates.get("leverage") == 7.0
    assert updates.get("timeframe") == "5m"


def test_extract_modification_multi_command_spelled_timeframe_sixty_minutes():
    from idith.orchestrator import _extract_modification_requests

    updates = _extract_modification_requests(
        "insert tp ten, leverage seven, sixty minutes",
        {},
    )
    assert updates.get("tp") == 10.0
    assert updates.get("leverage") == 7.0
    assert updates.get("timeframe") == "1h"


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("five minutes", "5m"),
        ("one hour", "1h"),
        ("4 hours", "4h"),
        ("fifteen minutes", "15m"),
        ("sixty minutes", "1h"),
    ],
)
def test_extract_timeframe_from_message_segments(phrase, expected):
    from idith.text_normalize_config_values import extract_timeframe_from_message

    assert extract_timeframe_from_message(f"insert tp 5, {phrase}") == expected


def test_handle_message_multi_command_spelled_leverage_pending():
    """Leva 7x (> soglia) va in pending, non resta al valore precedente."""
    from idith.orchestrator import handle_message

    state, handle_message_fn = _complete_state()
    state["config_state"]["params"]["leverage"] = 3
    out = handle_message_fn(
        "insert tp five, leverage seven, five minutes",
        state,
        [],
    )
    cs = out["state"]["config_state"]
    assert cs.get("pending_leverage_confirmation") == 7.0
    assert float(cs["params"]["leverage"]) == 3.0
    assert "leva 7x" in out["reply"].lower() or "7x" in out["reply"]


def test_format_percent_config_string():
    assert format_percent_config_string("1,5") == "1.5%"
    assert format_percent_config_string(2.5) == "2.5%"


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "—"),
        ("", "—"),
        (5, "5x"),
        (5.0, "5x"),
        (5.5, "5.5x"),
        (10.5, "10.5x"),
        ("5,5", "5.5x"),
    ],
)
def test_format_leverage(value, expected):
    assert format_leverage(value) == expected


def test_build_summary_shows_decimal_leverage():
    from idith.orchestrator import _apply_operating_mode_preset, _build_summary, DEFAULT_PARAMS

    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(
        {
            "market_type": "futures",
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "leverage": 5.5,
            "risk_pct": 2.0,
            "sl": "3.0%",
            "tp": "5.0%",
        }
    )
    params = _apply_operating_mode_preset(params, "equilibrata")
    summary = _build_summary(params, full_config=True)
    assert "Leva: 5.5x" in summary

    params["leverage"] = 10.5
    summary2 = _build_summary(params, full_config=True)
    assert "Leva: 10.5x" in summary2


def _complete_state():
    from idith.orchestrator import DEFAULT_PARAMS, _apply_operating_mode_preset, handle_message

    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(
        {
            "market_type": "futures",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "leverage": 10,
            "risk_pct": 2.0,
            "sl": "3.0%",
            "tp": "5.0%",
        }
    )
    params = _apply_operating_mode_preset(params, "equilibrata")
    return {
        "config_status": "complete",
        "config_state": {
            "step": None,
            "params": params,
            "error_count": {},
            "pending_sl_confirmation": None,
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
        },
    }, handle_message


@pytest.mark.parametrize(
    "user_text,field,expected",
    [
        ("metti sl 1,5", "sl", "1.5%"),
        ("metti tp 2,5", "tp", "2.5%"),
        ("rischio 3,5", "risk_pct", 3.5),
        ("leva 2,5", "leverage", 2.5),
    ],
)
def test_handle_message_decimal_comma(user_text, field, expected):
    state, handle_message = _complete_state()
    out = handle_message(user_text, state, [])
    val = out["state"]["config_state"]["params"][field]
    if isinstance(expected, float):
        assert float(val) == expected
    else:
        assert val == expected
    assert str(expected).replace(".", ",") in user_text or str(expected) in out["reply"]


def test_handle_message_combined_decimal_comma():
    state, handle_message = _complete_state()
    user_text = "metti sl 1,5 tp 2,5 rischio 3,5 leva 2,5"
    out = handle_message(user_text, state, [])
    params = out["state"]["config_state"]["params"]
    assert params["sl"] == "1.5%"
    assert params["tp"] == "2.5%"
    assert float(params["risk_pct"]) == 3.5
    assert float(params["leverage"]) == 2.5

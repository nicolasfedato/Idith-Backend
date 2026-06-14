"""Test normalizzazione centralizzata market_type / operating_mode."""

from __future__ import annotations

import copy

import pytest

from idith.text_normalize_config_values import (
    STRATEGY_ID_BY_MODE,
    extract_market_type_from_text,
    extract_operating_mode_from_text,
    normalize_market_type,
    normalize_operating_mode,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sport", "spot"),
        ("spot", "spot"),
        ("SPOT", "spot"),
        ("spoot", "spot"),
        ("futures", "futures"),
        ("futurs", "futures"),
        ("perpetual", "futures"),
        ("xyz", None),
    ],
)
def test_normalize_market_type(raw, expected):
    assert normalize_market_type(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("aggressiva", "aggressiva"),
        ("agressiva", "aggressiva"),
        ("agggressiva", "aggressiva"),
        ("aggresssiva", "aggressiva"),
        ("equilibrata", "equilibrata"),
        ("ecquilibrata", "equilibrata"),
        ("equillibrata", "equilibrata"),
        ("selettiva", "selettiva"),
        ("balanced", "equilibrata"),
        ("aggressive", "aggressiva"),
        ("xyz", None),
    ],
)
def test_normalize_operating_mode(raw, expected):
    assert normalize_operating_mode(raw) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("metti sport", "spot"),
        ("metti spot", "spot"),
        ("passa a futures", "futures"),
        ("metti sport e futures", None),
    ],
)
def test_extract_market_type_from_text(text, expected):
    assert extract_market_type_from_text(text) == expected


@pytest.mark.parametrize(
    "text,expected_mode,expected_sid",
    [
        ("metti agggressiva", "aggressiva", "1"),
        ("metti agressiva", "aggressiva", "1"),
        ("metti ecquilibrata", "equilibrata", "2"),
        ("metti selettiva", "selettiva", "3"),
        ("metti aggressiva", "aggressiva", "1"),
    ],
)
def test_extract_operating_mode_from_text(text, expected_mode, expected_sid):
    mode = extract_operating_mode_from_text(text)
    assert mode == expected_mode
    assert STRATEGY_ID_BY_MODE[mode] == expected_sid


def _complete_config_params():
    from idith.orchestrator import DEFAULT_PARAMS, _apply_operating_mode_preset

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
    return _apply_operating_mode_preset(params, "equilibrata")


def _complete_state():
    params = _complete_config_params()
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
    }


@pytest.mark.parametrize(
    "user_text,field,expected",
    [
        ("metti sport", "market_type", "spot"),
        ("metti spot", "market_type", "spot"),
        ("metti agggressiva", "operating_mode", "aggressiva"),
        ("metti agressiva", "operating_mode", "aggressiva"),
        ("metti ecquilibrata", "operating_mode", "equilibrata"),
        ("metti selettiva", "operating_mode", "selettiva"),
    ],
)
def test_handle_message_applies_normalized_values(user_text, field, expected):
    from idith.orchestrator import handle_message

    state = _complete_state()
    out = handle_message(user_text, state, [])
    params = out["state"]["config_state"]["params"]

    assert params[field] == expected
    assert expected in out["reply"].lower()

    if field == "operating_mode":
        assert params["strategy_id"] == STRATEGY_ID_BY_MODE[expected]
        assert "Modalità operativa:" in out["reply"]

    if field == "market_type":
        assert f"Tipo di mercato: {expected}" in out["reply"]

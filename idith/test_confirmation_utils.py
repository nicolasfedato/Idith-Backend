"""Test conferme pending IT/EN."""

from __future__ import annotations

import copy

import pytest

from idith.confirmation_utils import is_confirmation, is_rejection, normalize_confirmation_text
from idith.orchestrator import (
    DEFAULT_PARAMS,
    _apply_operating_mode_preset,
    handle_message,
)


@pytest.mark.parametrize(
    "text",
    ["si", "sì", "yes", "y", "ok", "okay", "confermo", "conferma", "confirm", "confirmed", "proceed", "procedi", "va bene"],
)
def test_is_confirmation_accepts_tokens(text: str):
    assert is_confirmation(text) is True


@pytest.mark.parametrize("text", ["yes.", " OK! ", "Sì"])
def test_is_confirmation_normalizes(text: str):
    assert is_confirmation(text) is True


@pytest.mark.parametrize("text", ["no", "n", "cancel", "annulla", "stop", "non confermo"])
def test_is_rejection_accepts_tokens(text: str):
    assert is_rejection(text) is True


def test_normalize_strips_accents_and_punctuation():
    assert normalize_confirmation_text(" Sì! ") == "si"
    assert normalize_confirmation_text("Confirm.") == "confirm"


def _futures_complete_state():
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(
        {
            "market_type": "futures",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "leverage": 2,
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
    }


@pytest.mark.parametrize("confirm_word", ["yes", "ok", "confermo", "confirm"])
def test_pending_leverage_english_confirm(confirm_word: str):
    state = _futures_complete_state()
    out1 = handle_message("set leverage 10", state, [])
    cs1 = out1["state"]["config_state"]
    assert cs1.get("pending_leverage_confirmation") == 10

    out2 = handle_message(confirm_word, out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2.get("pending_leverage_confirmation") is None
    assert float(cs2["params"]["leverage"]) == 10


def test_pending_risk_yes_confirm():
    state = _futures_complete_state()
    out1 = handle_message("set risk 10.5", state, [])
    assert out1["state"]["config_state"].get("pending_risk_confirmation") == 10.5

    out2 = handle_message("yes", out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2.get("pending_risk_confirmation") is None
    assert float(cs2["params"]["risk_pct"]) == 10.5


def test_pending_sl_confirm_confirm():
    state = _futures_complete_state()
    out1 = handle_message("set stop loss 5.5", state, [])
    assert out1["state"]["config_state"].get("pending_sl_confirmation") == 5.5

    out2 = handle_message("confirm", out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2.get("pending_sl_confirmation") is None
    assert cs2["params"]["sl"] == "5.5%"


@pytest.mark.parametrize("reject_word", ["no", "cancel"])
def test_pending_leverage_reject_clears_pending(reject_word: str):
    state = _futures_complete_state()
    out1 = handle_message("set leverage 10", state, [])
    prev_lev = out1["state"]["config_state"]["params"]["leverage"]

    out2 = handle_message(reject_word, out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2.get("pending_leverage_confirmation") is None
    assert cs2["params"]["leverage"] == prev_lev
    assert out2["state"]["config_status"] == "complete"
    assert cs2.get("step") is None

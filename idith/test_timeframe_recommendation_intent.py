"""Test intent raccomandazione timeframe EN/IT nello step timeframe."""

from __future__ import annotations

import copy

import pytest

from idith.app import _deep_merge_config_state
from idith.orchestrator import (
    DEFAULT_PARAMS,
    _is_timeframe_recommendation_intent,
    handle_message,
)


def _timeframe_step_state(*, market_type: str = "futures") -> dict:
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(
        {
            "market_type": market_type,
            "symbol": "BTCUSDT",
        }
    )
    return {
        "config_status": "in_progress",
        "config_state": {
            "step": "timeframe",
            "params": params,
            "error_count": {},
            "pending_sl_confirmation": None,
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
            "pending_symbol_confirmation": None,
            "pending_timeframe_confirmation": None,
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        "what do you recommend?",
        "recommend a timeframe",
        "best timeframe?",
        "which timeframe",
        "i don't know",
        "choose for me",
        "you decide",
        "suggest one",
        "recommend one",
        "not sure",
        "non lo so",
        "scegli tu",
        "consigliami un timeframe",
        "che timeframe mi consigli",
    ],
)
def test_timeframe_recommendation_intent_detected(text: str):
    assert _is_timeframe_recommendation_intent(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "15m",
        "1h",
        "metti sl 2%",
        "what is a timeframe",
    ],
)
def test_timeframe_recommendation_intent_not_detected(text: str):
    assert _is_timeframe_recommendation_intent(text) is False


def test_i_dont_know_proposes_15m():
    state = _timeframe_step_state()
    out = handle_message("i don't know", state, [])
    cs = out["state"]["config_state"]
    assert cs["pending_timeframe_confirmation"] == "15m"
    assert "15m" in out["reply"]
    assert "Would you like to use 15m?" in out["reply"]
    assert out.get("skip_llm") is True


def test_yes_saves_timeframe_and_advances():
    state = _timeframe_step_state()
    out1 = handle_message("i don't know", state, [])
    out2 = handle_message("yes", out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2["params"]["timeframe"] == "15m"
    assert cs2["pending_timeframe_confirmation"] is None
    assert cs2["step"] != "timeframe"
    assert "Quale timeframe?" not in out2["reply"]


def test_pending_timeframe_survives_config_state_merge():
    state = _timeframe_step_state()
    out1 = handle_message("choose for me", state, [])
    merged = _deep_merge_config_state({}, out1["state"]["config_state"])
    assert merged.get("pending_timeframe_confirmation") == "15m"
    state2 = {"config_status": "in_progress", "config_state": merged}
    out2 = handle_message("yes", state2, [])
    cs2 = out2["state"]["config_state"]
    assert cs2["params"]["timeframe"] == "15m"
    assert "Quale timeframe?" not in out2["reply"]


@pytest.mark.parametrize("confirm_word", ["yes", "sì", "confermo"])
def test_timeframe_recommendation_confirm_tokens(confirm_word: str):
    state = _timeframe_step_state()
    out1 = handle_message("you decide", state, [])
    out2 = handle_message(confirm_word, out1["state"], [])
    assert out2["state"]["config_state"]["params"]["timeframe"] == "15m"


def test_timeframe_recommendation_reject_asks_again():
    state = _timeframe_step_state()
    out1 = handle_message("recommend a timeframe", state, [])
    out2 = handle_message("no", out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2["pending_timeframe_confirmation"] is None
    assert cs2["step"] == "timeframe"

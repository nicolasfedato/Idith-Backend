"""Mandatory lang=en chat message tests for orchestrator static replies."""

from __future__ import annotations

import copy

from idith import ai_lang
from idith.orchestrator import (
    DEFAULT_PARAMS,
    _apply_operating_mode_preset,
    handle_message,
)


def _complete_state():
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
    }


def _fresh_wizard_state():
    return {
        "config_status": "in_progress",
        "config_state": {
            "step": "market_type",
            "params": copy.deepcopy(DEFAULT_PARAMS),
            "error_count": {},
            "pending_sl_confirmation": None,
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
        },
    }


def test_en_insert_sl_and_timeframe_shows_english_confirm():
    state = _complete_state()
    out = handle_message(
        "insert sl 5 and timeframe 3 minutes",
        state,
        [],
        lang="en",
    )
    reply = out["reply"].lower()
    assert "confirm" in reply
    assert "stai impostando" not in reply
    assert "confermi" not in reply


def test_en_new_bot_builder_questions_in_english():
    state = _fresh_wizard_state()
    out = handle_message("reset", state, [], lang="en")
    reply = out["reply"]
    assert "Spot or Futures" in reply or "spot or futures" in reply.lower()
    assert "Iniziamo" not in reply
    assert "Vuoi operare" not in reply


def test_en_complete_config_summary_in_english():
    state = _complete_state()
    cs = state["config_state"]
    cs["step"] = "sl"
    cs["pending_sl_confirmation"] = 5.5
    cs["pending_risk_confirmation"] = 5.5
    state["config_status"] = "in_progress"

    out = handle_message("yes", state, [], lang="en")
    reply = out["reply"]
    assert "Configuration complete" in reply
    assert "Pair:" in reply
    assert "Market type:" in reply
    assert "Configurazione completata" not in reply
    assert "Coppia:" not in reply


def test_it_default_unchanged_pending_confirm():
    state = _complete_state()
    state["config_state"]["params"]["market_type"] = "futures"
    state["config_state"]["params"]["leverage"] = 10

    out = handle_message(
        "remove leverage, insert aggressive, insert sl ten, tp 10, risk ten",
        state,
        [],
        lang="it",
    )
    assert out["reply"] == ai_lang.build_pending_confirm_prompt(
        {"sl": 10.0, "risk_pct": 10.0},
        lang="it",
    )


def test_it_default_unchanged_operating_mode_question():
    from idith.orchestrator import _step_question

    ai_lang.set_request_lang("it")
    assert _step_question("operating_mode", {}) == ai_lang.chat("ask_operating_mode", "it")

"""Smoke test: sostituzione pending_risk_confirmation durante conferma."""

from __future__ import annotations

import copy

import pytest

from idith import ai_lang
from idith.orchestrator import (
    DEFAULT_PARAMS,
    _apply_operating_mode_preset,
    handle_message,
    resolve_input,
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


def test_resolve_input_replaces_pending_risk_without_keyword():
    state = _complete_state()
    params = state["config_state"]["params"]
    pending = {"risk_pct": 10.5}

    merged_params, merged_pending = resolve_input(params, pending, "metti 8,5")

    assert merged_params["risk_pct"] == 2.0
    assert merged_pending["risk_pct"] == 8.5


def test_pending_risk_replacement_then_confirm_saves_new_value():
    state = _complete_state()
    cs = state["config_state"]

    out1 = handle_message("metti rischio 10,5", state, [])
    state1 = out1["state"]
    cs1 = state1["config_state"]

    assert cs1.get("pending_risk_confirmation") == 10.5
    assert float(cs1["params"]["risk_pct"]) == 2.0
    assert "10.5" in out1["reply"] or "10,5" in out1["reply"].lower()
    assert "confermi" in out1["reply"].lower()

    out2 = handle_message("metti 8,5", state1, [])
    state2 = out2["state"]
    cs2 = state2["config_state"]

    assert cs2.get("pending_risk_confirmation") == 8.5
    assert float(cs2["params"]["risk_pct"]) == 2.0
    assert "8.5" in out2["reply"]
    assert "10.5" not in out2["reply"]

    out3 = handle_message("sì", state2, [])
    cs3 = out3["state"]["config_state"]

    assert cs3.get("pending_risk_confirmation") is None
    assert float(cs3["params"]["risk_pct"]) == 8.5


def _spot_complete_state():
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(
        {
            "market_type": "spot",
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "leverage": None,
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


def test_spot_to_futures_high_leverage_then_replace_and_confirm_leva():
    state = _spot_complete_state()

    out1 = handle_message(
        "metti btcusdt, 15 minuti, equilibrata, rischio 10,5, sl 10,5 e leva 10,5",
        state,
        [],
    )
    cs1 = out1["state"]["config_state"]
    assert cs1["params"]["market_type"] == "futures"
    assert cs1.get("pending_sl_confirmation") == 10.5
    assert cs1.get("pending_risk_confirmation") == 10.5
    assert cs1.get("pending_leverage_confirmation") == 10.5
    assert "confermi" in out1["reply"].lower()

    out2 = handle_message("metti rischio 5,5, sl 5,5 e confermo leva", out1["state"], [])
    cs2 = out2["state"]["config_state"]
    params2 = cs2["params"]

    assert float(params2["leverage"]) == 10.5
    assert cs2.get("pending_leverage_confirmation") is None
    assert cs2.get("pending_risk_confirmation") == 5.5
    assert cs2.get("pending_sl_confirmation") == 5.5
    assert float(params2["risk_pct"]) == 2.0
    assert cs2.get("step") != "leverage"
    assert "leva" not in out2["reply"].lower()


def test_generic_si_confirms_all_pending_sl_and_risk():
    state = _complete_state()
    cs = state["config_state"]
    cs["step"] = "sl"
    cs["pending_sl_confirmation"] = 5.5
    cs["pending_risk_confirmation"] = 5.5
    state["config_status"] = "in_progress"

    out = handle_message("sì", state, [])
    cs_out = out["state"]["config_state"]
    params = cs_out["params"]

    assert cs_out.get("pending_sl_confirmation") is None
    assert cs_out.get("pending_risk_confirmation") is None
    assert params["sl"] == "5.5%"
    assert float(params["risk_pct"]) == 5.5
    assert cs_out.get("step") is None
    assert out["state"]["config_status"] == "complete"
    assert "Configurazione completata" in out["reply"]


def test_remove_leverage_multiparam_with_risky_pending_shows_batch_confirm():
    """remove leverage + safe params + risky sl/risk: pending batch, no summary skip_llm."""
    state = _spot_complete_state()
    state["config_state"]["params"]["market_type"] = "futures"
    state["config_state"]["params"]["leverage"] = 10

    out = handle_message(
        "remove leverage, insert aggressive, insert sl ten, tp 10, risk ten",
        state,
        [],
    )
    cs = out["state"]["config_state"]
    params = cs["params"]

    assert params["market_type"] == "spot"
    assert params["leverage"] is None
    assert params["operating_mode"] == "aggressiva"
    assert params["tp"] == "10.0%"
    assert float(str(params["sl"]).replace("%", "")) == 3.0
    assert float(params["risk_pct"]) == 2.0
    assert cs.get("pending_sl_confirmation") == 10.0
    assert cs.get("pending_risk_confirmation") == 10.0
    assert out["state"]["config_status"] == "in_progress"
    assert cs.get("step") == "sl"
    assert out.get("skip_llm") is None
    assert out["reply"] == ai_lang.build_pending_confirm_prompt(
        {"sl": 10.0, "risk_pct": 10.0},
        lang="it",
    )

    out2 = handle_message("sì", out["state"], [])
    cs2 = out2["state"]["config_state"]
    params2 = cs2["params"]
    assert cs2.get("pending_sl_confirmation") is None
    assert cs2.get("pending_risk_confirmation") is None
    assert params2["sl"] == "10.0%"
    assert float(params2["risk_pct"]) == 10.0
    assert out2["state"]["config_status"] == "complete"

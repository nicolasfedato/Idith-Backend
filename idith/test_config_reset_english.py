"""Test reset configurazione con frasi inglesi (stesso comportamento dell'italiano)."""

from __future__ import annotations

import copy

import pytest

from idith.orchestrator import (
    DEFAULT_PARAMS,
    FORCE_FULL_RESET_CONFIG_STATE_SNAPSHOT,
    _detect_english_config_reset_intent,
    handle_message,
    run,
)


def _filled_config_state() -> dict:
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(
        {
            "market_type": "futures",
            "symbol": "SOLUSDT",
            "timeframe": "5m",
            "operating_mode": "aggressiva",
            "strategy_id": "1",
            "sl": "2.0%",
            "tp": "5.0%",
            "risk_pct": 2.0,
            "leverage": 10,
        }
    )
    return {
        "config_status": "complete",
        "config_state": {
            "step": "leverage",
            "params": params,
            "error_count": {"symbol": 1},
            "pending_sl_confirmation": 3.0,
            "pending_risk_confirmation": 5.0,
            "pending_leverage_confirmation": 8,
            "pending_symbol_confirmation": "ETHUSDT",
            "pending_timeframe_confirmation": "15m",
        },
    }


@pytest.mark.parametrize(
    "phrase",
    [
        "reset configuration",
        "reset config",
        "reset my configuration",
        "clear configuration",
        "clear config",
        "restart configuration",
        "start over",
        "start from scratch",
        "reset setup",
    ],
)
def test_detect_english_config_reset_intent(phrase):
    assert _detect_english_config_reset_intent(phrase)


@pytest.mark.parametrize(
    "phrase",
    [
        "reset configuration",
        "reset config",
        "clear configuration",
        "start over",
        "start from scratch",
    ],
)
def test_handle_message_english_reset_clears_state(phrase):
    state = _filled_config_state()
    out = handle_message(phrase, state, [])
    cs = out["state"]["config_state"]
    params = cs["params"]

    assert out["state"]["config_status"] == "in_progress"
    assert cs["step"] == "market_type"
    assert params["symbol"] is None
    assert params["timeframe"] is None
    assert params["market_type"] is None
    assert params["operating_mode"] is None
    assert params["sl"] is None
    assert params["tp"] is None
    assert params["risk_pct"] is None
    assert params["leverage"] is None
    assert cs["pending_sl_confirmation"] is None
    assert cs["pending_risk_confirmation"] is None
    assert cs["pending_leverage_confirmation"] is None
    assert cs["pending_symbol_confirmation"] is None
    assert cs["pending_timeframe_confirmation"] is None
    assert "Spot o in Futures" in out["reply"]


@pytest.mark.parametrize(
    "phrase",
    [
        "reset configuration",
        "clear config",
        "start over",
    ],
)
def test_run_english_reset_before_wizard(phrase):
    state = _filled_config_state()
    out = run({"message": phrase}, state=state)
    cs = out["state"]["config_state"]

    assert cs["step"] == "market_type"
    assert cs["params"]["symbol"] is None
    assert cs["params"]["timeframe"] is None
    assert cs["__force_full_reset"] is True
    assert cs == copy.deepcopy(FORCE_FULL_RESET_CONFIG_STATE_SNAPSHOT) | {
        "__force_full_reset": True,
    }
    assert "Spot o in Futures" in out["reply"]

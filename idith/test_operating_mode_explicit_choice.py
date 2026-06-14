"""Test: operating_mode/strategy_id solo dopo scelta esplicita dell'utente."""

from __future__ import annotations

import copy

import pytest

from idith.app import _sync_config_state_operating_mode_from_reply
from idith.orchestrator import DEFAULT_PARAMS, handle_message


def _base_params(**overrides):
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(
        {
            "market_type": "futures",
            "symbol": "BTCUSDT",
        }
    )
    params.update(overrides)
    return params


def _state(*, step: str, params: dict, pending_timeframe: str | None = None) -> dict:
    return {
        "config_status": "in_progress",
        "config_state": {
            "step": step,
            "params": params,
            "error_count": {},
            "pending_sl_confirmation": None,
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
            "pending_symbol_confirmation": None,
            "pending_timeframe_confirmation": pending_timeframe,
        },
    }


def test_timeframe_ok_advances_without_operating_mode_preset():
    """Conferma timeframe consigliato: solo timeframe salvato, operating_mode ancora None."""
    state = _state(step="timeframe", params=_base_params(), pending_timeframe="15m")
    out = handle_message("ok", state, [])
    cs = out["state"]["config_state"]
    p = cs["params"]

    assert p["timeframe"] == "15m"
    assert p["operating_mode"] is None
    assert p["strategy_id"] is None
    assert p["strategy_params"] is None
    assert cs["step"] == "operating_mode"
    assert cs["pending_timeframe_confirmation"] is None
    assert "Scegli la modalità operativa" in out["reply"]

    synced = _sync_config_state_operating_mode_from_reply(copy.deepcopy(cs), out["reply"])
    sp = synced["params"]
    assert sp["operating_mode"] is None
    assert sp["strategy_id"] is None
    assert sp["strategy_params"] is None


def test_operating_mode_explain_stays_on_step_without_saving():
    """Domanda informativa su operating_mode: spiega, resta sullo step, non salva."""
    state = _state(step="operating_mode", params=_base_params(timeframe="15m"))
    out = handle_message("explain the difference to me", state, [])
    cs = out["state"]["config_state"]
    p = cs["params"]

    assert cs["step"] == "operating_mode"
    assert p["operating_mode"] is None
    assert p["strategy_id"] is None
    assert p["strategy_params"] is None
    assert "Aggressiva" in out["reply"]
    assert "Equilibrata" in out["reply"]
    assert "Selettiva" in out["reply"]
    assert "Scegli la modalità operativa" in out["reply"]


def test_operating_mode_explicit_aggressive_sets_preset_and_advances():
    """Scelta esplicita aggressive: preset aggressiva + strategy_id 1."""
    state = _state(step="operating_mode", params=_base_params(timeframe="15m"))
    out = handle_message("aggressive", state, [])
    cs = out["state"]["config_state"]
    p = cs["params"]

    assert p["operating_mode"] == "aggressiva"
    assert p["strategy_id"] == "1"
    assert isinstance(p["strategy_params"], dict)
    assert p["strategy_params"].get("rsi_period") == 5
    assert cs["step"] != "operating_mode"

    synced = _sync_config_state_operating_mode_from_reply(copy.deepcopy(cs), out["reply"])
    assert synced["params"]["operating_mode"] == "aggressiva"
    assert synced["params"]["strategy_id"] == "1"


@pytest.mark.parametrize(
    "text",
    [
        "i dont know",
        "non so",
        "recommend one",
        "choose for me",
    ],
)
def test_operating_mode_recommendation_does_not_save_mode(text: str):
    state = _state(step="operating_mode", params=_base_params(timeframe="15m"))
    out = handle_message(text, state, [])
    cs = out["state"]["config_state"]
    p = cs["params"]
    assert cs["step"] == "operating_mode"
    assert p["operating_mode"] is None
    assert p["strategy_id"] is None

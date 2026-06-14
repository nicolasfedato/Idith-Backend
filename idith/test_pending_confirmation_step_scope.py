"""Test: conferme generiche applicate solo alla pending dello step corrente."""

from __future__ import annotations

import copy
import logging

import pytest

from idith import validators
from idith.orchestrator import (
    DEFAULT_PARAMS,
    _make_pending_symbol_confirmation,
    handle_message,
)


@pytest.fixture
def listed_symbols(monkeypatch):
    def _listed(_client, market_type: str, symbol: str) -> bool:
        return symbol in {"BTCUSDT", "ETHUSDT", "SOLUSDT"} and market_type in ("spot", "futures")

    monkeypatch.setattr(validators, "is_symbol_listed", _listed)
    return _listed


def test_ok_on_timeframe_confirms_tf_not_stale_symbol_pending(listed_symbols, caplog):
    """ok su step=timeframe conferma solo pending_timeframe, non pending_symbol obsoleta."""
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update({"market_type": "futures", "symbol": "SOLUSDT"})
    state = {
        "config_status": "in_progress",
        "config_state": {
            "step": "timeframe",
            "params": params,
            "error_count": {},
            "pending_sl_confirmation": None,
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
            "pending_symbol_confirmation": _make_pending_symbol_confirmation("BTCUSDT"),
            "pending_timeframe_confirmation": "15m",
        },
    }

    with caplog.at_level(logging.INFO):
        caplog.clear()
        out = handle_message("ok", state, [])

    cs = out["state"]["config_state"]
    p = cs["params"]

    assert p["symbol"] == "SOLUSDT"
    assert p["timeframe"] == "15m"
    assert cs["pending_symbol_confirmation"] is None
    assert cs["pending_timeframe_confirmation"] is None
    assert cs["step"] == "operating_mode"
    assert "PAIR_RECOMMEND_CONFIRM" not in caplog.text
    assert "TF_RECOMMEND_CONFIRM" in caplog.text


def test_explicit_symbol_insert_clears_stale_symbol_pending(listed_symbols):
    """Inserimento esplicito coppia: salva symbol e pulisce pending/suggested obsoleti."""
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update({"market_type": "futures"})
    state = {
        "config_status": "in_progress",
        "config_state": {
            "step": "symbol",
            "params": params,
            "error_count": {},
            "pending_sl_confirmation": None,
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
            "pending_symbol_confirmation": _make_pending_symbol_confirmation("BTCUSDT"),
            "pending_timeframe_confirmation": None,
            "suggested_symbol": "BTCUSDT",
        },
    }

    out = handle_message("insert solusdt", state, [])
    cs = out["state"]["config_state"]
    p = cs["params"]

    assert p["symbol"] == "SOLUSDT"
    assert cs["pending_symbol_confirmation"] is None
    assert cs.get("suggested_symbol") is None
    assert cs["step"] != "symbol"

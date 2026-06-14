"""Test intent raccomandazione coppia EN/IT nello step symbol."""

from __future__ import annotations

import copy

import pytest

from idith import validators
from idith.app import _deep_merge_config_state
from idith.orchestrator import (
    DEFAULT_PARAMS,
    _is_high_volatility_pair_intent,
    _is_pair_recommendation_intent,
    _make_pending_symbol_confirmation,
    handle_message,
)


def _symbol_step_state(*, market_type: str = "futures") -> dict:
    params = copy.deepcopy(DEFAULT_PARAMS)
    params["market_type"] = market_type
    return {
        "config_status": "in_progress",
        "config_state": {
            "step": "symbol",
            "params": params,
            "error_count": {},
            "pending_sl_confirmation": None,
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
            "pending_symbol_confirmation": None,
        },
    }


@pytest.fixture
def listed_symbols(monkeypatch):
    def _listed(_client, market_type: str, symbol: str) -> bool:
        allowed = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        return symbol in allowed and market_type in ("spot", "futures")

    monkeypatch.setattr(validators, "is_symbol_listed", _listed)
    return _listed


@pytest.mark.parametrize(
    "text",
    [
        "recommend pair",
        "recommend me a pair",
        "suggest pair",
        "volatile pair",
        "high volatility pair",
        "pair with high volatility",
        "pair with a lot of volatility",
        "tell me a pair with a lot of volatility",
        "coppia volatile",
        "consigliami una coppia",
        "che coppia mi consigli",
    ],
)
def test_pair_recommendation_intent_detected(text: str):
    assert _is_pair_recommendation_intent(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "BTCUSDT",
        "metti sl 2%",
        "what is volatility",
    ],
)
def test_pair_recommendation_intent_not_detected(text: str):
    assert _is_pair_recommendation_intent(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "pair with high volatility",
        "pair with a lot of volatility",
        "volatile pair",
        "coppia volatile",
    ],
)
def test_high_volatility_pair_intent(text: str):
    assert _is_high_volatility_pair_intent(text) is True


def test_recommend_me_a_pair_proposes_btcusdt(listed_symbols):
    state = _symbol_step_state(market_type="spot")
    out = handle_message("Recommend me a pair", state, [])
    pending = out["state"]["config_state"]["pending_symbol_confirmation"]
    assert pending == _make_pending_symbol_confirmation("BTCUSDT")
    assert "BTCUSDT" in out["reply"]
    assert out.get("skip_llm") is True


def test_recommend_me_a_pair_yes_saves_symbol(listed_symbols):
    state = _symbol_step_state(market_type="spot")
    out1 = handle_message("Recommend me a pair", state, [])
    out2 = handle_message("yes", out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2["params"]["symbol"] == "BTCUSDT"
    assert cs2["pending_symbol_confirmation"] is None
    assert cs2["step"] != "symbol"
    assert "Che coppia vuoi usare?" not in out2["reply"]


def test_pending_symbol_survives_config_state_merge(listed_symbols):
    state = _symbol_step_state(market_type="spot")
    out1 = handle_message("Recommend me a pair", state, [])
    merged = _deep_merge_config_state({}, out1["state"]["config_state"])
    assert merged.get("pending_symbol_confirmation") == _make_pending_symbol_confirmation("BTCUSDT")
    state2 = {"config_status": "in_progress", "config_state": merged}
    out2 = handle_message("yes", state2, [])
    cs2 = out2["state"]["config_state"]
    assert cs2["params"]["symbol"] == "BTCUSDT"
    assert "Che coppia vuoi usare?" not in out2["reply"]


def test_high_volatility_pair_suggests_solusdt(listed_symbols):
    state = _symbol_step_state(market_type="futures")
    out = handle_message("pair with high volatility", state, [])
    cs = out["state"]["config_state"]
    assert cs["pending_symbol_confirmation"] == _make_pending_symbol_confirmation("SOLUSDT")
    assert "SOLUSDT" in out["reply"]
    assert "Vuoi usare SOLUSDT?" in out["reply"]
    assert out.get("skip_llm") is True


def test_high_volatility_english_variant(listed_symbols):
    state = _symbol_step_state(market_type="futures")
    out = handle_message("tell me a pair with a lot of volatility", state, [])
    assert out["state"]["config_state"]["pending_symbol_confirmation"] == _make_pending_symbol_confirmation(
        "SOLUSDT"
    )
    assert "SOLUSDT" in out["reply"]


def test_pair_recommendation_confirm_yes_advances(listed_symbols):
    state = _symbol_step_state(market_type="futures")
    out1 = handle_message("pair with high volatility", state, [])
    out2 = handle_message("yes", out1["state"], [])
    cs2 = out2["state"]["config_state"]
    assert cs2["pending_symbol_confirmation"] is None
    assert cs2["params"]["symbol"] == "SOLUSDT"
    assert cs2["step"] != "symbol"


@pytest.mark.parametrize("confirm_word", ["yes", "sì", "confermo"])
def test_pair_recommendation_confirm_italian_tokens(confirm_word: str, listed_symbols):
    state = _symbol_step_state(market_type="futures")
    out1 = handle_message("consigliami una coppia", state, [])
    pending = out1["state"]["config_state"]["pending_symbol_confirmation"]["symbol"]
    assert pending in ("BTCUSDT", "ETHUSDT")
    out2 = handle_message(confirm_word, out1["state"], [])
    assert out2["state"]["config_state"]["params"]["symbol"] == pending


def test_spot_default_pair_recommendation(listed_symbols):
    state = _symbol_step_state(market_type="spot")
    out = handle_message("recommend pair", state, [])
    pending = out["state"]["config_state"]["pending_symbol_confirmation"]["symbol"]
    assert pending in ("BTCUSDT", "ETHUSDT")
    assert pending in out["reply"]


def test_high_volatility_falls_back_to_eth_when_sol_unlisted(monkeypatch):
    def _listed(_client, market_type: str, symbol: str) -> bool:
        return symbol in {"BTCUSDT", "ETHUSDT"} and market_type in ("spot", "futures")

    monkeypatch.setattr(validators, "is_symbol_listed", _listed)
    state = _symbol_step_state(market_type="spot")
    out = handle_message("volatile pair", state, [])
    assert out["state"]["config_state"]["pending_symbol_confirmation"] == _make_pending_symbol_confirmation(
        "ETHUSDT"
    )
    assert "ETHUSDT" in out["reply"]

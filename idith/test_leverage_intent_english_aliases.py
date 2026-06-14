"""Test alias inglesi remove leverage → stessa pipeline italiana (spot + leverage=None)."""

from __future__ import annotations

import pytest

from idith.orchestrator import (
    _detect_leverage_intent,
    _detect_remove_leverage_intent,
    _lev_intent_test_state,
    _normalize_lev_intent_text,
    handle_message,
)


@pytest.mark.parametrize(
    "english,italian_equivalent",
    [
        ("remove leverage", "togli leva"),
        ("delete leverage", "rimuovi leva"),
        ("clear leverage", "cancella leva"),
        ("no leverage", "senza leva"),
        ("without leverage", "senza leva"),
    ],
)
def test_english_remove_aliases_normalize_to_italian(english, italian_equivalent):
    assert italian_equivalent in _normalize_lev_intent_text(english)
    assert _detect_remove_leverage_intent(english)
    assert _detect_leverage_intent(english) == {"action": "remove"}


def test_remove_leverage_insert_aggressive_multiparam():
    state = _lev_intent_test_state(
        market_type="futures", leverage=10, step=None, config_status="complete"
    )
    out = handle_message("remove leverage, insert aggressive", state, [])
    p = out["state"]["config_state"]["params"]
    reply = out["reply"]
    assert p["leverage"] is None
    assert p["market_type"] == "spot"
    assert p["operating_mode"] == "aggressiva"
    assert p["strategy_id"] == "1"
    assert "Tipo di mercato: spot" in reply
    assert "Leva: —" in reply
    assert "Modalità operativa: aggressiva" in reply
    assert out.get("skip_llm") is True

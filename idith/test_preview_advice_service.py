"""Test unitari per preview_advice_service (detection, snapshot, fallback, struttura risposta)."""
from preview_advice_service import (
    CONFIRMATION_QUESTION,
    build_confirmation_suffix,
    build_preview_snapshot,
    build_rule_based_advice,
    compute_win_rate_pct,
    ensure_confirmation_suffix,
    is_preview_advice_request,
)


def test_advice_detection_positive():
    assert is_preview_advice_request("spiegami questa preview")
    assert is_preview_advice_request("Spiegami la preview")
    assert is_preview_advice_request("spiegami questa preview storica")
    assert is_preview_advice_request("perché questa preview è negativa?")
    assert is_preview_advice_request("perché ho perso?")
    assert is_preview_advice_request("come posso migliorare questa preview?")
    assert is_preview_advice_request("come posso migliorare?")
    assert is_preview_advice_request("come riduco le perdite?")
    assert is_preview_advice_request("riduci le perdite")
    assert is_preview_advice_request("consigliami i parametri")
    assert is_preview_advice_request("cosa devo cambiare?")
    assert is_preview_advice_request("come migliorare il risultato?")
    assert is_preview_advice_request("analizza l'ultima preview")


def test_advice_detection_negative():
    assert not is_preview_advice_request("fammi una preview")
    assert not is_preview_advice_request("storico preview")
    assert not is_preview_advice_request("mostrami le preview")
    assert not is_preview_advice_request("mostra preview")
    assert not is_preview_advice_request("backtest")
    assert not is_preview_advice_request("")
    assert not is_preview_advice_request("simula gli ultimi 30 giorni")


def test_compute_win_rate_pct():
    assert compute_win_rate_pct(38, 62) == 38.0
    assert compute_win_rate_pct(0, 0) is None
    assert compute_win_rate_pct(None, 10) is None


def test_build_preview_snapshot():
    row = {
        "created_at": "2026-06-06T21:41:00+00:00",
        "symbol": "BTCUSDT",
        "timeframe": "3m",
        "market_type": "futures",
        "operating_mode": "Aggressiva",
        "strategy_id": "default",
        "stop_loss_pct": 2.0,
        "take_profit_pct": 2.0,
        "leverage": 5.0,
        "risk_pct": 1.0,
        "pnl_pct": -8.4,
        "max_drawdown_pct": 12.1,
        "estimated_trades": 142,
        "closed_trades": 140,
        "wins": 53,
        "losses": 87,
    }
    snap = build_preview_snapshot(row)
    assert snap["symbol"] == "BTCUSDT"
    assert snap["timeframe"] == "3m"
    assert snap["sl_pct"] == 2.0
    assert snap["tp_pct"] == 2.0
    assert snap["pnl_pct"] == -8.4
    assert snap["win_rate_pct"] == 37.9
    assert snap["trades_count"] == 140
    assert snap["preview_date_rome"] == "06/06/2026 23:41"


def test_build_rule_based_advice_structure():
    snapshot = {
        "symbol": "BTCUSDT",
        "timeframe": "3m",
        "market_type": "futures",
        "operating_mode": "Aggressiva",
        "strategy_id": "default",
        "sl_pct": 2.0,
        "tp_pct": 2.0,
        "leverage": 5.0,
        "risk_pct": 1.0,
        "pnl_pct": -8.4,
        "win_rate_pct": 38.0,
        "max_drawdown_pct": 12.1,
        "trades_count": 140,
        "preview_date_rome": "06/06/2026 23:41",
    }
    text = build_rule_based_advice(snapshot)
    assert "Perché la preview è andata così" in text
    assert "Cosa proverei a cambiare" in text
    assert CONFIRMATION_QUESTION in text
    assert "-8.4%" in text
    assert "38" in text
    assert "140" in text
    assert "12.1%" in text
    assert "3m" in text
    assert "1." in text
    assert "2." in text


def test_ensure_confirmation_suffix():
    assert ensure_confirmation_suffix("test") == "test\n\n" + CONFIRMATION_QUESTION
    assert ensure_confirmation_suffix(CONFIRMATION_QUESTION) == CONFIRMATION_QUESTION
    assert CONFIRMATION_QUESTION in ensure_confirmation_suffix(
        "Cosa proverei a cambiare\n1. timeframe 15m\n\n" + CONFIRMATION_QUESTION
    )


def test_confirmation_suffix_only_advised_params():
    snapshot = {
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "market_type": "futures",
        "operating_mode": "aggressiva",
        "sl_pct": 8.5,
        "tp_pct": 10.5,
        "risk_pct": 10.5,
    }
    llm_reply = (
        "Perché la preview è andata così\n"
        "Risultato negativo con rischio elevato.\n\n"
        "Cosa proverei a cambiare\n"
        "1. riduci risk_pct da 10.5 a 2-3%\n"
        "2. passa da aggressiva a equilibrata/selettiva\n"
        "3. riduci SL a 4-5%\n"
        "4. prova timeframe 30m/1h\n"
    )
    suffix = build_confirmation_suffix(llm_reply, snapshot)
    assert "modifica questi parametri:" in suffix
    assert "- timeframe: 1h" in suffix
    assert "- modalità operativa: selettiva" in suffix
    assert "- stop loss: 4%" in suffix
    assert "- rischio per trade: 2%" in suffix
    assert "8.5%" not in suffix
    assert "10.5%" not in suffix
    assert "coppia:" not in suffix
    assert "take profit:" not in suffix


def test_confirmation_suffix_rule_based_excludes_unchanged():
    snapshot = {
        "symbol": "BTCUSDT",
        "timeframe": "3m",
        "market_type": "futures",
        "operating_mode": "Aggressiva",
        "strategy_id": "default",
        "sl_pct": 2.0,
        "tp_pct": 2.0,
        "leverage": 5.0,
        "risk_pct": 1.0,
        "pnl_pct": -8.4,
        "win_rate_pct": 38.0,
        "max_drawdown_pct": 12.1,
        "trades_count": 140,
        "preview_date_rome": "06/06/2026 23:41",
    }
    text = build_rule_based_advice(snapshot)
    params_block = text.split("modifica questi parametri:")[1]
    assert "BTCUSDT" not in params_block
    assert "- timeframe:" in params_block
    assert "- modalità operativa: selettiva" in params_block
    assert "- stop loss:" in params_block
    assert "- take profit:" in params_block
    assert "rischio per trade:" not in params_block


def test_advice_import_as_idith_package():
    try:
        import idith.preview_advice_service as mod
    except ModuleNotFoundError:
        return
    assert mod.is_preview_advice_request("spiegami questa preview")


if __name__ == "__main__":
    test_advice_detection_positive()
    test_advice_detection_negative()
    test_compute_win_rate_pct()
    test_build_preview_snapshot()
    test_build_rule_based_advice_structure()
    test_ensure_confirmation_suffix()
    test_confirmation_suffix_only_advised_params()
    test_confirmation_suffix_rule_based_excludes_unchanged()
    test_advice_import_as_idith_package()
    print("test_preview_advice_service: OK")

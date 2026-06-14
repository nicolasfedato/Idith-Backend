"""Test unitari per preview_service (detection, lookback, validazione config, formatter)."""
from preview_service import (
    _build_backtest_previews_row,
    extract_lookback,
    format_preview_done_reply,
    is_preview_request,
    validate_preview_config_params,
)


def test_preview_detection_positive():
    assert is_preview_request("fammi una preview")
    assert is_preview_request("preview ultimi 30 giorni")
    assert is_preview_request("anteprima storica")
    assert is_preview_request("backtest")
    assert is_preview_request("preview storica")
    assert is_preview_request("simulazione storica")
    assert is_preview_request("simula gli ultimi 7 giorni")
    assert is_preview_request("simula ultimi 30 giorni")
    assert is_preview_request("come si sarebbe comportato")
    assert is_preview_request("come avrebbe performato")


def test_preview_detection_simula_alone_negative():
    assert not is_preview_request("simula")
    assert not is_preview_request("Simula un bot")
    assert not is_preview_request("voglio simulare spot")


def test_preview_detection_simulazione_without_storica_negative():
    assert not is_preview_request("simulazione")
    assert not is_preview_request("fammi una simulazione")


def test_extract_lookback_user_specified():
    days, specified = extract_lookback("preview ultimi 30 giorni")
    assert days == 30
    assert specified is True

    days, specified = extract_lookback("30 giorni")
    assert days == 30
    assert specified is True

    days, specified = extract_lookback("simula gli ultimi 7 giorni")
    assert days == 7
    assert specified is True

    days, specified = extract_lookback("365 giorni")
    assert days == 365
    assert specified is True


def test_extract_lookback_not_specified():
    days, specified = extract_lookback("preview")
    assert days == 30
    assert specified is False

    days, specified = extract_lookback("fammi una preview")
    assert specified is False


def test_validate_config_complete():
    params = {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "market_type": "spot",
        "sl": "2%",
        "tp": "3%",
    }
    assert validate_preview_config_params(params) == []


def test_validate_config_missing():
    missing = validate_preview_config_params(
        {"symbol": "BTCUSDT", "market_type": "spot", "sl": "2%"}
    )
    assert "timeframe" in missing
    assert "tp" in missing


def test_extract_lookback_two_years():
    days, specified = extract_lookback("preview ultimi 2 anni")
    assert days == 730
    assert specified is True


def test_format_preview_done_reply_full():
    payload = {
        "effective_lookback_days": 7,
        "symbol": "BTCUSDT",
        "timeframe": "3m",
        "market_type": "linear",
        "operating_mode": "aggressiva",
        "leverage": 3,
        "risk_pct": 2,
        "sl_pct": 5.0,
        "tp_pct": 10.0,
        "pnl_pct": -20.0,
        "simulated_trades": 5,
        "wins": 0,
        "losses": 5,
        "max_drawdown_pct": -18.6,
    }
    text = format_preview_done_reply(payload, 30)
    assert "📊 Preview indicativa — ultimi 7 giorni" in text
    assert "BTCUSDT · 3m · futures" in text
    assert "Strategia: aggressiva" in text
    assert "Leva 3x · Rischio 2%" in text
    assert "SL 5% · TP 10%" in text
    assert "Risultato stimato: -20.0%" in text
    assert "Operazioni simulate: 5" in text
    assert "Win rate: 0%" in text
    assert "Drawdown massimo: -18.6%" in text
    assert "Positive:" not in text
    assert "Negative:" not in text
    assert "Nota:" not in text
    assert "Capitale" not in text
    assert "Preview basata su dati storici Bybit." in text
    assert "Non garantisce risultati futuri." in text


def test_format_preview_done_reply_leverage_from_config():
    """Runner DONE spesso non include leva/rischio: li prendiamo da config_state."""
    payload = {
        "effective_lookback_days": 7,
        "symbol": "BTCUSDT",
        "timeframe": "3m",
        "market_type": "linear",
        "operating_mode": "aggressiva",
        "sl_pct": 5.0,
        "tp_pct": 10.0,
        "pnl_pct": -20.0,
        "simulated_trades": 5,
        "wins": 0,
        "losses": 5,
        "max_drawdown_pct": -18.6,
    }
    config_params = {"leverage": 3, "risk_pct": 2}
    text = format_preview_done_reply(payload, 30, config_params=config_params)
    assert "Leva 3x · Rischio 2%" in text


def test_format_preview_done_reply_minimal():
    text = format_preview_done_reply({"effective_lookback_days": 7, "pnl_pct": 5.0}, 30)
    assert "📊 Preview indicativa — ultimi 7 giorni" in text
    assert "Risultato stimato: +5.0%" in text
    assert "Operazioni simulate:" not in text
    assert "Strategia:" not in text


def test_build_backtest_previews_row_maps_columns():
    payload = {
        "effective_lookback_days": 7,
        "symbol": "BTCUSDT",
        "timeframe": "3m",
        "market_type": "linear",
        "operating_mode": "aggressiva",
        "sl_pct": 5.0,
        "tp_pct": 10.0,
        "simulated_trades": 5,
        "closed_trades_count": 4,
        "wins": 1,
        "losses": 3,
        "pnl_pct": -20.0,
        "max_drawdown_pct": -18.6,
        "capital_usdt": 1000.0,
        "final_capital_usdt": 800.0,
        "capital_source": "default_1000",
    }
    config_params = {
        "leverage": 3,
        "risk_pct": 2,
        "strategy_id": "1",
        "strategy_params": {"rsi_period": 5},
    }
    row = _build_backtest_previews_row(
        payload,
        user_id="user-uuid",
        chat_id="chat-uuid",
        command_id="cmd-uuid",
        config_params=config_params,
    )
    assert row["user_id"] == "user-uuid"
    assert row["chat_id"] == "chat-uuid"
    assert row["symbol"] == "BTCUSDT"
    assert row["market_type"] == "linear"
    assert row["timeframe"] == "3m"
    assert row["operating_mode"] == "aggressiva"
    assert row["strategy_id"] == "1"
    assert row["stop_loss_pct"] == 5.0
    assert row["take_profit_pct"] == 10.0
    assert row["leverage"] == 3.0
    assert row["risk_pct"] == 2.0
    assert row["lookback_days"] == 7
    assert row["estimated_trades"] == 5
    assert row["closed_trades"] == 4
    assert row["wins"] == 1
    assert row["losses"] == 3
    assert row["pnl_pct"] == -20.0
    assert row["max_drawdown_pct"] == -18.6
    assert row["capital_simulated"] == 1000.0
    assert row["final_capital"] == 800.0
    assert row["capital_source"] == "default_1000"
    assert row["preview_payload"]["command_id"] == "cmd-uuid"
    assert row["strategy_params"] == {"rsi_period": 5}


if __name__ == "__main__":
    test_preview_detection_positive()
    test_preview_detection_simula_alone_negative()
    test_preview_detection_simulazione_without_storica_negative()
    test_extract_lookback_user_specified()
    test_extract_lookback_not_specified()
    test_validate_config_complete()
    test_validate_config_missing()
    test_extract_lookback_two_years()
    test_format_preview_done_reply_full()
    test_format_preview_done_reply_leverage_from_config()
    test_format_preview_done_reply_minimal()
    test_build_backtest_previews_row_maps_columns()
    print("test_preview_service: OK")

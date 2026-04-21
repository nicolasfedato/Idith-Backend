from generate_bybit_bot import generate_bybit_bot, save_prototype

bp = {
    "name": "futures",
    "mode": "spot",
    "pairs": "BTCUSDT",
    "timeframe": "15m",
    "strategy": "trend (EMA 50/200 + filtro volumi)",
    "risk": {"risk_pct": "1%", "sl": "ATR 2x", "tp": "RR 1.5x", "leverage": "n/a"},
    "schedule": "24/7",
    "notifications": "sì",
    "environment": "demo/testnet",
    "warmup_trade": "no",
    "account_known": True,
    "has_api_keys": True,
    "keys_env": None,
}

code = generate_bybit_bot(bp)
path = save_prototype(code, name_hint=bp.get("name","bybit_bot"))
print("✅ Prototipo generato e salvato come:", path)

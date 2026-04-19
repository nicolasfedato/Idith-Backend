# -*- coding: utf-8 -*-
# idith/runner_demo.py

from __future__ import annotations
import os, json
from typing import Dict, Any, List

from bybit_testnet import get_recent_prices, get_balance_demo
from signal_engine import decide_signals
from sizing import position_size

def run_demo(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Esegue una simulazione estremamente semplice:
    - scarica (stub) prezzi sintetici
    - genera segnali dal signal_engine
    - calcola una size approssimativa
    - ritorna un breve report
    """
    symbol = (config.get("pairs") or ["BTCUSDT"])[0] if isinstance(config.get("pairs"), list) else (config.get("pairs") or "BTCUSDT")
    timeframe = config.get("timeframe", "1h")
    strategy = config.get("strategy", "trend")
    indicators = config.get("indicators", [])
    risk_pct = config.get("risk", {}).get("pct") if isinstance(config.get("risk"), dict) else config.get("risk_pct", "1%")
    sl = (config.get("risk", {}).get("atr_mult") if isinstance(config.get("risk"), dict) else config.get("sl", "ATR 2x"))
    lev = (config.get("risk", {}).get("leverage") if isinstance(config.get("risk"), dict) else config.get("leverage", "1x"))

    prices = get_recent_prices(symbol, limit=300)
    signals = decide_signals(prices, indicators, strategy)

    bal = get_balance_demo()
    sizing = position_size(balance_usdt=bal["USDT"], risk_pct=risk_pct, atr_mult=sl, leverage=lev)

    report = {
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "last_price": prices[-1] if prices else None,
        "last_signal": signals[-1] if signals else "hold",
        "sizing": sizing,
        "samples": len(prices),
    }
    return report

if __name__ == "__main__":
    # Esempio di config minimo per test manuale
    example = {
        "env": "demo",
        "mode": "spot",
        "pairs": ["BTCUSDT"],
        "timeframe": "1h",
        "strategy": "trend",
        "indicators": ["EMA"],
        "risk_pct": "1%",
        "sl": "ATR 2x",
        "tp": "RR 1.5x",
        "leverage": "2x",
        "schedule": "24/7",
        "notify": "no",
        "warmup": "si"
    }
    out = run_demo(example)
    print(json.dumps(out, ensure_ascii=False, indent=2))

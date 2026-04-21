# quick_suite_policy.py — test "tono vivo", bullet points e anti-ripetizione
# Uso:  python quick_suite_policy.py
from __future__ import annotations
import importlib
import os
import sys
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from . import policy_engine
from .memory_manager import memory

def run_case(title: str, prompts: List[str]):
    print("\n" + "="*72)
    print(title)
    print("="*72)
    prof = memory.load_profile()
    for i, msg in enumerate(prompts, start=1):
        print(f"\n[{i:02d}] USER: {msg}")
        out = policy_engine.reply_for(msg, profile=prof)
        print(f"BOT:\n{out}")

def main():
    memory.update({
        "tone": "amichevole_pro",
        "language": "it",
        "exchange": "bybit_testnet",
        "last_timeframe": None,
        "last_strategy": None,
    })

    run_case("SPIEGAZIONI DIDATTICHE (indicatori/strategie)", [
        "mi spieghi come funziona EMA?",
        "spiega RSI",
        "come funziona la strategia trend",
        "spiega breakout",
    ])

    run_case("PARSING ABBREVIATO + CONFERMA (tf, rr, atr, risk)", [
        "BTCUSDT tf 1h trend rr 1.5x sl atr 2x risk 1%",
        "ETHUSDT 15m mean reversion rr 2x risk 0.5%",
    ])

    run_case("UTENTE INESPERTO (messaggi incompleti/sporchi)", [
        "boh metti roba che va bene tu",
        "btc 1h boh fai te",
        "non so cosa vuol dire rr…",
        "fammi andare piano che non capisco",
    ])

    run_case("ANTI-RIPETIZIONE (stessa richiesta 3 volte)", [
        "spiega rsi",
        "spiega rsi",
        "spiega rsi",
    ])

    memory.set("last_timeframe", "15m")
    memory.set("last_strategy", "trend")
    run_case("RIPARTENZA DALLA MEMORIA (last_timeframe/last_strategy)", [
        "ok, da dove riprendiamo?",
        "non ho capito niente, fai tu",
    ])

if __name__ == "__main__":
    main()

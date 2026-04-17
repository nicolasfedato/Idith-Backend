# quick_test_policy.py
from . import policy_engine
from .memory_manager import memory

print("Profile start:", memory.load_profile())

print("\n--- Spiegazione EMA ---")
print(policy_engine.reply_for("mi spieghi come funziona EMA?", profile=memory.load_profile()))

print("\n--- Parse abbreviato (+ conferma) ---")
print(policy_engine.reply_for("BTCUSDT tf 1h trend rr 1.5x sl atr 2x risk 1%", profile=memory.load_profile()))

print("\n--- Richiamo ultime scelte ---")
memory.set("last_timeframe","15m")
memory.set("last_strategy","trend")
print(policy_engine.reply_for("ok", profile=memory.load_profile()))

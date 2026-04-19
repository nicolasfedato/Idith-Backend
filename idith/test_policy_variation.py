# test_policy_variation.py
from .policy_engine import reply_for

cases = [
    "spiega ema",
    "spiega rsi",
    "spiega breakout",
    "BTCUSDT 1h trend rr 1.5x sl atr 2x risk 1%",
    "sono inesperto, aiutami",
    "da dove riprendiamo?",
    "ok",
]

for i in range(3): # ripeti per vedere variazioni
    print(f"\n=== Round {i+1} ===")
    for c in cases:
        print("\nUSER:", c)
        print("BOT:", reply_for(c))

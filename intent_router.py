from __future__ import annotations

import re
from typing import Dict, Any

# This module MUST NOT crash if optional lexicon files are missing.

# Optional external lexicon (if you have it, we use it; otherwise we fallback)
try:
    from intent_lexicon import INTENT_KEYWORDS  # type: ignore
except Exception:
    INTENT_KEYWORDS = {
        "greet": ["ciao", "buongiorno", "buonasera", "hey", "hello"],
        "open_long": ["open long", "apri long", "long", "compra", "buy"],
        "open_short": ["open short", "apri short", "short", "vendi", "sell"],
        "close_position": ["chiudi", "close", "close position", "chiudi posizione"],
    }

PAIR_RE = re.compile(r"\b([A-Z]{3,10}USDT)\b", re.IGNORECASE)
TF_RE = re.compile(r"\b(1m|3m|5m|15m|30m|1h|2h|4h|6h|12h|1d)\b", re.IGNORECASE)
QTY_RE = re.compile(r"\b(qty|quantita|quantità)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)\b", re.IGNORECASE)

def classify_intent(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return "unknown"

    for intent, kws in INTENT_KEYWORDS.items():
        for kw in kws:
            if kw in t:
                return intent

    # Simple heuristics
    if "btc" in t or "eth" in t or "usdt" in t:
        return "unknown"
    return "unknown"

def extract_entities(text: str) -> Dict[str, Any]:
    t = (text or "").strip()
    out: Dict[str, Any] = {}

    m = PAIR_RE.search(t)
    if m:
        out["pair"] = m.group(1).upper()

    m = TF_RE.search(t)
    if m:
        out["timeframe"] = m.group(1).lower()

    m = QTY_RE.search(t)
    if m:
        out["qty"] = float(m.group(2))

    # Extra: detect "trend" word
    if re.search(r"\btrend\b", t, re.IGNORECASE):
        out["strategy"] = "trend"

    return out

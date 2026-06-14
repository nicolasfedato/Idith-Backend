# -*- coding: utf-8 -*-
# idith/sizing.py

from __future__ import annotations
from typing import Dict, Any, Tuple

def position_size(balance_usdt: float, risk_pct: str, atr_mult: str, leverage: str) -> Dict[str, Any]:
    """
    Calcolo super semplificato per DEMO.
    - risk_pct es. "1%"
    - atr_mult es. "ATR 2x" (usato solo come meta per demo)
    - leverage es. "2x"
    """
    try:
        from .text_normalize_user_numbers import parse_config_float

        r = parse_config_float(risk_pct)
        if r is None:
            raise ValueError(f"risk_pct non valido: {risk_pct!r}")
    except Exception:
        r = 1.0
    lev = 1.0
    try:
        if str(leverage).lower().endswith("x"):
            lev = float(str(leverage)[:-1])
        else:
            lev = float(leverage)
    except Exception:
        lev = 1.0
    risk_amount = balance_usdt * (r/100.0)
    notional = risk_amount * lev * 10  # euristica demo
    qty = notional  # in USDT equivalenti (senza prezzo base, per semplicità)
    return {
        "risk_amount_usdt": round(risk_amount, 2),
        "leverage": leverage,
        "qty_notional": round(qty, 2),
        "atr_meta": atr_mult,
    }

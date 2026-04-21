# -*- coding: utf-8 -*-
# idith/bybit_testnet.py

"""
Stub di sola lettura per Bybit Testnet.
Per una vera integrazione, sostituire le funzioni con chiamate HTTP/SDK.
"""

from __future__ import annotations
from typing import List, Dict, Any

def get_recent_prices(symbol: str, limit: int = 200) -> List[float]:
    """
    Placeholder: genera una serie sintetica crescente con rumore.
    Sostituisci con fetch reale dalla testnet.
    """
    import math, random
    base = 100.0
    series = []
    for i in range(limit):
        base += math.sin(i/10.0) * 0.5 + random.uniform(-0.2, 0.2)
        series.append(round(base, 2))
    return series

def get_balance_demo() -> Dict[str, Any]:
    return {"USDT": 1000.0}

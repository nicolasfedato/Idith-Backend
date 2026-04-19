# -*- coding: utf-8 -*-
# idith/signal_engine.py

from __future__ import annotations
from typing import List, Dict, Any, Optional
import math

def ema(series: List[float], period: int) -> List[Optional[float]]:
    """Semplice EMA per demo/backtest. Ritorna lista con None per i primi periodi."""
    if period <= 0 or not series:
        return [None] * len(series)
    k = 2.0 / (period + 1)
    out: List[Optional[float]] = []
    ema_val: Optional[float] = None
    for i, p in enumerate(series):
        if ema_val is None:
            if i < period - 1:
                out.append(None); continue
            seed = sum(series[:period]) / period
            ema_val = seed
            out.append(seed)
        else:
            ema_val = p * k + ema_val * (1 - k)
            out.append(ema_val)
    return out

def rsi(series: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI base per demo. Non ottimizzato, ma sufficiente per test rapidi."""
    if period <= 0 or len(series) < period + 1:
        return [None]*len(series)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(series)):
        change = series[i] - series[i-1]
        gains.append(max(0.0, change))
        losses.append(max(0.0, -change))
    out: List[Optional[float]] = [None]*(period)
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    if avg_loss == 0:
        out += [100.0]
    else:
        rs = avg_gain / avg_loss
        out += [100.0 - (100.0 / (1+rs))]
    for i in range(period+1, len(series)):
        avg_gain = (avg_gain*(period-1) + gains[i]) / period
        avg_loss = (avg_loss*(period-1) + losses[i]) / period
        if avg_loss == 0:
            out.append(100.0)
        else:
            rs = avg_gain / avg_loss
            out.append(100.0 - (100.0 / (1+rs)))
    # pad head to length
    while len(out) < len(series):
        out.insert(0, None)
    return out[:len(series)]

def decide_signals(prices: List[float], indicators: List[str], strategy: str) -> List[str]:
    """
    Genera segnali semplici per DEMO:
    - Strategy 'trend' con EMA(50): buy quando close > ema50; sell quando close < ema50.
    - Strategy 'reversion' con RSI(14): buy quando RSI<30; sell quando RSI>70.
    - Strategy 'breakout': buy quando close fa nuovo massimo 20-bar; sell quando fa nuovo minimo 20-bar.
    Ritorna lista di 'buy'/'sell'/'hold' per ogni barra.
    """
    n = len(prices)
    sig = ["hold"] * n
    if n == 0:
        return sig

    if strategy == "trend":
        ema50 = ema(prices, 50)
        for i in range(n):
            e = ema50[i]
            if e is None: 
                sig[i] = "hold"; continue
            sig[i] = "buy" if prices[i] > e else "sell" if prices[i] < e else "hold"

    elif strategy == "reversion":
        r = rsi(prices, 14)
        for i in range(n):
            rv = r[i]
            if rv is None:
                sig[i] = "hold"; continue
            if rv < 30: sig[i] = "buy"
            elif rv > 70: sig[i] = "sell"
            else: sig[i] = "hold"

    elif strategy == "breakout":
        lookback = 20
        for i in range(n):
            if i < lookback:
                sig[i] = "hold"; continue
            window = prices[i-lookback:i]
            if prices[i] > max(window):
                sig[i] = "buy"
            elif prices[i] < min(window):
                sig[i] = "sell"
            else:
                sig[i] = "hold"
    else:
        # fallback neutro
        sig = ["hold"] * n

    return sig


# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Any, Dict, List, Tuple
import re

# In questa versione (piano FREE) limitiamo gli indicatori selezionabili a:
# - EMA
# - RSI
#
# Altri indicatori (MACD, Bollinger, ATR, ecc.) saranno disponibili nel piano PRO.
# Se l'utente prova a inserirli, verrà mostrato un avviso e verranno ignorati.

FIELDS: List[str] = [
    "mode", "pair", "timeframe", "strategy",
    "risk_pct", "sl", "tp", "leverage",
    "schedule", "notify", "env",
    "warmup", "indicators"
]

DEFAULTS: Dict[str, Any] = {
    "mode": "spot",
    "pair": "BTCUSDT",
    "timeframe": "15m",
    "strategy": "trend",
    "risk_pct": "1%",
    "sl": "ATR 2x",
    "tp": "RR 1.5x",
    "leverage": "1x",
    "schedule": "24/7",
    "notify": "no",
    "env": "demo",
    "warmup": "si",
    "indicators": [],
}

ALLOWED = {
    "mode": ["spot", "futures"],
    "strategy": ["trend", "breakout", "reversion"],
    "env": ["demo", "live"],
    "notify": ["si", "no"],
    "warmup": ["si", "no"],
    "timeframe": ["1m","3m","5m","15m","30m","45m","1h","2h","4h","6h","12h","1d","1w","1M"],
}

# SOLO indicatori permessi nel piano FREE
SUPPORTED_INDICATORS = ["EMA", "RSI"]

# Alias che convergono solo sugli indicatori supportati
INDICATOR_ALIASES = {
    "ema": "EMA",
    "sma": "EMA",   # se l'utente scrive SMA la mappiamo su EMA e poi lo avvisiamo
    "ma": "EMA",
    "rsi": "RSI",
    "rse": "RSI",
}

SYN_FIELD = {
    "modalita": "mode","modalità": "mode","mode": "mode",
    "pair": "pair","pairs": "pair","coppia": "pair","coppie": "pair","symbol": "pair",
    "tf": "timeframe","time frame": "timeframe","timeframe": "timeframe",
    "strategia": "strategy","strategy": "strategy",
    "rischio": "risk_pct","risk": "risk_pct","risk_pct": "risk_pct",
    "sl": "sl","stop": "sl","stoploss": "sl","stop-loss": "sl",
    "tp": "tp","take": "tp","takeprofit": "tp","take-profit": "tp",
    "leva": "leverage","leverage": "leverage",
    "operativita": "schedule","operatività": "schedule","schedule": "schedule","h24": "schedule",
    "notifiche": "notify","notify": "notify",
    "ambiente": "env","env": "env","testnet": "env",
    "warmup": "warmup","riscaldamento": "warmup",
    "indicatori": "indicators","indicators": "indicators"
}

SYN_VALUE = {
    "sì": "si","yes": "si","y": "si","on": "si","true": "si","enable": "si","attiva": "si",
    "no": "no","off": "no","false": "no","disable": "no","disattiva": "no",
    "mean reversion": "reversion","revert": "reversion",
    "demo/testnet": "demo","testnet": "demo"
}

def _compact(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()

def _norm_bool(s: Any) -> str|None:
    v = _compact(s).lower()
    if v in {"si","sì","yes","y","on","true","enable","attiva"}:
        return "si"
    if v in {"no","off","false","disable","disattiva"}:
        return "no"
    return None

def _norm_percent(s: Any) -> str|None:
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*%?\s*$", str(s))
    if not m:
        return None
    return f"{m.group(1).replace(',','.')}%"

def _norm_rr(s: Any) -> str|None:
    m = re.search(r"(\d+(?:[.,][0-9]+)?)", str(s))
    if not m:
        return None
    return f"RR {m.group(1).replace(',','.')}x"

def _norm_atr(s: Any) -> str|None:
    m = re.search(r"(\d+(?:[.,][0-9]+)?)", str(s))
    if not m:
        return None
    return f"ATR {m.group(1).replace(',','.')}x"

def _norm_leverage(s: Any) -> str|None:
    v = _compact(s).lower()
    v = re.sub(r"\b(\d+)\s*x\b", r"\1x", v)
    if re.fullmatch(r"\d+x", v):
        return v
    if v in {"n/a","na","none","0x"}:
        return "n/a"
    return None

def _norm_timeframe(s: Any) -> str|None:
    v = _compact(s).lower()
    v = v.replace(" h", "h").replace(" m", "m")
    v = SYN_VALUE.get(v, v)
    if re.fullmatch(r"[1-9]\d{0,3}[mhdwM]", v):
        return v
    return None

def _norm_pair(s: Any) -> str|None:
    v = _compact(s).upper().replace("/", "")
    if re.fullmatch(r"[A-Z]{3,5}USDT", v):
        return v
    return None

def _norm_indicators(val: Any) -> Tuple[List[str], List[str]]:
    """Normalizza la lista di indicatori.

    Restituisce:
      - lista di indicatori validi (limitata a SUPPORTED_INDICATORS)
      - lista di nomi che l'utente ha chiesto ma che NON sono disponibili nel piano attuale
    """
    if val is None:
        return [], []
    if isinstance(val, list):
        raw = val
    else:
        raw = re.split(r"[,\s]+", str(val))

    out: List[str] = []
    dropped: List[str] = []
    seen = set()

    for w in raw:
        original = _compact(w)
        if not original:
            continue

        key = original.lower()
        key = INDICATOR_ALIASES.get(key, key)

        up = key.upper()
        if up in SUPPORTED_INDICATORS:
            if up not in seen:
                seen.add(up)
                out.append(up)
        else:
            dropped.append(original)

    return out, dropped

def canonical_field(token: str) -> str|None:
    t = _compact(token).lower()
    t = SYN_FIELD.get(t, t)
    return t if t in FIELDS else None

def normalize_value(val: Any) -> str:
    s = _compact(val)
    s = s.replace(" %", "%")
    s = re.sub(r"\b(\d+)\s*x\b", r"\1x", s)
    s = re.sub(r"\brr\s*([0-9]+(?:[.,][0-9]+)?)\s*x?\b", r"RR \1x", s, flags=re.I)
    s = re.sub(r"\batr\s*([0-9]+(?:[.,][0-9]+)?)\s*x?\b", r"ATR \1x", s, flags=re.I)
    return SYN_VALUE.get(s.lower(), s)

def coerce_and_validate(ans: Dict[str, Any]) -> tuple[Dict[str, Any], List[str], List[str]]:
    a = dict(DEFAULTS)
    a.update(ans or {})
    warnings: List[str] = []
    errors: List[str] = []

    for k in ("mode","env","strategy","notify","warmup"):
        v = a.get(k)
        if v is None:
            continue
        v = normalize_value(v)
        if k in ("notify","warmup"):
            bv = _norm_bool(v)
            if bv is None:
                errors.append(f"Valore non valido per {k}: {v}. Usa sì/no.")
            else:
                a[k] = bv
        else:
            if k in ALLOWED and v not in ALLOWED[k]:
                errors.append(f"Valore non ammesso per {k}: {v}.")
            else:
                a[k] = v

    tf = a.get("timeframe")
    if tf:
        tfn = _norm_timeframe(tf)
        if not tfn or (tfn not in ALLOWED["timeframe"]):
            errors.append(f"Timeframe non valido: {tf}. Esempi: 15m, 1h, 4h, 1d.")
        else:
            a["timeframe"] = tfn

    rp = a.get("risk_pct")
    rpn = _norm_percent(rp)
    if not rpn:
        errors.append(f"Rischio non valido: {rp}. Esempi: 1%, 0.5%.")
    else:
        a["risk_pct"] = rpn

    sl = a.get("sl")
    tp = a.get("tp")
    sln = _norm_percent(sl) or _norm_atr(sl)
    tpn = _norm_percent(tp) or _norm_rr(tp)

    if not sln:
        errors.append(f"Stop-loss non valido: {sl}. Esempi: ATR 2x oppure 1.0%.")
    else:
        a["sl"] = sln

    if not tpn:
        errors.append(f"Take-profit non valido: {tp}. Esempi: RR 1.5x oppure 3%.")
    else:
        a["tp"] = tpn

    lev = a.get("leverage", "1x")
    ln = _norm_leverage(lev)
    if a.get("mode", "spot") == "futures":
        if not ln:
            errors.append(f"Leva non valida: {lev}. Esempi: 1x, 2x, 3x.")
        else:
            a["leverage"] = ln
    else:
        a["leverage"] = "1x"

    pr = a.get("pair")
    pn = _norm_pair(pr)
    if not pn:
        errors.append(f"Coppia non valida: {pr}. Esempio: BTCUSDT.")
    else:
        a["pair"] = pn

    inds_raw = a.get("indicators", [])
    inds, dropped = _norm_indicators(inds_raw)
    a["indicators"] = inds

    if dropped:
        lista = ", ".join(sorted(set(dropped)))
        warnings.append(
            "Alcuni indicatori non sono disponibili nel piano attuale e verranno ignorati "
            f"(puoi usare solo EMA e RSI in questa versione): {lista}."
        )

    strat = a.get("strategy")
    if inds:
        if strat == "reversion" and "EMA" in inds:
            warnings.append("EMA è tipico del trend-following; con reversion potrebbe essere incoerente.")
        if strat == "trend" and "RSI" in inds:
            warnings.append("RSI è spesso usato per condizioni di ipercomprato/ipervenduto; ok come supporto, ma valuta bene i livelli.")

    return a, warnings, errors

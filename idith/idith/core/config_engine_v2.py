"""
Motore di configurazione v2: estrazione campi da testo, merge con regole spot/futures,
prossimo campo mancante. Nessuna logica basata su step numerici.
"""

from __future__ import annotations

import re
from typing import Any

# Chiavi gestite dal motore (ordine per compute_next_missing)
_FIELD_ORDER = (
    "market_type",
    "symbol",
    "timeframe",
    "operating_mode",
    "sl",
    "tp",
    "risk_pct",
    "leverage",
)


def _parse_num(s: str) -> float:
    return float(s.replace(",", "."))


def extract_all_fields(user_text: str) -> dict[str, Any]:
    """
    Estrae dal testo solo i campi riconosciuti. Ritorna un dict con le sole chiavi trovate.
    """
    if not user_text:
        return {}

    raw = user_text.strip()
    lower = raw.lower()
    out: dict[str, Any] = {}

    # market_type
    if re.search(r"\bfutures\b|passa\s+a\s+futures", lower):
        out["market_type"] = "futures"
    elif re.search(r"\bspot\b", lower):
        out["market_type"] = "spot"

    # operating_mode (italiano)
    for mode in ("aggressiva", "equilibrata", "selettiva"):
        if re.search(rf"\b{re.escape(mode)}\b", lower):
            out["operating_mode"] = mode
            break

    # symbol: btcusdt, eth-usdt, SOL / USDT
    sym = re.search(
        r"\b([a-z]{2,10})[-/\s]+(usdt|usd|busd)\b",
        lower,
        re.IGNORECASE,
    )
    if sym:
        out["symbol"] = (sym.group(1) + sym.group(2)).upper()
    else:
        sym2 = re.search(r"\b([a-z]{2,10}usdt|usdc)\b", lower, re.IGNORECASE)
        if sym2:
            out["symbol"] = sym2.group(1).upper()

    # timeframe: 15m, 1h, 4h, 1d, oppure "5 min", "1 hour"
    tf_compact = re.search(r"\b(\d+)\s*([mhd])\b", lower)
    if tf_compact:
        out["timeframe"] = f"{tf_compact.group(1)}{tf_compact.group(2).lower()}"
    else:
        tf_verbose = re.search(
            r"\b(\d+)\s*(?:min|mins|minutes?|h|hr|hrs|hours?|d|days?)\b",
            lower,
        )
        if tf_verbose:
            n = tf_verbose.group(1)
            span = tf_verbose.group(0)
            if re.search(r"\bmin", span):
                out["timeframe"] = f"{n}m"
            elif re.search(r"\bh(?:r|ours?)?\b", span) or " hr" in span:
                out["timeframe"] = f"{n}h"
            elif re.search(r"\bd(?:ays?)?\b", span):
                out["timeframe"] = f"{n}d"

    # stop loss / take profit (percentuali numeriche; varianti naturali IT/EN)
    sl_m = re.search(
        r"(?:\bsl\b|stop\s*loss)\s*[:]?\s*(\d+(?:[.,]\d+)?)\s*%?",
        lower,
    )
    if sl_m:
        out["sl"] = _parse_num(sl_m.group(1))

    tp_m = re.search(
        r"(?:\btp\b|take\s*profit)\s*[:]?\s*(\d+(?:[.,]\d+)?)\s*%?",
        lower,
    )
    if tp_m:
        out["tp"] = _parse_num(tp_m.group(1))

    # risk_pct: rischio / capitale rischio / risk (opz. %)
    risk_m = re.search(
        r"(?:\bcapitale\s+rischio\b|\brischio\b|\brisk(?:\s*pct)?\b)\s*[:]?\s*(\d+(?:[.,]\d+)?)\s*%?",
        lower,
    )
    if risk_m:
        out["risk_pct"] = _parse_num(risk_m.group(1))

    # leverage: leva/leverage + numero, opz. x; fallback Nx
    lev_m = re.search(
        r"(?:\bleva\b|\bleverage\b)\s*[:]?\s*(\d+(?:[.,]\d+)?)\s*x?\b",
        lower,
    )
    if lev_m:
        lev_val = _parse_num(lev_m.group(1))
        out["leverage"] = int(lev_val) if lev_val == int(lev_val) else lev_val
    else:
        x_m = re.search(r"\b(\d{1,3})\s*x\b", lower)
        if x_m:
            lev_val = _parse_num(x_m.group(1))
            out["leverage"] = int(lev_val) if lev_val == int(lev_val) else lev_val

    return out


def merge_valid_fields(config: dict, patch: dict) -> dict:
    """
    Applica il patch sovrascrivendo i campi presenti.
    - Valori None nel patch non sovrascrivono (non si azzerano campi validi).
    - Con market_type == \"spot\", leverage è None.
    - Se il patch porta leverage ma il mercato effettivo è spot, leverage non viene salvato dal patch.
    """
    out = dict(config)

    prospective_mt = out.get("market_type")
    if patch.get("market_type") is not None:
        prospective_mt = patch["market_type"]

    for key, value in patch.items():
        if value is None:
            continue
        if key == "leverage" and prospective_mt == "spot":
            continue
        out[key] = value

    if out.get("market_type") == "spot":
        out["leverage"] = None

    return out


def _is_missing(val: Any) -> bool:
    return val is None or val == ""


def compute_next_missing(config: dict) -> str | None:
    """
    Primo campo mancante nell'ordine richiesto. leverage solo per futures.
    """
    for key in _FIELD_ORDER:
        if key == "leverage":
            if config.get("market_type") != "futures":
                continue
        if _is_missing(config.get(key)):
            return key
    return None


def process_message_v2(user_text: str, config: dict) -> tuple[dict, str | None]:
    patch = extract_all_fields(user_text)
    config = merge_valid_fields(config, patch)
    next_step = compute_next_missing(config)
    return config, next_step


if __name__ == "__main__":
    config = {
        "market_type": None,
        "symbol": None,
        "timeframe": None,
        "operating_mode": None,
        "sl": None,
        "tp": None,
        "risk_pct": None,
        "leverage": None,
    }

    inputs = [
        "Futures",
        "BTCUSDT",
        "15m",
        "equilibrata",
        "Stop loss 2%, take profit 5%, capitale rischio 1%, leva 10",
    ]

    for msg in inputs:
        config, step = process_message_v2(msg, config)
        print("MSG:", msg)
        print("CONFIG:", config)
        print("NEXT:", step)
        print("------")

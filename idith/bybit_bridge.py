# bybit_bridge.py
# Bridge minimale e robusto per Bybit Testnet (V5) usato dal runner.
# - legge BYBIT_API_KEY / BYBIT_API_SECRET da env
# - espone open_long/open_short/close_position/get_positions/get_last_price/set_tp_sl
# NOTE: non logga MAI le chiavi.
#
# IMPORTANTE: Idith attualmente supporta ESCLUSIVAMENTE Bybit TESTNET.
# Non esiste supporto per ambiente LIVE. Il runner è forzato a testnet=True.

import os
from typing import Optional, Any, Dict
from pybit.unified_trading import HTTP

CATEGORY = os.getenv("BYBIT_CATEGORY", "linear")
# Idith supporta ESCLUSIVAMENTE testnet - forzato a True
TESTNET = True
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"

_session: Optional[HTTP] = None

def _get_session() -> HTTP:
    global _session

    if _session is not None:
        return _session

    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")

    if not api_key or not api_secret:
        raise RuntimeError("BYBIT API KEY / SECRET mancanti")

    _session = HTTP(
        testnet=TESTNET,
        api_key=api_key,
        api_secret=api_secret,
        recv_window=60000,
    )
    return _session


# =========================
# Helpers
# =========================
def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if not s:
            return default
        return float(s)
    except Exception:
        return default


def get_last_price(symbol: str) -> float:
    """
    Ritorna l'ultimo prezzo (LastPrice) per symbol.
    """
    session = _get_session()
    r = session.get_tickers(category=CATEGORY, symbol=symbol)
    lst = (r or {}).get("result", {}).get("list", []) or []
    if not lst:
        raise RuntimeError(f"Ticker non trovato per {symbol}: {r}")
    last = _safe_float(lst[0].get("lastPrice"), 0.0)
    if last <= 0:
        raise RuntimeError(f"LastPrice non valido per {symbol}: {lst[0]}")
    return last


def get_positions(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Ritorna la posizione aperta su symbol (se size>0), altrimenti None.
    """
    session = _get_session()
    r = session.get_positions(category=CATEGORY, symbol=symbol)
    pos_list = (r or {}).get("result", {}).get("list", []) or []
    for p in pos_list:
        size = _safe_float(p.get("size"), 0.0)
        if size > 0:
            return p
    return None


def _place_market(symbol: str, side: str, qty: float, reduce_only: bool) -> Dict[str, Any]:
    """
    Esegue un Market order.
    """
    session = _get_session()
    qty = float(qty)

    # Bybit (specie su testnet) può rifiutare qty troppo piccole con errore "min limit".
    if qty <= 0:
        raise ValueError("qty deve essere > 0")

    return session.place_order(
        category=CATEGORY,
        symbol=symbol,
        side=side,                 # "Buy" / "Sell"
        orderType="Market",
        qty=str(qty),
        timeInForce="IOC",
        reduceOnly=reduce_only,
    )


def open_long(symbol: str, qty: float) -> Dict[str, Any]:
    if DEMO_MODE:
        return {
            "status": "demo",
            "action": "OPEN_LONG",
            "symbol": symbol,
            "qty": qty
        }
    return _place_market(symbol, "Buy", qty, reduce_only=False)


def open_short(symbol: str, qty: float) -> Dict[str, Any]:
    if DEMO_MODE:
        return {
            "status": "demo",
            "action": "OPEN_SHORT",
            "symbol": symbol,
            "qty": qty
        }
    return _place_market(symbol, "Sell", qty, reduce_only=False)


def close_position(symbol: str) -> Optional[Dict[str, Any]]:
    if DEMO_MODE:
        return {
            "status": "demo",
            "action": "CLOSE_POSITION",
            "symbol": symbol
        }

    pos = get_positions(symbol)
    if not pos:
        return None

    side = (pos.get("side") or "").lower()
    size = _safe_float(pos.get("size"), 0.0)
    if size <= 0:
        return None

    close_side = "Sell" if side == "buy" else "Buy"
    return _place_market(symbol, close_side, size, reduce_only=True)


def set_tp_sl(
    symbol: str,
    side: str,
    entry_price: float,
    qty: float,
    tp_pct: float = 0.01,
    sl_pct: float = 0.005,
    position_idx: int = 0,
) -> Dict[str, Any]:
    """
    Imposta Take Profit e Stop Loss sulla posizione.
    - side: "buy" oppure "sell"
    - entry_price: prezzo entry (float)
    """
    session = _get_session()

    entry_price = float(entry_price)
    if entry_price <= 0:
        raise ValueError(f"entry_price non valido: {entry_price}")

    side_l = side.lower().strip()

    if side_l == "buy":
        take_profit = round(entry_price * (1 + tp_pct), 2)
        stop_loss = round(entry_price * (1 - sl_pct), 2)
    else:
        take_profit = round(entry_price * (1 - tp_pct), 2)
        stop_loss = round(entry_price * (1 + sl_pct), 2)

    resp = session.set_trading_stop(
        category=CATEGORY,
        symbol=symbol,
        takeProfit=str(take_profit),
        stopLoss=str(stop_loss),
        tpTriggerBy="LastPrice",
        slTriggerBy="LastPrice",
        tpOrderType="Market",
        slOrderType="Market",
        tpslMode="Full",
        positionIdx=position_idx,
    )
    return resp

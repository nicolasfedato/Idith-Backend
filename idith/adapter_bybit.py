
# adapter_bybit.py
# Minimal adapter for Bybit **Testnet** using pybit (Unified Trading HTTP).
# Falls back to a no-op mock if keys or library are missing.
from __future__ import annotations

import os
from typing import Optional, Dict, Any

try:
    # pybit 2.x unified
    from pybit.unified_trading import HTTP
    _HAS_PYBIT = True
except Exception:
    _HAS_PYBIT = False

TESTNET_BASE_URL = "https://api-testnet.bybit.com"

from dotenv import load_dotenv
import os

# carica automaticamente il file .env, anche se non siamo nella stessa cartella
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '..', '.env'))

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

class BybitTestnetAdapter:
    """
    Small wrapper around pybit Unified HTTP for Testnet.
    Only the endpoints we need for a demo runner:
    - create market/limit orders
    - amend (SL/TP) via 'set_trading_stop'
    - cancel order
    Returns uniform dicts for events/logging.
    """

    def __init__(self, api_key: Optional[str] = None, api_secret: Optional[str] = None):
        self.api_key = api_key or os.getenv("BYBIT_API_KEY", "")
        self.api_secret = api_secret or os.getenv("BYBIT_API_SECRET", "")
        self.category = "linear"  # USDT perpetual
        self.session = None

        if _HAS_PYBIT and self.api_key and self.api_secret:
            try:
                self.session = HTTP(testnet=True, api_key=self.api_key, api_secret=self.api_secret)
            except Exception as e:
                # fall back to mock
                self.session = None
                self._init_error = f"Errore init pybit: {e!r}"
        else:
            miss = []
            if not _HAS_PYBIT:
                miss.append("pybit non installato")
            if not self.api_key:
                miss.append("BYBIT_API_KEY vuota")
            if not self.api_secret:
                miss.append("BYBIT_API_SECRET vuota")
            self._init_error = " | ".join(miss) if miss else ""

    # -------- utilities

    def ok(self) -> bool:
        return self.session is not None

    def why_not_ok(self) -> str:
        return getattr(self, "_init_error", "Motivo sconosciuto")

    # -------- orders

    def create_order(self, symbol: str, side: str, qty: float, order_type: str = "Market",
                     price: Optional[float] = None, reduce_only: bool = False) -> Dict[str, Any]:
        """
        Place an order. For Market orders omit price.
        Returns a dict with 'orderId' and echo of inputs. If not ok(), returns mock result.
        """
        order = {
            "symbol": symbol.upper(),
            "side": side.capitalize(),
            "qty": qty,
            "orderType": order_type.capitalize(),
            "price": price,
            "reduceOnly": reduce_only,
        }

        if not self.ok():
            # mock ID for offline
            return {"ok": False, "mock": True, "orderId": "offline", **order, "reason": self.why_not_ok()}

        try:
            params = dict(
                category=self.category,
                symbol=symbol.upper(),
                side=side.capitalize(),
                orderType=order_type.capitalize(),
                qty=str(qty),
                reduceOnly=reduce_only,
            )
            if price is not None and order_type.lower() == "limit":
                params["price"] = str(price)

            resp = self.session.place_order(**params)
            oid = ((resp or {}).get("result") or {}).get("orderId")
            return {"ok": True, "orderId": oid, **order, "raw": resp}
        except Exception as e:
            return {"ok": False, "orderId": None, **order, "error": repr(e)}

    def set_trading_stop(self, symbol: str, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict[str, Any]:
        """
        Set SL/TP on position (or last order) via 'set_trading_stop' endpoint.
        """
        if not self.ok():
            return {"ok": False, "mock": True, "symbol": symbol.upper(), "sl": sl, "tp": tp, "reason": self.why_not_ok()}
        try:
            params = dict(category=self.category, symbol=symbol.upper())
            if sl is not None:
                params["sl"] = str(sl)
            if tp is not None:
                params["tp"] = str(tp)
            resp = self.session.set_trading_stop(**params)
            return {"ok": True, "raw": resp, "symbol": symbol.upper(), "sl": sl, "tp": tp}
        except Exception as e:
            return {"ok": False, "symbol": symbol.upper(), "sl": sl, "tp": tp, "error": repr(e)}

    def cancel_all(self, symbol: str) -> Dict[str, Any]:
        if not self.ok():
            return {"ok": False, "mock": True, "symbol": symbol.upper(), "reason": self.why_not_ok()}
        try:
            resp = self.session.cancel_all_orders(category=self.category, symbol=symbol.upper())
            return {"ok": True, "raw": resp}
        except Exception as e:
            return {"ok": False, "error": repr(e)}

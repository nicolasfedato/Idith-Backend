# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Tuple, Optional, Dict
import re, random

_BAD = ["cazzo","merda","stronzo","troia","vaffanculo","puttana","idiota","imbecille","deficiente","pezzo di merda","fanculo"]
_DEESCALATE = [
    "Capisco la frustrazione, ma teniamo il linguaggio pulito e risolviamo subito.",
    "Ti seguo, proviamo a sistemare passo-passo — evitiamo insulti così andiamo dritti al punto.",
    "Resto volentieri sul tecnico: dimmi cosa non funziona e lo aggiustiamo insieme."
]
_OPENERS_FRIENDLY_PRO = ["Volentieri!","Certo 🙂","Assolutamente.","Ti spiego in modo semplice:"]

def moderate_input(text: str) -> Tuple[str, bool]:
    t=(text or "").strip(); low=t.lower()
    for w in _BAD:
        if re.search(r"\b"+re.escape(w)+r"\b", low):
            return t, True
    return t, False

def deescalate_message() -> str:
    return random.choice(_DEESCALATE)

def choose_tone(profile: Optional[Dict]=None) -> Dict[str, str]:
    style = (profile or {}).get("tone_style","friendly_pro")
    return {"didactic": True, "style": style}

def _prefix(profile: Optional[Dict]=None) -> str:
    style = (profile or {}).get("tone_style","friendly_pro")
    if style == "friendly_pro":
        return random.choice(_OPENERS_FRIENDLY_PRO)
    return ""

def format_explanation(body: str, didactic: bool = True, profile: Optional[Dict]=None) -> str:
    if not didactic: return body
    pre = _prefix(profile)
    return f"{pre} {body}" if pre else body

# --- Event formatter -------------------------------------------------

def _pct(p):
    try:
        return f"{float(p):.2f}%"
    except Exception:
        return str(p)

def format_trade_event(evt: dict) -> str:
    """
    Converte un evento JSONL del runner in una riga leggibile in chat.
    Campi attesi: ts, type, side, symbol, price, rr, pnl_pct, reason
    """
    t = evt.get("type", "").lower()
    sym = evt.get("symbol", "")
    px = evt.get("price", evt.get("fill_price", evt.get("entry", "")))
    rr = evt.get("rr", None)
    pnl = evt.get("pnl_pct", None)
    side = (evt.get("side") or "").upper()

    when = evt.get("ts_hhmm") or evt.get("ts") or ""
    if isinstance(when, (int, float)):
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(when).strftime("%H:%M")

    if t == "open":
        emoji = "🟢" if side == "LONG" else "🟠"
        extra = f"RR tgt {rr:+.1f}x" if isinstance(rr, (int, float)) else ""
        return f"- [{when}] {emoji} Apertura {side} su {sym} @ {px} {extra}".strip()

    if t in ("close", "tp", "sl"):
        if t == "tp":
            emoji = "🔴"; lab = "Chiusura (TP)"
        elif t == "sl":
            emoji = "🟣"; lab = "Chiusura (SL)"
        else:
            emoji = "⚪"; lab = "Chiusura"
        extra = f"({_pct(pnl)})" if pnl is not None else (f"(RR {rr:+.1f}x)" if isinstance(rr, (int, float)) else "")
        return f"- [{when}] {emoji} {lab} @ {px} {extra}".strip()

    reason = evt.get("reason", "")
    return f"- [{when}] 📄 {t or 'evento'} {sym} @ {px} {reason}".strip()

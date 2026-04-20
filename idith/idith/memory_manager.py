
# memory_manager.py
# Semplice gestore profilo persistente su file JSON (memory/profile.json)

from __future__ import annotations
import json, os, time
from typing import Dict, Any

ROOT = os.path.dirname(os.path.abspath(__file__))
MEM_DIR = os.path.join(ROOT, "memory")
os.makedirs(MEM_DIR, exist_ok=True)
PROFILE_PATH = os.path.join(MEM_DIR, "profile.json")

_DEFAULT_PROFILE: Dict[str, Any] = {
    "language": "it",
    "tone": "amichevole_pro",           # tono FISSO (non modificabile via chat)
    "style_variation": "alta",
    "identity": "consulente_empatico",
    "ui_theme": "dark",
    "exchange": "bybit_testnet",
    "live_mode_enabled": False,
    "pair": "BTCUSDT",
    "timeframe": "1h",
    "strategy": "trend",
    "risk_pct": "1%",
    "sl": "ATR 2x",
    "tp": "RR 1.5x",
    "leverage": "1x",
    "lang_register": "adaptive",
    # memorie recenti per ripresa
    "last_pair": None,
    "last_timeframe": None,
    "last_strategy": None,
    "last_indicators": None,
    "updated_at": int(time.time())
}

class Memory:
    def __init__(self, path: str = PROFILE_PATH):
        self.path = path
        if not os.path.exists(self.path):
            self.save(_DEFAULT_PROFILE.copy())

    def load_profile(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            self.save(_DEFAULT_PROFILE.copy())
            return _DEFAULT_PROFILE.copy()

    def save(self, prof: Dict[str, Any]) -> Dict[str, Any]:
        prof["updated_at"] = int(time.time())
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False, indent=2)
        return prof

    # Imposta una singola chiave
    def set(self, key: str, value: Any) -> Dict[str, Any]:
        prof = self.load_profile()
        prof[key] = value
        return self.save(prof)

    # Aggiorna più chiavi
    def update(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        prof = self.load_profile()
        prof.update(changes or {})
        return self.save(prof)

    # Riepilogo in righe leggibili
    def summary_lines(self) -> list[str]:
        p = self.load_profile()
        out = [
            f"exchange: {p.get('exchange')}",
            f"pair: {p.get('pair')}",
            f"timeframe: {p.get('timeframe')}",
            f"strategia: {p.get('strategy')}",
            f"rischio: {p.get('risk_pct')} | SL: {p.get('sl')} | TP: {p.get('tp')}",
            f"tone (fisso): {p.get('tone')}",
        ]
        return out

memory = Memory()

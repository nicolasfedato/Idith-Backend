
# -*- coding: utf-8 -*-
"""
Standalone: Exporta un file di configurazione per il runner DEMO.
Uso:  python export_config_cli.py
- Prende l'ultimo blueprint esportato (YAML o JSON) in memory/exports/
  oppure, se non esiste, usa un blueprint minimale di default (BTCUSDT, 1h).
- Salva il file in memory/configs/config_YYYYMMDD-HHMMSS.json
"""
import os, json, glob, time
from datetime import datetime

# --- cartelle di lavoro relative al progetto ---
ROOT = os.path.dirname(os.path.abspath(__file__))
MEM  = os.path.join(ROOT, "memory")
EXPO = os.path.join(MEM, "exports")
CONF = os.path.join(MEM, "configs")
os.makedirs(EXPO, exist_ok=True)
os.makedirs(CONF, exist_ok=True)

# --- helpers ---
def _latest_export_path() -> str | None:
    files = sorted(glob.glob(os.path.join(EXPO, "blueprint_*.*")), key=os.path.getmtime, reverse=True)
    return files[0] if files else None

def _load_blueprint(p: str | None) -> dict:
    if not p:
        # blueprint minimale di fallback
        return {
            "mode": "futures",
            "pairs": ["BTCUSDT"],
            "timeframe": "1h",
            "strategy": "trend",
            "risk_pct": "1%",
            "sl": "ATR 2x",
            "tp": "RR 1.5x",
            "leverage": "2x",
            "schedule": "24/7",
            "notify": "si",
            "env": "demo",
            "warmup": "si",
            "indicators": ["EMA", "RSI", "ATR"],
        }
    name = os.path.basename(p).lower()
    if name.endswith(".json"):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        # YAML semplice senza dipendenze: parsing ultra-basico (chiave: valore)
        bp = {}
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line and not line.strip().startswith("#"):
                    k, v = line.split(":", 1)
                    bp[k.strip()] = v.strip().strip("'"")
        # normalizza liste se presenti in YAML semplice
        for key in ("pairs", "indicators"):
            val = bp.get(key, "")
            if isinstance(val, str):
                parts = [x.strip().strip("'"") for x in val.replace("[","").replace("]","").split(",") if x.strip()]
                bp[key] = parts
        return bp

def _make_config_from_blueprint(bp: dict) -> dict:
    symbol = (bp.get("pairs") or ["BTCUSDT"])[0]
    tf = bp.get("timeframe", "1h")
    last_price = 100.0  # placeholder (il runner sovrascrive con i dati reali / CSV)
    return {
        "symbol": symbol,
        "timeframe": tf,
        "strategy": bp.get("strategy", "trend"),
        "last_price": last_price,
        "last_signal": "flat",
        "sizing": {
            "risk_amount_usdt": float(str(bp.get("risk_pct","1%")).replace("%","") or "1"),
            "leverage": bp.get("leverage", "1x"),
            "qty_notional": 200.0,
            "sl": bp.get("sl", "ATR 2x"),
            "tp": bp.get("tp", "RR 1.5x")
        },
        "ind_list": bp.get("indicators", []),
        "samples": 300
    }

if __name__ == "__main__":
    last = _latest_export_path()
    bp = _load_blueprint(last)
    cfg = _make_config_from_blueprint(bp)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(CONF, f"config_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[OK] Config esportata: {out_path}")

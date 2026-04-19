# testnet_runner_rt.py (STEP B)
# Runner "esecutore" (Bybit Testnet) che:
# - NON decide (nessuna strategia qui)
# - legge comandi strutturati da una coda su file (commands.jsonl)
# - esegue (open/close/set TP-SL)
# - scrive eventi su file (events.jsonl) per la chat/dashboard
#
# STOP: solo Ctrl+C (l'utente).
#
# Formato comando (una riga JSON):
# {"id":"cmd_123","action":"OPEN_LONG","symbol":"ETHUSDT","qty":0.02,"tp_pct":0.01,"sl_pct":0.005}
#
# Eventi (una riga JSON):
# {"ts":"...","type":"ORDER_OPENED","symbol":"ETHUSDT","side":"Buy","qty":0.02,"entry":2515.67}

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# =========================
# Caricamento variabili d'ambiente PRIMA di importare bybit_bridge
# =========================
from dotenv import load_dotenv

# Carica .env dalla stessa directory del file
env_path = Path(__file__).with_name(".env")
load_dotenv(dotenv_path=env_path, override=False)

# Verifica e log diagnostico
# NOTA: BYBIT_ENV non viene più utilizzato - Idith supporta ESCLUSIVAMENTE testnet
api_key_present = bool(os.getenv("BYBIT_API_KEY"))
api_secret_present = bool(os.getenv("BYBIT_API_SECRET"))

print(f"[ENV] BYBIT_API_KEY presente: {api_key_present}", flush=True)
print(f"[ENV] BYBIT_API_SECRET presente: {api_secret_present}", flush=True)
print(f"[ENV] Path .env usato: {env_path.absolute()}", flush=True)
print(f"[ENV] Modalità: TESTNET (forzato - Idith supporta solo testnet)", flush=True)

if not api_key_present or not api_secret_present:
    print("[ERROR] BYBIT_API_KEY e/o BYBIT_API_SECRET mancanti nel file .env", flush=True)
    print(f"[ERROR] Verificare che il file .env esista in: {env_path.absolute()}", flush=True)
    sys.exit(1)

# Import di bybit_bridge DOPO il caricamento di .env
from .bybit_bridge import (
    get_positions,
    open_long,
    open_short,
    close_position,
    set_tp_sl,
)

# =========================
# Percorsi coda (relativi al file)
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = os.path.join(BASE_DIR, "memory", "queue")
os.makedirs(QUEUE_DIR, exist_ok=True)

COMMANDS_PATH = os.path.join(QUEUE_DIR, "commands.jsonl")
EVENTS_PATH = os.path.join(QUEUE_DIR, "events.jsonl")
STATE_PATH = os.path.join(QUEUE_DIR, "runner_state.json")

# =========================
# Parametri runner
# =========================
POLL_SECONDS = 2
SYMBOL_DEFAULT = "ETHUSDT"

# =========================
# Utils
# =========================
def ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)

def emit(event: Dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("ts", ts())
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"offset": 0, "seen_ids": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        if "offset" not in s:
            s["offset"] = 0
        if "seen_ids" not in s or not isinstance(s["seen_ids"], list):
            s["seen_ids"] = []
        # cap seen_ids to avoid growing forever
        s["seen_ids"] = s["seen_ids"][-500:]
        return s
    except Exception:
        return {"offset": 0, "seen_ids": []}

def _save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)

def _entry_from_pos(pos: dict) -> float:
    for k in ("avgPrice", "entryPrice"):
        v = pos.get(k)
        try:
            f = float(v)
            if f > 0:
                return f
        except Exception:
            pass
    return 0.0

def _read_new_commands(offset: int) -> Tuple[int, list]:
    if not os.path.exists(COMMANDS_PATH):
        return offset, []
    with open(COMMANDS_PATH, "r", encoding="utf-8") as f:
        f.seek(offset)
        lines = f.readlines()
        new_offset = f.tell()

    cmds = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            cmds.append(json.loads(ln))
        except Exception as e:
            emit({"type": "COMMAND_PARSE_ERROR", "raw": ln, "error": str(e)})
    return new_offset, cmds

def _ensure_cmd_id(cmd: Dict[str, Any]) -> str:
    cid = cmd.get("id")
    if not cid:
        cid = f"cmd_{int(time.time()*1000)}"
        cmd["id"] = cid
    return cid

# =========================
# Esecutori azioni
# =========================
def _execute_open(side: str, symbol: str, qty: float, tp_pct: Optional[float], sl_pct: Optional[float]) -> None:
    # single-position guard: se c'è già una posizione, non aprire una seconda
    existing = get_positions(symbol)
    if existing:
        emit({"type": "OPEN_SKIPPED_ALREADY_OPEN", "symbol": symbol, "side": existing.get("side"), "size": existing.get("size")})
        return

    if side == "BUY":
        open_long(symbol, qty)
    else:
        open_short(symbol, qty)

    time.sleep(2)
    pos = get_positions(symbol)
    if not pos:
        emit({"type": "OPEN_FAILED_NO_POSITION", "symbol": symbol, "requested_side": side, "qty": qty})
        return

    entry = _entry_from_pos(pos)
    emit({"type": "ORDER_OPENED", "symbol": symbol, "side": pos.get("side"), "qty": float(pos.get("size", qty) or qty), "entry": entry})

    # set TP/SL lato exchange (consigliato)
    if tp_pct is not None or sl_pct is not None:
        # fallback a default se uno dei due manca
        _tp = float(tp_pct) if tp_pct is not None else 0.01
        _sl = float(sl_pct) if sl_pct is not None else 0.005
        try:
            # prova signature estesa, altrimenti fallback
            try:
                set_tp_sl(symbol, "buy" if side=="BUY" else "sell", entry, qty, tp_pct=_tp, sl_pct=_sl)
            except TypeError:
                set_tp_sl(symbol, "buy" if side=="BUY" else "sell", entry, qty)
            emit({"type": "TP_SL_SET", "symbol": symbol, "tp_pct": _tp, "sl_pct": _sl, "entry": entry})
        except Exception as e:
            emit({"type": "TP_SL_ERROR", "symbol": symbol, "error": str(e)})

def _execute_close(symbol: str) -> None:
    pos = get_positions(symbol)
    if not pos:
        emit({"type": "CLOSE_SKIPPED_NO_POSITION", "symbol": symbol})
        return
    close_position(symbol)
    emit({"type": "CLOSE_SENT", "symbol": symbol})
    time.sleep(2)
    if not get_positions(symbol):
        emit({"type": "POSITION_FLAT", "symbol": symbol})

def _execute_set_tp_sl(symbol: str, tp_pct: float, sl_pct: float) -> None:
    pos = get_positions(symbol)
    if not pos:
        emit({"type": "TP_SL_SKIPPED_NO_POSITION", "symbol": symbol})
        return
    entry = _entry_from_pos(pos)
    side = (pos.get("side") or "").lower()
    side_key = "buy" if side == "buy" else "sell"
    try:
        try:
            set_tp_sl(symbol, side_key, entry, float(pos.get("size") or 0), tp_pct=float(tp_pct), sl_pct=float(sl_pct))
        except TypeError:
            set_tp_sl(symbol, side_key, entry, float(pos.get("size") or 0))
        emit({"type": "TP_SL_UPDATED", "symbol": symbol, "tp_pct": float(tp_pct), "sl_pct": float(sl_pct), "entry": entry})
    except Exception as e:
        emit({"type": "TP_SL_ERROR", "symbol": symbol, "error": str(e)})

# =========================
# Main loop
# =========================
def main():
    log("Runner STEP B avviato (comandi da queue)")
    emit({"type": "RUNNER_STARTED", "pid": os.getpid()})

    state = _load_state()
    offset = int(state.get("offset", 0))
    seen = set(state.get("seen_ids", []))

    last_pos_state = None

    while True:
        try:
            # 1) monitora stato posizione (solo per log/eventi)
            pos = get_positions(SYMBOL_DEFAULT)
            pos_state = "FLAT" if not pos else f"OPEN:{pos.get('side')}:{pos.get('size')}:{pos.get('entryPrice') or pos.get('avgPrice')}"
            if pos_state != last_pos_state:
                last_pos_state = pos_state
                emit({"type": "POSITION_STATE", "symbol": SYMBOL_DEFAULT, "state": pos_state})

            # 2) leggi comandi nuovi
            new_offset, cmds = _read_new_commands(offset)

            for cmd in cmds:
                cid = _ensure_cmd_id(cmd)
                if cid in seen:
                    continue

                action = (cmd.get("action") or "").upper().strip()
                symbol = (cmd.get("symbol") or SYMBOL_DEFAULT).upper().strip()
                qty = float(cmd.get("qty") or 0)
                tp_pct = cmd.get("tp_pct")
                sl_pct = cmd.get("sl_pct")

                emit({"type": "COMMAND_RECEIVED", "id": cid, "action": action, "symbol": symbol})

                try:
                    if action == "OPEN_LONG":
                        if qty <= 0:
                            raise ValueError("qty mancante o <= 0")
                        _execute_open("BUY", symbol, qty, tp_pct, sl_pct)
                        emit({"type": "COMMAND_DONE", "id": cid, "status": "ok"})
                    elif action == "OPEN_SHORT":
                        if qty <= 0:
                            raise ValueError("qty mancante o <= 0")
                        _execute_open("SELL", symbol, qty, tp_pct, sl_pct)
                        emit({"type": "COMMAND_DONE", "id": cid, "status": "ok"})
                    elif action == "CLOSE_POSITION":
                        _execute_close(symbol)
                        emit({"type": "COMMAND_DONE", "id": cid, "status": "ok"})
                    elif action == "SET_TP_SL":
                        _tp = float(cmd.get("tp_pct", 0.01))
                        _sl = float(cmd.get("sl_pct", 0.005))
                        _execute_set_tp_sl(symbol, _tp, _sl)
                        emit({"type": "COMMAND_DONE", "id": cid, "status": "ok"})
                    elif action == "STATUS":
                        emit({"type": "STATUS", "symbol": symbol, "position": pos or None})
                        emit({"type": "COMMAND_DONE", "id": cid, "status": "ok"})
                    else:
                        emit({"type": "COMMAND_UNKNOWN", "id": cid, "action": action})
                        emit({"type": "COMMAND_DONE", "id": cid, "status": "error", "error": "unknown_action"})
                except Exception as e:
                    emit({"type": "COMMAND_DONE", "id": cid, "status": "error", "error": str(e)})

                seen.add(cid)

            # 3) salva stato (offset + dedupe ids)
            offset = new_offset
            state["offset"] = offset
            state["seen_ids"] = list(seen)[-500:]
            _save_state(state)

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log("Runner fermato manualmente")
            emit({"type": "RUNNER_STOPPED"})
            break
        except Exception as e:
            # resilienza: logga e continua
            emit({"type": "RUNNER_ERROR", "error": str(e)})
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()

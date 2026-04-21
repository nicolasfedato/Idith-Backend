#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vedi_eventi.py — Visualizza gli eventi della sessione in tempo reale (o ultimi N).
Compatibile con Windows/macOS/Linux. Non richiede librerie esterne.

Usage:
  python vedi_eventi.py --session nico-run            # segue in tempo reale
  python vedi_eventi.py --session nico-run --last 30  # mostra ultimi 30 e continua a seguire
  python vedi_eventi.py --session nico-run --once     # mostra ultimi 30 e termina
  python vedi_eventi.py --file memory/events/nico-run.jsonl --once

Il file eventi è un JSONL (una riga JSON per evento) generato dal runner testnet.
"""

from __future__ import annotations
import os, sys, json, time, argparse
from datetime import datetime
from pathlib import Path

EMOJI = {
    "runner_start": "🟦",
    "order_open": "🟢",
    "sl_update": "🔧",
    "take_profit": "✅",
    "stop_loss": "❌",
    "order_close": "🔵",
}

def _fmt_ts(ts: str) -> str:
    # ts già in ISOZ; mostriamo hh:mm:ss locale
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts

def _fmt_line(obj: dict) -> str:
    k = obj.get("type", "?")
    d = obj.get("data", {}) or {}
    emo = EMOJI.get(k, "•")
    if k == "runner_start":
        return f"{emo} Avvio runner  • Pair: {d.get('pair','?')} • TF: {d.get('timeframe','?')}"
    if k == "order_open":
        return f"{emo} Apertura ordine • {d.get('side','?').upper()} @ {d.get('price','?')} • qty {d.get('qty','?')}"
    if k == "sl_update":
        return f"{emo} Aggiorno SL → {d.get('sl','?')}"
    if k == "take_profit":
        return f"{emo} Take Profit @ {d.get('price','?')}  • RR {d.get('rr','?')}"
    if k == "stop_loss":
        return f"{emo} Stop Loss eseguito @ {d.get('price','?')}  • RR {d.get('rr','?')}"
    if k == "order_close":
        return f"{emo} Chiusura ordine @ {d.get('price','?')}  • RR {d.get('rr','?')}"
    return f"{emo} {k} • {d}"

def tail_file(path: Path, last: int = 30, follow: bool = True, once: bool = False) -> int:
    if not path.exists():
        print(f"⚠️  File non trovato: {path}", file=sys.stderr)
        return 2

    # Leggi tutte le righe una volta
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if last > 0 and len(lines) > last:
        lines = lines[-last:]
    for ln in lines:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        print(f"[{_fmt_ts(obj.get('ts',''))}] {_fmt_line(obj)}")

    if once:
        return 0

    # Segui file come tail -f
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        f.seek(0, os.SEEK_END)
        while True:
            where = f.tell()
            chunk = f.readline()
            if not chunk:
                time.sleep(0.2)
                f.seek(where)
                continue
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            print(f"[{_fmt_ts(obj.get('ts',''))}] {_fmt_line(obj)}")

def main():
    ap = argparse.ArgumentParser(description="Visualizza gli eventi della sessione (JSONL)")
    ap.add_argument("--session", help="ID sessione (es. nico-run)")
    ap.add_argument("--file", help="Percorso file JSONL (override di --session)")
    ap.add_argument("--last", type=int, default=30, help="Quante righe iniziali mostrare (default 30)")
    ap.add_argument("--once", action="store_true", help="Mostra e termina (non segue in tempo reale)")
    args = ap.parse_args()

    if args.file:
        path = Path(args.file)
    else:
        if not args.session:
            print("❌ Specifica --session oppure --file", file=sys.stderr)
            return 2
        path = Path("memory") / "events" / f"{args.session}.jsonl"

    follow = not args.once
    return tail_file(path, last=args.last, follow=follow, once=args.once) or 0

if __name__ == "__main__":
    raise SystemExit(main())

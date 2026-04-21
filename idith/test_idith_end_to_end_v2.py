
# test_idith_end_to_end_v2.py
# Idith – end-to-end checks on brain + policy + memory
# Run:  python test_idith_end_to_end_v2.py

import re
import json
from copy import deepcopy

from .memory_manager import memory
from .brain import handle_message

GREEN = "✅"
RED = "❌"
SEP = "-" * 72

def line(msg=""):
    print(SEP)
    if msg:
        print(msg)
        print(SEP)

def show(title):
    print("\n" + title)
    print(SEP)

def ok(msg):
    print(f"{GREEN} {msg}")

def ko(msg):
    print(f"{RED} {msg}")

def send(msg):
    """Helper to call brain and return reply text + profile snapshot"""
    out = handle_message("test-id", msg)
    return str(out.get("reply", "")), out.get("profile", memory.load_profile())

def must_contain(text, needles):
    text_low = text.lower()
    for n in needles:
        if isinstance(n, (list, tuple)):
            # any of the alternatives
            if not any(alt.lower() in text_low for alt in n):
                return False
        else:
            if n.lower() not in text_low:
                return False
    return True

def print_profile_state(title):
    p = memory.load_profile()
    print(title, json.dumps(p, ensure_ascii=False))

def main():
    # Snapshot profile to restore later
    original_profile = deepcopy(memory.load_profile())

    total_errors = 0
    line("IDITH – END TO END v2 (variazioni, parsing, memoria, tono fisso)")

    # -- SCENARIO 1: Impostazioni exchange / piano free
    show("SCENARIO 1: Impostazioni exchange / piano free")
    r, prof = send("usa bybit testnet")
    cond1 = (prof.get("exchange") == "bybit_testnet")
    cond2 = (prof.get("live_mode_enabled") is False)
    if cond1: ok("Exchange impostato a bybit_testnet.")
    else: ko("Exchange NON risulta 'bybit_testnet'.")
    if cond2: ok("Modalità live disabilitata nel piano free (ok).")
    else: ko("La modalità live non risulta disabilitata.")
    total_errors += 0 if (cond1 and cond2) else 1

    # -- SCENARIO 2: Spiegazioni didattiche
    show("SCENARIO 2: Spiegazioni didattiche")
    r_ema, _ = send("spiega ema")
    ema_ok = must_contain(r_ema, [["media mobile", "media mobile esponenziale"], "trend", ["incroci ema", "incroci", "sopra/sotto"]])
    if ema_ok: ok("Spiegazione EMA trovata e coerente.")
    else: ko("Spiegazione EMA mancante o incoerente."); total_errors += 1

    r_rsi, _ = send("spiega rsi")
    rsi_ok = must_contain(r_rsi, [["oscillatore", "0-100", "ipercomprato", "ipervenduto"]])
    if rsi_ok: ok("Spiegazione RSI trovata e coerente.")
    else: ko("Spiegazione RSI mancante o incoerente."); total_errors += 1

    r_brk, _ = send("spiega breakout")
    brk_ok = must_contain(r_brk, [["rottura", "breakout"], ["livelli", "box"], ["retest", "volume"]])
    if brk_ok: ok("Spiegazione Breakout trovata e coerente.")
    else: ko("Spiegazione Breakout mancante o incoerente."); total_errors += 1

    # -- SCENARIO 3: Parsing abbreviato + conferma
    show("SCENARIO 3: Parsing abbreviato + conferma")
    r_parse, _ = send("BTCUSDT 1h trend rr 1.5x sl atr 2x risk 1%")
    parse_ok = must_contain(r_parse, ["btcusdt", "1h", "trend", "atr", "rr 1.5"])
    if parse_ok: ok("Riepilogo proposto correttamente.")
    else: ko("Parsing abbreviato non ha prodotto il riepilogo atteso."); total_errors += 1

    # -- SCENARIO 4: Ripresa dal profilo (continuità)
    show("SCENARIO 4: Ripresa dal profilo")
    # Set explicit last_* (simulate ultimo lavoro salvato)
    prof_now = memory.load_profile()
    memory.update({
        "last_pair": "BTCUSDT",
        "last_timeframe": "15m",
        "last_strategy": "trend",
        "last_indicators": ["EMA", "ATR"],
    })
    r_resume, _ = send("da dove riprendiamo")
    resume_ok = must_contain(r_resume, ["btcusdt", "15m", "trend"])
    if resume_ok: ok("Ripresa parametri dal profilo: OK.")
    else: ko("Ripresa parametri assente."); total_errors += 1

    # -- SCENARIO 5: Utente inesperto / guida passo-passo
    show("SCENARIO 5: Utente inesperto / guida passo-passo")
    r_guid, _ = send("sono inesperto, aiutami")
    guid_ok = must_contain(r_guid, [["dimmi", "partiamo da"], "coppia", "timeframe"])
    if guid_ok: ok("Risposta guida con elenco puntato (ok).")
    else: ko("Mancano istruzioni chiare per utente inesperto."); total_errors += 1

    # -- SCENARIO 6: Variazioni risposte (anti-fotocopia)
    show("SCENARIO 6: Variazioni risposte (anti-fotocopia)")
    v1, _ = send("spiega ema")
    v2, _ = send("spiega ema")
    v3, _ = send("spiega ema")
    vary_ok = (v1 != v2) or (v2 != v3) or (v1 != v3)
    if vary_ok: ok("Le 3 risposte su EMA non sono identiche (ok).")
    else: ko("Le spiegazioni su EMA sono identiche (mancano variazioni)."); total_errors += 1

    # -- SCENARIO 7: Tono fisso (non modificabile)
    show("SCENARIO 7: Tono fisso (non modificabile)")
    before = deepcopy(memory.load_profile())
    _ , _ = send("imposta tono empatico")  # questo non deve cambiare il profilo
    after = memory.load_profile()
    tone_ok = (after.get("tone") == before.get("tone") == "amichevole_pro")
    if tone_ok: ok("Tono invariato (amichevole_pro) come previsto.")
    else: ko("Il tono è cambiato ma NON deve essere modificabile."); total_errors += 1

    line(f"TEST COMPLETATI con {total_errors} errore/i." if total_errors else "TUTTI I TEST SUPERATI ✔")

    # Restore original profile
    memory.update(original_profile)
    print("\nProfilo ripristinato.")

if __name__ == "__main__":
    main()

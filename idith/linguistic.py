# linguistics.py
# Modulo drop-in per Idith: normalizza input, capisce slang/ITA+ENG, ironia/metafore,
# rileva emozioni, gestisce chiarimenti progressivi infiniti e suggerisce lo stile di risposta.

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Dict, Optional

# -----------------------
# Normalizzazione & correzioni silenziose
# -----------------------

_COMMON_FIXES = {
    # errori comuni / chat-slang -> forma standard
    "xke": "perché", "xkè": "perché", "xké": "perché", "xche": "perché", "ke": "che",
    "cmq": "comunque", "nn": "non", "qnd": "quando", "qlc": "qualcosa", "x": "per",
    "pls": "per favore", "per fav": "per favore", "x favore": "per favore",
    "futur": "futures", "future": "futures", "futuro": "futures",
    "spoot": "spot", "spo": "spot",
    "1 ora": "1h", "una ora": "1h", "un ora": "1h",
    "me lo spieghi": "spiega", "spiegami meglio": "spiega",
    "non ho cpito": "non ho capito",
    "cos e": "cos'è", "cos e'": "cos'è",
    "30 x": "30x", "20 x": "20x", "10 x": "10x",
}

def normalize_text(text: str) -> str:
    t = (text or "").strip()
    # correzioni silenziose “soft” (case-insensitive)
    for k, v in _COMMON_FIXES.items():
        t = re.sub(re.compile(re.escape(k), re.I), v, t)
    return t

# -----------------------
# Intent “aiuto/spiega”
# -----------------------

_HELP_PAT = re.compile(r"\b(aiuto|help|spiega|non ho capito|non capisco|chiarisci|più semplice|a prova di bambino)\b", re.I)

def wants_help(text: str) -> bool:
    return bool(_HELP_PAT.search(text or ""))

# -----------------------
# Ironia / metafore / iperboli
# -----------------------

_IRONY_PAT = re.compile(r"(milionario|lamborghini|scherzavo|lol|ahah|ironico|ironia)", re.I)
_METAPHOR_PAT = re.compile(r"(dare un calcio al passato|mi butto|sto impazzendo|crollo mentale|fuoco|a razzo|alla luna)", re.I)

def detect_irony(text: str) -> bool:
    return bool(_IRONY_PAT.search(text or ""))

def detect_metaphor(text: str) -> bool:
    return bool(_METAPHOR_PAT.search(text or ""))

# -----------------------
# Emozioni / frustrazione
# -----------------------

_EMO_PAT = re.compile(
    r"(non ci capisco|sono confuso|mi gira male|lascia stare|non serve a niente|sto per lanciare|mi incazzo|mi dà ai nervi)",
    re.I
)

def detect_emotion(text: str) -> bool:
    return bool(_EMO_PAT.search(text or ""))

# -----------------------
# Ambiguità (es. “leva”)
# -----------------------

def is_ambiguous_leva(text: str) -> bool:
    t = (text or "").lower()
    # Se parla di “leva” senza x (3x, 5x) e senza futures, potrebbe essere ambigua
    if "leva" in t and not re.search(r"\b\d+\s*x\b", t) and "future" not in t:
        return True
    return False

# -----------------------
# Chiarimenti progressivi infiniti
# -----------------------

@dataclass
class ClarifyEngine:
    counts: Dict[str, int] # es. {"mode": 2, "strategy": 1}

    def bump(self, topic: str) -> int:
        self.counts[topic] = self.counts.get(topic, 0) + 1
        return self.counts[topic]

    def reply_for(self, topic: str, level: int) -> str:
        """Restituisce una spiegazione sempre più ricca ad ogni livello."""
        if topic == "mode":
            if level == 1:
                return ("**Spot** compra/vende l’asset. **Futures** usa **leva** → profitti/perdite amplificati e funding.")
            elif level == 2:
                return ("Pensa a Spot come avere BTC in portafoglio. Con Futures puoi anche **shortare**. La leva moltiplica **guadagni e perdite**.")
            else:
                return ("Esempio: con **2x** un movimento del 5% diventa ≈ **10%** sul P&L. Inizia in **demo** e leva **1–3x**.")
        if topic == "strategy":
            if level == 1:
                return ("**Trend (EMA)** segue il movimento; **Breakout** entra su strappi; **Reversion (RSI)** compra i ritracciamenti.")
            elif level == 2:
                return ("Direzionale → Trend; Laterale → Reversion; Volatile/strappi → Breakout. Possiamo partire da Trend + filtri.")
            else:
                return ("Preset consigliato: **EMA 50/200 + filtro volumi + SL ATR 2x + TP RR 1.5x**. Poi si affina.")
        if topic == "risk":
            if level == 1:
                return ("La **% per trade** è quanto capitale rischi a operazione. Per bot prudente: **0.5–1%**.")
            elif level == 2:
                return ("Sopra **2–3%** diventa **aggressivo**: drawdown più profondi e recovery più lenti.")
            else:
                return ("Esempio: con 1% per trade servono ~**-100 trade** per azzerare il capitale (teorico). Tieni basso e costante.")
        if topic == "timeframe":
            if level == 1:
                return ("Timeframe bassi = più trade/rumore; alti = meno rumore/meno trade. 15m è un buon compromesso.")
            else:
                return ("Se vuoi **scalping** → 1–5m; **intra** → 15m–1h; **swing** → 4h–1d. Partiamo da **15m** in demo.")
        if topic == "leverage":
            if level == 1:
                return ("La **leva** (es. 3x) aumenta esposizione e **rischio**. Commissioni/funding incidono di più.")
            else:
                return ("Sopra **10x** è **molto rischiosa**: drawdown lampo. Suggerito test con **1–3x** max.")
        # fallback generico
        if level == 1:
            return ("Ok, te lo spiego in modo più semplice.")
        elif level == 2:
            return ("Proviamo con un esempio pratico per fissare i concetti.")
        else:
            return ("Resto con te: dimmi pure cosa non è chiaro e cambio modo di spiegare finché va bene.")

# -----------------------
# Scelta stile di risposta (tono tecnico + umano)
# -----------------------

@dataclass
class StyleHint:
    empathetic_prefix: Optional[str] = None # es. “Tranquillo, ci arriviamo insieme.”
    playful_one_liner: Optional[str] = None # piccola battuta prima del rientro
    technical_bias: bool = True # mantieni linguaggio tecnico
    ask_confirm: bool = False # chiedi conferma quando rilevi rischio/ambiguità

def style_from(text: str) -> StyleHint:
    emo = detect_emotion(text)
    irony = detect_irony(text)
    meta = detect_metaphor(text)
    hint = StyleHint(technical_bias=True)

    if emo:
        hint.empathetic_prefix = "Tranquillo, ci arriviamo insieme. "
    if irony or meta:
        hint.playful_one_liner = "Capito il mood 😄. "
    return hint

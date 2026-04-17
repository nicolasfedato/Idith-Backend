# nlg.py — piccolo motore di stile per Idith (IT)
from __future__ import annotations
import random, re, time
from typing import List, Dict, Any, Optional

# === impostazioni base ===
RANDOM_SEED_WINDOW_SEC = 90 # piccola finestra per variare senza diventare caotico
MAX_RECENT_PHRASES = 12 # antiripetizione micro

_openers = [
    "Certo,", "Volentieri!", "Ok,", "Ti spiego in modo semplice:",
    "Facciamo così:", "Ecco la sintesi:", "Riassumo bene:"
]
_transitions = [
    "In pratica,", "Detto questo,", "Tradotto operativamente,", "In due righe:",
    "Passo-passo:", "Se vuoi applicarla:"
]
_closers = [
    "Se vuoi, la configuro subito.", "Posso mostrarti un esempio reale.",
    "Dimmi se vuoi più dettagli su un punto.", "Vuoi che la applichi alla tua coppia/timeframe?",
    "Se qualcosa non è chiaro, ci torniamo."
]

def _rng() -> random.Random:
    # seme “morbido” per cambiare frasi e ordine senza ripetere sempre le stesse
    t = int(time.time() // RANDOM_SEED_WINDOW_SEC)
    r = random.Random(t)
    return r

def _pick(pool: List[str], recent: List[str]) -> str:
    r = _rng()
    choices = [p for p in pool if p not in recent] or pool
    return r.choice(choices)

def _clean(txt: str) -> str:
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt

def paragraphize(text: str) -> str:
    """Se il testo contiene molte ' - ' o righe spezzate, converte in fraseggi fluidi."""
    raw = text.replace("\r", "")
    # bullet grezzi -> frasi
    bullets = [b.strip(" -•\t") for b in raw.split("\n") if b.strip()]
    if len(bullets) >= 3 and all(len(b) < 140 for b in bullets):
        # tieni elenco ma più leggibile
        bullets = [f"- {b}" for b in bullets]
        return "\n".join(bullets)
    else:
        # trasformo in 2-3 frasi
        s = re.split(r"[.\n]+", raw)
        s = [x.strip(" -•") for x in s if x.strip()]
        if not s:
            return _clean(raw)
        if len(s) == 1:
            return _clean(s[0]) + "."
        # unisci con virgole/transizioni
        mid = _pick(_transitions, [])
        out = s[0].rstrip(".") + ". " + mid + " " + "; ".join(s[1:]) + "."
        return _clean(out)

def bullets_or_paragraph(points: List[str]) -> str:
    points = [p.strip() for p in points if p and p.strip()]
    if not points:
        return ""
    # se >3 punti brevi -> elenco, altrimenti prosa
    short = all(len(p) <= 120 for p in points)
    if len(points) >= 3 and short:
        return "\n".join(f"- {p}" for p in points)
    else:
        return paragraphize(" ".join(points))

class NLGSession:
    """Gestisce anti-ripetizione di microfrasi (aperture/transizioni/closers)."""
    def __init__(self):
        self.recent: List[str] = []

    def remember(self, s: str):
        s = s.strip()
        if not s: return
        self.recent.append(s)
        if len(self.recent) > MAX_RECENT_PHRASES:
            self.recent = self.recent[-MAX_RECENT_PHRASES:]

    def compose(self, topic: str, points: List[str], style: str = "friendly_pro") -> str:
        op = _pick(_openers, self.recent)
        self.remember(op)

        body = bullets_or_paragraph(points)
        if body and not body.endswith(".") and not body.endswith("!"):
            body += "."

        cl = _pick(_closers, self.recent)
        self.remember(cl)

        # stile: niente emoji, tono amichevole/pro, frasi corte + alcune transizioni
        parts = [op, topic.rstrip(":"), body, cl]
        text = "\n".join([p for p in parts if _clean(p)])
        # rifinitura
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text

# funzione rapida da usare direttamente
_session_cache: Dict[str, NLGSession] = {}

def humanize(session_id: str, title: str, raw_points: List[str]|str) -> str:
    if isinstance(raw_points, str):
        # se arriva un testo libero, prova a “scioglierlo”
        return paragraphize(raw_points)
    sess = _session_cache.setdefault(session_id, NLGSession())
    return sess.compose(title, raw_points)

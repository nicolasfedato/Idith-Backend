# idith/intent_lexicon.py
# Lessico, normalizzazione e regole base per intent routing

from __future__ import annotations
import re
from typing import List

# -----------------------------
# Normalizzazione & tokenizzazione
# -----------------------------

def normalize(text: str) -> str:
    """Lowercase + strip. Correzioni minime e silenziose per casi comuni."""
    t = (text or "").strip()

    # normalizza spazi/apici tipografici
    t = t.replace("’", "'")

    # lowercase per matching lessicale
    tl = t.lower()

    # correzioni frequenti (senza annunciarle in output)
    fixes = [
        (r"\bfutur(e|i|o|a|)\b", "futures"),
        (r"\bspo(t)?\b", "spot"),
        (r"\bnon ho cpito\b", "non ho capito"),
        (r"\bcos['’]?e\b", "cos e"),
        (r"\bsì\b", "si"), # uniformo per controlli, poi accetto "si" come "sì"
    ]
    for pat, repl in fixes:
        tl = re.sub(pat, repl, tl, flags=re.I)

    # normalizza espressioni di tempo comuni (es. "un'ora" -> 1h)
    tl = re.sub(r"\bun['’]?ora\b", "1h", tl)
    tl = re.sub(r"\b1\s*ora\b", "1h", tl)
    tl = re.sub(r"\b15\s*min(uti)?\b", "15m", tl)

    return tl


def tokenize(text: str) -> List[str]:
    """Tokenizzazione semplice alfanumerica."""
    return re.findall(r"[a-z0-9]+", (text or "").lower())


# -----------------------------
# Parole in-scope & controlli
# -----------------------------

# Termini che consideriamo legati al dominio (trading/bot/Bybit)
IN_SCOPE_TERMS = {
    "bot","idith","configura","configurazione","setup","generare","codice","prototipo",
    "parametri","riepilogo","riassunto","strategia","aiuto","leva","leverage","warning",
    "trading","bybit","spot","futures","ema","rsi","breakout","reversion","timeframe",
    "coppie","btc","eth","usdt","risk","rischio","sl","stop","stoploss","tp","take","takeprofit",
    "atr","rr","notifiche","demo","testnet","live","warmup","report","api","chiavi","account",
    "name","nome","pairs","timeframe","strategy","percentuale","percento","size"
}

# Trigger che permettono SEMPRE di parlare (anche se il resto è off-topic)
CONTROL_ALLOW = re.compile(
    r"\b(riassunto|riepilogo|genera(re)?\s*codice|prototip|codice\s*bot|inizia|partiamo|"
    r"si|sì|ok|help|aiuto|spiega|chiar|ricomincia|restart|ciao|hey|salve|buongiorno|buonasera)\b",
    re.I
)

# Pattern per abuso/volgarità (estendibile)
ABUSE_PATTERNS = [
    r"\bpezzo di merda\b",
    r"\bfiglio di putt",
    r"\bbestemm",
    r"\bvaffanculo\b",
    r"\bti ammazzo\b",
    r"\bmerda\b",
    r"\bfrocio\b",
    r"\bnegro\b",
]


def is_abusive(text: str) -> bool:
    """True se contiene insulti/abusi espliciti."""
    t = text or ""
    for pat in ABUSE_PATTERNS:
        if re.search(pat, t, flags=re.I):
            return True
    return False


# -----------------------------
# Coppie di mercato (BTCUSDT ecc.)
# -----------------------------

PAIR_REGEX = re.compile(r"\b[A-Z]{2,10}/?USDT\b")

def has_pair(text: str) -> bool:
    """Rileva la presenza di almeno una coppia valida (BTCUSDT, ETH/USDT, ecc.)."""
    if not text:
        return False
    return bool(PAIR_REGEX.search(text.upper()))


# -----------------------------
# Off-topic gate
# -----------------------------

def is_offtopic(text: str) -> bool:
    """
    True se il messaggio è fuori tema (trading/bot) e non contiene comandi di controllo.
    Eccezioni:
      - se contiene una coppia valida (BTCUSDT / ETH/USDT...) → NON off-topic
      - se contiene parole di controllo (riepilogo, spiega, ok, ciao...) → NON off-topic
    """
    t = normalize(text)
    if not t:
        return False

    # Sblocchi sempre permessi (comandi di controllo)
    if CONTROL_ALLOW.search(t):
        return False

    # Coppie come BTCUSDT/ETHUSDT devono essere considerate in-scope
    if has_pair(text):
        return False

    # Score in-scope: se nessun token del dominio è presente, allora off-topic
    toks = tokenize(t)
    return all(tok not in IN_SCOPE_TERMS for tok in toks)


# -----------------------------
# Utility extra (opzionali ma utili)
# -----------------------------

YES = re.compile(r"^\s*(si|sì|ok|va bene|certo|procedi|andiamo|dai|yes|vai)\s*$", re.I)
NO = re.compile(r"^\s*(no|nah|nope)\s*$", re.I)

def is_yes(text: str) -> bool:
    return bool(YES.search(normalize(text)))

def is_no(text: str) -> bool:
    return bool(NO.search(normalize(text)))
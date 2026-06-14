"""
Normalizzazione centralizzata per market_type e operating_mode.

Unica fonte di verità per:
- lowercase, spazi, accenti, caratteri ripetuti
- alias diretti + fuzzy matching controllato
- valori canonici finali: spot/futures, aggressiva/equilibrata/selettiva
"""

from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Optional

logger = logging.getLogger(__name__)

CANONICAL_MARKET_TYPES = ("spot", "futures")
CANONICAL_OPERATING_MODES = ("aggressiva", "equilibrata", "selettiva")

STRATEGY_ID_BY_MODE = {
    "aggressiva": "1",
    "equilibrata": "2",
    "selettiva": "3",
}

MARKET_TYPE_ALIASES: dict[str, str] = {
    "spot": "spot",
    "sport": "spot",
    "spoot": "spot",
    "spott": "spot",
    "futures": "futures",
    "future": "futures",
    "futurs": "futures",
    "perpetual": "futures",
    "perpetuo": "futures",
    "perpetua": "futures",
}

OPERATING_MODE_ALIASES: dict[str, str] = {
    "aggressiva": "aggressiva",
    "aggressivo": "aggressiva",
    "aggressive": "aggressiva",
    # preprocess_config_token collassa "aggressive" -> "agresive"
    "agresive": "aggressiva",
    "equilibrata": "equilibrata",
    "equilibrato": "equilibrata",
    "balanced": "equilibrata",
    "selettiva": "selettiva",
    "selettivo": "selettiva",
    "selective": "selettiva",
}

_FUZZY_CUTOFF = 0.84


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _collapse_repeated_chars(text: str) -> str:
    return re.sub(r"(.)\1+", r"\1", text)


def preprocess_config_token(raw: Any) -> str:
    """Normalizza un token singolo: lowercase, no accenti, no spazi, collapse ripetizioni."""
    s = _strip_accents(str(raw or "").strip().lower())
    s = re.sub(r"\s+", "", s)
    return _collapse_repeated_chars(s)


def _fuzzy_match_canonical(token: str, canonical: tuple[str, ...]) -> Optional[str]:
    if not token:
        return None
    best: Optional[str] = None
    best_ratio = 0.0
    for candidate in canonical:
        ratio = SequenceMatcher(None, token, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = candidate
    if best and best_ratio >= _FUZZY_CUTOFF:
        logger.info(
            "[CONFIG_NORM] fuzzy match token=%r -> %s ratio=%.3f",
            token,
            best,
            best_ratio,
        )
        return best
    return None


def normalize_market_type(raw: Any) -> Optional[str]:
    """
    Ritorna 'spot' o 'futures' se la correzione è sicura, altrimenti None.
    """
    if raw is None:
        return None
    token = preprocess_config_token(raw)
    if not token:
        return None
    if token in MARKET_TYPE_ALIASES:
        return MARKET_TYPE_ALIASES[token]
    if token in CANONICAL_MARKET_TYPES:
        return token
    return _fuzzy_match_canonical(token, CANONICAL_MARKET_TYPES)


def normalize_operating_mode(raw: Any) -> Optional[str]:
    """
    Ritorna 'aggressiva', 'equilibrata' o 'selettiva' se la correzione è sicura, altrimenti None.
    """
    if raw is None:
        return None
    token = preprocess_config_token(raw)
    if not token:
        return None
    if token in OPERATING_MODE_ALIASES:
        return OPERATING_MODE_ALIASES[token]
    if token in CANONICAL_OPERATING_MODES:
        return token
    return _fuzzy_match_canonical(token, CANONICAL_OPERATING_MODES)


def normalize_operating_mode_value(raw: Any) -> Optional[str]:
    """Alias per compatibilità con import esistenti."""
    return normalize_operating_mode(raw)


def extract_market_type_from_text(text: str) -> Optional[str]:
    """
    Estrae market_type da un messaggio utente.
    Ritorna None se assente, ambiguo (spot+futures) o non riconosciuto con sicurezza.
    """
    if not text or not str(text).strip():
        return None

    lt = str(text).strip().lower()
    spot_hit: Optional[str] = None
    futures_hit: Optional[str] = None

    for word in re.findall(r"\b[\w']+\b", lt, re.UNICODE):
        norm = normalize_market_type(word)
        if norm == "spot":
            spot_hit = "spot"
        elif norm == "futures":
            futures_hit = "futures"

    if spot_hit and futures_hit:
        return None
    if futures_hit:
        return futures_hit
    if spot_hit:
        return spot_hit
    return None


# EN: numeri composti usati spesso nei segmenti durata (es. "sixty minutes" → 60 minutes).
_TF_SEGMENT_EN_NUMBERS: dict[str, str] = {
    "thirty": "30",
    "forty": "40",
    "fifty": "50",
    "sixty": "60",
    "seventy": "70",
    "eighty": "80",
    "ninety": "90",
    "hundred": "100",
}

# Segmenti durata/timeframe nel messaggio, anche senza keyword "timeframe".
_TF_DURATION_SEGMENT_RE = re.compile(
    r"\b("
    r"(?:\d+|[a-z']+)\s+"
    r"(?:min(?:ute)?s?|minut[oi]|hours?|hrs?|ore?|ora)"
    r"|"
    r"\d+[mhd]"
    r")\b",
    re.IGNORECASE,
)


def _normalize_timeframe_segment(segment: str) -> str:
    """Normalizza numeri in lettere nel segmento prima di normalize_timeframe_input."""
    from idith.text_normalize_user_numbers import (
        normalize_decimal_commas_in_text,
        replace_spelled_numbers_in_text,
    )

    s = str(segment or "").strip().lower()
    for word, digit in _TF_SEGMENT_EN_NUMBERS.items():
        s = re.sub(rf"\b{re.escape(word)}\b", digit, s, flags=re.I)
    s = replace_spelled_numbers_in_text(s)
    return normalize_decimal_commas_in_text(s)


def extract_timeframe_from_message(text: str) -> Optional[str]:
    """
    Estrae timeframe da messaggi multi-parametro.
    Cerca segmenti tipo "sixty minutes", "five minutes", "one hour", "4 hours", "15m"
    e li passa a normalize_timeframe_input (dopo normalizzazione numerica del segmento).
    """
    if not text or not str(text).strip():
        return None

    from idith.validators import normalize_timeframe_input
    from idith.text_normalize_user_numbers import normalize_user_numeric_input

    resolved: Optional[str] = None
    for match in _TF_DURATION_SEGMENT_RE.finditer(str(text)):
        segment = match.group(1).strip()
        normalized_segment = _normalize_timeframe_segment(segment)
        tf = normalize_timeframe_input(normalized_segment)
        if tf:
            resolved = tf

    if resolved:
        return resolved

    return normalize_timeframe_input(normalize_user_numeric_input(text))


def extract_operating_mode_from_text(text: str) -> Optional[str]:
    """
    Estrae operating_mode da un messaggio utente.
    Ritorna None se assente, ambiguo o non riconosciuto con sicurezza.
    """
    if not text or not str(text).strip():
        return None

    lt = str(text).strip().lower()
    found: Optional[str] = None

    for word in re.findall(r"\b[\w']+\b", lt, re.UNICODE):
        norm = normalize_operating_mode(word)
        if not norm:
            continue
        if found and found != norm:
            return None
        found = norm

    if found:
        logger.info("[OPERATING_MODE_EXTRACT] explicit=%s text=%r", found, text)
    else:
        logger.info("[OPERATING_MODE_EXTRACT] explicit=%s text=%r", None, text)
    return found

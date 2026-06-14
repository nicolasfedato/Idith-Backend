"""Riconoscimento intenti utente riutilizzabili (IT + EN): raccomandazione, conferma, rifiuto."""

from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

FUZZY_MATCH_THRESHOLD = 0.82
SHORT_TOKEN_FUZZY_THRESHOLD = 0.85
CONFIRMATION_TOKEN_FUZZY_THRESHOLD = 0.8

RECOMMENDATION_EXACT_PHRASES = frozenset(
    {
        "non so",
        "non saprei",
        "non lo so",
        "consigliami",
        "scegli tu",
        "fai tu",
        "decidi tu",
        "scegli per me",
        "cosa consigli",
        "cosa mi consigli",
        "migliore",
        "consigliato",
        "i dont know",
        "not sure",
        "recommend",
        "recommend me",
        "what do you recommend",
        "suggest",
        "suggest one",
        "choose for me",
        "you decide",
        "best",
        "help",
        "aiuto",
        "help me",
        "aiutami",
    }
)

RECOMMENDATION_CONTAINS_PHRASES = (
    "non so",
    "non saprei",
    "non lo so",
    "non sono sicuro",
    "i dont know",
    "not sure",
    "what do you recommend",
    "cosa consigli",
    "cosa mi consigli",
    "choose for me",
    "you decide",
    "scegli tu",
    "fai tu",
    "decidi tu",
    "scegli per me",
    "suggest one",
    "recommend me",
    "recommend one",
    "consigliami",
    "consigliato",
    "mi consigli",
    "aiutami a scegliere",
    "cosa scelgo",
)

RECOMMENDATION_KEYWORD_TOKENS = frozenset(
    {
        "recommend",
        "suggest",
        "best",
        "migliore",
        "consigliami",
        "consigliato",
        "consigli",
    }
)

CONFIRMATION_EXACT_TOKENS = frozenset(
    {
        "si",
        "yes",
        "y",
        "ok",
        "okay",
        "confermo",
        "conferma",
        "confirm",
        "confirmed",
        "proceed",
        "procedi",
        "va bene",
        "default",
        "accetto",
    }
)

CONFIRMATION_CONTAINS_TOKENS = frozenset(
    {
        "si",
        "yes",
        "y",
        "ok",
        "okay",
        "confermo",
        "conferma",
        "confirm",
        "confirmed",
        "proceed",
        "procedi",
        "accetto",
    }
)

CONFIRMATION_CONTAINS_PHRASES = ("va bene",)

REJECTION_EXACT_TOKENS = frozenset(
    {
        "no",
        "n",
        "cancel",
        "annulla",
        "stop",
        "non confermo",
    }
)

REJECTION_CONTAINS_TOKENS = frozenset({"no", "n", "cancel", "annulla", "stop"})

REJECTION_CONTAINS_PHRASES = ("non confermo",)

_APOSTROPHE_CHARS = ("\u2019", "\u2018", "`", "\xb4")


def normalize_intent_text(text: str) -> str:
    """Lowercase, trim, strip accenti, normalizza apostrofi e punteggiatura ai bordi."""
    s = str(text or "").strip().lower()
    for ch in _APOSTROPHE_CHARS:
        s = s.replace(ch, "'")
    s = s.replace("'", "")
    nfkd = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in nfkd if not unicodedata.combining(c))
    s = re.sub(r"^[\s.!?,;:]+", "", s)
    s = re.sub(r"[\s.!?,;:]+$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _best_fuzzy_ratio(text: str, candidates: tuple[str, ...] | frozenset[str]) -> float:
    best = 0.0
    for candidate in candidates:
        ratio = _fuzzy_ratio(text, candidate)
        if ratio > best:
            best = ratio
    return best


def _token_fuzzy_match(text: str, tokens: frozenset[str], threshold: float) -> bool:
    for token in text.split():
        if not token:
            continue
        if _best_fuzzy_ratio(token, tokens) >= threshold:
            return True
    return False


def _contains_phrase(text: str, phrase: str) -> bool:
    if " " in phrase:
        return phrase in text
    return bool(re.search(rf"\b{re.escape(phrase)}\b", text))


def _contains_any_phrase(text: str, phrases: tuple[str, ...] | frozenset[str]) -> bool:
    return any(_contains_phrase(text, phrase) for phrase in phrases)


def _contains_any_token(text: str, tokens: frozenset[str]) -> bool:
    return any(_contains_phrase(text, token) for token in tokens)


def _detect_recommendation(normalized: str) -> bool:
    if not normalized:
        return False
    if normalized in RECOMMENDATION_EXACT_PHRASES:
        return True
    if _contains_any_phrase(normalized, RECOMMENDATION_CONTAINS_PHRASES):
        return True
    if _contains_any_token(normalized, RECOMMENDATION_KEYWORD_TOKENS):
        return True
    if _best_fuzzy_ratio(normalized, RECOMMENDATION_EXACT_PHRASES) >= FUZZY_MATCH_THRESHOLD:
        return True
    if _token_fuzzy_match(normalized, RECOMMENDATION_KEYWORD_TOKENS, SHORT_TOKEN_FUZZY_THRESHOLD):
        return True
    return False


def _detect_confirmation(normalized: str) -> bool:
    if not normalized:
        return False
    if normalized in CONFIRMATION_EXACT_TOKENS:
        return True
    if _contains_any_phrase(normalized, CONFIRMATION_CONTAINS_PHRASES):
        return True
    if _contains_any_token(normalized, CONFIRMATION_CONTAINS_TOKENS):
        return True
    if _best_fuzzy_ratio(normalized, CONFIRMATION_EXACT_TOKENS) >= SHORT_TOKEN_FUZZY_THRESHOLD:
        return True
    if _token_fuzzy_match(normalized, CONFIRMATION_CONTAINS_TOKENS, CONFIRMATION_TOKEN_FUZZY_THRESHOLD):
        return True
    return False


def _detect_rejection(normalized: str) -> bool:
    if not normalized:
        return False
    if normalized in REJECTION_EXACT_TOKENS:
        return True
    if _contains_any_phrase(normalized, REJECTION_CONTAINS_PHRASES):
        return True
    if _contains_any_token(normalized, REJECTION_CONTAINS_TOKENS):
        return True
    if _best_fuzzy_ratio(normalized, REJECTION_EXACT_TOKENS) >= SHORT_TOKEN_FUZZY_THRESHOLD:
        return True
    return False


def is_recommendation_request(text: str) -> bool:
    normalized = normalize_intent_text(text)
    result = _detect_recommendation(normalized)
    logger.info(
        "[INTENT_DETECT] kind=recommendation raw=%r normalized=%r result=%s",
        text,
        normalized,
        result,
    )
    return result


def is_confirmation(text: str) -> bool:
    normalized = normalize_intent_text(text)
    result = _detect_confirmation(normalized)
    logger.info(
        "[INTENT_DETECT] kind=confirmation raw=%r normalized=%r result=%s",
        text,
        normalized,
        result,
    )
    return result


def is_rejection(text: str) -> bool:
    normalized = normalize_intent_text(text)
    result = _detect_rejection(normalized)
    logger.info(
        "[INTENT_DETECT] kind=rejection raw=%r normalized=%r result=%s",
        text,
        normalized,
        result,
    )
    return result

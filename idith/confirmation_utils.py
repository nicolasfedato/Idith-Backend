"""Normalizzazione e riconoscimento conferme/rifiuti utente (IT + EN)."""

from __future__ import annotations

import re

from .intent_detectors import (
    CONFIRMATION_CONTAINS_TOKENS,
    is_confirmation,
    is_rejection,
    normalize_intent_text,
)

CONFIRMATION_TOKENS = frozenset(
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
    }
)

REJECTION_TOKENS = frozenset(
    {
        "no",
        "n",
        "cancel",
        "annulla",
        "stop",
        "non confermo",
    }
)

_CONFIRM_TOKEN_PATTERN = "|".join(
    sorted(
        (
            re.escape(token) if " " not in token else re.escape(token).replace(r"\ ", r"\s+")
            for token in CONFIRMATION_CONTAINS_TOKENS | {"va bene"}
        ),
        key=len,
        reverse=True,
    )
)
CONFIRM_TOKEN_IN_MESSAGE_RE = re.compile(rf"\b(?:{_CONFIRM_TOKEN_PATTERN})\b", re.I)


def normalize_confirmation_text(text: str) -> str:
    """Lowercase, trim, strip accenti, rimuove punteggiatura semplice ai bordi."""
    return normalize_intent_text(text)


__all__ = [
    "CONFIRMATION_TOKENS",
    "CONFIRM_TOKEN_IN_MESSAGE_RE",
    "REJECTION_TOKENS",
    "is_confirmation",
    "is_rejection",
    "normalize_confirmation_text",
]

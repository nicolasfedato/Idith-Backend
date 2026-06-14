"""
Normalizzazione centralizzata dei numeri in input utente.

- Numeri in lettere (IT/EN) → cifre (es. "sl dieci" → "sl 10")
- Virgola decimale italiana → punto (es. "2,5" → "2.5")
"""

from __future__ import annotations

import re
from typing import Any, Optional, Union

from idith.validators import _TF_NUMBER_WORDS

_DECIMAL_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")
_NUMERIC_LITERAL_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")

# Estensione parole numeriche oltre a _TF_NUMBER_WORDS (timeframe).
_EXTRA_SPELLING_WORDS: dict[str, str] = {
    "zero": "0",
    "tredici": "13",
    "quattordici": "14",
    "quindici": "15",
    "sedici": "16",
    "diciassette": "17",
    "diciotto": "18",
    "diciannove": "19",
    "venti": "20",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}

_SPELLING_WORDS_CACHE: dict[str, str] | None = None


def _spelling_words() -> dict[str, str]:
    global _SPELLING_WORDS_CACHE
    if _SPELLING_WORDS_CACHE is None:
        merged = dict(_TF_NUMBER_WORDS)
        merged.update(_EXTRA_SPELLING_WORDS)
        _SPELLING_WORDS_CACHE = merged
    return _SPELLING_WORDS_CACHE


def replace_spelled_numbers_in_text(text: str) -> str:
    """Sostituisce numeri scritti in lettere (IT/EN) con cifre, solo parole intere."""
    if not text:
        return text
    out = str(text)
    for word, digit in _spelling_words().items():
        out = re.sub(rf"\b{re.escape(word)}\b", digit, out, flags=re.I)
    return out


def normalize_decimal_commas_in_text(text: str) -> str:
    """Applica la sostituzione virgola->punto su un messaggio o token (solo decimali)."""
    if not text:
        return text
    return _DECIMAL_COMMA_RE.sub(".", str(text))


def normalize_user_numeric_input(text: str) -> str:
    """
    Normalizza input numerico utente prima di estrazione/validazione parametri:
    numeri in lettere, poi virgole decimali.
    """
    if not text:
        return text
    return normalize_decimal_commas_in_text(replace_spelled_numbers_in_text(text))


def normalize_numeric_string(raw: Any) -> str:
    """Normalizza una stringa numerica singola (virgole decimali, strip)."""
    if raw is None:
        return ""
    return normalize_decimal_commas_in_text(str(raw).strip())


def strip_percent_suffix(raw: str) -> str:
    return raw.strip().rstrip("%").strip()


def parse_config_float(raw: Any) -> Optional[float]:
    """
    Interpreta valori numerici di configurazione: 1,5 / 2.5 / 3,5% / 10.
    Ritorna None se non è un numero sicuro.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    s = normalize_numeric_string(raw)
    s = strip_percent_suffix(s)
    if not s:
        return None
    if not _NUMERIC_LITERAL_RE.fullmatch(s):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def format_percent_config_string(value: Union[int, float, str]) -> Optional[str]:
    """Formato sl/tp in config_state: '1.5%', '2.5%'."""
    parsed = parse_config_float(value)
    if parsed is None:
        return None
    if abs(parsed - round(parsed)) < 1e-9:
        return f"{int(round(parsed))}%"
    return f"{parsed:.1f}%"


def format_leverage(value: Any) -> str:
    """
    Display leva per chat/riepilogo.
    None -> '—', 5.0 -> '5x', 5.5 -> '5.5x'.
    """
    if value is None or value == "":
        return "—"
    parsed = parse_config_float(value)
    if parsed is None:
        s = str(value).strip().rstrip("x").strip()
        return f"{s}x" if s else "—"
    if abs(parsed - round(parsed)) < 1e-9:
        return f"{int(round(parsed))}x"
    s = f"{parsed:.10f}".rstrip("0").rstrip(".")
    return f"{s}x"

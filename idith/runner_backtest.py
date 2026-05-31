"""
Rilevamento richieste BACKTEST_PREVIEW e costruzione payload per runner_commands.
Fase 1: solo accodamento comando, nessuna simulazione lato backend.
"""
from __future__ import annotations

import re
import string
from typing import Any, Dict, Optional

# Stesso mapping accenti usato in app.py (normalize_user_text)
ACCENT_MAP = {
    "à": "a",
    "è": "e",
    "é": "e",
    "ì": "i",
    "ò": "o",
    "ù": "u",
    "À": "a",
    "È": "e",
    "É": "e",
    "Ì": "i",
    "Ò": "o",
    "Ù": "u",
}

BACKTEST_PREVIEW_CANDIDATE_PHRASES = (
    "preview ultimi 30 giorni",
    "anteprima ultimi 30 giorni",
    "simulazione ultimi 30 giorni",
    "backtest preview",
    "preview degli ultimi 30 giorni",
    "anteprima degli ultimi 30 giorni",
    "fammi una preview degli ultimi 30 giorni",
    "fammi vedere come si sarebbe comportato",
    "come si sarebbe comportato",
    "simulazione degli ultimi 30 giorni",
)

BACKTEST_PREVIEW_KEYWORDS = (
    "preview",
    "anteprima",
    "simulazione",
    "backtest",
)

BACKTEST_LOOKBACK_KEYWORDS = (
    "30",
    "giorni",
    "ultimi",
    "ultimo mese",
)

BACKTEST_BEHAVIOR_KEYWORDS = (
    "comportato",
    "andata",
    "andato",
    "sarebbe andata",
)


def normalize_for_matching(text: str) -> str:
    if not text:
        return ""
    normalized = text.strip().lower()
    for accented, unaccented in ACCENT_MAP.items():
        normalized = normalized.replace(accented, unaccented)
    normalized = normalized.translate(str.maketrans("", "", string.punctuation))
    return " ".join(normalized.split())


def extract_lookback_days(text: str, default: int = 30) -> int:
    """Estrae lookback_days dal testo; default 30."""
    normalized = normalize_for_matching(text)
    if not normalized:
        return default
    m = re.search(r"\bultim[oi]?\s+(\d{1,3})\s+giorn", normalized)
    if m:
        try:
            days = int(m.group(1))
            if days > 0:
                return days
        except (TypeError, ValueError):
            pass
    m = re.search(r"\b(\d{1,3})\s+giorn", normalized)
    if m:
        try:
            days = int(m.group(1))
            if days > 0:
                return days
        except (TypeError, ValueError):
            pass
    return default


def is_backtest_preview_request(text: str) -> bool:
    """
    True se il messaggio chiede una preview / anteprima / simulazione (ultimi N giorni).
    Intercetta prima dell'orchestrator per non disturbare il flow di configurazione.
    """
    normalized = normalize_for_matching(text)
    if not normalized:
        return False

    for phrase in BACKTEST_PREVIEW_CANDIDATE_PHRASES:
        if phrase in normalized:
            return True

    has_preview_kw = any(kw in normalized for kw in BACKTEST_PREVIEW_KEYWORDS)
    has_lookback = any(kw in normalized for kw in BACKTEST_LOOKBACK_KEYWORDS)
    has_behavior = any(kw in normalized for kw in BACKTEST_BEHAVIOR_KEYWORDS)

    if has_preview_kw and has_lookback:
        return True

    if has_preview_kw and "backtest" in normalized:
        return True

    if has_behavior and (has_lookback or has_preview_kw):
        return True

    if "simulazione" in normalized and has_lookback:
        return True

    return False


def build_config_from_config_state(config_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Mappa config_state.params nel sotto-oggetto config atteso dal runner.
    Usa time_frame (alias timeframe in DB).
    """
    params: Dict[str, Any] = {}
    if isinstance(config_state, dict):
        raw_params = config_state.get("params")
        if isinstance(raw_params, dict):
            params = raw_params

    timeframe = params.get("timeframe")
    strategy_params = params.get("strategy_params")
    if strategy_params is not None and not isinstance(strategy_params, dict):
        strategy_params = None

    return {
        "symbol": params.get("symbol"),
        "market_type": params.get("market_type"),
        "time_frame": timeframe,
        "operating_mode": params.get("operating_mode"),
        "strategy_id": params.get("strategy_id"),
        "strategy_params": strategy_params,
        "leverage": params.get("leverage"),
        "risk_pct": params.get("risk_pct"),
        "sl": params.get("sl"),
        "tp": params.get("tp"),
    }


def build_backtest_preview_payload(
    chat_id: str,
    config_state: Optional[Dict[str, Any]],
    lookback_days: int = 30,
) -> Dict[str, Any]:
    return {
        "action": "BACKTEST_PREVIEW",
        "chat_id": chat_id,
        "lookback_days": lookback_days,
        "config": build_config_from_config_state(config_state),
        "source": "backend",
    }

"""
Servizio isolato per lo storico delle preview salvate in Supabase.
Solo lettura da public.backtest_previews — nessun runner, nessuna nuova preview.
"""
from __future__ import annotations

import logging
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ROME_TZ = ZoneInfo("Europe/Rome")
HISTORY_LIMIT = 10

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

_HISTORY_PHRASE_TRIGGERS = (
    "mostrami le preview",
    "mostra le preview",
    "le mie preview",
    "cronologia preview",
    "storico preview",
    "ultime preview",
    "mostra preview",
    "mie preview",
)


@dataclass
class PreviewHistoryDeps:
    supabase: Any


def normalize_for_matching(text: str) -> str:
    if not text:
        return ""
    normalized = text.strip().lower()
    for accented, unaccented in ACCENT_MAP.items():
        normalized = normalized.replace(accented, unaccented)
    normalized = normalized.translate(str.maketrans("", "", string.punctuation))
    return " ".join(normalized.split())


def is_preview_history_request(text: str) -> bool:
    normalized = normalize_for_matching(text)
    if not normalized:
        return False
    return any(phrase in normalized for phrase in _HISTORY_PHRASE_TRIGGERS)


def _parse_created_at(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_created_at_rome(value: Any) -> str:
    """Converte created_at UTC → Europe/Rome, formato DD/MM/YYYY HH:mm."""
    dt = _parse_created_at(value)
    if dt is None:
        return "N/D"
    return dt.astimezone(ROME_TZ).strftime("%d/%m/%Y %H:%M")


def format_pnl_pct(value: Any) -> str:
    if value is None:
        return "N/D"
    try:
        if isinstance(value, str):
            value = value.strip().replace("%", "")
        pnl = float(value)
    except (TypeError, ValueError):
        return "N/D"
    if pnl > 0:
        return f"+{pnl:.1f}%"
    if pnl < 0:
        return f"-{abs(pnl):.1f}%"
    return "+0.0%"


def _display_field(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "N/D"


def format_preview_history(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "Non hai ancora generato nessuna preview."

    lines = ["📊 Storico Preview", ""]
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        timestamp = format_created_at_rome(row.get("created_at"))
        symbol = _display_field(row.get("symbol"))
        timeframe = _display_field(row.get("timeframe"))
        operating_mode = _display_field(row.get("operating_mode"))
        pnl_display = format_pnl_pct(row.get("pnl_pct"))

        lines.append(f"{timestamp} • {symbol} • {timeframe} • {operating_mode}")
        lines.append(f"Risultato: {pnl_display}")
        if i < len(rows) - 1:
            lines.append("")

    return "\n".join(lines)


def fetch_preview_history(
    user_id: str,
    deps: PreviewHistoryDeps,
    limit: int = HISTORY_LIMIT,
) -> List[Dict[str, Any]]:
    logger.info("[PREVIEW_HISTORY] loading history")
    try:
        res = (
            deps.supabase.table("backtest_previews")
            .select("created_at, symbol, timeframe, operating_mode, pnl_pct")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        if not isinstance(rows, list):
            rows = []
        logger.info("[PREVIEW_HISTORY] rows_found=%s", len(rows))
        return rows
    except Exception as e:
        logger.error(
            "[PREVIEW_HISTORY] fetch failed user_id=%s error=%s",
            user_id,
            e,
            exc_info=True,
        )
        return []


def handle_preview_history_request(
    user_id: str,
    deps: PreviewHistoryDeps,
) -> Tuple[str, str, str]:
    rows = fetch_preview_history(user_id, deps)
    if not rows:
        logger.info("[PREVIEW_HISTORY] no_history")
    reply = format_preview_history(rows)
    return reply, "preview_history", "preview_history_list"

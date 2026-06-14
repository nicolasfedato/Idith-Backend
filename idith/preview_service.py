"""
Servizio isolato per preview/backtest storico.
Legge config_state (solo lettura), accoda BACKTEST_PREVIEW al runner, formatta la risposta.
Non modifica orchestrator né config_state.
"""
from __future__ import annotations

import logging
import re
import string
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _normalize_config_market_type(raw: Any) -> Optional[str]:
    from .text_normalize_config_values import normalize_market_type

    return normalize_market_type(raw)


def _normalize_config_operating_mode(raw: Any) -> Optional[str]:
    from .text_normalize_config_values import normalize_operating_mode

    return normalize_operating_mode(raw)


runner_backtest_mod = None
try:
    from . import runner_backtest as runner_backtest_mod
except Exception:
    try:
        import idith.runner_backtest as runner_backtest_mod
    except Exception:
        try:
            from idith import runner_backtest as runner_backtest_mod
        except Exception:
            try:
                import runner_backtest as runner_backtest_mod
            except Exception:
                runner_backtest_mod = None

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

# Frasi intere (ordine: più lunghe prima per match corretto)
_PREVIEW_PHRASE_TRIGGERS = (
    "simulazione storica",
    "preview storica",
    "simula gli ultimi",
    "simula ultimi",
    "come si sarebbe comportato",
    "come avrebbe performato",
)

# Parole/chiavi singole sufficienti da sole
_PREVIEW_WORD_TRIGGERS = (
    "preview",
    "anteprima",
    "backtest",
)

_FIELD_LABELS = {
    "symbol": "coppia",
    "timeframe": "timeframe",
    "market_type": "tipo di mercato",
    "sl": "stop loss",
    "tp": "take profit",
}

POLL_TIMEOUT_SECONDS = 450.0
POLL_INTERVAL_SECONDS = 0.5


@dataclass
class PreviewDeps:
    supabase: Any
    supabase_queue: Any
    load_chat_state: Callable[[str], dict]
    resolve_runner_device_id: Callable[[str, str], Optional[str]]


def normalize_for_matching(text: str) -> str:
    if not text:
        return ""
    normalized = text.strip().lower()
    for accented, unaccented in ACCENT_MAP.items():
        normalized = normalized.replace(accented, unaccented)
    normalized = normalized.translate(str.maketrans("", "", string.punctuation))
    return " ".join(normalized.split())


def is_preview_request(text: str) -> bool:
    """
    Detection conservativa: 'simula' da solo NON basta.
    """
    normalized = normalize_for_matching(text)
    if not normalized:
        return False
    for phrase in _PREVIEW_PHRASE_TRIGGERS:
        if phrase in normalized:
            return True
    for word in _PREVIEW_WORD_TRIGGERS:
        if word in normalized:
            return True
    return False


def extract_lookback(text: str, default: int = 30) -> Tuple[int, bool]:
    """
    Ritorna (lookback_days, user_specified_lookback).
    user_specified_lookback=True solo se l'utente indica esplicitamente un periodo.
    """
    normalized = normalize_for_matching(text)
    if not normalized:
        return default, False
    m = re.search(r"\bultim[oi]?\s+(\d{1,2})\s+ann", normalized)
    if m:
        try:
            years = int(m.group(1))
            if years > 0:
                return years * 365, True
        except (TypeError, ValueError):
            pass
    m = re.search(r"\b(\d{1,2})\s+ann", normalized)
    if m:
        try:
            years = int(m.group(1))
            if years > 0:
                return years * 365, True
        except (TypeError, ValueError):
            pass
    m = re.search(r"\bultim[oi]?\s+(\d{1,3})\s+giorn", normalized)
    if m:
        try:
            days = int(m.group(1))
            if days > 0:
                return days, True
        except (TypeError, ValueError):
            pass
    m = re.search(r"\b(\d{1,3})\s+giorn", normalized)
    if m:
        try:
            days = int(m.group(1))
            if days > 0:
                return days, True
        except (TypeError, ValueError):
            pass
    return default, False


def _has_nonempty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _params_from_config_state(config_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(config_state, dict):
        return {}
    raw_params = config_state.get("params")
    if isinstance(raw_params, dict):
        return raw_params
    return {}


def validate_preview_config_params(params: Dict[str, Any]) -> List[str]:
    """
    Valida campi minimi richiesti prima di avviare il runner.
    Ritorna lista di chiavi mancanti (symbol, timeframe, market_type, sl, tp).
    """
    missing: List[str] = []
    if not _has_nonempty_value(params.get("symbol")):
        missing.append("symbol")
    timeframe = params.get("timeframe") or params.get("time_frame")
    if not _has_nonempty_value(timeframe):
        missing.append("timeframe")
    if not _has_nonempty_value(params.get("market_type")):
        missing.append("market_type")
    sl = params.get("sl") or params.get("stop_loss")
    if not _has_nonempty_value(sl):
        missing.append("sl")
    tp = params.get("tp") or params.get("take_profit")
    if not _has_nonempty_value(tp):
        missing.append("tp")
    return missing


def _missing_fields_message(missing_keys: List[str]) -> str:
    labels = [_FIELD_LABELS.get(k, k) for k in missing_keys]
    if len(labels) == 1:
        fields_text = labels[0]
    elif len(labels) == 2:
        fields_text = f"{labels[0]} e {labels[1]}"
    else:
        fields_text = ", ".join(labels[:-1]) + f" e {labels[-1]}"
    return (
        f"Per generare la preview mi mancano ancora: {fields_text}. "
        "Completiamo prima questi parametri."
    )


def build_preview_payload(
    chat_id: str,
    config_state: Optional[Dict[str, Any]],
    lookback_days: int,
    user_specified_lookback: bool,
) -> Dict[str, Any]:
    if runner_backtest_mod:
        payload = runner_backtest_mod.build_backtest_preview_payload(
            chat_id=chat_id,
            config_state=config_state if isinstance(config_state, dict) else None,
            lookback_days=lookback_days,
        )
    else:
        payload = {
            "action": "BACKTEST_PREVIEW",
            "chat_id": chat_id,
            "lookback_days": lookback_days,
            "config": {},
            "source": "backend",
        }
    payload["lookback_days"] = lookback_days
    payload["user_specified_lookback"] = user_specified_lookback
    return payload


def _format_pct(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n > 0:
        return f"+{n:.1f}%"
    return f"{n:.1f}%"


def _format_level_pct(value: Any) -> Optional[str]:
    """Percentuale semplice per SL/TP (senza segno +)."""
    from .text_normalize_user_numbers import parse_config_float

    if value is None:
        return None
    n = parse_config_float(value)
    if n is None:
        return None
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}".rstrip("0").rstrip(".")


def _format_market_type_display(market_type: Any) -> Optional[str]:
    from .text_normalize_config_values import normalize_market_type

    if not isinstance(market_type, str) or not market_type.strip():
        return None
    mt = normalize_market_type(market_type)
    if mt:
        return mt
    raw = market_type.strip().lower()
    if raw in ("linear", "future"):
        return "futures"
    return raw


def _format_leverage(value: Any) -> Optional[str]:
    from .text_normalize_user_numbers import format_leverage, parse_config_float

    n = parse_config_float(value)
    if n is None or n <= 0:
        return None
    formatted = format_leverage(value)
    return None if formatted == "—" else formatted


def _format_risk_pct(value: Any) -> Optional[str]:
    from .text_normalize_user_numbers import parse_config_float

    if value is None:
        return None
    n = parse_config_float(value)
    if n is None:
        return None
    if n <= 0:
        return None
    if n == int(n):
        return f"{int(n)}%"
    return f"{n:.1f}".rstrip("0").rstrip(".") + "%"


def _format_win_rate_pct(value: float) -> str:
    if value == int(value):
        return f"{int(value)}%"
    return f"{value:.1f}%"


def _positive_int(value: Any, fallback: int) -> int:
    try:
        n = int(value)
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    return fallback


def _compute_win_rate(event_payload: Dict[str, Any]) -> Optional[float]:
    raw = event_payload.get("win_rate")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    wins = event_payload.get("wins")
    losses = event_payload.get("losses")
    if wins is None or losses is None:
        return None
    try:
        w = int(wins)
        l = int(losses)
    except (TypeError, ValueError):
        return None
    total = w + l
    if total <= 0:
        return None
    return (w / total) * 100.0


def _merge_config_for_display(
    event_payload: Dict[str, Any],
    config_params: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Arricchisce il payload evento solo per la formattazione risposta
    (leva/rischio dalla config se assenti nel DONE runner).
    """
    display = dict(event_payload)
    if not isinstance(config_params, dict):
        return display
    if display.get("leverage") is None and config_params.get("leverage") is not None:
        display["leverage"] = config_params.get("leverage")
    if display.get("risk_pct") is None and config_params.get("risk_pct") is not None:
        display["risk_pct"] = config_params.get("risk_pct")
    return display


def _coalesce_for_persist(
    event_payload: Dict[str, Any],
    config_params: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Unisce event_payload con config_params per persistenza (solo lettura config)."""
    merged = dict(event_payload)
    if not isinstance(config_params, dict):
        return merged
    for key in (
        "symbol",
        "market_type",
        "timeframe",
        "operating_mode",
        "strategy_id",
        "leverage",
        "risk_pct",
    ):
        if merged.get(key) is None and config_params.get(key) is not None:
            merged[key] = config_params.get(key)
    if merged.get("timeframe") is None:
        tf = config_params.get("timeframe") or config_params.get("time_frame")
        if tf is not None:
            merged["timeframe"] = tf
    sp = config_params.get("strategy_params")
    if merged.get("strategy_params") is None and isinstance(sp, dict):
        merged["strategy_params"] = sp
    return merged


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    s = str(value).strip()
    return s or None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    from .text_normalize_user_numbers import parse_config_float

    return parse_config_float(value)


def _build_backtest_previews_row(
    event_payload: Dict[str, Any],
    *,
    user_id: str,
    chat_id: str,
    command_id: str,
    config_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Mappa BACKTEST_PREVIEW_DONE → riga public.backtest_previews (colonne reali Supabase).
    command_id non è colonna tabella: finisce in preview_payload JSONB.
    """
    merged = _coalesce_for_persist(event_payload, config_params)
    preview_payload = dict(event_payload)
    preview_payload["command_id"] = command_id
    preview_payload["user_id"] = user_id
    preview_payload["chat_id"] = chat_id

    lookback = merged.get("effective_lookback_days")
    if lookback is None:
        lookback = merged.get("lookback_days")

    estimated = merged.get("simulated_trades")
    if estimated is None:
        estimated = merged.get("entries_count")

    closed = merged.get("closed_trades_count")
    if closed is None:
        closed = merged.get("trades_count")

    strategy_params = merged.get("strategy_params")
    if strategy_params is not None and not isinstance(strategy_params, dict):
        strategy_params = None

    row: Dict[str, Any] = {
        "user_id": user_id,
        "chat_id": chat_id,
        "symbol": _optional_str(merged.get("symbol")),
        "market_type": _optional_str(
            _normalize_config_market_type(merged.get("market_type"))
        ),
        "timeframe": _optional_str(merged.get("timeframe")),
        "operating_mode": _optional_str(
            _normalize_config_operating_mode(merged.get("operating_mode"))
        ),
        "strategy_id": _optional_str(merged.get("strategy_id")),
        "strategy_params": strategy_params,
        "stop_loss_pct": _optional_float(merged.get("sl_pct")),
        "take_profit_pct": _optional_float(merged.get("tp_pct")),
        "leverage": _optional_float(merged.get("leverage")),
        "risk_pct": _optional_float(merged.get("risk_pct")),
        "lookback_days": _optional_int(lookback),
        "estimated_trades": _optional_int(estimated),
        "closed_trades": _optional_int(closed),
        "wins": _optional_int(merged.get("wins")),
        "losses": _optional_int(merged.get("losses")),
        "pnl_pct": _optional_float(merged.get("pnl_pct")),
        "pnl_usdt": _optional_float(merged.get("pnl_usdt")),
        "max_drawdown_pct": _optional_float(merged.get("max_drawdown_pct")),
        "max_drawdown_usdt": _optional_float(merged.get("max_drawdown_usdt")),
        "final_capital": _optional_float(merged.get("final_capital_usdt")),
        "capital_simulated": _optional_float(merged.get("capital_usdt")),
        "capital_source": _optional_str(merged.get("capital_source")),
        "preview_payload": preview_payload,
    }
    return row


def _save_backtest_preview(
    deps: PreviewDeps,
    row: Dict[str, Any],
) -> None:
    """Inserisce in backtest_previews; errori non propagati."""
    logger.info("[PREVIEW_SERVICE] saving preview result")
    try:
        res = deps.supabase.table("backtest_previews").insert(row).execute()
        saved_id = None
        if res.data and isinstance(res.data, list) and res.data:
            saved_id = res.data[0].get("id")
        logger.info("[PREVIEW_SERVICE] preview saved id=%s", saved_id)
    except Exception as e:
        logger.error(
            "[PREVIEW_SERVICE] preview save failed error=%s",
            e,
            exc_info=True,
        )


def format_preview_done_reply(
    event_payload: Dict[str, Any],
    lookback_fallback: int,
    config_params: Optional[Dict[str, Any]] = None,
) -> str:
    display = _merge_config_for_display(event_payload, config_params)
    effective_lb = _positive_int(
        display.get("effective_lookback_days"), lookback_fallback
    )

    lines = [f"📊 Preview indicativa — ultimi {effective_lb} giorni", ""]

    meta_parts: List[str] = []
    symbol = display.get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        meta_parts.append(symbol.strip().upper())
    timeframe = display.get("timeframe")
    if isinstance(timeframe, str) and timeframe.strip():
        meta_parts.append(timeframe.strip())
    market_display = _format_market_type_display(display.get("market_type"))
    if market_display:
        meta_parts.append(market_display)
    if meta_parts:
        lines.append(" · ".join(meta_parts))

    operating_mode = _normalize_config_operating_mode(display.get("operating_mode"))
    if operating_mode:
        lines.append(f"Strategia: {operating_mode}")

    lev_display = _format_leverage(display.get("leverage"))
    risk_display = _format_risk_pct(display.get("risk_pct"))
    if lev_display is not None or risk_display is not None:
        lev_risk_parts: List[str] = []
        if lev_display is not None:
            lev_risk_parts.append(f"Leva {lev_display}")
        if risk_display is not None:
            lev_risk_parts.append(f"Rischio {risk_display}")
        lines.append(" · ".join(lev_risk_parts))

    sl_val = _format_level_pct(display.get("sl_pct"))
    tp_val = _format_level_pct(display.get("tp_pct"))
    if sl_val is not None or tp_val is not None:
        sl_tp_parts: List[str] = []
        if sl_val is not None:
            sl_tp_parts.append(f"SL {sl_val}%")
        if tp_val is not None:
            sl_tp_parts.append(f"TP {tp_val}%")
        lines.append(" · ".join(sl_tp_parts))

    lines.append("")

    pnl_pct = _format_pct(display.get("pnl_pct"))
    if pnl_pct:
        lines.append(f"Risultato stimato: {pnl_pct}")

    lines.append("")

    trades = display.get("simulated_trades")
    if trades is None:
        trades = display.get("closed_trades_count")
    if trades is None:
        trades = display.get("entries_count")
    if trades is not None:
        lines.append(f"Operazioni simulate: {trades}")

    win_rate = _compute_win_rate(display)
    if win_rate is not None:
        lines.append(f"Win rate: {_format_win_rate_pct(win_rate)}")

    dd_pct = _format_pct(display.get("max_drawdown_pct"))
    if dd_pct:
        lines.append(f"Drawdown massimo: {dd_pct}")

    lines.extend(
        [
            "",
            "Preview basata su dati storici Bybit.",
            "Non garantisce risultati futuri.",
        ]
    )
    return "\n".join(lines)


def format_preview_failed_reply(event_payload: Dict[str, Any]) -> str:
    error = event_payload.get("error") or "Errore sconosciuto durante la preview."
    return f"❌ Preview non completata: {error}"


def _poll_runner_preview_result(
    deps: PreviewDeps,
    device_id: str,
    command_id: str,
    deadline: float,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Poll runner_events per BACKTEST_PREVIEW_DONE o BACKTEST_PREVIEW_FAILED.
    Ritorna (event_type, payload) oppure (None, None) su timeout.
    """
    while time.monotonic() < deadline:
        for event_type in ("BACKTEST_PREVIEW_DONE", "BACKTEST_PREVIEW_FAILED"):
            try:
                res = (
                    deps.supabase.table("runner_events")
                    .select("payload")
                    .eq("device_id", device_id)
                    .eq("command_id", command_id)
                    .eq("type", event_type)
                    .order("created_at", desc=True)
                    .limit(1)
                    .execute()
                )
                rows = res.data or []
                if rows and isinstance(rows[0], dict):
                    raw_payload = rows[0].get("payload")
                    if isinstance(raw_payload, dict):
                        return event_type, raw_payload
            except Exception as poll_err:
                logger.warning(
                    "[PREVIEW_SERVICE] poll error command_id=%s type=%s: %s",
                    command_id,
                    event_type,
                    poll_err,
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    return None, None


def handle_preview_request(
    chat_id: str,
    user_id: str,
    user_message: str,
    deps: PreviewDeps,
) -> Tuple[str, str, str]:
    """
    Gestisce una richiesta preview end-to-end.
    Ritorna (assistant_reply, source, mode). Solo lettura su config_state.
    """
    logger.info("[PREVIEW_SERVICE] detected request chat_id=%s user_id=%s", chat_id, user_id)

    if not runner_backtest_mod:
        return (
            "❌ Modulo backtest preview non disponibile sul server.",
            "backtest_preview",
            "backtest_preview_error",
        )
    if not deps.supabase_queue:
        return (
            "❌ Modulo coda runner non disponibile sul server.",
            "backtest_preview",
            "backtest_preview_error",
        )

    chat_state = deps.load_chat_state(chat_id)
    config_state = chat_state.get("config_state") if isinstance(chat_state, dict) else None
    params = _params_from_config_state(
        config_state if isinstance(config_state, dict) else None
    )

    missing = validate_preview_config_params(params)
    if missing:
        logger.info("[PREVIEW_SERVICE] config validation missing=%s", missing)
        return (
            _missing_fields_message(missing),
            "backtest_preview",
            "backtest_preview_config_incomplete",
        )

    logger.info("[PREVIEW_SERVICE] config validation ok chat_id=%s", chat_id)

    device_id = deps.resolve_runner_device_id(chat_id, user_id)
    if not device_id:
        return (
            "Per generare la preview serve il runner collegato.",
            "backtest_preview",
            "backtest_preview_no_runner",
        )

    lookback_days, user_specified_lookback = extract_lookback(user_message)
    payload = build_preview_payload(
        chat_id=chat_id,
        config_state=config_state if isinstance(config_state, dict) else None,
        lookback_days=lookback_days,
        user_specified_lookback=user_specified_lookback,
    )
    payload["user_id"] = user_id

    try:
        command_id = deps.supabase_queue.enqueue_runner_command(
            device_id, payload, user_id=user_id
        )
        logger.info(
            "[PREVIEW_SERVICE] enqueue BACKTEST_PREVIEW command_id=%s chat_id=%s "
            "device_id=%s lookback_days=%s user_specified_lookback=%s",
            command_id,
            chat_id,
            device_id,
            lookback_days,
            user_specified_lookback,
        )

        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        event_type, event_payload = _poll_runner_preview_result(
            deps, device_id, command_id, deadline
        )

        if event_type == "BACKTEST_PREVIEW_FAILED" and event_payload:
            logger.info(
                "[PREVIEW_SERVICE] received FAILED command_id=%s chat_id=%s",
                command_id,
                chat_id,
            )
            return (
                format_preview_failed_reply(event_payload),
                "backtest_preview",
                "backtest_preview_failed",
            )

        if event_type == "BACKTEST_PREVIEW_DONE" and event_payload:
            logger.info(
                "[PREVIEW_SERVICE] received DONE command_id=%s chat_id=%s",
                command_id,
                chat_id,
            )
            save_row = _build_backtest_previews_row(
                event_payload,
                user_id=user_id,
                chat_id=chat_id,
                command_id=command_id,
                config_params=params,
            )
            _save_backtest_preview(deps, save_row)
            return (
                format_preview_done_reply(
                    event_payload, lookback_days, config_params=params
                ),
                "backtest_preview",
                "backtest_preview_done",
            )

        logger.warning(
            "[PREVIEW_SERVICE] timeout command_id=%s device_id=%s timeout_seconds=%s",
            command_id,
            device_id,
            POLL_TIMEOUT_SECONDS,
        )
        return (
            "Ho inviato la richiesta al runner, ma non ho ancora ricevuto il risultato. "
            "Verifica che il runner sia collegato e riprova.",
            "backtest_preview",
            "backtest_preview_timeout",
        )
    except Exception as e:
        logger.error(
            "[PREVIEW_SERVICE] enqueue failed chat_id=%s user_id=%s error=%s",
            chat_id,
            user_id,
            e,
            exc_info=True,
        )
        return (
            f"❌ Errore nell'invio della preview al runner: {e}",
            "backtest_preview",
            "backtest_preview_enqueue_failed",
        )

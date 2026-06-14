"""
Servizio isolato per spiegazione AI e consigli sulla ultima preview salvata.
Solo lettura da public.backtest_previews — nessun runner, nessuna scrittura su config_state.
"""
from __future__ import annotations

import json
import logging
import re
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    from . import ai_lang
except Exception:
    try:
        import ai_lang
    except Exception:
        ai_lang = None  # type: ignore[assignment]

try:
    from .preview_history_service import format_created_at_rome
except Exception:
    try:
        from idith.preview_history_service import format_created_at_rome
    except Exception:
        try:
            from preview_history_service import format_created_at_rome
        except Exception:
            _ROME_TZ = ZoneInfo("Europe/Rome")

            def format_created_at_rome(value: Any) -> str:
                if value is None:
                    return "N/D"
                if isinstance(value, datetime):
                    dt = value
                elif isinstance(value, str):
                    try:
                        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        return "N/D"
                else:
                    return "N/D"
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(_ROME_TZ).strftime("%d/%m/%Y %H:%M")

logger = logging.getLogger(__name__)


def _resolve_lang(lang: Optional[str] = None) -> str:
    if ai_lang is not None:
        return ai_lang.normalize_lang(lang or ai_lang.get_request_lang())
    code = (lang or "it").strip().lower()
    return "en" if code.startswith("en") else "it"


def _texts(lang: Optional[str] = None) -> Dict[str, str]:
    return _PREVIEW_TEXTS[_resolve_lang(lang)]


_LEGACY_CLOSING = "Vuoi che aggiorni la configurazione con questi parametri?"

_CHAT_PARAMS_HEADER = (
    "Puoi provare scrivendo in chat:\n"
    "modifica questi parametri:"
)

# Retrocompatibilità per import esistenti.
CONFIRMATION_QUESTION = _CHAT_PARAMS_HEADER

_SUGGESTIONS_HEADER = "Cosa proverei a cambiare"
_PREVIEW_ADVICE_CTA = "Vuoi modificare qualche parametro oppure vuoi avviare il bot?"

_PREVIEW_TEXTS: Dict[str, Dict[str, str]] = {
    "it": {
        "legacy_closing": _LEGACY_CLOSING,
        "chat_params_header": _CHAT_PARAMS_HEADER,
        "suggestions_header": _SUGGESTIONS_HEADER,
        "why_header": "Perché la preview è andata così",
        "preview_advice_cta": _PREVIEW_ADVICE_CTA,
        "start_bot_prompt": "Vuoi avviare il bot adesso?",
        "no_preview_reply": (
            'Non ho ancora una preview salvata per te. Genera prima una preview '
            '(es. "fammi una preview" o "simula gli ultimi 30 giorni") e poi chiedimi '
            "di spiegarla o di suggerirti come migliorare."
        ),
        "params_fallback": (
            "Puoi provare scrivendo in chat i parametri suggeriti sopra, "
            "specificando coppia, timeframe, modalità, stop loss, take profit e rischio."
        ),
        "label_operating_mode": "modalità operativa",
        "label_risk": "rischio per trade",
        "label_stop_loss": "stop loss",
        "label_take_profit": "take profit",
        "label_pair": "coppia",
        "nd": "N/D",
        "why_opening": (
            "La preview su {symbol} (timeframe {timeframe}) chiude a {pnl} "
            "con win rate {win_rate} su {trades} operazioni. "
            "Il drawdown massimo è stato {drawdown}."
        ),
        "why_negative": " Il risultato negativo indica che le perdite hanno superato i guadagni.",
        "why_positive": " Il risultato positivo indica che la strategia ha chiuso in guadagno nel periodo simulato.",
        "why_neutral": " Il risultato è in pareggio nel periodo simulato.",
        "why_low_wr": " Un win rate sotto il 45% rende difficile recuperare anche con buon risk/reward.",
        "why_high_dd": " Un drawdown elevato segnala sequenze di perdite consecutive pesanti.",
        "why_short_tf": " Il timeframe {timeframe} tende a generare più segnali e più rumore.",
        "why_sl_tp_tight": (
            " Con SL/TP a {sl}/{tp} i trade hanno poco margine prima di chiudersi in stop o in target."
        ),
        "why_sl_tp_equal": (
            " Con SL e TP entrambi al {sl}, servono molti trade vincenti per compensare le perdite."
        ),
        "why_aggressive_mode": " La modalità Aggressiva aumenta la frequenza dei segnali.",
        "suggest_tf_15m_1h": "timeframe 15m o 1h",
        "suggest_tf_1h": "timeframe 1h",
        "suggest_sl": "stop loss {value}",
        "suggest_tp": "take profit {value}",
        "suggest_sl_3": "stop loss 3%",
        "suggest_tp_4_5": "take profit 4-5%",
        "suggest_tp_above_sl": "take profit leggermente sopra lo stop (es. SL {sl}, TP 4%)",
        "suggest_pair": "coppia {symbol}",
        "suggest_pair_or": "coppia {primary} o {secondary}",
        "suggest_mode_selective_balanced": "modalità Selettiva o Equilibrata",
        "suggest_mode_balanced": "modalità Equilibrata",
        "suggest_mode_selective": "modalità Selettiva",
        "suggest_leverage": "leva più contenuta (es. {value}x)",
        "suggest_risk_1": "rischio 1% per trade",
        "fallback_keep": "mantenere timeframe e modalità attuali",
        "fallback_protect": "stop loss 2.5% e take profit 4% per proteggere i guadagni",
        "fallback_tf_15m": "timeframe 15m",
        "fallback_sl_3": "stop loss 3%",
        "fallback_tp_4": "take profit 4%",
        "hypo_generic": (
            "È una domanda ipotetica: chiedi cosa cambierebbe un parametro, non di applicarlo. "
            "Prima di applicarlo, userei la preview attuale solo come riferimento per l'impatto."
        ),
        "hypo_leverage": (
            "Se cambi la leva, modifichi l'esposizione senza cambiare il capitale impegnato. "
            "Leva più alta amplifica guadagni e perdite e può peggiorare il drawdown. "
            "Prima di applicarlo, valuterei una leva più contenuta se il drawdown attuale è già elevato."
        ),
        "hypo_risk": (
            "Se aumenti il rischio per trade, ogni operazione pesa di più sul capitale totale. "
            "Il drawdown può crescere più rapidamente su sequenze negative. "
            "Prima di applicarlo, valuterei se il rischio attuale è già sostenibile."
        ),
    },
    "en": {
        "legacy_closing": "Do you want me to update the configuration with these parameters?",
        "chat_params_header": (
            "You can try typing in chat:\n"
            "modify these parameters:"
        ),
        "suggestions_header": "What I would try changing",
        "why_header": "Why the preview turned out this way",
        "preview_advice_cta": "Do you want to change any parameters or start the bot?",
        "start_bot_prompt": "Do you want to start the bot now?",
        "no_preview_reply": (
            "I don't have a saved preview for you yet. Generate a preview first "
            '(e.g. "run a preview" or "simulate the last 30 days") and then ask me '
            "to explain it or suggest how to improve it."
        ),
        "params_fallback": (
            "You can try typing the parameters suggested above in chat, "
            "specifying pair, timeframe, mode, stop loss, take profit and risk."
        ),
        "label_operating_mode": "operating mode",
        "label_risk": "risk per trade",
        "label_stop_loss": "stop loss",
        "label_take_profit": "take profit",
        "label_pair": "pair",
        "nd": "N/A",
        "why_opening": (
            "The preview on {symbol} (timeframe {timeframe}) closes at {pnl} "
            "with a {win_rate} win rate over {trades} trades. "
            "Maximum drawdown was {drawdown}."
        ),
        "why_negative": " The negative result means losses outweighed gains.",
        "why_positive": " The positive result means the strategy closed in profit over the simulated period.",
        "why_neutral": " The result is break-even over the simulated period.",
        "why_low_wr": " A win rate below 45% makes recovery difficult even with good risk/reward.",
        "why_high_dd": " A high drawdown signals heavy consecutive losing streaks.",
        "why_short_tf": " The {timeframe} timeframe tends to generate more signals and more noise.",
        "why_sl_tp_tight": (
            " With SL/TP at {sl}/{tp}, trades have little room before closing at stop or target."
        ),
        "why_sl_tp_equal": (
            " With SL and TP both at {sl}, you need many winning trades to offset losses."
        ),
        "why_aggressive_mode": " Aggressive mode increases signal frequency.",
        "suggest_tf_15m_1h": "timeframe 15m or 1h",
        "suggest_tf_1h": "timeframe 1h",
        "suggest_sl": "stop loss {value}",
        "suggest_tp": "take profit {value}",
        "suggest_sl_3": "stop loss 3%",
        "suggest_tp_4_5": "take profit 4-5%",
        "suggest_tp_above_sl": "take profit slightly above stop (e.g. SL {sl}, TP 4%)",
        "suggest_pair": "pair {symbol}",
        "suggest_pair_or": "pair {primary} or {secondary}",
        "suggest_mode_selective_balanced": "Selective or Balanced mode",
        "suggest_mode_balanced": "Balanced mode",
        "suggest_mode_selective": "Selective mode",
        "suggest_leverage": "lower leverage (e.g. {value}x)",
        "suggest_risk_1": "risk 1% per trade",
        "fallback_keep": "keep current timeframe and mode",
        "fallback_protect": "stop loss 2.5% and take profit 4% to protect gains",
        "fallback_tf_15m": "timeframe 15m",
        "fallback_sl_3": "stop loss 3%",
        "fallback_tp_4": "take profit 4%",
        "hypo_generic": (
            "This is a hypothetical question: you're asking what would change if you adjusted a parameter, "
            "not to apply it. Before applying it, I'd use the current preview only as a reference for impact."
        ),
        "hypo_leverage": (
            "If you change leverage, you change exposure without changing committed capital. "
            "Higher leverage amplifies gains and losses and can worsen drawdown. "
            "Before applying it, I'd consider lower leverage if current drawdown is already high."
        ),
        "hypo_risk": (
            "If you increase risk per trade, each operation weighs more on total capital. "
            "Drawdown can grow faster during losing streaks. "
            "Before applying it, I'd check whether current risk is already sustainable."
        ),
    },
}

_PREVIEW_ADVICE_SYSTEM: Dict[str, str] = {
    "it": (
        "Sei Idith. L'utente chiede spiegazione e suggerimenti sulla sua ultima preview backtest salvata.\n\n"
        "Rispondi SEMPRE in italiano con ESATTAMENTE questa struttura (tre sezioni, titoli come scritti sotto):\n\n"
        "Perché la preview è andata così\n"
        "[2-4 frasi che spiegano il motivo principale del risultato. DEVI citare esplicitamente: "
        "pnl_pct, win rate, numero operazioni, drawdown, timeframe. "
        "Cita SL/TP se rilevanti per spiegare stop frequenti o risk/reward.]\n\n"
        "Cosa proverei a cambiare\n"
        "[elenco numerato 1-4 con modifiche concrete: timeframe, SL, TP, modalità operativa, rischio, leva]\n\n"
        "Regole:\n"
        "- Usa SOLO i numeri presenti nel JSON della preview; non inventare metriche.\n"
        "- Valori ammessi: timeframe 1m,3m,5m,15m,1h,4h,1d; modalità Aggressiva/Equilibrata/Selettiva.\n"
        "- NON promettere guadagni. NON applicare modifiche: solo consiglio testuale.\n"
        "- Non usare markdown o asterischi. Non chiedere API key. Tutta la risposta in italiano."
    ),
    "en": (
        "You are Idith. The user asks for explanation and suggestions about their last saved backtest preview.\n\n"
        "ALWAYS respond in English with EXACTLY this structure (three sections, titles as written below):\n\n"
        "Why the preview turned out this way\n"
        "[2-4 sentences explaining the main reason for the result. You MUST explicitly cite: "
        "pnl_pct, win rate, number of trades, drawdown, timeframe. "
        "Mention SL/TP if relevant to explain frequent stops or risk/reward.]\n\n"
        "What I would try changing\n"
        "[numbered list 1-4 with concrete changes: timeframe, SL, TP, operating mode, risk, leverage]\n\n"
        "Rules:\n"
        "- Use ONLY numbers from the preview JSON; do not invent metrics.\n"
        "- Allowed values: timeframe 1m,3m,5m,15m,1h,4h,1d; modes Aggressive/Balanced/Selective.\n"
        "- Do NOT promise profits. Do NOT apply changes: text advice only.\n"
        "- No markdown or asterisks. Do not ask for API key. Entire response in English."
    ),
}

_HYPOTHETICAL_ADVICE_SYSTEM_BY_LANG: Dict[str, str] = {
    "it": (
        "Sei Idith. L'utente fa una domanda IPOTETICA o teorica (es. 'what if', 'e se', "
        "'cosa succede se', 'if I set', 'if I change') su un parametro di trading.\n"
        "La preview salvata è solo CONTESTO di riferimento, non il soggetto principale.\n\n"
        "Rispondi SEMPRE in italiano, in prosa diretta compatta: massimo 4-5 righe, un solo paragrafo.\n\n"
        "REGOLE OBBLIGATORIE per domande ipotetiche:\n"
        "- NON scrivere 'Perché la preview è andata così' come titolo o apertura.\n"
        "- NON fare mini-report lunghi, NON usare elenchi numerati o puntati.\n"
        "- NON dare consigli generici da preview: al massimo UNA frase finale di valutazione.\n"
        "- NON suggerire 'modifica questi parametri' né chiedere di aggiornare la configurazione.\n"
        "- Apri subito con 'Se imposti/cambi/usassi ...' citando il parametro e, se possibile, "
        "il confronto con il valore attuale della preview.\n"
        "- Spiega solo l'impatto diretto (rischio/rendimento, drawdown, stop prematuri) se pertinente.\n"
        "- Usa SOLO i numeri presenti nel JSON della preview; non inventare metriche.\n"
        "- NON promettere guadagni. NON applicare modifiche: solo spiegazione teorica.\n"
        "- Non usare markdown o asterischi. Tutta la risposta in italiano."
    ),
    "en": (
        "You are Idith. The user asks a HYPOTHETICAL or theoretical question (e.g. 'what if', "
        "'what happens if', 'if I set', 'if I change') about a trading parameter.\n"
        "The saved preview is only REFERENCE context, not the main subject.\n\n"
        "ALWAYS respond in English, in compact direct prose: at most 4-5 lines, one paragraph only.\n\n"
        "MANDATORY rules for hypothetical questions:\n"
        "- Do NOT write 'Why the preview turned out this way' as a title or opening.\n"
        "- Do NOT write long mini-reports, do NOT use numbered or bulleted lists.\n"
        "- Do NOT give generic preview advice: at most ONE final evaluation sentence.\n"
        "- Do NOT suggest 'modify these parameters' or ask to update the configuration.\n"
        "- Open immediately with 'If you set/change/use ...' citing the parameter and, if possible, "
        "comparison with the current preview value.\n"
        "- Explain only direct impact (risk/reward, drawdown, premature stops) if relevant.\n"
        "- Use ONLY numbers from the preview JSON; do not invent metrics.\n"
        "- Do NOT promise profits. Do NOT apply changes: theoretical explanation only.\n"
        "- No markdown or asterisks. Entire response in English."
    ),
}
_VALID_TIMEFRAMES = ("1d", "4h", "1h", "15m", "5m", "3m", "1m")
_OPERATING_MODES = ("selettiva", "equilibrata", "aggressiva")
_PREFERRED_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
)
_MIN_TRADES_FOR_SYMBOL_ADVICE = 15

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

_ADVICE_PHRASE_TRIGGERS = (
    "spiegami questa preview",
    "spiegami la preview",
    "spiegami l ultima preview",
    "spiegami questa preview storica",
    "analizza questa preview",
    "analizza la preview",
    "analizza l ultima preview",
    "perche questa preview",
    "perche la preview",
    "perche ho perso",
    "perche e negativa",
    "come posso migliorare questa preview",
    "come posso migliorare",
    "come migliorare il risultato",
    "come migliorare",
    "come riduco le perdite",
    "riduci le perdite",
    "ridurre le perdite",
    "consigliami i parametri",
    "consigli parametri",
    "suggerisci parametri",
    "cosa devo cambiare",
    "explain this preview",
    "explain the preview",
    "explain the last preview",
    "explain this historical preview",
    "analyze this preview",
    "analyze the preview",
    "analyze the last preview",
    "why this preview",
    "why the preview",
    "why did i lose",
    "why is it negative",
    "how can i improve this preview",
    "how can i improve",
    "how can i improve the result",
    "how to improve",
    "how can i reduce losses",
    "how can i reduce my losses",
    "reduce losses",
    "reduce my losses",
    "lower losses",
    "suggest parameters",
    "suggest settings",
    "recommend parameters",
    "what should i change",
    "what can i change",
)

# Domande ipotetiche/teoriche: routing preview advice, mai update config.
_HYPOTHETICAL_QUESTION_PATTERNS = (
    re.compile(r"\bwhat if\b"),
    re.compile(r"\bwhat happens if\b"),
    re.compile(r"\bwhat would happen if\b"),
    re.compile(r"\bwhat happens\b"),
    re.compile(r"\bif i set\b"),
    re.compile(r"\bif i change\b"),
    re.compile(r"\bif i use\b"),
    re.compile(r"\bif i insert\b"),
    re.compile(r"\be se\b"),
    re.compile(r"\bcosa succede se\b"),
    re.compile(r"\bche succede se\b"),
    re.compile(r"\bse imposto\b"),
    re.compile(r"\bse metto\b"),
    re.compile(r"\bse cambio\b"),
    re.compile(r"\bse uso\b"),
)

_PREVIEW_COLUMNS = (
    "created_at, symbol, timeframe, market_type, operating_mode, strategy_id, "
    "stop_loss_pct, take_profit_pct, leverage, risk_pct, "
    "pnl_pct, max_drawdown_pct, estimated_trades, closed_trades, wins, losses"
)

_SHORT_TIMEFRAMES = frozenset({"1m", "3m", "5m"})


@dataclass
class PreviewAdviceDeps:
    supabase: Any


def normalize_for_matching(text: str) -> str:
    if not text:
        return ""
    normalized = text.strip().lower()
    for accented, unaccented in ACCENT_MAP.items():
        normalized = normalized.replace(accented, unaccented)
    for ch in ("'", "\u2019", "`"):
        normalized = normalized.replace(ch, " ")
    normalized = normalized.translate(str.maketrans("", "", string.punctuation))
    return " ".join(normalized.split())


def is_hypothetical_question(text: str) -> bool:
    """True se il messaggio è una domanda ipotetica/teorica (preview advice, non update config)."""
    normalized = normalize_for_matching(text)
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _HYPOTHETICAL_QUESTION_PATTERNS)


def _has_advice_intent(normalized: str) -> bool:
    if any(phrase in normalized for phrase in _ADVICE_PHRASE_TRIGGERS):
        return True

    if "spiegami" in normalized and "preview" in normalized:
        return True

    if "explain" in normalized and "preview" in normalized:
        return True

    if "perche" in normalized and any(
        word in normalized for word in ("preview", "perso", "perdite", "negativa", "negativo")
    ):
        return True

    if "why" in normalized and any(
        word in normalized for word in ("preview", "lose", "losses", "negative")
    ):
        return True

    if "migliorare" in normalized:
        return True

    if any(
        phrase in normalized
        for phrase in ("riduco le perdite", "riduci le perdite", "ridurre le perdite")
    ):
        return True

    if any(
        p in normalized
        for p in (
            "reduce my losses",
            "reduce losses",
            "lower losses",
            "how can i reduce",
        )
    ):
        return True

    if ("suggest" in normalized or "recommend" in normalized) and any(
        word in normalized for word in ("parameters", "settings")
    ):
        return True

    if "consigliami" in normalized and "parametri" in normalized:
        return True

    if "cosa devo cambiare" in normalized:
        return True

    return False


def is_preview_advice_request(text: str) -> bool:
    hypothetical = is_hypothetical_question(text)
    logger.info("[PREVIEW_ADVICE] hypothetical_detected=%s", hypothetical)
    if hypothetical:
        return True
    normalized = normalize_for_matching(text)
    if not normalized:
        return False
    return _has_advice_intent(normalized)


def compute_win_rate_pct(wins: Any, losses: Any) -> Optional[float]:
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
    return round(w * 100.0 / total, 1)


def _optional_float(value: Any) -> Optional[float]:
    from .text_normalize_user_numbers import parse_config_float

    return parse_config_float(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_str(value: Any, fallback: str = "N/D", *, lang: Optional[str] = None) -> str:
    t = _texts(lang)
    fb = t["nd"] if fallback == "N/D" else fallback
    if isinstance(value, str) and value.strip():
        return value.strip()
    if value is not None and not isinstance(value, str):
        return str(value)
    return fb


def _trade_count(row: Dict[str, Any]) -> Optional[int]:
    closed = _optional_int(row.get("closed_trades"))
    if closed is not None and closed >= 0:
        return closed
    estimated = _optional_int(row.get("estimated_trades"))
    if estimated is not None and estimated >= 0:
        return estimated
    return None


def fetch_latest_preview(user_id: str, deps: PreviewAdviceDeps) -> Optional[Dict[str, Any]]:
    logger.info("[PREVIEW_ADVICE] loading latest preview")
    try:
        res = (
            deps.supabase.table("backtest_previews")
            .select(_PREVIEW_COLUMNS)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not isinstance(rows, list) or not rows:
            logger.info("[PREVIEW_ADVICE] no_preview_found")
            return None
        row = rows[0]
        if not isinstance(row, dict):
            return None
        logger.info("[PREVIEW_ADVICE] preview_found symbol=%s", row.get("symbol"))
        return row
    except Exception as e:
        logger.error(
            "[PREVIEW_ADVICE] fetch failed user_id=%s error=%s",
            user_id,
            e,
            exc_info=True,
        )
        return None


def build_preview_snapshot(row: Dict[str, Any], *, lang: Optional[str] = None) -> Dict[str, Any]:
    sl_pct = _optional_float(row.get("stop_loss_pct"))
    tp_pct = _optional_float(row.get("take_profit_pct"))
    trades = _trade_count(row)
    return {
        "symbol": _display_str(row.get("symbol"), lang=lang),
        "timeframe": _display_str(row.get("timeframe"), lang=lang),
        "market_type": _display_str(row.get("market_type"), lang=lang),
        "operating_mode": _display_str(row.get("operating_mode"), lang=lang),
        "strategy_id": _display_str(row.get("strategy_id"), lang=lang),
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "leverage": _optional_float(row.get("leverage")),
        "risk_pct": _optional_float(row.get("risk_pct")),
        "pnl_pct": _optional_float(row.get("pnl_pct")),
        "win_rate_pct": compute_win_rate_pct(row.get("wins"), row.get("losses")),
        "max_drawdown_pct": _optional_float(row.get("max_drawdown_pct")),
        "estimated_trades": _optional_int(row.get("estimated_trades")),
        "closed_trades": _optional_int(row.get("closed_trades")),
        "trades_count": trades,
        "preview_date_rome": format_created_at_rome(row.get("created_at")),
    }


def _format_pct_value(value: Optional[float], *, lang: Optional[str] = None) -> str:
    if value is None:
        return _texts(lang)["nd"]
    if value > 0:
        return f"+{value:.1f}%"
    if value < 0:
        return f"-{abs(value):.1f}%"
    return "+0.0%"


def _format_win_rate(value: Optional[float], *, lang: Optional[str] = None) -> str:
    if value is None:
        return _texts(lang)["nd"]
    if value == int(value):
        return f"{int(value)}%"
    return f"{value:.1f}%"


def _format_level_pct(value: Optional[float], *, lang: Optional[str] = None) -> str:
    if value is None:
        return _texts(lang)["nd"]
    if value == int(value):
        return f"{int(value)}%"
    return f"{value:.1f}%"


def _build_why_section(snapshot: Dict[str, Any], *, lang: Optional[str] = None) -> str:
    t = _texts(lang)
    symbol = snapshot.get("symbol", t["nd"])
    timeframe = snapshot.get("timeframe", t["nd"])
    pnl = _format_pct_value(snapshot.get("pnl_pct"), lang=lang)
    win_rate = _format_win_rate(snapshot.get("win_rate_pct"), lang=lang)
    trades = snapshot.get("trades_count")
    trades_text = str(trades) if trades is not None else t["nd"]
    drawdown_val = snapshot.get("max_drawdown_pct")
    if drawdown_val is None:
        drawdown = t["nd"]
    else:
        drawdown = f"{abs(drawdown_val):.1f}%"

    sl_pct = snapshot.get("sl_pct")
    tp_pct = snapshot.get("tp_pct")
    sl_tp_note = ""
    if sl_pct is not None and tp_pct is not None:
        sl_tp_note = t["why_sl_tp_tight"].format(
            sl=_format_level_pct(sl_pct, lang=lang),
            tp=_format_level_pct(tp_pct, lang=lang),
        )
        if sl_pct == tp_pct and (snapshot.get("pnl_pct") or 0) < 0:
            sl_tp_note = t["why_sl_tp_equal"].format(
                sl=_format_level_pct(sl_pct, lang=lang),
            )

    operating_mode = snapshot.get("operating_mode", t["nd"])
    mode_note = ""
    if isinstance(operating_mode, str) and operating_mode.lower() in ("aggressiva", "aggressive"):
        mode_note = t["why_aggressive_mode"]

    tf_note = ""
    tf = str(snapshot.get("timeframe", "")).lower()
    if tf in _SHORT_TIMEFRAMES:
        tf_note = t["why_short_tf"].format(timeframe=timeframe)

    pnl_val = snapshot.get("pnl_pct")
    outcome_hint = ""
    if pnl_val is not None:
        if pnl_val < 0:
            outcome_hint = t["why_negative"]
        elif pnl_val > 0:
            outcome_hint = t["why_positive"]
        else:
            outcome_hint = t["why_neutral"]

    win_rate_val = snapshot.get("win_rate_pct")
    wr_note = ""
    if win_rate_val is not None and win_rate_val < 45:
        wr_note = t["why_low_wr"]

    dd_val = snapshot.get("max_drawdown_pct")
    dd_note = ""
    if dd_val is not None and dd_val > 10:
        dd_note = t["why_high_dd"]

    body = t["why_opening"].format(
        symbol=symbol,
        timeframe=timeframe,
        pnl=pnl,
        win_rate=win_rate,
        trades=trades_text,
        drawdown=drawdown,
    )
    body += f"{outcome_hint}{wr_note}{dd_note}{tf_note}{sl_tp_note}{mode_note}"
    return body.strip()


def _suggest_timeframe(snapshot: Dict[str, Any], *, lang: Optional[str] = None) -> Optional[str]:
    t = _texts(lang)
    tf = str(snapshot.get("timeframe", "")).lower()
    if tf in _SHORT_TIMEFRAMES:
        return t["suggest_tf_15m_1h"]
    if tf in {"15m", "5m"}:
        return t["suggest_tf_1h"]
    return None


def _suggest_sl_tp(snapshot: Dict[str, Any], *, lang: Optional[str] = None) -> List[str]:
    t = _texts(lang)
    suggestions: List[str] = []
    sl = snapshot.get("sl_pct")
    tp = snapshot.get("tp_pct")
    pnl = snapshot.get("pnl_pct")

    if sl is not None and tp is not None and sl == tp and (pnl is None or pnl < 0):
        new_sl = max(sl + 0.5, 2.5)
        new_tp = max(tp + 1.5, 4.0)
        suggestions.append(t["suggest_sl"].format(value=_format_level_pct(new_sl, lang=lang)))
        suggestions.append(t["suggest_tp"].format(value=_format_level_pct(new_tp, lang=lang)))
    elif sl is not None and sl <= 2.0 and (pnl is None or pnl < 0):
        suggestions.append(t["suggest_sl_3"])
        suggestions.append(t["suggest_tp_4_5"])
    elif tp is not None and sl is not None and tp <= sl:
        suggestions.append(
            t["suggest_tp_above_sl"].format(sl=_format_level_pct(sl, lang=lang))
        )

    return suggestions


def _import_validators():
    try:
        from . import validators
        return validators
    except Exception:
        try:
            import validators
            return validators
        except Exception:
            return None


def _market_type_for_symbols(snapshot: Dict[str, Any]) -> str:
    from .text_normalize_config_values import normalize_market_type

    market = normalize_market_type(snapshot.get("market_type")) or "futures"
    return market


def _fetch_whitelisted_symbols(market_type: str) -> set[str]:
    validators = _import_validators()
    if validators is None:
        return set(_PREFERRED_SYMBOLS)
    try:
        symbols = validators.fetch_valid_symbols(market_type)
        if symbols:
            return set(symbols)
    except Exception as e:
        logger.warning("[PREVIEW_ADVICE] fetch_valid_symbols failed: %s", e)
    return set(_PREFERRED_SYMBOLS)


def _normalize_advice_symbol(raw: str, market_type: str) -> Optional[str]:
    validators = _import_validators()
    if validators is None:
        candidate = raw.strip().upper()
        return candidate if candidate in _PREFERRED_SYMBOLS else None
    normalized = validators.normalize_symbol_strict(raw)
    if not normalized:
        return None
    try:
        if validators.is_symbol_listed(None, market_type, normalized):
            return normalized
    except Exception:
        pass
    if normalized in _fetch_whitelisted_symbols(market_type):
        return normalized
    return None


def _symbol_alternatives(snapshot: Dict[str, Any]) -> List[str]:
    market_type = _market_type_for_symbols(snapshot)
    valid = _fetch_whitelisted_symbols(market_type)
    current = str(snapshot.get("symbol") or "").strip().upper()
    alternatives: List[str] = []
    for symbol in _PREFERRED_SYMBOLS:
        if symbol in valid and symbol != current and symbol not in alternatives:
            alternatives.append(symbol)
    if alternatives:
        return alternatives
    for symbol in sorted(valid):
        if symbol != current and symbol.endswith("USDT"):
            alternatives.append(symbol)
        if len(alternatives) >= 3:
            break
    return alternatives


def _should_suggest_symbol_change(snapshot: Dict[str, Any]) -> bool:
    pnl = snapshot.get("pnl_pct")
    trades = snapshot.get("trades_count")
    if pnl is not None and pnl < 0:
        return True
    if trades is not None and trades < _MIN_TRADES_FOR_SYMBOL_ADVICE:
        return True
    return False


def _suggest_symbol(snapshot: Dict[str, Any], *, lang: Optional[str] = None) -> Optional[str]:
    if not _should_suggest_symbol_change(snapshot):
        return None
    alternatives = _symbol_alternatives(snapshot)
    if not alternatives:
        return None
    t = _texts(lang)
    primary = alternatives[0]
    if len(alternatives) >= 2:
        return t["suggest_pair_or"].format(primary=primary, secondary=alternatives[1])
    return t["suggest_pair"].format(symbol=primary)


def _suggest_operating_mode(snapshot: Dict[str, Any], *, lang: Optional[str] = None) -> Optional[str]:
    t = _texts(lang)
    mode = str(snapshot.get("operating_mode", "")).lower()
    trades = snapshot.get("trades_count")
    pnl = snapshot.get("pnl_pct")

    if mode in ("aggressiva", "aggressive") and (pnl is None or pnl < 0):
        return t["suggest_mode_selective_balanced"]
    if mode in ("selettiva", "selective") and trades is not None and trades < 15:
        return t["suggest_mode_balanced"]
    if mode not in ("selettiva", "selective") and (pnl is None or pnl < 0):
        win_rate = snapshot.get("win_rate_pct")
        if win_rate is not None and win_rate < 45:
            return t["suggest_mode_selective"]
    return None


def _suggest_risk_leverage(snapshot: Dict[str, Any], *, lang: Optional[str] = None) -> List[str]:
    t = _texts(lang)
    suggestions: List[str] = []
    leverage = snapshot.get("leverage")
    risk = snapshot.get("risk_pct")
    dd = snapshot.get("max_drawdown_pct")
    market = str(snapshot.get("market_type", "")).lower()

    if dd is not None and dd > 10:
        if leverage is not None and leverage >= 5 and market != "spot":
            suggestions.append(
                t["suggest_leverage"].format(value=max(3, int(leverage // 2)))
            )
        if risk is not None and risk >= 1.5:
            suggestions.append(t["suggest_risk_1"])

    return suggestions


_TIMEFRAME_RANK = {"1m": 0, "3m": 1, "5m": 2, "15m": 3, "1h": 4, "4h": 5, "1d": 6}

_TIMEFRAME_CHANGE_HINTS = (
    "prova",
    "passa",
    "cambia",
    "usa",
    "aumenta",
    "riduci",
    "sposta",
    "porta",
    "imposta",
    "provare",
    "posto",
    "invece",
    "timeframe",
    "tf",
    "su ",
)

_MODE_CHANGE_HINTS = (
    "modalit",
    "operating_mode",
    "operating mode",
    "prova",
    "passa",
    "meno",
    "piu",
    "più",
    "conservat",
    "aggressiv",
    "da ",
    "a ",
)


@dataclass
class _AdvisedChanges:
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    operating_mode: Optional[str] = None
    sl_pct: Optional[float] = None
    tp_pct: Optional[float] = None
    risk_pct: Optional[float] = None
    intents: set[str] = field(default_factory=set)


def _current_timeframe(snapshot: Dict[str, Any]) -> str:
    return str(snapshot.get("timeframe") or "").strip().lower()


def _current_mode(snapshot: Dict[str, Any]) -> str:
    from .text_normalize_config_values import normalize_operating_mode

    return normalize_operating_mode(snapshot.get("operating_mode")) or ""


def _extract_timeframes(text: str) -> List[str]:
    matches = re.findall(r"\b(1d|4h|1h|15m|5m|3m|1m)\b", text, re.I)
    found: List[str] = []
    seen: set[str] = set()
    for match in matches:
        tf = match.lower()
        if tf not in seen:
            seen.add(tf)
            found.append(tf)
    return found


def _extract_suggestions_section(reply: str, *, lang: Optional[str] = None) -> str:
    t = _texts(lang)
    lower = reply.lower()
    header = t["suggestions_header"].lower()
    idx = lower.find(header)
    if idx < 0:
        return ""
    start = idx + len(t["suggestions_header"])
    rest = reply[start:].lstrip("\n:")
    stop_markers = (
        t["chat_params_header"].split("\n")[0],
        "Se vuoi provare queste modifiche",
        "If you want to try these changes",
        t["legacy_closing"],
        "Vuoi che aggiorni",
        "Do you want me to update",
        "modifica questi parametri",
        "modify these parameters",
    )
    for stop in stop_markers:
        stop_idx = rest.lower().find(stop.lower())
        if stop_idx >= 0:
            rest = rest[:stop_idx]
    return rest.strip()


def _parse_numbered_suggestions(section: str) -> List[str]:
    items: List[str] = []
    for line in section.splitlines():
        match = re.match(r"^\s*\d+\.\s*(.+)", line)
        if match:
            items.append(match.group(1).strip())
            continue
        match = re.match(r"^\s*[-•]\s*(.+)", line)
        if match:
            items.append(match.group(1).strip())
    return items


def _parse_symbol_from_suggestion(suggestion: str, market_type: str) -> Optional[str]:
    lower = suggestion.lower()
    if not any(word in lower for word in ("coppia", "symbol", "simbolo", "pair", "usdt")):
        if not re.search(r"\b[A-Z0-9]{2,15}USDT\b", suggestion, re.I):
            return None
    current_candidates: List[str] = []
    for match in re.finditer(r"\b([A-Z0-9]{2,15}USDT)\b", suggestion, re.I):
        normalized = _normalize_advice_symbol(match.group(1), market_type)
        if normalized and normalized not in current_candidates:
            current_candidates.append(normalized)
    if not current_candidates:
        return None
    return current_candidates[0]


def _pick_prudent_pct_from_range(low: Optional[float], high: Optional[float]) -> Optional[float]:
    if low is None and high is None:
        return None
    if low is None:
        return high
    if high is None:
        return low
    return min(low, high)


def _parse_param_pct_value(suggestion: str, keyword_pattern: str) -> Optional[float]:
    patterns = (
        rf"(?:{keyword_pattern}).*?da\s+\d+(?:[.,]\d+)?\s*%?\s+a\s+"
        r"(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*%",
        rf"(?:{keyword_pattern}).*?da\s+\d+(?:[.,]\d+)?\s*%?\s+a\s+(\d+(?:[.,]\d+)?)\s*%",
        rf"(?:{keyword_pattern}).*?(?:a|al|allo?)\s+(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*%",
        rf"(?:{keyword_pattern}).*?(?:a|al|allo?)\s+(\d+(?:[.,]\d+)?)\s*%",
        rf"(?:{keyword_pattern}).*?(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)\s*%",
        rf"(?:{keyword_pattern}).*?(\d+(?:[.,]\d+)?)\s*%",
    )
    for pattern in patterns:
        match = re.search(pattern, suggestion, re.I)
        if not match:
            continue
        low = _optional_float(match.group(1))
        high = _optional_float(match.group(2)) if match.lastindex and match.lastindex >= 2 else None
        value = _pick_prudent_pct_from_range(low, high)
        if value is not None:
            return value
    return None


def _is_tp_example_line(suggestion: str) -> bool:
    lower = suggestion.lower()
    has_tp = bool(re.search(r"take\s*profit|take_profit|\btp\b", lower))
    has_example_sl = bool(re.search(r"\bes\.?\s*(?:sl|stop)", lower))
    return has_tp and has_example_sl


def _suggestion_advises_symbol(suggestion: str) -> bool:
    lower = suggestion.lower()
    if any(word in lower for word in ("coppia", "symbol", "simbolo", "pair")):
        return True
    return bool(re.search(r"\b[A-Z0-9]{2,15}USDT\b", suggestion, re.I))


def _suggestion_advises_timeframe(suggestion: str) -> bool:
    if not _extract_timeframes(suggestion):
        return False
    lower = suggestion.lower()
    return any(hint in lower for hint in _TIMEFRAME_CHANGE_HINTS)


def _suggestion_advises_mode(suggestion: str) -> bool:
    lower = suggestion.lower().strip().rstrip(".")
    if any(hint in lower for hint in _MODE_CHANGE_HINTS):
        return any(mode in lower for mode in _OPERATING_MODES)
    return lower in _OPERATING_MODES


def _suggestion_advises_sl(suggestion: str) -> bool:
    if _is_tp_example_line(suggestion):
        return False
    return bool(re.search(r"stop\s*loss|stop_loss|\bsl\b", suggestion, re.I))


def _suggestion_advises_tp(suggestion: str) -> bool:
    return bool(re.search(r"take\s*profit|take_profit|\btp\b", suggestion, re.I))


def _suggestion_advises_risk(suggestion: str) -> bool:
    lower = suggestion.lower()
    return any(
        keyword in lower
        for keyword in ("rischio", "risk_pct", "risk per trade", "rischio per trade")
    ) or bool(re.search(r"\brisk\b", lower))


def _suggestion_intents(suggestion: str) -> set[str]:
    intents: set[str] = set()
    if _suggestion_advises_symbol(suggestion):
        intents.add("symbol")
    if _suggestion_advises_timeframe(suggestion):
        intents.add("timeframe")
    if _suggestion_advises_mode(suggestion):
        intents.add("operating_mode")
    if _suggestion_advises_sl(suggestion):
        intents.add("sl_pct")
    if _suggestion_advises_tp(suggestion):
        intents.add("tp_pct")
    if _suggestion_advises_risk(suggestion):
        intents.add("risk_pct")
    return intents


def _parse_mode_value(suggestion: str) -> Optional[str]:
    lower = suggestion.lower().strip().rstrip(".")

    op_match = re.search(
        r"operating[_ ]mode\s*:?\s*(aggressiva|equilibrata|selettiva)",
        lower,
    )
    if op_match:
        return op_match.group(1)

    transition = re.search(
        r"da\s+(?:aggressiva|equilibrata|selettiva)\s+a\s+(.+)",
        lower,
    )
    if transition:
        tail_modes = [mode for mode in _OPERATING_MODES if mode in transition.group(1)]
        if tail_modes:
            if "selettiva" in tail_modes:
                return "selettiva"
            if "equilibrata" in tail_modes:
                return "equilibrata"
            return tail_modes[0]

    if lower in _OPERATING_MODES:
        return lower

    found_modes = [mode for mode in _OPERATING_MODES if mode in lower]
    if not found_modes:
        return None
    if "selettiva" in found_modes:
        return "selettiva"
    if "equilibrata" in found_modes:
        return "equilibrata"
    return found_modes[0]


def _parse_timeframe_value(suggestion: str, current_timeframe: str) -> Optional[str]:
    timeframes = _extract_timeframes(suggestion)
    if not timeframes:
        return None
    candidates = [tf for tf in timeframes if tf != current_timeframe] or timeframes
    return max(candidates, key=lambda tf: _TIMEFRAME_RANK.get(tf, 0))


def _collect_advised_changes_from_reply(
    reply: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> _AdvisedChanges:
    section = _extract_suggestions_section(reply, lang=lang)
    suggestions = _parse_numbered_suggestions(section)
    if not suggestions and section:
        suggestions = [line.strip() for line in section.splitlines() if line.strip()]

    changes = _AdvisedChanges()
    market_type = _market_type_for_symbols(snapshot)
    current_timeframe = _current_timeframe(snapshot)

    for suggestion in suggestions:
        intents = _suggestion_intents(suggestion)
        if not intents:
            continue

        if "symbol" in intents:
            symbol = _parse_symbol_from_suggestion(suggestion, market_type)
            if symbol:
                changes.symbol = symbol
                changes.intents.add("symbol")

        if "timeframe" in intents:
            timeframe = _parse_timeframe_value(suggestion, current_timeframe)
            if timeframe:
                changes.timeframe = timeframe
                changes.intents.add("timeframe")

        if "operating_mode" in intents:
            mode = _parse_mode_value(suggestion)
            if mode:
                changes.operating_mode = mode
                changes.intents.add("operating_mode")

        if "sl_pct" in intents:
            sl = _parse_param_pct_value(suggestion, r"stop\s*loss|stop_loss|\bsl\b")
            if sl is not None:
                changes.sl_pct = sl
                changes.intents.add("sl_pct")

        if "tp_pct" in intents:
            tp = _parse_param_pct_value(suggestion, r"take\s*profit|take_profit|\btp\b")
            if tp is not None:
                changes.tp_pct = tp
                changes.intents.add("tp_pct")

        if "risk_pct" in intents:
            risk = _parse_param_pct_value(suggestion, r"rischio|risk(?:_pct)?")
            if risk is not None:
                changes.risk_pct = risk
                changes.intents.add("risk_pct")

    return changes


def build_final_params_section_from_advice(
    reply: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> str:
    """Costruisce il blocco finale solo dai parametri consigliati in 'Cosa proverei a cambiare'."""
    t = _texts(lang)
    changes = _collect_advised_changes_from_reply(reply, snapshot, lang=lang)
    lines: List[str] = []

    if "timeframe" in changes.intents and changes.timeframe:
        lines.append(f"- timeframe: {changes.timeframe}")
    if "operating_mode" in changes.intents and changes.operating_mode:
        lines.append(f"- {t['label_operating_mode']}: {changes.operating_mode}")
    if "risk_pct" in changes.intents and changes.risk_pct is not None:
        lines.append(
            f"- {t['label_risk']}: {_format_level_pct(changes.risk_pct, lang=lang)}"
        )
    if "sl_pct" in changes.intents and changes.sl_pct is not None:
        lines.append(
            f"- {t['label_stop_loss']}: {_format_level_pct(changes.sl_pct, lang=lang)}"
        )
    if "tp_pct" in changes.intents and changes.tp_pct is not None:
        lines.append(
            f"- {t['label_take_profit']}: {_format_level_pct(changes.tp_pct, lang=lang)}"
        )
    if "symbol" in changes.intents and changes.symbol:
        lines.append(f"- {t['label_pair']}: {changes.symbol}")

    if not lines:
        return t["params_fallback"]
    return f"{t['chat_params_header']}\n" + "\n".join(lines)


def build_confirmation_suffix(
    reply: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> str:
    return build_final_params_section_from_advice(reply, snapshot, lang=lang)


def _strip_existing_confirmation(text: str, *, lang: Optional[str] = None) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    t = _texts(lang)
    for closing in (t["legacy_closing"], _LEGACY_CLOSING):
        if closing.lower() in cleaned.lower():
            idx = cleaned.lower().find(closing.lower())
            cleaned = cleaned[:idx].rstrip()
    for marker in (
        t["chat_params_header"].split("\n")[0],
        "Puoi provare scrivendo in chat",
        "You can try typing in chat",
        "Se vuoi provare queste modifiche",
        "If you want to try these changes",
    ):
        if marker in cleaned:
            idx = cleaned.find(marker)
            cleaned = cleaned[:idx].rstrip()
    return cleaned


def _finalize_preview_advice_reply(text: str, *, lang: Optional[str] = None) -> str:
    """Aggiunge la CTA standard come ultima frase, una sola volta."""
    t = _texts(lang)
    cleaned = (text or "").strip()
    if not cleaned:
        return t["preview_advice_cta"]
    cleaned = _strip_existing_confirmation(cleaned, lang=lang)
    for marker in (t["start_bot_prompt"], t["preview_advice_cta"], _PREVIEW_ADVICE_CTA):
        lower = cleaned.lower()
        idx = lower.find(marker.lower())
        if idx >= 0:
            cleaned = cleaned[:idx].rstrip()
    return f"{cleaned}\n\n{t['preview_advice_cta']}"


def build_rule_based_advice(snapshot: Dict[str, Any], *, lang: Optional[str] = None) -> str:
    t = _texts(lang)
    why = _build_why_section(snapshot, lang=lang)

    raw_suggestions: List[str] = []
    symbol = _suggest_symbol(snapshot, lang=lang)
    if symbol:
        raw_suggestions.append(symbol)
    tf = _suggest_timeframe(snapshot, lang=lang)
    if tf:
        raw_suggestions.append(tf)
    raw_suggestions.extend(_suggest_sl_tp(snapshot, lang=lang))
    mode = _suggest_operating_mode(snapshot, lang=lang)
    if mode:
        raw_suggestions.append(mode)
    raw_suggestions.extend(_suggest_risk_leverage(snapshot, lang=lang))

    if not raw_suggestions:
        pnl = snapshot.get("pnl_pct")
        if pnl is not None and pnl >= 0:
            raw_suggestions = [
                t["fallback_keep"],
                t["fallback_protect"],
            ]
        else:
            raw_suggestions = [
                t["fallback_tf_15m"],
                t["fallback_sl_3"],
                t["fallback_tp_4"],
            ]

    seen = set()
    unique: List[str] = []
    for item in raw_suggestions:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    unique = unique[:5]
    if len(unique) < 2:
        for filler in (t["fallback_sl_3"], t["fallback_tp_4"]):
            if filler.lower() not in seen:
                unique.append(filler)
                seen.add(filler.lower())
            if len(unique) >= 2:
                break

    suggestion_lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(unique))

    advice_body = (
        f"{t['why_header']}\n"
        f"{why}\n\n"
        f"{t['suggestions_header']}\n"
        f"{suggestion_lines}"
    )
    return f"{advice_body}\n\n{build_confirmation_suffix(advice_body, snapshot, lang=lang)}"


def ensure_confirmation_suffix(
    reply: str,
    snapshot: Optional[Dict[str, Any]] = None,
    *,
    lang: Optional[str] = None,
) -> str:
    text = _strip_existing_confirmation(reply, lang=lang)
    suffix = build_confirmation_suffix(text, snapshot or {}, lang=lang)
    if not text:
        return suffix
    return f"{text}\n\n{suffix}"


def _import_llm_client():
    try:
        from . import llm_client
        return llm_client
    except Exception:
        try:
            import llm_client
            return llm_client
        except Exception:
            return None


def _detect_hypothetical_topic(text: str) -> str:
    normalized = normalize_for_matching(text)
    lower = (text or "").lower()
    if re.search(r"stop\s*loss|\bsl\b", lower):
        return "sl"
    if re.search(r"take\s*profit|\btp\b", lower):
        return "tp"
    if any(w in normalized for w in ("coppia", "pair", "symbol", "simbolo")) or re.search(
        r"\b[a-z0-9]{2,15}usdt\b", normalized
    ):
        return "symbol"
    if "timeframe" in normalized or "tf" in normalized.split() or re.search(
        r"\b(1d|4h|1h|15m|5m|3m|1m)\b", lower
    ):
        return "timeframe"
    if any(w in normalized for w in ("leva", "leverage")):
        return "leverage"
    if any(w in normalized for w in ("rischio", "risk")):
        return "risk"
    if any(w in normalized for w in ("modalit", "aggressiv", "selettiv", "equilibrat", "operating")):
        return "operating_mode"
    return "generic"


_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "uno": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
    "sei": 6, "sette": 7, "otto": 8, "nove": 9, "dieci": 10,
}


def _parse_word_or_digit(raw: str) -> Optional[float]:
    token = (raw or "").strip().lower()
    if token in _WORD_TO_NUM:
        return float(_WORD_TO_NUM[token])
    try:
        return float(token.replace(",", "."))
    except (TypeError, ValueError):
        return None


def _extract_pct_near_keywords(text: str, keyword_re: str) -> Optional[float]:
    lower = (text or "").lower()
    word_alt = "|".join(_WORD_TO_NUM.keys())
    patterns = (
        rf"(?:{keyword_re}).{{0,50}}?(?:to|a|al|at)\s*(\d+(?:[.,]\d+)?|{word_alt})\s*%?",
        rf"(?:{keyword_re}).{{0,50}}?(\d+(?:[.,]\d+)?|{word_alt})\s*%",
        rf"(?:{keyword_re}).{{0,50}}?(?:to|a|al|at)\s*(\d+(?:[.,]\d+)?|{word_alt})\b",
        rf"(?:{keyword_re}).{{0,50}}?(\d+(?:[.,]\d+)?|{word_alt})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, lower)
        if not match:
            continue
        value = _parse_word_or_digit(match.group(1))
        if value is not None:
            return value
    return None


def _extract_symbol_from_user_text(text: str, snapshot: Dict[str, Any]) -> Optional[str]:
    for match in re.finditer(r"\b([A-Z0-9]{2,15}USDT)\b", text or "", re.I):
        symbol = _normalize_advice_symbol(match.group(1), _market_type_for_symbols(snapshot))
        if symbol:
            return symbol
    return None


def _build_hypothetical_sl_reply(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> str:
    proposed = _extract_pct_near_keywords(user_message, r"stop\s*loss|\bsl\b")
    current_sl = snapshot.get("sl_pct")
    current_tp = snapshot.get("tp_pct")
    sl_val = proposed if proposed is not None else current_sl
    code = _resolve_lang(lang)

    if code == "en":
        if proposed is not None and current_sl is not None and proposed != current_sl:
            opener = (
                f"If you set Stop Loss to {_format_level_pct(proposed, lang=lang)} "
                f"instead of the current {_format_level_pct(current_sl, lang=lang)}, "
            )
            if proposed < current_sl:
                opener += "you close losing positions sooner."
                tradeoff = (
                    "This can reduce maximum drawdown, but increases the risk of being stopped out "
                    "on normal price swings."
                )
                closing = (
                    f"Before applying it, I'd also consider a higher TP or SL closer to "
                    f"{_format_level_pct(current_sl, lang=lang)}."
                )
            else:
                opener += "you tolerate larger losses before closing."
                tradeoff = (
                    "This can reduce premature stops, but if the trade goes wrong drawdown can worsen."
                )
                closing = "Before applying it, I'd consider a tighter SL or higher TP."
        elif sl_val is not None:
            opener = (
                f"If you set Stop Loss to {_format_level_pct(sl_val, lang=lang)}, "
                "you change the maximum loss per trade."
            )
            tradeoff = "Tighter values close sooner at a loss; wider values tolerate more movement."
            closing = "Before applying it, I'd balance it with the current TP."
        else:
            return (
                "If you change Stop Loss, you change how much you're willing to lose per trade. "
                "Before applying it, compare it with TP to see if risk/reward holds up."
            )
        sentences = [opener, tradeoff]
        if sl_val is not None and current_tp is not None and sl_val > current_tp:
            sl_i = int(sl_val) if sl_val == int(sl_val) else sl_val
            tp_i = int(current_tp) if current_tp == int(current_tp) else current_tp
            sentences.append(
                f"With TP at {_format_level_pct(current_tp, lang=lang)}, risk/reward stays delicate "
                f"because you risk {sl_i} to target {tp_i}."
            )
        sentences.append(closing)
        return " ".join(sentences)

    if proposed is not None and current_sl is not None and proposed != current_sl:
        opener = (
            f"Se imposti lo Stop Loss al {_format_level_pct(proposed, lang=lang)} "
            f"invece dell'attuale {_format_level_pct(current_sl, lang=lang)}, "
        )
        if proposed < current_sl:
            opener += "chiudi prima le posizioni in perdita."
            tradeoff = (
                "Questo può ridurre il drawdown massimo, ma aumenta il rischio di essere stoppato "
                "su oscillazioni normali del prezzo."
            )
            closing = (
                f"Prima di applicarlo, valuterei anche TP più alto o SL più vicino al "
                f"{_format_level_pct(current_sl, lang=lang)}."
            )
        else:
            opener += "tolleri perdite più ampie prima di chiudere."
            tradeoff = (
                "Questo può ridurre stop prematuri, ma se il trade va male il drawdown può peggiorare."
            )
            closing = "Prima di applicarlo, valuterei uno SL più contenuto o un TP più alto."
    elif sl_val is not None:
        opener = (
            f"Se imposti lo Stop Loss al {_format_level_pct(sl_val, lang=lang)}, "
            "cambi la perdita massima per trade."
        )
        tradeoff = "Valori più stretti chiudono prima in perdita; valori più larghi tollerano più movimento."
        closing = "Prima di applicarlo, valuterei l'equilibrio con il TP attuale."
    else:
        return (
            "Se modifichi lo Stop Loss, cambi quanto sei disposto a perdere per trade. "
            "Prima di applicarlo, confrontalo con il TP per capire se il rapporto rischio/rendimento regge."
        )

    sentences = [opener, tradeoff]
    if sl_val is not None and current_tp is not None and sl_val > current_tp:
        sl_i = int(sl_val) if sl_val == int(sl_val) else sl_val
        tp_i = int(current_tp) if current_tp == int(current_tp) else current_tp
        sentences.append(
            f"Con TP al {_format_level_pct(current_tp, lang=lang)}, il rapporto rischio/rendimento resta ancora delicato "
            f"perché rischi {sl_i} per puntare a {tp_i}."
        )
    sentences.append(closing)
    return " ".join(sentences)


def _build_hypothetical_symbol_reply(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> str:
    t = _texts(lang)
    current = snapshot.get("symbol", t["nd"])
    proposed = _extract_symbol_from_user_text(user_message, snapshot)
    if _resolve_lang(lang) == "en":
        if proposed and proposed != current:
            return (
                f"If you use {proposed} instead of {current}, volatility and signal frequency change. "
                f"Results from the preview on {current} don't transfer automatically. "
                "Before applying it, I'd run a new preview on that pair."
            )
        return (
            f"If you change pair from {current}, volatility, liquidity and entry opportunities change. "
            f"The current preview only applies to {current}. "
            "Before applying it, I'd simulate the new pair to see real drawdown and win rate."
        )
    if proposed and proposed != current:
        return (
            f"Se usi {proposed} al posto di {current}, cambiano volatilità e frequenza dei segnali. "
            f"I risultati della preview su {current} non si trasferiscono automaticamente. "
            "Prima di applicarlo, farei una nuova preview su quella coppia."
        )
    return (
        f"Se cambi coppia rispetto a {current}, cambiano volatilità, liquidità e opportunità di ingresso. "
        f"La preview attuale vale solo per {current}. "
        "Prima di applicarlo, simulerei la nuova coppia per vedere drawdown e win rate reali."
    )


def _build_hypothetical_tp_reply(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> str:
    proposed = _extract_pct_near_keywords(user_message, r"take\s*profit|\btp\b")
    current_tp = snapshot.get("tp_pct")
    current_sl = snapshot.get("sl_pct")
    tp_val = proposed if proposed is not None else current_tp
    code = _resolve_lang(lang)

    if code == "en":
        if proposed is not None and current_tp is not None and proposed != current_tp:
            opener = (
                f"If you set Take Profit to {_format_level_pct(proposed, lang=lang)} "
                f"instead of the current {_format_level_pct(current_tp, lang=lang)}, "
            )
            if proposed > current_tp:
                opener += "you aim for larger gains per trade."
                tradeoff = "You hit target less often, but each winning trade offsets losses better."
            else:
                opener += "you close in profit sooner."
                tradeoff = "You increase target probability, but each win weighs less on overall results."
        elif tp_val is not None:
            opener = (
                f"If you set Take Profit to {_format_level_pct(tp_val, lang=lang)}, "
                "you change the exit target per trade."
            )
            tradeoff = (
                "Higher TPs improve return per winning trade; lower TPs increase target closures."
            )
        else:
            return (
                "If you change Take Profit, you change how much you seek to gain per trade. "
                "Before applying it, compare it with SL to verify risk/reward."
            )
        sentences = [opener, tradeoff]
        if tp_val is not None and current_sl is not None and tp_val <= current_sl:
            sentences.append(
                f"With SL at {_format_level_pct(current_sl, lang=lang)}, risk/reward stays delicate."
            )
        sentences.append("Before applying it, I'd balance it with the current SL.")
        return " ".join(sentences)

    if proposed is not None and current_tp is not None and proposed != current_tp:
        opener = (
            f"Se imposti il Take Profit al {_format_level_pct(proposed, lang=lang)} "
            f"invece dell'attuale {_format_level_pct(current_tp, lang=lang)}, "
        )
        if proposed > current_tp:
            opener += "punti a guadagni più ampi per trade."
            tradeoff = "Chiudi meno spesso in target, ma ogni trade vincente compensa meglio le perdite."
        else:
            opener += "chiudi prima in guadagno."
            tradeoff = "Aumenti la probabilità di target, ma ogni vincita pesa meno sul risultato complessivo."
    elif tp_val is not None:
        opener = (
            f"Se imposti il Take Profit al {_format_level_pct(tp_val, lang=lang)}, "
            "cambi il target di uscita per trade."
        )
        tradeoff = "TP più alti migliorano il rendimento per trade vincente, TP più bassi aumentano le chiusure in target."
    else:
        return (
            "Se modifichi il Take Profit, cambi quanto cerchi di guadagnare per trade. "
            "Prima di applicarlo, confrontalo con lo SL per verificare il rapporto rischio/rendimento."
        )

    sentences = [opener, tradeoff]
    if tp_val is not None and current_sl is not None and tp_val <= current_sl:
        sentences.append(
            f"Con SL al {_format_level_pct(current_sl, lang=lang)}, il rapporto rischio/rendimento resta delicato."
        )
    sentences.append("Prima di applicarlo, valuterei l'equilibrio con lo SL attuale.")
    return " ".join(sentences)


def _build_hypothetical_timeframe_reply(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> str:
    proposed_tfs = _extract_timeframes(user_message)
    t = _texts(lang)
    current_tf = snapshot.get("timeframe", t["nd"])
    if proposed_tfs:
        new_tf = proposed_tfs[0]
        if _resolve_lang(lang) == "en":
            direction = (
                "more signals and more noise"
                if new_tf in _SHORT_TIMEFRAMES
                else "fewer but more filtered signals"
            )
            return (
                f"If you switch to timeframe {new_tf} instead of {current_tf}, you tend to get {direction}. "
                "Trade frequency changes and it can affect win rate and drawdown. "
                "Before applying it, I'd consider whether you want more trades or more selective signals."
            )
        direction = "più segnali e più rumore" if new_tf in _SHORT_TIMEFRAMES else "meno segnali ma più filtrati"
        return (
            f"Se passi al timeframe {new_tf} invece di {current_tf}, tendi ad avere {direction}. "
            "Cambia frequenza dei trade e può influire su win rate e drawdown. "
            "Prima di applicarlo, valuterei se vuoi più operazioni o segnali più selezionati."
        )
    if _resolve_lang(lang) == "en":
        return (
            f"If you change timeframe from {current_tf}, you change how many signals you get and how much noise you include. "
            "Shorter timeframes increase trades, longer ones reduce them. "
            "Before applying it, I'd simulate the new timeframe in a preview."
        )
    return (
        f"Se cambi timeframe rispetto a {current_tf}, modifichi quanti segnali ricevi e quanto rumore includi. "
        "Timeframe più corti aumentano le operazioni, quelli più lunghi le riducono. "
        "Prima di applicarlo, simulerei il nuovo timeframe in preview."
    )


def build_hypothetical_rule_based_advice(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> str:
    t = _texts(lang)
    topic = _detect_hypothetical_topic(user_message)
    builders = {
        "sl": _build_hypothetical_sl_reply,
        "tp": _build_hypothetical_tp_reply,
        "symbol": _build_hypothetical_symbol_reply,
        "timeframe": _build_hypothetical_timeframe_reply,
    }
    builder = builders.get(topic)
    if builder:
        return builder(user_message, snapshot, lang=lang)
    if topic == "leverage":
        return t["hypo_leverage"]
    if topic == "risk":
        return t["hypo_risk"]
    return t["hypo_generic"]


def _try_llm_hypothetical_advice(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> Optional[str]:
    llm_client = _import_llm_client()
    if llm_client is None:
        return None
    code = _resolve_lang(lang)
    if code == "en":
        payload = (
            f"User hypothetical question:\n{(user_message or '').strip()}\n\n"
            f"Saved preview (context only, do not open with generic analysis):\n"
            f"{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
        )
    else:
        payload = (
            f"Domanda ipotetica dell'utente:\n{(user_message or '').strip()}\n\n"
            f"Preview salvata (solo contesto, non aprire con analisi generica):\n"
            f"{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
        )
    messages = [
        {"role": "system", "content": _HYPOTHETICAL_ADVICE_SYSTEM_BY_LANG[code]},
        {"role": "user", "content": payload},
    ]
    try:
        res = llm_client.client.responses.create(
            model=llm_client.MODEL_PRO,
            input=messages,
            max_output_tokens=260,
        )
        return (res.output_text or "").strip()
    except Exception as e:
        logger.error("[PREVIEW_ADVICE] hypothetical_llm_failed error=%s", e, exc_info=True)
        return None


def _try_llm_preview_advice(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    lang: Optional[str] = None,
) -> Optional[str]:
    llm_client = _import_llm_client()
    if llm_client is None:
        return None
    code = _resolve_lang(lang)
    if code == "en":
        payload = (
            f"User question:\n{(user_message or '').strip()}\n\n"
            f"Last saved preview (real data, do not invent other numbers):\n"
            f"{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
        )
    else:
        payload = (
            f"Domanda dell'utente:\n{(user_message or '').strip()}\n\n"
            f"Ultima preview salvata (dati reali, non inventare altri numeri):\n"
            f"{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
        )
    messages = [
        {"role": "system", "content": _PREVIEW_ADVICE_SYSTEM[code]},
        {"role": "user", "content": payload},
    ]
    try:
        res = llm_client.client.responses.create(
            model=llm_client.MODEL_PRO,
            input=messages,
            max_output_tokens=450,
        )
        return (res.output_text or "").strip()
    except Exception as e:
        logger.error("[PREVIEW_ADVICE] preview_llm_failed error=%s", e, exc_info=True)
        return None


def _try_llm_advice(
    user_message: str,
    snapshot: Dict[str, Any],
    *,
    hypothetical: bool = False,
    lang: Optional[str] = None,
) -> Optional[str]:
    llm_client = _import_llm_client()
    if llm_client is None:
        return None
    try:
        if hypothetical:
            return _try_llm_hypothetical_advice(user_message, snapshot, lang=lang)
        return _try_llm_preview_advice(user_message, snapshot, lang=lang)
    except Exception as e:
        logger.error("[PREVIEW_ADVICE] llm_failed error=%s", e, exc_info=True)
        return None


def handle_preview_advice_request(
    user_id: str,
    user_message: str,
    deps: PreviewAdviceDeps,
    *,
    lang: Optional[str] = None,
) -> Tuple[str, str, str]:
    resolved_lang = _resolve_lang(lang)
    t = _texts(resolved_lang)
    row = fetch_latest_preview(user_id, deps)
    if not row:
        logger.info("[PREVIEW_LANG] lang=%s", resolved_lang)
        logger.info("[PREVIEW_LANG] mode=advice")
        return (
            _finalize_preview_advice_reply(t["no_preview_reply"], lang=resolved_lang),
            "preview_advice",
            "preview_advice_no_data",
        )

    snapshot = build_preview_snapshot(row, lang=resolved_lang)
    hypothetical = is_hypothetical_question(user_message)
    mode_label = "hypothetical" if hypothetical else "advice"
    logger.info("[PREVIEW_LANG] lang=%s", resolved_lang)
    logger.info("[PREVIEW_LANG] mode=%s", mode_label)

    llm_reply = _try_llm_advice(
        user_message,
        snapshot,
        hypothetical=hypothetical,
        lang=resolved_lang,
    )
    if llm_reply:
        if hypothetical:
            return (
                _finalize_preview_advice_reply(llm_reply, lang=resolved_lang),
                "preview_advice",
                "preview_advice_hypothetical_llm",
            )
        reply = ensure_confirmation_suffix(llm_reply, snapshot, lang=resolved_lang)
        return (
            _finalize_preview_advice_reply(reply, lang=resolved_lang),
            "preview_advice",
            "preview_advice_llm",
        )

    if hypothetical:
        reply = build_hypothetical_rule_based_advice(
            user_message, snapshot, lang=resolved_lang
        )
        return (
            _finalize_preview_advice_reply(reply, lang=resolved_lang),
            "preview_advice",
            "preview_advice_hypothetical_fallback",
        )

    reply = build_rule_based_advice(snapshot, lang=resolved_lang)
    return (
        _finalize_preview_advice_reply(reply, lang=resolved_lang),
        "preview_advice",
        "preview_advice_fallback",
    )

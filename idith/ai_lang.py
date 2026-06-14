"""Centralized AI response language handling for Idith chat."""
from __future__ import annotations

import logging
import random
import re
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SUPPORTED_LANGS = frozenset({"it", "en"})
DEFAULT_LANG = "it"

_request_lang: ContextVar[str] = ContextVar("idith_chat_lang", default=DEFAULT_LANG)

# ---------------------------------------------------------------------------
# Static chat templates (it / en). Use chat() — never duplicate in orchestrator.
# ---------------------------------------------------------------------------

_CHAT: Dict[str, Dict[str, str]] = {
    "config_complete_header": {
        "it": "Configurazione completata ✅",
        "en": "Configuration complete ✅",
    },
    "config_complete_followup": {
        "it": (
            "\n\nPuoi:\n"
            "• avviare il bot\n"
            "• modificare la configurazione\n"
            "• richiedere una preview storica\n\n"
            "Cosa vuoi fare?"
        ),
        "en": (
            "\n\nYou can:\n"
            "• start the bot\n"
            "• change the configuration\n"
            "• request a historical preview\n\n"
            "What would you like to do?"
        ),
    },
    "config_updated_header": {
        "it": "Configurazione aggiornata ✅",
        "en": "Configuration updated ✅",
    },
    "config_reset_reply": {
        "it": "Configurazione resettata. Partiamo da capo: vuoi operare in Spot o in Futures?",
        "en": "Configuration reset. Let's start over: do you want to trade Spot or Futures?",
    },
    "config_reset_full": {
        "it": "Ho resettato la configurazione. Iniziamo da capo.",
        "en": "I've reset the configuration. Let's start from scratch.",
    },
    "invalid_not_applied_prefix": {
        "it": "Non ho applicato questi valori:\n",
        "en": "I did not apply these values:\n",
    },
    "pending_confirm_default": {
        "it": "Confermi i valori proposti?",
        "en": "Do you confirm the proposed values?",
    },
    "pending_confirm_setting": {
        "it": "Stai impostando {details}. Confermi?",
        "en": "You are setting {details}. Confirm?",
    },
    "pending_label_risk": {"it": "rischio", "en": "risk"},
    "pending_label_leverage": {"it": "leva", "en": "leverage"},
    "pending_join_two": {"it": "{a} e {b}", "en": "{a} and {b}"},
    "pending_join_three": {"it": "{a}, {b} e {c}", "en": "{a}, {b} and {c}"},
    "cancel_pending_modify": {
        "it": "Ok, ho annullato la modifica in attesa di conferma.",
        "en": "OK, I cancelled the change awaiting confirmation.",
    },
    "confirm_what_exactly": {
        "it": "Mi dici cosa vuoi confermare esattamente? (es. 'confermo leva' o 'confermo sl')",
        "en": "What exactly do you want to confirm? (e.g. 'confirm leverage' or 'confirm sl')",
    },
    "modify_or_start_bot": {
        "it": "Vuoi modificare altro o avviare il bot?",
        "en": "Do you want to change anything else or start the bot?",
    },
    "want_start_bot_now": {
        "it": "Vuoi avviare il bot adesso?",
        "en": "Do you want to start the bot now?",
    },
    "ask_operating_mode": {
        "it": "Scegli la modalità operativa: Aggressiva, Equilibrata o Selettiva.",
        "en": "Choose the operating mode: Aggressive, Balanced, or Selective.",
    },
    "ask_strategy_mode": {
        "it": "Che modalità preferisci: aggressiva, equilibrata o selettiva?",
        "en": "Which mode do you prefer: aggressive, balanced, or selective?",
    },
    "ask_strategy_fallback": {
        "it": "Che modalità vuoi usare? Aggressiva / Equilibrata / Selettiva",
        "en": "Which mode do you want? Aggressive / Balanced / Selective",
    },
    "ask_risk_pct": {
        "it": "Che percentuale del capitale vuoi rischiare per trade?",
        "en": "What percentage of capital do you want to risk per trade?",
    },
    "ask_sl": {
        "it": "Quale stop loss in percentuale?",
        "en": "What stop loss percentage?",
    },
    "ask_tp": {
        "it": "Quale take profit in percentuale?",
        "en": "What take profit percentage?",
    },
    "ask_leverage": {
        "it": "Che leva vuoi utilizzare?",
        "en": "What leverage do you want to use?",
    },
    "confirm_leverage": {
        "it": "Conferma leva.",
        "en": "Confirm leverage.",
    },
    "futures_need_leverage": {
        "it": "Nei Futures è necessario impostare una leva.\nChe leva vuoi usare?",
        "en": "Futures require leverage.\nWhat leverage do you want to use?",
    },
    "spot_to_futures_leverage": {
        "it": "Ok, passiamo da Spot a Futures 👍\nNei Futures è necessario impostare una leva.\nChe leva vuoi usare?",
        "en": "OK, switching from Spot to Futures 👍\nFutures require leverage.\nWhat leverage do you want to use?",
    },
    "futures_to_spot_no_leverage": {
        "it": "Ok, passiamo da Futures a Spot 👍\nIn modalità Spot la leva non si utilizza, quindi la rimuovo.",
        "en": "OK, switching from Futures to Spot 👍\nSpot mode does not use leverage, so I'm removing it.",
    },
    "ok_updating_one": {
        "it": "Ok, aggiorno {item}.",
        "en": "OK, updating {item}.",
    },
    "ok_updating_many_header": {
        "it": "Ok, aggiorno:",
        "en": "OK, updating:",
    },
    "config_updated_perfect": {
        "it": "Perfetto 👍 Ho aggiornato la configurazione.",
        "en": "Perfect 👍 I've updated the configuration.",
    },
    "param_modify_unclear": {
        "it": "Non ho capito quale parametro vuoi modificare. Puoi essere più specifico? (es. 'voglio modificare il timeframe', 'cambia leva a 5x', 'voglio cambiare EMA')",
        "en": "I'm not sure which parameter you want to change. Can you be more specific? (e.g. 'change timeframe', 'set leverage to 5x')",
    },
    "bot_started_summary": {
        "it": "Bot avviato con la seguente configurazione:",
        "en": "Bot started with the following configuration:",
    },
    "bot_cmd_start_reply": {
        "it": "",
        "en": (
            "✅ The bot is now running.\n\n"
            "You can follow the bot status in the table on the left.\n\n"
            "To stop it at any time, write:\n"
            "• stop bot\n"
            "• block everything"
        ),
    },
    "bot_cmd_stop_reply": {
        "it": "",
        "en": (
            "✅ The bot is now inactive.\n\n"
            "Do you want to change any parameters or restart the bot?\n\n"
            "You can:\n"
            "• modify the configuration\n"
            "• start the bot again"
        ),
    },
    "bot_cmd_start_footer": {
        "it": (
            "\n\n"
            "Puoi seguire lo stato del bot nella tabella a sinistra.\n\n"
            "Per interromperlo in qualsiasi momento scrivi in chat:\n"
            "• ferma bot\n"
            "• stop bot\n"
            "• blocca tutto"
        ),
        "en": "",
    },
    "bot_cmd_stop_footer": {
        "it": (
            "\n\n"
            "Vuoi modificare qualche parametro o riavviare il bot?\n\n"
            "Puoi:\n"
            "• modificare la configurazione\n"
            "• avviare nuovamente il bot"
        ),
        "en": "",
    },
    "config_already_ready": {
        "it": "Ciao! La configurazione è già pronta. Vuoi modificare qualcosa o avviare il bot?",
        "en": "Hi! The configuration is already ready. Do you want to change something or start the bot?",
    },
    "config_complete_answer_info": {
        "it": "La configurazione è completa. Rispondo alla tua domanda.",
        "en": "The configuration is complete. I'll answer your question.",
    },
    "free_plan_modes": {
        "it": "Nel piano Free puoi scegliere tra tre modalità operative:",
        "en": "On the Free plan you can choose among three operating modes:",
    },
    "lev_remove_prefix": {
        "it": "Ho rimosso la leva e aggiornato il mercato a Spot.",
        "en": "I removed leverage and updated the market to Spot.",
    },
    "invalid_value_for_key": {
        "it": "Valore non valido per {key}.",
        "en": "Invalid value for {key}.",
    },
    "leverage_invalid_range": {
        "it": "Leva non valida. Inserisci un valore tra 1x e 100x.",
        "en": "Invalid leverage. Enter a value between 1x and 100x.",
    },
    "invalid_leverage_value": {
        "it": "Valore di leva non valido.",
        "en": "Invalid leverage value.",
    },
    "retry_valid_value": {
        "it": "Riprova inserendo un valore valido.",
        "en": "Try again with a valid value.",
    },
    "must_enter_valid_number": {
        "it": "Devi inserire un numero valido.",
        "en": "You must enter a valid number.",
    },
    "modification_not_applicable": {
        "it": "Modifica non applicabile.",
        "en": "Change not applicable.",
    },
    "this_pair": {
        "it": "questa coppia",
        "en": "this pair",
    },
    "symbol_current_ask": {
        "it": "Perfetto 👍 Attualmente la coppia è {current}. Che coppia vuoi usare?",
        "en": "Great 👍 The current pair is {current}. Which pair do you want to use?",
    },
    "symbol_not_set": {
        "it": "non impostata",
        "en": "not set",
    },
    "symbol_example_suffix": {
        "it": "(es. BTCUSDT)",
        "en": "(e.g. BTCUSDT)",
    },
    "summary_verify_warning": {
        "it": (
            "⚠️ Prima di avviare il bot, verifica che i parametri mostrati siano quelli che hai richiesto.\n\n"
            "Se noti che manca qualche modifica, scrivimelo in chat e la aggiornerò.\n\n"
            "Esempi:\n"
            '- "Hai dimenticato di aggiornare lo stop loss"\n'
            '- "Aggiorna il rischio a 7%"\n'
            '- "Imposta la leva a 10x"'
        ),
        "en": (
            "⚠️ Before starting the bot, check that the parameters shown match what you requested.\n\n"
            "If something is missing, tell me in chat and I'll update it.\n\n"
            "Examples:\n"
            '- "You forgot to update the stop loss"\n'
            '- "Set risk to 7%"\n'
            '- "Set leverage to 10x"'
        ),
    },
    "summary_verify_warning_snippet": {
        "it": "Prima di avviare il bot, verifica che i parametri mostrati",
        "en": "Before starting the bot, check that the parameters shown",
    },
    "summary_verify_end_marker": {
        "it": '- "Imposta la leva a 10x"',
        "en": '- "Set leverage to 10x"',
    },
    "config_summary_title": {
        "it": "Riepilogo configurazione",
        "en": "Configuration summary",
    },
    "config_updated_phrase": {
        "it": "Ho aggiornato la configurazione",
        "en": "I've updated the configuration",
    },
    "ok_updating_phrase": {
        "it": "Ok, aggiorno",
        "en": "OK, updating",
    },
    "timeframe_invalid_format": {
        "it": "Il timeframe '{tf}' non è nel formato corretto. Valori supportati: {examples}. Inserisci uno di questi valori.",
        "en": "The timeframe '{tf}' is not in the correct format. Supported values: {examples}. Enter one of these values.",
    },
    "pair_recommend_volatile": {
        "it": "Posso impostare {symbol}: è una coppia più volatile rispetto a BTC/ETH. Vuoi usare {symbol}?",
        "en": "I can set {symbol}: it's a more volatile pair than BTC/ETH. Do you want to use {symbol}?",
    },
    "pair_recommend_simple": {
        "it": "Posso impostare {symbol}. Vuoi usare {symbol}?",
        "en": "I can set {symbol}. Do you want to use {symbol}?",
    },
    "warning_risk_aggressive": {
        "it": (
            "⚠️ Attenzione: rischiare il {risk_pct}% per trade è molto aggressivo. "
            "Confermi di volerlo impostare?"
        ),
        "en": (
            "⚠️ Warning: risking {risk_pct}% per trade is very aggressive. "
            "Do you confirm you want to set it?"
        ),
    },
    "warning_leverage_confirm": {
        "it": (
            "⚠️ Attenzione: una leva di {leverage_int}x aumenta molto il rischio. "
            "Confermi di volerla impostare?"
        ),
        "en": (
            "⚠️ Warning: leverage of {leverage_int}x greatly increases risk. "
            "Do you confirm you want to set it?"
        ),
    },
    "warning_sl_very_high": {
        "it": (
            "⚠️ Attenzione: stai impostando uno stop loss del {sl_pct}%, che è molto alto e rischioso. "
            "Ti suggerisco un valore più prudente del {suggested_sl}%. "
            "Vuoi usare {suggested_sl}% o preferisci confermare {sl_pct}%?"
        ),
        "en": (
            "⚠️ Warning: you are setting a Stop Loss of {sl_pct}%, which is very high and risky. "
            "I suggest a more conservative value of {suggested_sl}%. "
            "Do you want to use {suggested_sl}%, or do you prefer to confirm {sl_pct}%?"
        ),
    },
    "warning_sl_high": {
        "it": (
            "⚠️ Attenzione: stai impostando uno stop loss del {sl_pct}%, che è alto. "
            "Assicurati di comprendere i rischi. "
            "Vuoi confermare {sl_pct}% o preferisci un valore più prudente?"
        ),
        "en": (
            "⚠️ Warning: you are setting a Stop Loss of {sl_pct}%, which is high. "
            "Make sure you understand the risks. "
            "Do you want to confirm {sl_pct}%, or would you prefer a more conservative value?"
        ),
    },
    "warning_leverage_high_soft": {
        "it": (
            "⚠️ Attenzione: stai usando una leva alta ({leverage_int}x) per {symbol}. "
            "Le leve elevate aumentano significativamente il rischio. "
            "Assicurati di comprendere i rischi prima di procedere."
        ),
        "en": (
            "⚠️ Warning: you are using high leverage ({leverage_int}x) for {symbol}. "
            "High leverage significantly increases risk. "
            "Make sure you understand the risks before proceeding."
        ),
    },
}

_SUMMARY_LABELS: Dict[str, Dict[str, str]] = {
    "pair": {"it": "Coppia", "en": "Pair"},
    "market_type": {"it": "Tipo di mercato", "en": "Market type"},
    "timeframe": {"it": "Timeframe", "en": "Timeframe"},
    "operating_mode": {"it": "Modalità operativa", "en": "Operating mode"},
    "leverage": {"it": "Leva", "en": "Leverage"},
    "risk_pct": {"it": "Rischio per trade", "en": "Risk per trade"},
    "sl": {"it": "Stop Loss", "en": "Stop Loss"},
    "tp": {"it": "Take Profit", "en": "Take Profit"},
}

_SUMMARY_FIELD_ORDER = (
    "pair",
    "market_type",
    "timeframe",
    "operating_mode",
    "leverage",
    "risk_pct",
    "sl",
    "tp",
)

# Phrase variant lists (rotating wizard / validation copy)
_PHRASE_VARIANTS: Dict[str, Dict[str, List[str]]] = {
    "ask_symbol": {
        "it": [
            "Che coppia vuoi usare?",
            "Quale coppia USDT inserisci?",
            "Scrivimi la coppia in formato BTCUSDT:",
        ],
        "en": [
            "Which pair do you want to use?",
            "Which USDT pair are you entering?",
            "Enter the pair in BTCUSDT format:",
        ],
    },
    "ask_timeframe": {
        "it": ["Quale timeframe?", "Che timeframe vuoi usare?", "Dimmi il timeframe che preferisci."],
        "en": ["Which timeframe?", "What timeframe do you want?", "Tell me your preferred timeframe."],
    },
    "ask_leverage": {
        "it": ["Che leva vuoi utilizzare?", "Quale leva scegli?", "Inserisci la leva:"],
        "en": ["What leverage do you want to use?", "Which leverage do you choose?", "Enter the leverage:"],
    },
    "positive_transition": {
        "it": ["Ok", "Va bene", "Dimmi", "Scrivimi"],
        "en": ["OK", "Sure", "Tell me", "Enter"],
    },
    "ask_ema_period": {
        "it": [
            "Che periodo vuoi usare per l'EMA? (predefinito: 200)",
            "Dimmi il periodo dell'EMA (default 200).",
            "Per l'EMA serve un numero: che periodo scegli? (200 se vuoi lasciare il default)",
        ],
        "en": [
            "What period do you want for the EMA? (default: 200)",
            "Tell me the EMA period (default 200).",
            "EMA needs a number: which period do you choose? (200 for default)",
        ],
    },
    "ask_rsi_period": {
        "it": [
            "Che periodo vuoi usare per l'RSI? (predefinito: 14)",
            "Dimmi il periodo dell'RSI (default 14).",
            "Per l'RSI serve un numero: che periodo scegli? (14 se vuoi lasciare il default)",
        ],
        "en": [
            "What period do you want for the RSI? (default: 14)",
            "Tell me the RSI period (default 14).",
            "RSI needs a number: which period do you choose? (14 for default)",
        ],
    },
    "ask_atr_period": {
        "it": [
            "Che periodo vuoi usare per l'ATR? (predefinito: 14)",
            "Dimmi il periodo dell'ATR (default 14).",
            "Per l'ATR serve un numero: che periodo scegli? (14 se vuoi lasciare il default)",
        ],
        "en": [
            "What period do you want for the ATR? (default: 14)",
            "Tell me the ATR period (default 14).",
            "ATR needs a number: which period do you choose? (14 for default)",
        ],
    },
    "market_type_greeting": {
        "it": [
            "Iniziamo! Vuoi operare in Spot o in Futures? ⚠️ Nota: visti i recenti aggiornamenti normativi, per alcuni account europei i Futures su Bybit potrebbero non essere disponibili. Se scegli Futures, il bot proverà comunque a tradare.",
            "Partiamo dalla modalità: Spot o Futures? ⚠️ Nota importante: per alcuni account europei i Futures su Bybit potrebbero non essere disponibili a causa di recenti aggiornamenti normativi. Se scegli Futures, il bot proverà comunque a operare.",
            "Prima scelta: preferisci Spot o Futures? ⚠️ Attenzione: a causa delle recenti normative, per alcuni account europe i Futures su Bybit potrebbero non essere disponibili. Se scegli Futures, il bot proverà comunque a tradare.",
        ],
        "en": [
            "Let's begin! Do you want to trade Spot or Futures? ⚠️ Note: due to recent regulatory updates, Futures on Bybit may not be available for some European accounts. If you choose Futures, the bot will still try to trade.",
            "First, choose the mode: Spot or Futures? ⚠️ Important: for some European accounts Futures on Bybit may be unavailable due to recent regulatory updates. If you choose Futures, the bot will still try to operate.",
            "First choice: Spot or Futures? ⚠️ Warning: due to recent regulations, Futures on Bybit may not be available for some European accounts. If you choose Futures, the bot will still try to trade.",
        ],
    },
    "step_question_symbol": {
        "it": [
            "Che coppia vuoi usare?",
            "Quale coppia USDT inserisci?",
            "Scrivimi la coppia in formato BTCUSDT:",
            "Ok, riproviamo: quale symbol USDT vuoi?",
            "Inserisci la coppia di trading:",
            "Quale coppia scegli?",
        ],
        "en": [
            "Which pair do you want to use?",
            "Which USDT pair are you entering?",
            "Enter the pair in BTCUSDT format:",
            "OK, let's try again: which USDT symbol?",
            "Enter the trading pair:",
            "Which pair do you choose?",
        ],
    },
    "step_question_timeframe": {
        "it": [
            "Quale timeframe?",
            "Scegli un timeframe:",
            "Che timeframe vuoi usare?",
            "Inserisci il timeframe:",
            "Quale timeframe preferisci?",
            "Ok, riproviamo: quale timeframe?",
        ],
        "en": [
            "Which timeframe?",
            "Choose a timeframe:",
            "What timeframe do you want?",
            "Enter the timeframe:",
            "Which timeframe do you prefer?",
            "OK, let's try again: which timeframe?",
        ],
    },
    "step_question_leverage": {
        "it": [
            "Che leva vuoi utilizzare?",
            "Quale leva scegli?",
            "Inserisci la leva:",
            "Che leva preferisci?",
            "Ok, riproviamo: quale leva?",
        ],
        "en": [
            "What leverage do you want to use?",
            "Which leverage do you choose?",
            "Enter the leverage:",
            "Which leverage do you prefer?",
            "OK, let's try again: which leverage?",
        ],
    },
    "bot_cmd_start_header": {
        "it": [
            "✅ Bot avviato.",
            "✅ Bot attivo.",
            "✅ Il bot è partito.",
            "✅ Bot in esecuzione.",
            "✅ Il bot è stato avviato.",
            "✅ Avvio completato.",
            "✅ Il bot è ora operativo.",
            "✅ Bot online.",
            "✅ Sistema avviato.",
            "✅ Tutto pronto, bot avviato.",
        ],
    },
    "bot_cmd_stop_header": {
        "it": [
            "✅ Bot fermato.",
            "✅ Bot arrestato.",
            "✅ Il bot è stato fermato.",
            "✅ Stop completato.",
            "✅ Il bot è ora inattivo.",
            "✅ Bot disattivato.",
            "✅ Esecuzione fermata.",
            "✅ Il bot si è fermato.",
            "✅ Bot offline.",
            "✅ Sistema fermato.",
        ],
    },
}

_GUARDRAIL_MARKERS_IT = (
    "Configurazione completata",
    "Configurazione aggiornata",
    "Riepilogo configurazione",
    "Ho aggiornato la configurazione",
    "Bot avviato con la seguente configurazione",
    "Ho rimosso la leva e aggiornato il mercato a Spot.",
    "Vuoi modificare altro o avviare il bot",
    "Ok, aggiorno",
)

_GUARDRAIL_MARKERS_EN = (
    "Configuration complete",
    "Configuration updated",
    "Configuration summary",
    "I've updated the configuration",
    "Bot started with the following configuration",
    "I removed leverage and updated the market to Spot.",
    "Do you want to change anything else or start the bot",
    "OK, updating",
)

_WIZARD_Q_MARKERS_IT = ("Che ", "Quale ", "Scegli ", "Scrivi ", "Imposta ")
_WIZARD_Q_MARKERS_EN = ("Which ", "What ", "Choose ", "Enter ", "Set ")


def normalize_lang(lang: Optional[str]) -> str:
    code = (lang or DEFAULT_LANG).strip().lower()
    if code.startswith("en"):
        return "en"
    if code.startswith("it"):
        return "it"
    return DEFAULT_LANG


def set_request_lang(lang: Optional[str]) -> None:
    """Set chat language for the current request (orchestrator / app)."""
    _request_lang.set(normalize_lang(lang))


def get_request_lang() -> str:
    return normalize_lang(_request_lang.get())


def chat(key: str, lang: Optional[str] = None, **kwargs: Any) -> str:
    """Return a localized static chat template."""
    code = normalize_lang(lang or get_request_lang())
    bucket = _CHAT.get(key) or {}
    template = bucket.get(code) or bucket.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except KeyError:
            return template
    return template


def warning_chat(key: str, lang: Optional[str] = None, **kwargs: Any) -> str:
    """Localized risk/SL/leverage warning with [LANG_WARNING] log."""
    code = normalize_lang(lang or get_request_lang())
    logger.info("[LANG_WARNING] key=%s lang=%s", key, code)
    return chat(key, code, **kwargs)


def phrase_variant(key: str, attempt: int = 0, lang: Optional[str] = None, **kwargs: Any) -> str:
    """Rotating phrase list by key (wizard questions, errors, etc.)."""
    code = normalize_lang(lang or get_request_lang())
    variants = (_PHRASE_VARIANTS.get(key) or {}).get(code) or []
    if not variants:
        variants = (_PHRASE_VARIANTS.get(key) or {}).get(DEFAULT_LANG) or [key]
    idx = attempt % len(variants)
    template = variants[idx]
    if kwargs:
        try:
            return template.format(**kwargs)
        except KeyError:
            return template
    return template


def build_bot_start_reply(lang: Optional[str] = None) -> str:
    """Localized reply after /bot start (runner command)."""
    code = normalize_lang(lang or get_request_lang())
    logger.info("[BOT_CMD_LANG] command=start lang=%s", code)
    if code == "en":
        return chat("bot_cmd_start_reply", code)
    header = random.choice(_PHRASE_VARIANTS["bot_cmd_start_header"]["it"])
    return header + chat("bot_cmd_start_footer", code)


def build_bot_stop_reply(lang: Optional[str] = None) -> str:
    """Localized reply after /bot stop (runner command)."""
    code = normalize_lang(lang or get_request_lang())
    logger.info("[BOT_CMD_LANG] command=stop lang=%s", code)
    if code == "en":
        return chat("bot_cmd_stop_reply", code)
    header = random.choice(_PHRASE_VARIANTS["bot_cmd_stop_header"]["it"])
    return header + chat("bot_cmd_stop_footer", code)


def summary_label(field: str, lang: Optional[str] = None) -> str:
    code = normalize_lang(lang or get_request_lang())
    bucket = _SUMMARY_LABELS.get(field) or {}
    return bucket.get(code) or bucket.get(DEFAULT_LANG) or field


def all_summary_labels(lang: Optional[str] = None) -> Dict[str, str]:
    return {field: summary_label(field, lang) for field in _SUMMARY_FIELD_ORDER}


def summary_line_regex() -> re.Pattern[str]:
    """Regex matching summary lines in any supported language."""
    labels: List[str] = []
    for field in _SUMMARY_FIELD_ORDER:
        bucket = _SUMMARY_LABELS.get(field) or {}
        for code in ("it", "en"):
            val = bucket.get(code)
            if val:
                labels.append(re.escape(val))
    pattern = r"^(" + "|".join(labels) + r")\s*:"
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


def guardrail_markers() -> tuple[str, ...]:
    return _GUARDRAIL_MARKERS_IT + _GUARDRAIL_MARKERS_EN


def wizard_question_markers(lang: Optional[str] = None) -> tuple[str, ...]:
    code = normalize_lang(lang or get_request_lang())
    if code == "en":
        return _WIZARD_Q_MARKERS_EN
    return _WIZARD_Q_MARKERS_IT


def summary_first_line_prefix(lang: Optional[str] = None) -> str:
    return summary_label("pair", lang) + ":"


def invalid_timeframe_message(tf: str, valid_tfs: Any, lang: Optional[str] = None) -> str:
    """Localized timeframe format error (matches validators.validate_timeframe copy)."""
    examples = ", ".join(
        sorted(valid_tfs, key=lambda x: (int(x[:-1]) if x[:-1].isdigit() else 999, x[-1]))
    )
    return chat("timeframe_invalid_format", lang, tf=tf, examples=examples)


def _fmt_pending_percent(value: Any) -> str:
    try:
        f = float(str(value).replace("%", "").replace(",", "."))
    except Exception:
        return str(value)
    return str(int(f)) if f.is_integer() else str(f)


def build_pending_confirm_prompt(
    pending: Dict[str, Any],
    *,
    lang: Optional[str] = None,
    format_leverage: Optional[Callable[[Any], str]] = None,
) -> str:
    """Build cumulative pending confirmation prompt (sl / risk / leverage)."""
    code = normalize_lang(lang or get_request_lang())
    pieces: List[str] = []
    if "sl" in pending:
        pieces.append(f"Stop Loss {_fmt_pending_percent(pending['sl'])}%")
    if "risk_pct" in pending:
        risk_l = chat("pending_label_risk", code)
        pieces.append(f"{risk_l} {_fmt_pending_percent(pending['risk_pct'])}%")
    if "leverage" in pending:
        lev_l = chat("pending_label_leverage", code)
        lev_val = pending["leverage"]
        if format_leverage is not None:
            lev_display = format_leverage(lev_val)
        else:
            lev_display = str(lev_val)
        pieces.append(f"{lev_l} {lev_display}")
    if not pieces:
        return chat("pending_confirm_default", code)
    if len(pieces) == 1:
        details = pieces[0]
    elif len(pieces) == 2:
        details = chat("pending_join_two", code, a=pieces[0], b=pieces[1])
    else:
        details = chat("pending_join_three", code, a=pieces[0], b=pieces[1], c=pieces[2])
    return chat("pending_confirm_setting", code, details=details)


def build_invalid_not_applied_block(messages: List[str], lang: Optional[str] = None) -> str:
    prefix = chat("invalid_not_applied_prefix", lang)
    return prefix + "\n".join(f"- {msg}" for msg in messages)


def build_language_system_prompt(lang: Optional[str]) -> str:
    """System prompt snippet: all user-facing model output must use this language."""
    code = normalize_lang(lang)
    if code == "en":
        return (
            "LANGUAGE (MANDATORY):\n"
            "You MUST write your entire reply in English.\n"
            "Use natural, clear English. Keep standard trading terms (Spot, Futures, USDT pair, "
            "timeframe, stop loss, take profit, leverage, operating mode).\n"
            "Do NOT reply in Italian unless quoting the user's exact words."
        )
    return (
        "LANGUAGE (MANDATORY):\n"
        "You MUST write your entire reply in Italian.\n"
        "Use natural, clear Italian. Keep standard trading terms where appropriate.\n"
        "Do NOT reply in English unless quoting the user's exact words."
    )


def build_orchestrator_wrap_lang_rules(lang: Optional[str], has_question: bool) -> str:
    """Language-specific rules for the orchestrator wrap prompt."""
    code = normalize_lang(lang)
    if code == "en":
        if has_question:
            return (
                "- Write your entire output in English.\n"
                "- Your output must contain EXACTLY ONE question in the whole text (one '?').\n"
                "- That question must be an accurate English translation of the orchestrator question below "
                "(same meaning and constraints; you may adjust wording for natural English).\n"
                "- FORBIDDEN: extra questions, parameter lists, new wizard steps.\n"
                "- You may write at most 2-3 sentences before the final question for minimal context."
            )
        return (
            "- Configuration is COMPLETE. Do NOT ask configuration questions.\n"
            "- Reply informatively to the user's question in English.\n"
            "- Do NOT re-propose strategy, timeframe, leverage, risk, SL/TP or other config parameters.\n"
            "- Do NOT reopen completed steps."
        )
    if has_question:
        return (
            "- Il tuo output deve contenere ESATTAMENTE UNA sola domanda in tutto il testo (un solo '?').\n"
            "- Quell'unica domanda deve essere IDENTICA alla domanda dell'orchestrator (sotto).\n"
            "- VIETATO fare altre domande, VIETATO elenchi di parametri, VIETATO introdurre nuovi step.\n"
            "- Puoi scrivere massimo 2-3 frasi prima della domanda finale, solo per contesto minimo."
        )
    return (
        "- La configurazione è COMPLETA. NON fare domande di configurazione.\n"
        "- Rispondi in modo informativo alla domanda dell'utente.\n"
        "- NON riproporre strategia, timeframe, leva, rischio, SL/TP o altri parametri di configurazione.\n"
        "- NON riaprire step già completati."
    )


_LOCALIZE_SYSTEM = (
    "You localize Idith trading-bot assistant messages.\n"
    "Target language: {target}.\n"
    "Rules:\n"
    "- If the text is already entirely in the target language, return it unchanged.\n"
    "- Otherwise translate user-facing prose to the target language.\n"
    "- Preserve emojis, numbers, percentages, symbols (e.g. BTCUSDT), line breaks, and structure.\n"
    "- Do NOT change parameter values or trading symbols.\n"
    "- Output ONLY the localized text, no commentary."
)


def localize_assistant_reply(
    text: str,
    lang: Optional[str],
    *,
    api_key: Optional[str] = None,
    model: str = "gpt-4o-mini",
) -> str:
    """
    Fallback: localize replies that still contain Italian when lang != it.
    Prefer chat() templates in orchestrator; use this only for residual dynamic text.
    """
    code = normalize_lang(lang)
    content = (text or "").strip()
    if code == "it" or not content:
        return text or ""

    if not api_key:
        logger.warning("[CHAT_LANG] localize skipped: missing API key")
        return text

    target = "English" if code == "en" else "Italian"
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _LOCALIZE_SYSTEM.format(target=target)},
                {"role": "user", "content": content},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        localized = (response.choices[0].message.content or "").strip()
        return localized or text
    except Exception as exc:
        logger.warning("[CHAT_LANG] localize failed: %s", exc)
        return text

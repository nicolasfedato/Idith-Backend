# llm_client.py — ROUTER DEFINITIVO mini + 4.1
# -------------------------------------------------
# Regole:
# - mini SOLO per small talk / info piattaforma
# - 4.1 SEMPRE per spiegazioni, bot, strategie, analisi
# - tono caldo, niente liste, niente markdown, niente asterischi
# - mai chiedere API key, mai salvarle
# -------------------------------------------------

import json
import os
import re
from typing import Any, Dict

from openai import OpenAI

client = OpenAI()

MODEL_MINI = os.getenv("IDITH_MODEL_MINI", "gpt-4.1-mini")
MODEL_PRO = os.getenv("IDITH_MODEL_PRO", "gpt-4.1")


# -------------------------------------------------
# INTENT CLASSIFICATION
# -------------------------------------------------

SMALL_TALK_PATTERNS = [
    r"\bciao\b", r"\bhey\b", r"\bhello\b",
    r"\bchi sei\b",
    r"\bche tempo fa\b",
    r"\bmeteo\b",
    r"\bapi key\b",
    r"\bdove .* api\b",
    r"\bcome funziona la piattaforma\b",
]

BOT_EXPLANATION_PATTERNS = [
    r"non ho capito",
    r"spiegami",
    r"cos.?è",
    r"rsi",
    r"ema",
    r"atr",
    r"strateg",
    r"bot",
    r"leva",
    r"rischio",
    r"timeframe",
    r"coppia",
    r"bybit",
    r"futures",
    r"spot",
    r"analisi",
    r"perché",
]


def _match_any(text: str, patterns) -> bool:
    for p in patterns:
        if re.search(p, text):
            return True
    return False


def choose_model(user_text: str) -> str:
    t = (user_text or "").lower().strip()

    # spiegazioni / bot / trading → SEMPRE 4.1
    if _match_any(t, BOT_EXPLANATION_PATTERNS):
        return MODEL_PRO

    # small talk / piattaforma → mini
    if _match_any(t, SMALL_TALK_PATTERNS):
        return MODEL_MINI

    # default prudente: mini
    return MODEL_MINI


# -------------------------------------------------
# SYSTEM PROMPT (tono caldo, no elenchi)
# -------------------------------------------------

SYSTEM_PROMPT = (
    "Sei Idith, un assistente amichevole e rassicurante. "
    "Parla in modo naturale, senza elenchi, senza punti numerati, "
    "senza markdown o asterischi. "
    "Non decidere mai al posto dell’utente, non dare consigli finanziari. "
    "Se rispondi a una spiegazione, fallo in modo semplice e chiaro. "
    "Non chiedere mai API key e non dire di inserirle in chat o piattaforma."
)


# -------------------------------------------------
# MAIN ENTRY
# -------------------------------------------------

def chat_completion(user_text: str, history=None) -> str:
    model = choose_model(user_text)

    print(f"[LLM] Model scelto: {model}")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if history:
        for h in history[-6:]:
            if h.get("role") in ("user", "assistant"):
                messages.append(
                    {"role": h["role"], "content": h.get("message", "")}
                )

    messages.append({"role": "user", "content": user_text})

    res = client.responses.create(
        model=model,
        input=messages,
        max_output_tokens=400,
    )

    return res.output_text.strip()


# -------------------------------------------------
# Wizard configurazione: domande informative (sempre modello PRO)
# -------------------------------------------------

WIZARD_QA_SYSTEM = (
    "Sei Idith. L'utente sta completando la configurazione guidata di un bot di trading "
    "(piano Free, integrazione tipo Bybit). "
    "Rispondi in modo chiaro e concreto. "
    "Se confronti più opzioni (es. tre modalità operative Aggressiva / Equilibrata / Selettiva), "
    "usa 2–4 righe bullet brevi con trattino (-) e una riga «In pratica:» se aiuta. "
    "Collega la spiegazione allo step indicato nel contesto quando è pertinente. "
    "Non inventare funzioni non presenti nel contesto. "
    "Non dare consulenza finanziaria personalizzata né promesse di guadagno; "
    "limitati a spiegare significati e differenze tra le scelte del configuratore. "
    "Non chiedere mai API key. "
    "Non chiudere ripetendo la domanda del wizard (la sistema la aggiungerà dopo)."
)


def _wizard_params_snapshot(params: Dict[str, Any]) -> str:
    """Compatto, solo valori già impostati."""
    if not params:
        return "(nessun parametro ancora impostato)"
    keys = (
        "market_type",
        "symbol",
        "timeframe",
        "operating_mode",
        "strategy_id",
        "leverage",
        "risk_pct",
        "sl",
        "tp",
    )
    parts = []
    for k in keys:
        v = params.get(k)
        if v is not None and v != "":
            parts.append(f"{k}={v}")
    return "; ".join(parts) if parts else "(nessun parametro ancora impostato)"


_STEP_LABEL_IT = {
    "market_type": "Spot vs Futures",
    "symbol": "coppia da tradare",
    "timeframe": "timeframe candele",
    "operating_mode": "modalità operativa (Aggressiva / Equilibrata / Selettiva)",
    "strategy": "modalità operativa",
    "strategy_params": "parametri strategia",
    "leverage": "leva",
    "risk_pct": "rischio per trade",
    "sl": "stop loss",
    "tp": "take profit",
}


def wizard_config_question_answer(
    user_question: str, wizard_step: str, params: Dict[str, Any], lang: str = "it"
) -> str:
    """
    Risposta LLM per una domanda dell'utente durante il wizard.
    Non modifica stato; il chiamante concatena la domanda dello step corrente.
    """
    try:
        from . import ai_lang
    except ImportError:
        import ai_lang  # type: ignore

    step_key = wizard_step or "market_type"
    step_human = _STEP_LABEL_IT.get(step_key, step_key)
    snapshot = _wizard_params_snapshot(params or {})
    payload = (
        f"Domanda dell'utente:\n{user_question.strip()}\n\n"
        f"Step wizard corrente (solo contesto): {step_human}\n"
        f"Parametri già compilati: {snapshot}"
    )
    messages = [
        {"role": "system", "content": WIZARD_QA_SYSTEM},
        {"role": "system", "content": ai_lang.build_language_system_prompt(lang)},
        {"role": "user", "content": payload},
    ]
    print(f"[LLM] Wizard QA model: {MODEL_PRO}")
    res = client.responses.create(
        model=MODEL_PRO,
        input=messages,
        max_output_tokens=500,
    )
    return (res.output_text or "").strip()


# -------------------------------------------------
# Preview advice: spiegazione + suggerimenti parametri
# -------------------------------------------------

PREVIEW_ADVICE_SYSTEM = (
    "Sei Idith. L'utente chiede spiegazione e suggerimenti sulla sua ultima preview backtest salvata.\n\n"
    "Rispondi SEMPRE in italiano con ESATTAMENTE questa struttura (tre sezioni, titoli come scritti sotto):\n\n"
    "Perché la preview è andata così\n"
    "[2-4 frasi che spiegano il motivo principale del risultato. DEVI citare esplicitamente: "
    "pnl_pct, win rate, numero operazioni, drawdown, timeframe. "
    "Cita SL/TP se rilevanti per spiegare stop frequenti o risk/reward.]\n\n"
    "Cosa proverei a cambiare\n"
    "[elenco numerato 1-4 con modifiche concrete: timeframe, SL, TP, modalità operativa, rischio, leva]\n\n"
    "Vuoi che aggiorni la configurazione con questi parametri?\n\n"
    "Regole:\n"
    "- Usa SOLO i numeri presenti nel JSON della preview; non inventare metriche.\n"
    "- Valori ammessi: timeframe 1m,3m,5m,15m,1h,4h,1d; modalità Aggressiva/Equilibrata/Selettiva.\n"
    "- NON promettere guadagni. NON applicare modifiche: solo consiglio testuale.\n"
    "- Non usare markdown o asterischi. Non chiedere API key."
)


def preview_advice_answer(user_message: str, snapshot: Dict[str, Any]) -> str:
    """
    Risposta LLM per spiegazione preview + suggerimenti parametri.
    Non modifica stato né configurazione.
    """
    payload = (
        f"Domanda dell'utente:\n{(user_message or '').strip()}\n\n"
        f"Ultima preview salvata (dati reali, non inventare altri numeri):\n"
        f"{json.dumps(snapshot, ensure_ascii=False, indent=2)}"
    )
    messages = [
        {"role": "system", "content": PREVIEW_ADVICE_SYSTEM},
        {"role": "user", "content": payload},
    ]
    print(f"[LLM] Preview advice model: {MODEL_PRO}")
    res = client.responses.create(
        model=MODEL_PRO,
        input=messages,
        max_output_tokens=450,
    )
    return (res.output_text or "").strip()

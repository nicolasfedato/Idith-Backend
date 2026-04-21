# llm_client.py — ROUTER DEFINITIVO mini + 4.1
# -------------------------------------------------
# Regole:
# - mini SOLO per small talk / info piattaforma
# - 4.1 SEMPRE per spiegazioni, bot, strategie, analisi
# - tono caldo, niente liste, niente markdown, niente asterischi
# - mai chiedere API key, mai salvarle
# -------------------------------------------------

import os
import re
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

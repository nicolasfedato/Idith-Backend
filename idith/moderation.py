# moderation.py
from __future__ import annotations
from typing import Dict
import re

SEVERE_PAT = re.compile(r"\b(kill|suicide|terror|bomba|odio razziale|nazista)\b", re.I)
ABUSE_PAT = re.compile(r"\b(figlio di|vaffan|stronzo|coglione)\b", re.I)

def check(text: str) -> Dict[str, str]:
    t = (text or "").strip()
    if SEVERE_PAT.search(t):
        return {"action": "block", "reply": "Ok. Manteniamo un tono civile. Dimmi cosa vuoi configurare sul bot."}
    if ABUSE_PAT.search(t):
        return {"action": "pass", "reply": ""}
    return {"action": "pass", "reply": ""}

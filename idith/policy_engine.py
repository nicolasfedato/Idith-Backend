# policy_engine.py
from __future__ import annotations
from typing import Dict, Any
import re


# ==========================================================
# POLICY / BINARI IDITH
# ==========================================================

DANGEROUS_LEVERAGE_PAT = re.compile(r"\b(x\s*20|20x|leva\s*20|leva\s*x20)\b", re.I)
FOREX_PAT = re.compile(r"\b(forex|eurusd|gbpusd|usdjpy|commodities|oro|gold)\b", re.I)
BINANCE_PAT = re.compile(r"\b(binance)\b", re.I)

# richieste che innescano spiegazione limitazioni
LIMITATIONS_PAT = re.compile(r"\b(bybit|testnet|pro|forex|azioni|stocks|binance|exchange)\b", re.I)


def apply(
    user_text: str,
    state: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """
    Applica policy e produce:
    - block: True/False
    - override_reply: risposta hard-coded se necessario
    - flags: info per orchestrator/brain
    """
    state = state or {}
    flags = {}

    text = (user_text or "").strip()
    low = text.lower()

    # ------------------------------------------------------------
    # NO FOREX
    # ------------------------------------------------------------
    if FOREX_PAT.search(low):
        return {
            "block": True,
            "reason": "no_forex",
            "message": (
                "Capito 😊\n\n"
                "Al momento Idith può configurare e gestire bot **solo su criptovalute** "
                "(quindi **no Forex**).\n\n"
                "Se vuoi, posso aiutarti a creare un bot crypto su Bybit Testnet: "
                "ti faccio 2 domande veloci e lo impostiamo insieme. Vuoi partire?"
            )
        }


    # ----------------------------------------------------------
    # LEVA PERICOLOSA (x20)
    # ----------------------------------------------------------
    if DANGEROUS_LEVERAGE_PAT.search(low):
        flags["dangerous_leverage"] = True

    # ----------------------------------------------------------
    # Limiti (Bybit testnet, Pro a breve) -> SOLO se l'utente chiede
    # ----------------------------------------------------------
    if LIMITATIONS_PAT.search(low):
        flags["needs_limitations_info"] = True

    # ----------------------------------------------------------
    # Binance -> non dire "Sì puoi usarla", ma ricondurre su Bybit testnet
    # ----------------------------------------------------------
    if BINANCE_PAT.search(low):
        flags["asked_binance"] = True

    return {"block": False, "flags": flags}
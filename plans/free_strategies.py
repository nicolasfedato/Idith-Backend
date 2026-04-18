from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

# ============================================================
# FREE v2: operating_mode → (strategy_id, strategy_params)
# ------------------------------------------------------------
# Tre sole modalità: aggressiva, equilibrata, selettiva.
# Fonte di verità per preset salvati insieme a strategy_id.
# Chiavi in strategy_params dipendono dalla strategia (1/2/3).
# ============================================================

OPERATING_MODE_STRATEGY_PARAMS: Dict[str, Dict[str, Any]] = {
    # 1 — RSI only (nessuna EMA, nessun ATR)
    "aggressiva": {
        "rsi_buy": 45,
        "rsi_sell": 55,
        "rsi_period": 5,
    },
    # 2 — EMA + RSI (nessun ATR)
    "equilibrata": {
        "rsi_buy": 45,
        "rsi_sell": 55,
        "rsi_period": 7,
        "ema_period": 7,
    },
    # 3 — EMA + RSI + ATR (volatilità minima)
    "selettiva": {
        "rsi_buy": 45,
        "rsi_sell": 55,
        "rsi_period": 7,
        "ema_period": 10,
        "atr_period": 7,
        "atr_min_threshold": 0.05,
    },
}


@dataclass(frozen=True)
class OperatingModePreset:
    strategy_id: str
    strategy_params: Dict[str, Any]


OPERATING_MODE_FULL_PRESETS: Dict[str, OperatingModePreset] = {
    "aggressiva": OperatingModePreset(
        strategy_id="1",
        strategy_params=OPERATING_MODE_STRATEGY_PARAMS["aggressiva"],
    ),
    "equilibrata": OperatingModePreset(
        strategy_id="2",
        strategy_params=OPERATING_MODE_STRATEGY_PARAMS["equilibrata"],
    ),
    "selettiva": OperatingModePreset(
        strategy_id="3",
        strategy_params=OPERATING_MODE_STRATEGY_PARAMS["selettiva"],
    ),
}


def get_preset_for_operating_mode(mode: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Ritorna (strategy_id, strategy_params) per operating_mode.
    strategy_params è una copia mutabile.
    """
    if not mode:
        return None
    key = mode.strip().lower()
    preset = OPERATING_MODE_FULL_PRESETS.get(key)
    if not preset:
        return None
    return preset.strategy_id, dict(preset.strategy_params)


def get_preset_by_operating_mode(mode: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    return get_preset_for_operating_mode(mode)


@dataclass
class StrategyBlock:
    """Blocco strategia legacy (indicator-level) per copy/UI opzionale."""

    id: str
    label: str
    description: str
    indicators: list[str]
    defaults: dict[str, int]


FREE_STRATEGY_BLOCKS: list[StrategyBlock] = [
    StrategyBlock(
        id="rsi_only_free",
        label="RSI (aggressiva)",
        description="Solo RSI: ingressi più frequenti, nessuna conferma EMA/ATR",
        indicators=["RSI"],
        defaults={"rsi_period": 5},
    ),
    StrategyBlock(
        id="ema_rsi_free",
        label="EMA + RSI (equilibrata)",
        description="Trend (cross prezzo/EMA) e timing RSI, senza filtro ATR",
        indicators=["EMA", "RSI"],
        defaults={"ema_period": 9, "rsi_period": 7},
    ),
    StrategyBlock(
        id="ema_rsi_atr_free",
        label="EMA + RSI + ATR (selettiva)",
        description="Trend + RSI + volatilità minima (ATR)",
        indicators=["EMA", "RSI", "ATR"],
        defaults={"ema_period": 21, "rsi_period": 14, "atr_period": 14},
    ),
    StrategyBlock(
        id="volatility_breakout",
        label="Volatility Breakout",
        description="EMA + ATR (legacy)",
        indicators=["EMA", "ATR"],
        defaults={"ema_period": 50, "atr_period": 14},
    ),
]


def get_free_strategy_block(block_id: str) -> Optional[StrategyBlock]:
    for block in FREE_STRATEGY_BLOCKS:
        if block.id == block_id:
            return block
    return None

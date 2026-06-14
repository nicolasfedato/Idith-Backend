"""Test normalizzazione timeframe in linguaggio naturale."""

from __future__ import annotations

import pytest

from idith import validators
from idith.orchestrator import _extract_modification_requests, _extract_step_value


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("60 minuti", "1h"),
        ("120 minuti", "2h"),
        ("240 minuti", "4h"),
        ("1 ora", "1h"),
        ("un'ora", "1h"),
        ("2 ore", "2h"),
        ("due ore", "2h"),
        ("4 ore", "4h"),
        ("quattro ore", "4h"),
        ("giornaliero", "1d"),
        ("daily", "1d"),
        ("un giorno", "1d"),
        ("1 minuto", "1m"),
        ("3 minuti", "3m"),
        ("5 minuti", "5m"),
        ("15 minuti", "15m"),
        ("30 minuti", "30m"),
        ("45 minuti", "45m"),
        ("metti timeframe 60 minuti", "1h"),
        ("set timeframe 60 minutes", "1h"),
        ("update timeframe to 4 hours", "4h"),
        ("metti timeframe due ore", "2h"),
        ("voglio operare su 60 minuti", "1h"),
        ("voglio operare su 120 minuti", "2h"),
        ("btcusdt 4 ore aggressiva", "4h"),
    ],
)
def test_normalize_timeframe_input(raw, expected):
    assert validators.normalize_timeframe_input(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("60 minuti", "1h"),
        ("120 minuti", "2h"),
        ("btcusdt 60 minuti aggressiva", "1h"),
        ("btcusdt 4 ore aggressiva", "4h"),
        ("1 ora", "1h"),
        ("due ore", "2h"),
        ("giornaliero", "1d"),
        ("btcusdt giornaliero selettiva", "1d"),
        ("voglio operare su 120 minuti", "2h"),
    ],
)
def test_multiparam_timeframe_extraction(raw, expected):
    updates = _extract_modification_requests(raw, {})
    assert updates.get("timeframe") == expected


def test_wizard_step_timeframe_extraction():
    assert _extract_step_value("60 minuti", "timeframe", {}) == "1h"


def test_multiparam_english_spelled_timeframe_sixty_minutes():
    updates = _extract_modification_requests(
        "insert tp ten, leverage seven, sixty minutes",
        {},
    )
    assert updates.get("tp") == 10.0
    assert updates.get("leverage") == 7.0
    assert updates.get("timeframe") == "1h"

#!/usr/bin/env python3
"""
Test script per validazione STRICT di symbol, timeframe, leverage.
Verifica che:
- AAAusdt → invalid (non listed)
- PPPUST → invalid (pattern)
- AAVEUSDT → valid (se listata davvero)
- timeframe 7m → invalid
- timeframe 5m → valid (se supportato)
- leverage 999x → invalid con range
"""

from __future__ import annotations

import sys
import os

# Aggiungi il path per importare validators
sys.path.insert(0, os.path.dirname(__file__))

try:
    from . import validators
except ImportError as e:
    print(f"ERRORE: Impossibile importare validators: {e}")
    sys.exit(1)


def test_normalize_symbol_strict():
    """Test normalize_symbol_strict."""
    print("\n=== TEST normalize_symbol_strict ===")
    
    test_cases = [
        ("BTCUSDT", "BTCUSDT"),  # Valido
        ("btcusdt", "BTCUSDT"),  # Lowercase → uppercase
        ("BTC/USDT", "BTCUSDT"),  # Rimuove /
        ("BTC-USDT", "BTCUSDT"),  # Rimuove -
        ("  BTCUSDT  ", "BTCUSDT"),  # Trim
        ("AAAusdt", "AAAUSDT"),  # Pattern valido (anche se non listato)
        ("PPPUST", None),  # Non termina con USDT
        ("BTC/USD", None),  # Non termina con USDT
        ("", None),  # Vuoto
        ("BTC@USDT", None),  # Carattere speciale
    ]
    
    for input_val, expected in test_cases:
        result = validators.normalize_symbol_strict(input_val)
        status = "✓" if result == expected else "✗"
        print(f"{status} '{input_val}' → {result} (atteso: {expected})")
        if result != expected:
            print(f"  ERRORE: atteso {expected}, ottenuto {result}")


def test_validate_symbol():
    """Test validate_symbol (richiede connessione Bybit)."""
    print("\n=== TEST validate_symbol ===")
    
    # Test pattern invalido (non richiede Bybit)
    print("Test pattern invalido:")
    is_valid, error_msg = validators.validate_symbol("PPPUST", "futures")
    print(f"  'PPPUST' → valid={is_valid}, error={error_msg}")
    assert not is_valid, "PPPUST dovrebbe essere invalido (pattern)"
    
    # Test simbolo non listato (richiede Bybit)
    print("\nTest simbolo non listato (richiede connessione Bybit):")
    try:
        is_valid, error_msg = validators.validate_symbol("AAAUSDT", "futures")
        print(f"  'AAAUSDT' → valid={is_valid}")
        if is_valid:
            print("  ATTENZIONE: AAAUSDT risulta valido (potrebbe essere listato su Bybit)")
        else:
            print(f"  ✓ AAAUSDT correttamente rifiutato: {error_msg[:80]}...")
    except Exception as e:
        print(f"  ⚠ Errore connessione Bybit: {e}")
        print("  (Test saltato - richiede API key Bybit)")


def test_validate_timeframe():
    """Test validate_timeframe."""
    print("\n=== TEST validate_timeframe ===")
    
    test_cases = [
        ("5m", True),  # Valido
        ("1h", True),  # Valido
        ("1d", True),  # Valido
        ("7m", False),  # INVALIDO (non supportato)
        ("17m", False),  # INVALIDO
        ("7ore", False),  # INVALIDO (formato sbagliato)
        ("45m", False),  # INVALIDO (non supportato)
        ("2h", True),  # Valido
        ("4h", True),  # Valido
    ]
    
    for tf, expected_valid in test_cases:
        is_valid, error_msg = validators.validate_timeframe(tf)
        status = "✓" if (is_valid == expected_valid) else "✗"
        print(f"{status} '{tf}' → valid={is_valid} (atteso: {expected_valid})")
        if not is_valid and error_msg:
            print(f"    Errore: {error_msg[:60]}...")


def test_validate_leverage():
    """Test validate_leverage."""
    print("\n=== TEST validate_leverage ===")
    
    # Test con limiti fittizi
    minLev, maxLev = 1.0, 125.0
    
    test_cases = [
        (10.0, True),  # Valido
        (1.0, True),  # Minimo
        (125.0, True),  # Massimo
        (999.0, False),  # INVALIDO (fuori range)
        (0.5, False),  # INVALIDO (sotto minimo)
        (200.0, False),  # INVALIDO (sopra massimo)
    ]
    
    for lev, expected_valid in test_cases:
        is_valid, error_msg = validators.validate_leverage(lev, minLev, maxLev)
        status = "✓" if (is_valid == expected_valid) else "✗"
        print(f"{status} {lev}x → valid={is_valid} (atteso: {expected_valid})")
        if not is_valid and error_msg:
            print(f"    Errore: {error_msg[:60]}...")


def main():
    """Esegue tutti i test."""
    print("=" * 60)
    print("TEST VALIDAZIONE STRICT - Idith Backend")
    print("=" * 60)
    
    test_normalize_symbol_strict()
    test_validate_symbol()
    test_validate_timeframe()
    test_validate_leverage()
    
    print("\n" + "=" * 60)
    print("Test completati!")
    print("=" * 60)


if __name__ == "__main__":
    main()


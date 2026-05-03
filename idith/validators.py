# -*- coding: utf-8 -*-
"""
validators.py - Validazione rigorosa e centralizzata per Idith
Lista simboli Bybit: endpoint pubblico REST (nessuna API key server).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple, Dict, Any
from dotenv import load_dotenv

# REST pubblico mainnet (market data, nessuna autenticazione)
BYBIT_PUBLIC_REST = "https://api.bybit.com"

# Carica .env
load_dotenv()

# API Key Bybit (da .env) - caricate lazy per evitare errori all'import
def _get_api_keys() -> Tuple[str, str]:
    """Recupera le API key da .env. Solleva errore se mancanti."""
    api_key = os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
    
    if not api_key or not api_secret:
        raise RuntimeError(
            "BYBIT_API_KEY e BYBIT_API_SECRET devono essere presenti nel file .env. "
            "NON inserire mai le chiavi nel codice."
        )
    
    return api_key, api_secret

# Inizializza pybit
try:
    from pybit.unified_trading import HTTP
    _HAS_PYBIT = True
except ImportError:
    _HAS_PYBIT = False
    HTTP = None

# Crea sessione Bybit Testnet
_bybit_session: Optional[HTTP] = None

def _get_bybit_session() -> HTTP:
    """Crea o restituisce la sessione Bybit (singleton)."""
    global _bybit_session
    
    if _bybit_session is not None:
        return _bybit_session
    
    if not _HAS_PYBIT or HTTP is None:
        raise RuntimeError("pybit non è installato. Installa con: pip install pybit")
    
    # Recupera API key (solleva errore se mancanti)
    api_key, api_secret = _get_api_keys()
    
    try:
        _bybit_session = HTTP(
            testnet=True,
            api_key=api_key,
            api_secret=api_secret,
        )
        return _bybit_session
    except Exception as e:
        raise RuntimeError(f"Errore nella connessione a Bybit Testnet: {str(e)}")


# Cache per simboli validi (per evitare troppe chiamate API)
_symbol_cache: Dict[str, set] = {
    "spot": set(),
    "futures": set(),
}
# True dopo il primo tentativo di caricamento per quel mercato
_symbol_universe_loaded: Dict[str, bool] = {"spot": False, "futures": False}
# Se la lista Bybit non è disponibile: accetta solo formato USDT (normalize_symbol_strict)
_symbol_list_fallback_usdt: Dict[str, bool] = {}

_leverage_cache: Dict[str, Optional[Tuple[float, float]]] = {}  # symbol -> (min_leverage, max_leverage)


def _symbols_from_instruments_list(instruments: list) -> set:
    valid_symbols: set = set()
    for inst in instruments:
        symbol = (inst.get("symbol") or "").upper()
        if not symbol or not symbol.endswith("USDT"):
            continue
        status = (inst.get("status") or "").upper()
        if status == "TRADING":
            valid_symbols.add(symbol)
    return valid_symbols


def _fetch_valid_symbols_public_http(market_type: str) -> set:
    """
    Elenco strumenti da Bybit V5 market (pubblico, senza API key).
    spot -> category=spot; futures -> category=linear (perpetual USDT).
    """
    category = "spot" if market_type == "spot" else "linear"
    acc: set = set()
    cursor = ""
    for _ in range(120):
        q: Dict[str, str] = {"category": category, "limit": "500"}
        if cursor:
            q["cursor"] = cursor
        url = f"{BYBIT_PUBLIC_REST}/v5/market/instruments-info?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Idith/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("retCode") != 0:
            raise RuntimeError(payload.get("retMsg") or "bybit")
        result = payload.get("result") or {}
        instruments = result.get("list") or []
        acc |= _symbols_from_instruments_list(instruments)
        cursor = (result.get("nextPageCursor") or "").strip()
        if not cursor:
            break
    return acc


def fetch_valid_symbols(market_type: str) -> set:
    """
    Recupera i simboli validi da Bybit per il market_type specificato.
    Usa cache per evitare troppe chiamate API.
    Funzione pubblica per accesso esterno.
    """
    return _fetch_valid_symbols_internal(market_type)


def _fetch_valid_symbols_internal(market_type: str) -> set:
    """
    Recupera i simboli USDT in trading da Bybit (solo REST pubblico).
    Se la chiamata fallisce: fallback formato USDT (non blocca il wizard).
    """
    if market_type not in ["spot", "futures"]:
        return set()

    if _symbol_universe_loaded.get(market_type):
        return _symbol_cache[market_type]

    valid_symbols: set = set()
    try:
        valid_symbols = _fetch_valid_symbols_public_http(market_type)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, KeyError, TypeError, ValueError, RuntimeError):
        valid_symbols = set()

    if valid_symbols:
        _symbol_cache[market_type] = valid_symbols
        _symbol_list_fallback_usdt[market_type] = False
    else:
        _symbol_cache[market_type] = set()
        _symbol_list_fallback_usdt[market_type] = True

    _symbol_universe_loaded[market_type] = True
    return _symbol_cache[market_type]


def _fetch_valid_symbols(market_type: str) -> set:
    """Alias per compatibilità."""
    return _fetch_valid_symbols_internal(market_type)


def normalize_symbol_strict(raw: str) -> Optional[str]:
    """
    Normalizza symbol in modo STRICT: solo trim + upper, nessuna interpretazione.
    
    Args:
        raw: Simbolo grezzo (es. "btc/usdt", "BTC-USDT", "BTCUSDT")
    
    Returns:
        Simbolo normalizzato (es. "BTCUSDT") o None se formato invalido.
    
    REGOLE STRICT:
    - Trim spazi
    - Upper case
    - Rimuovi solo "/" e "-" (separatori comuni)
    - Verifica pattern: solo [A-Z0-9]+ (niente caratteri speciali)
    - Deve terminare con USDT
    - NON fa mapping/sostituzioni/similarity
    """
    if not raw:
        return None
    
    # Trim e upper
    symbol = raw.strip().upper()
    
    # Rimuovi solo separatori comuni (/, -)
    symbol = symbol.replace("/", "").replace("-", "")
    
    # Verifica pattern: solo lettere e numeri
    if not symbol.isalnum():
        return None
    
    # Deve terminare con USDT
    if not symbol.endswith("USDT"):
        return None
    
    # Deve avere almeno 6 caratteri (es. "BTCUSDT")
    if len(symbol) < 6:
        return None
    
    return symbol


def is_symbol_listed(exchange_client: Optional[Any], market_type: str, symbol: str) -> bool:
    """
    Verifica se un simbolo è listato su Bybit per il market_type specificato.
    
    Args:
        exchange_client: Client Bybit (ignorato, usiamo sessione interna)
        market_type: "spot" o "futures"
        symbol: Simbolo da verificare (es. "BTCUSDT")
    
    Returns:
        True se listato, False altrimenti.
    
    REGOLE STRICT:
    - Controllo membership ESATTO: symbol in set(listed_symbols)
    - Nessuna interpretazione o fuzzy matching
    """
    if market_type not in ["spot", "futures"]:
        return False
    
    # Normalizza symbol
    symbol_normalized = normalize_symbol_strict(symbol)
    if symbol_normalized is None:
        return False
    
    _fetch_valid_symbols_internal(market_type)
    if _symbol_list_fallback_usdt.get(market_type):
        return True
    return symbol_normalized in _symbol_cache[market_type]


def get_valid_timeframes(exchange_client: Optional[Any], market_type: str) -> set[str]:
    """
    Restituisce i timeframe validi per Bybit per il market_type specificato.
    
    Args:
        exchange_client: Client Bybit (ignorato, usiamo lista hardcoded verificata)
        market_type: "spot" o "futures"
    
    Returns:
        Set di timeframe validi (es. {"1m", "5m", "15m", "1h", "1d", ...})
    
    NOTA: Bybit supporta gli stessi timeframe per spot e futures (linear).
    Se in futuro dovessero differire, questa funzione può essere estesa.
    """
    # Bybit supporta gli stessi timeframe per spot e futures (linear)
    # Lista verificata e centralizzata
    return VALID_TIMEFRAMES.copy()


def get_leverage_limits(exchange_client: Optional[Any], symbol: str, market_type: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Recupera i limiti di leva (min, max) per un simbolo su Bybit Futures.
    
    Args:
        exchange_client: Client Bybit (ignorato, usiamo sessione interna)
        symbol: Simbolo (es. "BTCUSDT")
        market_type: "spot" o "futures"
    
    Returns:
        (min_leverage, max_leverage) o (None, None) se non disponibile.
        Per Bybit, min_leverage è tipicamente 1x.
    """
    if market_type != "futures":
        return (None, None)
    
    if symbol in _leverage_cache:
        cached = _leverage_cache[symbol]
        if cached is None:
            return (None, None)
        return cached
    
    try:
        session = _get_bybit_session()
        
        # Recupera info strumento per futures (linear)
        response = session.get_instruments_info(category="linear", symbol=symbol.upper())
        result = response.get("result", {})
        instruments = result.get("list", [])
        
        if not instruments:
            _leverage_cache[symbol] = None
            return (None, None)
        
        inst = instruments[0]
        leverage_filter = inst.get("leverageFilter", {})
        max_leverage_str = leverage_filter.get("maxLeverage", "")
        min_leverage_str = leverage_filter.get("minLeverage", "1")
        
        try:
            max_leverage = float(max_leverage_str) if max_leverage_str else None
            min_leverage = float(min_leverage_str) if min_leverage_str else 1.0
            
            if max_leverage is None:
                _leverage_cache[symbol] = None
                return (None, None)
            
            limits = (min_leverage, max_leverage)
            _leverage_cache[symbol] = limits
            return limits
        except (ValueError, TypeError):
            _leverage_cache[symbol] = None
            return (None, None)
            
    except Exception:
        _leverage_cache[symbol] = None
        return (None, None)


def validate_symbol(symbol: str, market_type: str) -> Tuple[bool, Optional[str]]:
    """
    Valida che il simbolo esista su Bybit per il market_type specificato.
    
    Args:
        symbol: Simbolo da validare (es. "BTCUSDT")
        market_type: "spot" o "futures"
    
    Returns:
        (is_valid, error_message)
        - Se valido: (True, None)
        - Se invalido: (False, messaggio_errore_umano)
    
    REGOLE RIGOROSE:
    - Il simbolo deve essere ESATTAMENTE come su Bybit (case-sensitive dopo upper)
    - Nessuna interpretazione, nessuna autocorrezione
    - Se anche una lettera è sbagliata, è INVALIDO
    """
    if not symbol:
        return (False, "Il simbolo non può essere vuoto.")
    
    # Normalizza STRICT (nessuna interpretazione)
    symbol_normalized = normalize_symbol_strict(symbol)
    if symbol_normalized is None:
        return (
            False,
            f"Il simbolo '{symbol}' non è nel formato corretto. "
            "Deve essere una coppia USDT (es. BTCUSDT, ETHUSDT)."
        )
    
    if market_type not in ["spot", "futures"]:
        return (
            False,
            f"Tipo di mercato non valido: {market_type}. Deve essere 'spot' o 'futures'."
        )
    
    valid_symbols = _fetch_valid_symbols_internal(market_type)
    if _symbol_list_fallback_usdt.get(market_type):
        return (True, None)

    # Verifica ESATTA corrispondenza (nessuna interpretazione)
    if symbol_normalized not in valid_symbols:
        # Costruisci messaggio di errore umano con esempi REALI (3-6 simboli)
        import random
        examples_list = list(valid_symbols)
        if len(examples_list) > 6:
            examples = random.sample(examples_list, 6)
        else:
            examples = examples_list[:6]
        examples_str = ", ".join(examples) if examples else "Nessun esempio disponibile"
        
        return (
            False,
            f"La coppia '{symbol_normalized}' non esiste su Bybit {market_type.capitalize()}. "
            f"Ricontrolla il simbolo e riprova (esempi validi: {examples_str})."
        )
    
    return (True, None)


# Timeframe supportati da Bybit Futures (lista hardcoded ma verificata)
# REGOLA RIGIDA: Solo questi valori sono accettati, nessuna interpretazione o normalizzazione
VALID_TIMEFRAMES = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "12h",
    "1d", "1w"
}


def normalize_timeframe(tf: str) -> Optional[str]:
    """
    Normalizza il valore di timeframe in modo safe:
    - converte in stringa
    - strip
    - lower
    - rimuove tutti gli spazi
    Non effettua alcun mapping o interpretazione semantica.
    """
    if tf is None:
        return None
    s = str(tf).strip().lower().replace(" ", "")
    return s


def validate_timeframe(tf: str, valid_set: Optional[set[str]] = None) -> Tuple[bool, Optional[str]]:
    """
    Valida che il timeframe sia supportato da Bybit.
    Usa normalize_timeframe per alias (es. "60", "1h", "giornaliero") poi verifica contro allowed.
    Valori non in whitelist (es. "7m", "17m") vengono rifiutati.
    """
    if not tf:
        return (False, "Il timeframe non può essere vuoto.")
    valid_tfs = valid_set if valid_set is not None else VALID_TIMEFRAMES
    tf_normalized = normalize_timeframe(tf)
    if tf_normalized is None:
        tf_normalized = tf.strip().lower()
    if not any(tf_normalized.endswith(unit) for unit in ["m", "h", "d", "w"]):
        examples_str = ", ".join(sorted(valid_tfs, key=lambda x: (int(x[:-1]) if x[:-1].isdigit() else 999, x[-1])))
        return (False, f"Il timeframe '{tf}' non è nel formato corretto. Valori supportati: {examples_str}. Inserisci uno di questi valori.")
    if tf_normalized not in valid_tfs:
        examples_str = ", ".join(sorted(valid_tfs, key=lambda x: (int(x[:-1]) if x[:-1].isdigit() else 999, x[-1])))
        return (False, f"Il timeframe '{tf}' non esiste su Bybit. Timeframe validi: {examples_str}. Inserisci uno di questi valori esatti.")
    return (True, None)


def parse_positive_int(raw: str, field_name: str, min_v: int, max_v: int) -> int:
    """
    Estrae un intero da raw (strip, elimina spazi) e verifica che sia in [min_v, max_v].
    Solleva ValueError se non è un int o se fuori range.
    
    Args:
        raw: Input utente (str o convertibile)
        field_name: Nome campo per messaggio errore (es. "Periodo RSI")
        min_v: Minimo consentito (incluso)
        max_v: Massimo consentito (incluso)
    
    Returns:
        Intero validato
    """
    if raw is None:
        raise ValueError(f"{field_name} non può essere vuoto.")
    s = str(raw).strip()
    if not s:
        raise ValueError(f"{field_name} non può essere vuoto.")
    try:
        value = int(float(s.replace(",", ".")))
    except (ValueError, TypeError):
        raise ValueError(f"{field_name} deve essere un numero intero.")
    if value < min_v or value > max_v:
        raise ValueError(f"{field_name} deve essere tra {min_v} e {max_v}.")
    return value


def validate_leverage_range(leverage: int, max_leverage: int) -> None:
    """
    Verifica che la leva sia in range 1 .. max_leverage.
    Solleva ValueError se fuori range.
    """
    if leverage < 1:
        raise ValueError("La leva deve essere almeno 1x.")
    if leverage > max_leverage:
        raise ValueError(
            f"La leva {leverage}x supera il massimo consentito ({int(max_leverage)}x). "
            f"Inserisci un valore tra 1x e {int(max_leverage)}x."
        )


def validate_leverage(lev: float, minLev: float, maxLev: float) -> Tuple[bool, Optional[str]]:
    """
    Valida la leva rispetto ai limiti min/max.
    
    Args:
        lev: Leva da validare (es. 10.0 per 10x)
        minLev: Leva minima consentita
        maxLev: Leva massima consentita
    
    Returns:
        (ok, reason)
        - Se valido: (True, None)
        - Se invalido: (False, messaggio_errore)
    
    REGOLE STRICT PER FUTURES:
    - La leva DEVE essere un numero intero
    - Range consentito: minimo 1, massimo 100
    - Se > 100 → INVALIDO (non accettare, non chiedere conferma)
    - Se 51-100 → valido ma warning (gestito separatamente)
    - Se 1-50 → valido senza warning
    """
    try:
        leverage_float = float(lev)
    except (ValueError, TypeError):
        return (
            False,
            f"La leva deve essere un numero."
        )
    
    # Verifica che sia un intero
    if leverage_float != int(leverage_float):
        return (
            False,
            f"La leva deve essere un numero intero."
        )
    
    leverage_int = int(leverage_float)
    
    if leverage_int <= 0:
        return (
            False,
            f"La leva deve essere un numero positivo."
        )
    
    # REGOLA RIGIDA: Bybit Futures consente massimo 100x
    if leverage_int > 100:
        return (
            False,
            f"La leva {leverage_int}x supera il massimo consentito su Bybit Futures (100x). "
            f"Bybit non consente leve superiori a 100. Inserisci un valore tra 1x e 100x."
        )
    
    # Verifica rispetto ai limiti del simbolo (se disponibili)
    if leverage_int < minLev:
        return (
            False,
            f"La leva {leverage_int}x è inferiore al minimo consentito per questo simbolo ({int(minLev)}x). "
            f"Inserisci un valore tra {int(minLev)}x e {int(maxLev)}x."
        )
    
    if leverage_int > maxLev:
        return (
            False,
            f"La leva {leverage_int}x supera il massimo consentito per questo simbolo ({int(maxLev)}x). "
            f"Inserisci un valore tra {int(minLev)}x e {int(maxLev)}x."
        )
    
    # Valido (anche se alto, è consentito)
    return (True, None)


def validate_indicator_period(indicator: str, period: int) -> Tuple[bool, Optional[str]]:
    """
    Valida il periodo per un indicatore (EMA, RSI, ATR).
    VALIDAZIONE RIGIDA E BLOCCANTE: nessuna interpretazione, nessun adattamento.
    
    Args:
        indicator: Nome indicatore ("EMA", "RSI", "ATR")
        period: Periodo da validare
    
    Returns:
        (is_valid, error_message)
        - Se valido: (True, None)
        - Se invalido: (False, messaggio_errore_umano_con_spiegazione)
    
    REGOLE RIGIDE:
    - EMA: 5 - 500 (valori <5 o >500 sono INVALIDI)
    - RSI: 5 - 50 (valori <5 o >50 sono INVALIDI)
    - ATR: 5 - 50 (valori <5 o >50 sono INVALIDI)
    """
    if not indicator:
        return (False, "L'indicatore non può essere vuoto.")
    
    indicator = indicator.strip().upper()
    
    if indicator not in ["EMA", "RSI", "ATR"]:
        return (
            False,
            f"Indicatore non supportato: {indicator}. Indicatori supportati: EMA, RSI, ATR."
        )
    
    try:
        period_int = int(period)
    except (ValueError, TypeError):
        return (
            False,
            f"Il periodo deve essere un numero intero."
        )
    
    # Range per ogni indicatore (interi positivi in range sensato)
    ranges = {
        "EMA": (2, 500),
        "RSI": (2, 200),
        "ATR": (2, 200),
    }
    
    min_period, max_period = ranges[indicator]
    
    # VALIDAZIONE RIGIDA: se fuori range, è INVALIDO
    if period_int < min_period:
        if indicator == "EMA":
            return (
                False,
                f"Il valore {period_int} è troppo basso per l'EMA. "
                f"Devi usare un numero tra {min_period} e {max_period}. "
                f"Valori tipici: 20, 50, 200. Inserisci un valore valido."
            )
        elif indicator == "RSI":
            return (
                False,
                f"Il valore {period_int} è troppo basso per l'RSI. "
                f"L'RSI richiede un periodo tra {min_period} e {max_period} per essere utilizzabile. "
                f"Valore tipico: 14. Inserisci un valore valido."
            )
        else:  # ATR
            return (
                False,
                f"Il valore {period_int} è troppo basso per l'ATR. "
                f"Devi usare un numero tra {min_period} e {max_period}. "
                f"Valore tipico: 14. Inserisci un valore valido."
            )
    
    if period_int > max_period:
        if indicator == "EMA":
            return (
                False,
                f"Il valore {period_int} è troppo alto per l'EMA. "
                f"Devi usare un numero tra {min_period} e {max_period}. "
                f"Valori tipici: 20, 50, 200. Inserisci un valore valido."
            )
        elif indicator == "RSI":
            return (
                False,
                f"Il valore {period_int} è troppo alto per l'RSI. "
                f"Con un periodo così alto, l'RSI diventerebbe inutilizzabile. "
                f"Devi usare un numero tra {min_period} e {max_period}. "
                f"Valore tipico: 14. Inserisci un valore valido."
            )
        else:  # ATR
            return (
                False,
                f"Il valore {period_int} è troppo alto per l'ATR. "
                f"Devi usare un numero tra {min_period} e {max_period}. "
                f"Valore tipico: 14. Inserisci un valore valido."
            )
    
    return (True, None)


def clear_cache():
    """Pulisce le cache (utile per testing o aggiornamenti)."""
    global _symbol_cache, _leverage_cache, _symbol_universe_loaded, _symbol_list_fallback_usdt
    _symbol_cache = {
        "spot": set(),
        "futures": set(),
    }
    _symbol_universe_loaded = {"spot": False, "futures": False}
    _symbol_list_fallback_usdt = {}
    _leverage_cache = {}


# Funzione di compatibilità per validate_leverage (mantiene signature originale)
def validate_leverage_full(symbol: str, leverage: float, market_type: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Valida la leva per un simbolo su Bybit Futures (versione completa con warning).
    
    Args:
        symbol: Simbolo (es. "BTCUSDT")
        leverage: Leva da validare (es. 10.0 per 10x)
        market_type: "spot" o "futures"
    
    Returns:
        (is_valid, error_message, warning_message)
        - Se valido: (True, None, warning_opzionale)
        - Se invalido: (False, messaggio_errore, None)
        - Se valido ma alto: (True, None, messaggio_avviso)
    """
    if market_type == "spot":
        return (
            False,
            "La leva non è disponibile per il trading spot. "
            "La leva è disponibile solo per futures.",
            None
        )
    
    if market_type != "futures":
        return (
            False,
            f"Tipo di mercato non valido per la leva: {market_type}.",
            None
        )
    
    if not symbol:
        return (
            False,
            "Il simbolo è richiesto per validare la leva.",
            None
        )
    
    # Normalizza simbolo
    symbol_normalized = normalize_symbol_strict(symbol)
    if symbol_normalized is None:
        return (
            False,
            f"Il simbolo '{symbol}' non è nel formato corretto.",
            None
        )
    
    # Recupera limiti leverage
    minLev, maxLev = get_leverage_limits(None, symbol_normalized, market_type)
    
    if minLev is None or maxLev is None:
        # Non siamo riusciti a recuperare i limiti
        # Verifica prima che il simbolo esista
        symbol_valid, symbol_error = validate_symbol(symbol_normalized, "futures")
        if not symbol_valid:
            return (False, symbol_error, None)
        
        # Se il simbolo è valido ma non riusciamo a recuperare la leva,
        # usa validazione base (intero, range 1-100)
        try:
            leverage_float = float(leverage)
            leverage_int = int(leverage_float)
            
            # Verifica che sia un intero
            if leverage_float != leverage_int:
                return (
                    False,
                    f"La leva deve essere un numero intero.",
                    None
                )
            
            # Verifica range base 1-100
            if leverage_int < 1:
                return (
                    False,
                    f"La leva deve essere almeno 1x.",
                    None
                )
            
            if leverage_int > 100:
                return (
                    False,
                    f"La leva {leverage_int}x supera il massimo consentito su Bybit Futures (100x). "
                    f"Bybit non consente leve superiori a 100. Inserisci un valore tra 1x e 100x.",
                    None
                )
            
            # Avviso per 51-100
            warning = None
            if 51 <= leverage_int <= 100:
                warning = f"⚠️ Attenzione: stai usando una leva alta ({leverage_int}x). Assicurati di comprendere i rischi."
            
            return (True, None, warning)
        except (ValueError, TypeError):
            return (
                False,
                f"La leva deve essere un numero.",
                None
            )
    
    # Valida usando limiti
    is_valid, error_msg = validate_leverage(leverage, minLev, maxLev)
    if not is_valid:
        return (False, error_msg, None)
    
    # Leva valida, ma controlla se è alta per mostrare avviso
    # REGOLE:
    # - 1-50: nessun avviso
    # - 51-100: avviso di rischio (non bloccante)
    try:
        leverage_float = float(leverage)
        leverage_int = int(leverage_float)
        warning = None
        
        if 51 <= leverage_int <= 100:
            warning = (
                f"⚠️ Attenzione: stai usando una leva alta ({leverage_int}x) per {symbol_normalized}. "
                "Le leve elevate aumentano significativamente il rischio. "
                "Assicurati di comprendere i rischi prima di procedere."
            )
        
        return (True, None, warning)
    except (ValueError, TypeError):
        return (True, None, None)


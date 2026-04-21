# -*- coding: utf-8 -*-
"""
phrases.py - Utility per gestire varianti di testo con rotazione
Evita ripetizioni identiche nelle risposte di Idith.
"""

from typing import List, Optional


# Varianti per domande EMA periodo
ASK_EMA_PERIOD = [
    "Che periodo vuoi usare per l'EMA? (predefinito: 200)",
    "Dimmi il periodo dell'EMA (default 200).",
    "Per l'EMA serve un numero: che periodo scegli? (200 se vuoi lasciare il default)"
]

# Varianti per errori EMA periodo invalido (range 5-500)
# REGOLA: Mai dire "perfetto" se il valore è sbagliato. Spiegare perché non va bene e suggerire valori tipici.
INVALID_EMA_PERIOD = [
    "Il valore {value} non è valido per l'EMA. Deve essere un numero tra 5 e 500. Valori tipici: 20, 50, 200. Inserisci un valore valido.",
    "{value} è fuori dal range consentito per l'EMA (5-500). Questo valore non può essere utilizzato. Valori tipici: 20, 50, 200. Che periodo vuoi usare?",
    "Non posso accettare {value} per l'EMA: il range è 5-500. Valori tipici: 20, 50, 200. Inserisci un numero valido."
]

# Varianti per domande RSI periodo
ASK_RSI_PERIOD = [
    "Che periodo vuoi usare per l'RSI? (predefinito: 14)",
    "Dimmi il periodo dell'RSI (default 14).",
    "Per l'RSI serve un numero: che periodo scegli? (14 se vuoi lasciare il default)"
]

# Varianti per errori RSI periodo invalido (range 5-50)
# REGOLA: Mai dire "perfetto" se il valore è sbagliato. Spiegare che l'RSI diventerebbe inutilizzabile.
INVALID_RSI_PERIOD = [
    "Il valore {value} non è valido per l'RSI. Deve essere un numero tra 5 e 50. Con valori fuori range, l'RSI diventerebbe inutilizzabile. Valore tipico: 14. Inserisci un valore valido.",
    "{value} è fuori dal range consentito per l'RSI (5-50). Questo valore non può essere utilizzato perché renderebbe l'RSI inutilizzabile. Valore tipico: 14. Che periodo vuoi usare?",
    "Non posso accettare {value} per l'RSI: il range è 5-50. Con questo valore l'RSI non funzionerebbe correttamente. Valore tipico: 14. Inserisci un numero valido."
]

# Varianti per domande ATR periodo
ASK_ATR_PERIOD = [
    "Che periodo vuoi usare per l'ATR? (predefinito: 14)",
    "Dimmi il periodo dell'ATR (default 14).",
    "Per l'ATR serve un numero: che periodo scegli? (14 se vuoi lasciare il default)"
]

# Varianti per errori ATR periodo invalido (range 5-50)
# REGOLA: Mai dire "perfetto" se il valore è sbagliato. Spiegare perché non va bene.
INVALID_ATR_PERIOD = [
    "Il valore {value} non è valido per l'ATR. Deve essere un numero tra 5 e 50. Valore tipico: 14. Inserisci un valore valido.",
    "{value} è fuori dal range consentito per l'ATR (5-50). Questo valore non può essere utilizzato. Valore tipico: 14. Che periodo vuoi usare?",
    "Non posso accettare {value} per l'ATR: il range è 5-50. Valore tipico: 14. Inserisci un numero valido."
]

# Varianti per domande timeframe
ASK_TIMEFRAME = [
    "Quale timeframe?",
    "Che timeframe vuoi usare?",
    "Dimmi il timeframe che preferisci."
]

# Varianti per errori timeframe invalido
# REGOLA: Mai dire "perfetto" se il valore è sbagliato. Dire chiaramente che il timeframe non esiste su Bybit.
INVALID_TIMEFRAME = [
    "Il timeframe '{input}' non esiste su Bybit. Timeframe validi: {allowlist}. Inserisci uno di questi valori.",
    "Non posso usare '{input}': questo timeframe non è supportato da Bybit. Valori accettati: {allowlist}. Quale timeframe vuoi usare?",
    "Timeframe non valido: '{input}' non esiste su Bybit. Devo usare uno tra {allowlist}. Inserisci un valore valido."
]

# Varianti per domande coppia
ASK_SYMBOL = [
    "Che coppia vuoi usare?",
    "Quale coppia USDT inserisci?",
    "Scrivimi la coppia in formato BTCUSDT:"
]

# Varianti per errori coppia invalida (NO esempi inventati)
INVALID_SYMBOL = [
    "Non trovo '{symbol}' su Bybit {market_type}. Ricontrolla che sia scritto identico (es. BTCUSDT) e riprova.",
    "Quel simbolo non risulta disponibile. Scrivimi una coppia USDT esatta, tipo BTCUSDT.",
    "Sembra una coppia inesistente. Inserisci il simbolo corretto in formato COINUSDT (es. ETHUSDT)."
]

# Varianti per domande leva
ASK_LEVERAGE = [
    "Che leva vuoi utilizzare?",
    "Quale leva scegli?",
    "Inserisci la leva:"
]

# Varianti per errori leva invalida
INVALID_LEVERAGE = [
    "Quella leva non è consentita per {symbol}. Inserisci un valore tra {minLev}x e {maxLev}x.",
    "Leva fuori range. Per {symbol} puoi usare {minLev}x–{maxLev}x. Che leva scegli?",
    "Il valore inserito non è valido per {symbol}. La leva deve essere tra {minLev}x e {maxLev}x."
]

# Varianti per transizioni positive (invece di "Perfetto")
POSITIVE_TRANSITIONS = [
    "Ok",
    "Va bene",
    "Dimmi",
    "Scrivimi"
]


def get_phrase(variants: List[str], attempt: int, **kwargs) -> str:
    """
    Seleziona una variante dalla lista usando attempt come indice (modulo).
    
    Args:
        variants: Lista di varianti
        attempt: Numero di tentativo (0-based)
        **kwargs: Parametri per formattare la stringa (es. value, allowlist, etc.)
    
    Returns:
        Stringa formattata con la variante selezionata
    """
    if not variants:
        return ""
    
    idx = attempt % len(variants)
    template = variants[idx]
    
    # Formatta la stringa con i parametri forniti
    try:
        return template.format(**kwargs)
    except KeyError:
        # Se mancano parametri, restituisci la template senza formattazione
        return template


def get_ask_ema_period(attempt: int = 0) -> str:
    """Restituisce una variante della domanda per EMA periodo."""
    return get_phrase(ASK_EMA_PERIOD, attempt)


def get_invalid_ema_period(value: str, attempt: int = 0) -> str:
    """Restituisce una variante del messaggio di errore per EMA periodo invalido."""
    return get_phrase(INVALID_EMA_PERIOD, attempt, value=value)


def get_ask_rsi_period(attempt: int = 0) -> str:
    """Restituisce una variante della domanda per RSI periodo."""
    return get_phrase(ASK_RSI_PERIOD, attempt)


def get_invalid_rsi_period(value: str, attempt: int = 0) -> str:
    """Restituisce una variante del messaggio di errore per RSI periodo invalido."""
    return get_phrase(INVALID_RSI_PERIOD, attempt, value=value)


def get_ask_atr_period(attempt: int = 0) -> str:
    """Restituisce una variante della domanda per ATR periodo."""
    return get_phrase(ASK_ATR_PERIOD, attempt)


def get_invalid_atr_period(value: str, attempt: int = 0) -> str:
    """Restituisce una variante del messaggio di errore per ATR periodo invalido."""
    return get_phrase(INVALID_ATR_PERIOD, attempt, value=value)


def get_ask_timeframe(attempt: int = 0) -> str:
    """Restituisce una variante della domanda per timeframe."""
    return get_phrase(ASK_TIMEFRAME, attempt)


def get_invalid_timeframe(input_value: str, allowlist: str, attempt: int = 0) -> str:
    """Restituisce una variante del messaggio di errore per timeframe invalido."""
    return get_phrase(INVALID_TIMEFRAME, attempt, input=input_value, allowlist=allowlist)


def get_ask_symbol(attempt: int = 0) -> str:
    """Restituisce una variante della domanda per coppia."""
    return get_phrase(ASK_SYMBOL, attempt)


def get_invalid_symbol(symbol: str, market_type: str, attempt: int = 0) -> str:
    """Restituisce una variante del messaggio di errore per coppia invalida."""
    return get_phrase(INVALID_SYMBOL, attempt, symbol=symbol, market_type=market_type)


def get_ask_leverage(attempt: int = 0) -> str:
    """Restituisce una variante della domanda per leva."""
    return get_phrase(ASK_LEVERAGE, attempt)


def get_invalid_leverage(symbol: str, minLev: float, maxLev: float, attempt: int = 0) -> str:
    """Restituisce una variante del messaggio di errore per leva invalida."""
    return get_phrase(INVALID_LEVERAGE, attempt, symbol=symbol, minLev=int(minLev), maxLev=int(maxLev))


def get_positive_transition(attempt: int = 0) -> str:
    """Restituisce una variante per transizioni positive (invece di 'Perfetto')."""
    return get_phrase(POSITIVE_TRANSITIONS, attempt)


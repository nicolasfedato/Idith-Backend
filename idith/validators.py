# -*- coding: utf-8 -*-
"""
validators.py - Validazione rigorosa e centralizzata per Idith
Usa Bybit come source of truth, nessuna interpretazione o autocorrezione.
"""

from __future__ import annotations

import os
import re
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, FrozenSet
from urllib import request, error
from urllib.parse import urlencode
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

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

_leverage_cache: Dict[str, Optional[Tuple[float, float]]] = {}  # symbol -> (min_leverage, max_leverage)

# Whitelist minima se Bybit non risponde e non c'è cache su disco (es. Railway + 403 CloudFront)
_SYMBOL_WHITELIST_USDT: FrozenSet[str] = frozenset(
    {
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "XRPUSDT",
        "ADAUSDT",
        "DOGEUSDT",
        "AVAXUSDT",
        "LINKUSDT",
        "LTCUSDT",
        "TRXUSDT",
    }
)

_INVALID_PAIR_USER_MSG = (
    "Coppia non valida. Inserisci una coppia USDT valida, ad esempio BTCUSDT o SOLUSDT."
)

_SYMBOLS_CACHE_PATH = Path(__file__).resolve().parent / "bybit_symbols_cache.json"


def _try_fetch_bybit_instruments(market_type: str) -> Optional[set]:
    """
    Scarica la lista reale da Bybit mainnet (public REST).
    Ritorna simboli TRADING *USDT, oppure None se la richiesta fallisce (403, timeout, rete, payload invalido).
    """
    if market_type not in ("spot", "futures"):
        return None

    valid_symbols: set[str] = set()
    category = "spot" if market_type == "spot" else "linear"
    cursor = ""

    try:
        while True:
            params: Dict[str, str] = {"category": category, "limit": "1000"}
            if cursor:
                params["cursor"] = cursor
            api_url = (
                "https://api.bybit.com/v5/market/instruments-info?"
                + urlencode(params)
            )
            req = request.Request(
                api_url,
                headers={
                    "User-Agent": "Idith/1.0 (symbol-validation; +https://api.bybit.com)",
                    "Accept": "application/json",
                },
                method="GET",
            )
            with request.urlopen(req, timeout=15) as resp:
                http_status = getattr(resp, "status", None) or resp.getcode()
                payload = resp.read().decode("utf-8")
            response = json.loads(payload)
            ret_code = response.get("retCode")
            ret_msg = response.get("retMsg")
            preview = payload[:300].replace("\n", " ")

            if ret_code not in (0, None, "0"):
                logger.warning(
                    "[BYBIT_SYMBOL_FETCH] unexpected retCode market_type=%s retCode=%s retMsg=%s",
                    market_type,
                    ret_code,
                    ret_msg,
                )
                return None

            result = response.get("result", {})
            if not isinstance(result, dict):
                return None

            instruments = result.get("list", [])
            if not isinstance(instruments, list):
                return None

            page_n = len(instruments)

            for inst in instruments:
                if not isinstance(inst, dict):
                    continue
                sym = inst.get("symbol", "").upper()
                if sym.endswith("USDT") and sym:
                    status = inst.get("status", "").upper()
                    if status == "TRADING":
                        valid_symbols.add(sym)

            logger.info(
                "[BYBIT_SYMBOL_FETCH] market_type=%s url=%s http_status=%s retCode=%s retMsg=%s "
                "response_head_300=%r page_instruments=%s cumulative_trading_usdt=%s",
                market_type,
                api_url,
                http_status,
                ret_code,
                ret_msg,
                preview,
                page_n,
                len(valid_symbols),
            )

            cursor = (result.get("nextPageCursor") or "").strip()
            if not cursor:
                break

        logger.info(
            "[BYBIT_SYMBOL_FETCH] done market_type=%s total_trading_usdt_symbols=%s",
            market_type,
            len(valid_symbols),
        )
        return valid_symbols

    except Exception as e:
        logger.warning(
            "[BYBIT_SYMBOL_FETCH] FAILED market_type=%s err_type=%s err=%r",
            market_type,
            type(e).__name__,
            e,
        )
        if isinstance(e, error.HTTPError):
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                err_body = "<unreadable>"
            logger.warning(
                "[BYBIT_SYMBOL_FETCH] HTTPError code=%s body_head_300=%r",
                e.code,
                err_body,
            )
        return None


def _load_symbols_from_disk_cache(market_type: str) -> Optional[set]:
    """Legge bybit_symbols_cache.json per il mercato richiesto."""
    path = _SYMBOLS_CACHE_PATH
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[BYBIT_SYMBOL_CACHE] read failed path=%s err=%r", path, e)
        return None
    if not isinstance(raw, dict):
        return None
    key = "spot" if market_type == "spot" else "futures"
    lst = raw.get(key)
    if not isinstance(lst, list) or not lst:
        return None
    out: set[str] = set()
    for x in lst:
        if isinstance(x, str):
            s = x.strip().upper()
            if s.endswith("USDT"):
                out.add(s)
    return out or None


def _persist_symbols_to_disk(market_type: str, symbols: set[str]) -> None:
    """Aggiorna JSON locale unendo con l'altro mercato già in file o in memoria."""
    path = _SYMBOLS_CACHE_PATH
    existing_spot: set[str] = set()
    existing_futures: set[str] = set()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for x in raw.get("spot", []) or []:
                    if isinstance(x, str) and x.strip().upper().endswith("USDT"):
                        existing_spot.add(x.strip().upper())
                for x in raw.get("futures", []) or []:
                    if isinstance(x, str) and x.strip().upper().endswith("USDT"):
                        existing_futures.add(x.strip().upper())
        except Exception as e:
            logger.warning("[BYBIT_SYMBOL_CACHE] merge read failed err=%r", e)

    if market_type == "spot":
        merged_spot = set(symbols)
        merged_futures = existing_futures or set(_symbol_cache.get("futures") or ())
    else:
        merged_futures = set(symbols)
        merged_spot = existing_spot or set(_symbol_cache.get("spot") or ())

    payload: Dict[str, Any] = {}
    if merged_spot:
        payload["spot"] = sorted(merged_spot)
    if merged_futures:
        payload["futures"] = sorted(merged_futures)
    if not payload:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            suffix=".json",
            prefix="bybit_symbols_",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.info(
            "[BYBIT_SYMBOL_CACHE] saved path=%s spot=%s futures=%s",
            path,
            len(merged_spot),
            len(merged_futures),
        )
    except Exception as e:
        logger.warning("[BYBIT_SYMBOL_CACHE] save failed path=%s err=%r", path, e)


def fetch_valid_symbols(market_type: str) -> set:
    """
    Recupera i simboli validi da Bybit per il market_type specificato.
    Usa cache per evitare troppe chiamate API.
    Funzione pubblica per accesso esterno.
    """
    return _fetch_valid_symbols_internal(market_type)


def _fetch_valid_symbols_internal(market_type: str) -> set:
    """
    Prova Bybit mainnet pubblico; se fallisce usa cache JSON locale; se assente whitelist minima.
    Non solleva: il wizard non si blocca per 403/timeout se esiste fallback.
    """
    if market_type not in ["spot", "futures"]:
        return set()

    if _symbol_cache[market_type]:
        return _symbol_cache[market_type]

    live = _try_fetch_bybit_instruments(market_type)
    if live:
        _symbol_cache[market_type] = live
        _persist_symbols_to_disk(market_type, live)
        return live

    from_disk = _load_symbols_from_disk_cache(market_type)
    if from_disk:
        _symbol_cache[market_type] = from_disk
        logger.info(
            "[BYBIT_SYMBOL_RESOLVE] using disk cache market_type=%s count=%s",
            market_type,
            len(from_disk),
        )
        return from_disk

    _symbol_cache[market_type] = set(_SYMBOL_WHITELIST_USDT)
    logger.warning(
        "[BYBIT_SYMBOL_RESOLVE] using minimal whitelist market_type=%s count=%s",
        market_type,
        len(_SYMBOL_WHITELIST_USDT),
    )
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
    
    # Recupera simboli validi da Bybit
    try:
        valid_symbols = _fetch_valid_symbols_internal(market_type)
        return symbol_normalized in valid_symbols
    except Exception:
        return False


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

    if symbol_normalized not in valid_symbols:
        return (False, _INVALID_PAIR_USER_MSG)

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


def validate_timeframe(value: str, valid_set: Optional[set[str]] = None) -> Tuple[bool, Optional[str]]:
    """
    Valida che il timeframe sia supportato da Bybit.
    Usa normalize_timeframe per alias (es. "60", "1h", "giornaliero") poi verifica contro allowed.
    Valori non in whitelist (es. "7m", "17m") vengono rifiutati.
    """
    if not value:
        return (False, "Il timeframe non può essere vuoto.")
    valid_tfs = valid_set if valid_set is not None else VALID_TIMEFRAMES
    tf_normalized = normalize_timeframe(value)
    if tf_normalized is None:
        tf_normalized = value.strip().lower()
    if not any(tf_normalized.endswith(unit) for unit in ["m", "h", "d", "w"]):
        examples_str = ", ".join(sorted(valid_tfs, key=lambda x: (int(x[:-1]) if x[:-1].isdigit() else 999, x[-1])))
        return (False, f"Il timeframe '{value}' non è nel formato corretto. Valori supportati: {examples_str}. Inserisci uno di questi valori.")
    if tf_normalized not in valid_tfs:
        examples_str = ", ".join(sorted(valid_tfs, key=lambda x: (int(x[:-1]) if x[:-1].isdigit() else 999, x[-1])))
        return (False, f"Il timeframe '{value}' non esiste su Bybit. Timeframe validi: {examples_str}. Inserisci uno di questi valori esatti.")
    return (True, None)


_LEVERAGE_INVALID_MSG = "Leva non valida. Inserisci un valore tra 1x e 50x."


def _parse_sl_tp_percent(value: Any) -> Optional[float]:
    """Estrae una percentuale come float da input tipo 2, 2%, 2.5, 2,5%."""
    if value is None:
        return None
    s = str(value).strip().replace("%", "").replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_futures_leverage_int(value: Any) -> Optional[int]:
    """Normalizza leva futures: 10, 10x, 5x → intero; None se non valido."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower().endswith("x"):
        s = s[:-1].strip()
    s = s.replace(",", ".")
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    if f != int(f):
        return None
    return int(f)


def validate_stop_loss(value: Any) -> Tuple[bool, Optional[str]]:
    val = _parse_sl_tp_percent(value)
    if val is None:
        return (False, "Stop loss non valido. Inserisci una percentuale valida.")
    if val <= 0:
        return (False, "Lo stop loss deve essere maggiore di 0%.")
    if val > 100:
        return (False, "Lo stop loss deve essere minore o uguale a 100%.")
    return (True, None)


def validate_take_profit(value: Any) -> Tuple[bool, Optional[str]]:
    val = _parse_sl_tp_percent(value)
    if val is None:
        return (False, "Take profit non valido. Inserisci una percentuale valida.")
    if val <= 0:
        return (False, "Il take profit deve essere maggiore di 0%.")
    if val > 100:
        return (False, "Il take profit deve essere minore o uguale a 100%.")
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


def _validate_leverage_min_max(lev: float, minLev: float, maxLev: float) -> Tuple[bool, Optional[str]]:
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
    - Range consentito globalmente: 1–50x
    - Verifica aggiuntiva rispetto ai limiti min/max del simbolo
    """
    try:
        leverage_float = float(lev)
    except (ValueError, TypeError):
        return (False, _LEVERAGE_INVALID_MSG)
    
    if leverage_float != int(leverage_float):
        return (False, _LEVERAGE_INVALID_MSG)
    
    leverage_int = int(leverage_float)
    
    if leverage_int <= 0 or leverage_int > 50:
        return (False, _LEVERAGE_INVALID_MSG)
    
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


def validate_leverage(*args: Any) -> Tuple[bool, Optional[str]]:
    """
    - Legacy: validate_leverage(lev, minLev, maxLev) — tre argomenti numerici.
    - Wizard/orchestrator: validate_leverage(value, market_type, min_lev, max_lev) con market_type 'futures'.
    - validate_leverage(value, market_type) oppure validate_leverage(value): controllo generico 1–50x (futures).
    """
    if len(args) == 3:
        lev_int = _parse_futures_leverage_int(args[0])
        if lev_int is None or lev_int <= 0 or lev_int > 50:
            return (False, _LEVERAGE_INVALID_MSG)
        return _validate_leverage_min_max(float(lev_int), float(args[1]), float(args[2]))
    if len(args) == 4:
        value, market_type, min_lev, max_lev = args[0], args[1], args[2], args[3]
        if market_type != "futures":
            return (
                False,
                "La leva non è disponibile per il trading spot. La leva è disponibile solo per futures.",
            )
        lev_int = _parse_futures_leverage_int(value)
        if lev_int is None or lev_int <= 0 or lev_int > 50:
            return (False, _LEVERAGE_INVALID_MSG)
        return _validate_leverage_min_max(float(lev_int), float(min_lev), float(max_lev))
    if len(args) == 2:
        value, market_type = args[0], args[1]
        if market_type == "spot":
            return (True, None)
        lev_int = _parse_futures_leverage_int(value)
        if lev_int is None or lev_int <= 0 or lev_int > 50:
            return (False, _LEVERAGE_INVALID_MSG)
        return _validate_leverage_min_max(float(lev_int), 1.0, 50.0)
    if len(args) == 1:
        lev_int = _parse_futures_leverage_int(args[0])
        if lev_int is None or lev_int <= 0 or lev_int > 50:
            return (False, _LEVERAGE_INVALID_MSG)
        return _validate_leverage_min_max(float(lev_int), 1.0, 50.0)
    return (False, "Argomenti non validi per la validazione della leva.")


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
    global _symbol_cache, _leverage_cache
    _symbol_cache = {
        "spot": set(),
        "futures": set(),
    }
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
    is_valid, error_msg = _validate_leverage_min_max(leverage, minLev, maxLev)
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


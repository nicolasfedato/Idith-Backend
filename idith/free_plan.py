"""
Modulo per gestire i preset di strategia del piano FREE.

Questo modulo contiene:
- Definizione dei 4 preset di strategia disponibili (ID numerici 1-4 come source of truth)
- Funzioni pure per gestire i preset
- Logica per applicare preset ai parametri
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple, Union, Callable


# Ordine obbligatorio del wizard piano FREE (source of truth per l'orchestrator).
FREE_WIZARD_SEQUENCE: Tuple[str, ...] = (
    "market_type",
    "symbol",
    "timeframe",
    "operating_mode",
    "sl",
    "tp",
    "risk_pct",
    "leverage",
)


def first_missing_free_wizard_field(
    params: Dict[str, Any],
    is_step_filled: Callable[[str, Dict[str, Any]], bool],
    cs: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Primo campo della sequenza FREE non ancora risolto.
    `is_step_filled` è fornito dall'orchestrator (validazione stretta + skip leva per spot).
    `cs` opzionale: se presente, pending_risk / pending_leverage / pending_sl bloccano lo step corrispondente.
    """
    cs = cs or {}
    for step in FREE_WIZARD_SEQUENCE:
        if step == "leverage" and params.get("market_type") == "spot":
            continue
        if step == "risk_pct" and cs.get("pending_risk_confirmation") is not None:
            return "risk_pct"
        if (
            step == "leverage"
            and params.get("market_type") == "futures"
            and cs.get("pending_leverage_confirmation") is not None
        ):
            return "leverage"
        if step == "sl" and cs.get("pending_sl_confirmation") is not None:
            return "sl"
        if not is_step_filled(step, params):
            return step
    return None


@dataclass
class StrategyPreset:
    """Preset di strategia per il piano FREE."""
    id: int  # 1, 2, 3, o 4 (source of truth)
    name: str  # Nome breve per display
    description: str  # Descrizione completa
    required_period_fields: List[str]  # Lista di chiavi tra ["ema_period", "rsi_period", "atr_period"]
    default_periods: Dict[str, int]  # Dict es {"rsi_period": 14, "atr_period": 14}


# Preset legacy (ID 1–4): allineati a strategy_id FREE v2 (runner/orchestrator).
# ID 4 resta alias di 3 per config salvate prima della riallineazione.
FREE_STRATEGY_PRESETS: Dict[int, StrategyPreset] = {
    1: StrategyPreset(
        id=1,
        name="RSI only",
        description="Solo RSI (modalità aggressiva FREE)",
        required_period_fields=["rsi_period"],
        default_periods={"rsi_period": 5},
    ),
    2: StrategyPreset(
        id=2,
        name="EMA + RSI",
        description="Trend + timing RSI (modalità equilibrata FREE)",
        required_period_fields=["ema_period", "rsi_period"],
        default_periods={"ema_period": 9, "rsi_period": 7},
    ),
    3: StrategyPreset(
        id=3,
        name="EMA + RSI + ATR",
        description="Trend + RSI + volatilità minima (modalità selettiva FREE)",
        required_period_fields=["ema_period", "rsi_period", "atr_period"],
        default_periods={"ema_period": 21, "rsi_period": 14, "atr_period": 14},
    ),
    4: StrategyPreset(
        id=4,
        name="EMA + RSI + ATR (legacy)",
        description="Stesso schema del preset 3 (compatibilità salvataggi vecchi)",
        required_period_fields=["ema_period", "rsi_period", "atr_period"],
        default_periods={"ema_period": 21, "rsi_period": 14, "atr_period": 14},
    ),
}


def list_free_strategies() -> List[StrategyPreset]:
    """
    Restituisce la lista di tutti i preset disponibili.
    
    Returns:
        Lista di StrategyPreset
    """
    return list(FREE_STRATEGY_PRESETS.values())


def get_free_preset(strategy_id: Union[int, str]) -> Optional[StrategyPreset]:
    """
    Restituisce un preset per ID numerico (1-4).
    
    Args:
        strategy_id: ID numerico del preset (1, 2, 3, o 4) o stringa "1", "2", "3", "4"
        
    Returns:
        StrategyPreset corrispondente o None se non trovato
    """
    # Normalizza a int
    if isinstance(strategy_id, str):
        try:
            strategy_id = int(strategy_id)
        except ValueError:
            return None
    
    if not isinstance(strategy_id, int) or strategy_id not in FREE_STRATEGY_PRESETS:
        return None
    
    return FREE_STRATEGY_PRESETS.get(strategy_id)


def apply_free_strategy_to_params(params: Dict[str, Any], strategy_id: Union[int, str]) -> Dict[str, Any]:
    """
    Applica una strategia ai parametri.
    
    IMPORTANTE: 
    - Imposta params["free_strategy_id"] come source of truth (1-4)
    - Per i periodi NON inclusi nella strategia: li imposta a None (rimuove indicatori non previsti)
    - NON imposta default per i periodi richiesti: devono essere chiesti all'utente
    - params["strategy"] può essere derivato ma NON è source of truth
    
    Args:
        params: Dict dei parametri da modificare
        strategy_id: ID numerico della strategia (1, 2, 3, o 4) o stringa "1", "2", "3", "4"
        
    Returns:
        Dict params modificato (modifica in-place, ma ritorna per comodità)
    """
    preset = get_free_preset(strategy_id)
    if preset is None:
        return params
    
    # Imposta free_strategy_id come source of truth (sempre int)
    params["free_strategy_id"] = preset.id
    
    # Tutti i possibili periodi
    all_period_fields = ["ema_period", "rsi_period", "atr_period"]
    
    # Per ogni periodo:
    for period_field in all_period_fields:
        if period_field not in preset.required_period_fields:
            # Periodo NON richiesto: imposta a None (rimuove indicatore)
            params[period_field] = None
    
    # NOTA: NON impostiamo i default per i periodi richiesti qui
    # Devono essere chiesti all'utente nello step "strategy_params"
    
    return params


def next_missing_period_question(params: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """
    Restituisce la prossima domanda da fare per un periodo mancante.
    
    Ordine coerente: EMA -> ATR -> RSI (stesso ordine usato nel sistema esistente)
    
    Args:
        params: Dict dei parametri correnti (deve contenere free_strategy_id)
    
    Returns:
        Tuple (field_name, question_text) se manca un periodo, None altrimenti
        Es: ("rsi_period", "Ok. Che periodo vuoi per RSI? (es: 14)")
    """
    strategy_id = params.get("free_strategy_id")
    if strategy_id is None:
        return None
    
    preset = get_free_preset(strategy_id)
    if preset is None:
        return None
    
    # Ordine fisso: EMA -> ATR -> RSI (coerente con il sistema esistente)
    order = {"ema_period": 0, "atr_period": 1, "rsi_period": 2}
    
    # Trova il primo periodo richiesto che è None, rispettando l'ordine
    required_sorted = sorted(
        preset.required_period_fields,
        key=lambda x: order.get(x, 999)
    )
    
    for period_field in required_sorted:
        if params.get(period_field) is None:
            # Costruisci la domanda
            indicator_name = period_field.replace("_period", "").upper()
            default_value = preset.default_periods.get(period_field, 14)
            question = f"Ok. Che periodo vuoi per {indicator_name}? (es: {default_value})"
            return (period_field, question)
    
    # Tutti i periodi richiesti sono presenti
    return None


def get_strategy_menu_text() -> str:
    """
    Restituisce il testo del menu strategie per il piano FREE.
    
    Returns:
        Stringa con il menu delle strategie disponibili (sempre le 4 strategie)
    """
    return (
        "Quale strategia vuoi utilizzare?\n\n"
        "1) RSI + ATR — Momentum + Volatilità\n"
        "2) EMA + RSI — Trend + Momentum\n"
        "3) EMA + ATR — Trend + Volatilità\n"
        "4) EMA + RSI + ATR — Completa\n\n"
        "Scegli 1, 2, 3 o 4."
    )


def get_strategy_name(strategy_id: Union[int, str]) -> Optional[str]:
    """
    Restituisce il nome della strategia per display.
    
    Args:
        strategy_id: ID numerico della strategia (1, 2, 3, o 4) o stringa "1", "2", "3", "4"
        
    Returns:
        Nome della strategia o None se non trovato
    """
    preset = get_free_preset(strategy_id)
    if preset is None:
        return None
    return preset.name


def get_strategy_description(strategy_id: Union[int, str]) -> Optional[str]:
    """
    Restituisce la descrizione della strategia per display.
    
    Args:
        strategy_id: ID numerico della strategia (1, 2, 3, o 4) o stringa "1", "2", "3", "4"
        
    Returns:
        Descrizione della strategia o None se non trovato
    """
    preset = get_free_preset(strategy_id)
    if preset is None:
        return None
    return preset.description

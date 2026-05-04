from __future__ import annotations

import copy
import os
import re
import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple
from . import validators
from . import phrases
from . import free_plan
from idith.core.config_engine_v2 import process_message_v2
try:
    # Preset di strategia ad alto livello per il piano FREE
    from .plans import free_strategies  # type: ignore
except Exception:
    free_strategies = None

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# DEBUG LOGGING
# ------------------------------------------------------------
DEBUG_ORCH = os.getenv("IDITH_DEBUG_ORCH", "1") == "1"

def _dlog(msg: str):
    if DEBUG_ORCH:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[ORCH][{ts}] {msg}")


# ------------------------------------------------------------
# State-machine Orchestrator for Idith - SEQUENZIALE
# ------------------------------------------------------------

# Sequenza FREE: source of truth in free_plan.FREE_WIZARD_SEQUENCE
STEPS = list(free_plan.FREE_WIZARD_SEQUENCE)

# DEFAULT_PARAMS: scheletro unico con tutte le chiavi necessarie
# ATTENZIONE: non rimuovere mai chiavi esistenti dallo state; usa questo solo
# per inizializzare nuove chat o riempire chiavi mancanti.
DEFAULT_PARAMS = {
    "market_type": None,
    "symbol": None,
    "timeframe": None,
    # Campo legacy: sempre presente ma nel piano FREE non viene usato.
    # Manteniamo una lista vuota per compatibilità.
    "strategy": [],
    "leverage": None,
    "risk_pct": None,
    "sl": None,
    "tp": None,
    "operating_mode": None,
    "strategy_id": None,
    "strategy_params": None,
}

# Snapshot DB dopo "reset configurazione" (__force_full_reset in save_chat_state).
# Ordine chiavi params allineato al contratto reset globale.
FORCE_FULL_RESET_CONFIG_STATE_SNAPSHOT: Dict[str, Any] = {
    "step": "market_type",
    "params": {
        "sl": None,
        "tp": None,
        "symbol": None,
        "leverage": None,
        "risk_pct": None,
        "strategy": [],
        "timeframe": None,
        "market_type": None,
        "strategy_id": None,
        "operating_mode": None,
        "strategy_params": None,
    },
    "error_count": {},
    "suggested_sl": None,
    "last_greeting_variant": None,
    "pending_sl_confirmation": None,
    "pending_risk_confirmation": None,
    "pending_leverage_confirmation": None,
}

# Soglie per warning + conferma obbligatoria (BUG3)
HIGH_LEVERAGE_WARNING_THRESHOLD = 4
HIGH_RISK_PCT_WARNING_THRESHOLD = 4


def _extract_leverage_int_from_text(user_text: str) -> Optional[int]:
    """
    Estrae la leva come intero da testo libero.
    Formati tipici: '2x', '2', '2 X', 'x10', 'leva 5', 'leverage 3'.
    """
    text = (user_text or "").strip()
    if not text:
        return None
    lt = text.lower()
    candidate_num_str: Optional[str] = None
    num_pattern = r"\d+(?:\.\d+)?"
    m = re.search(rf"(?:leva|leverag|lev)\s*[:=]?\s*(?:x\s*({num_pattern})|({num_pattern})\s*x?)", lt)
    if m:
        candidate_num_str = m.group(1) or m.group(2)
    if not candidate_num_str:
        m = re.search(rf"x\s*({num_pattern})", lt)
        if m:
            candidate_num_str = m.group(1)
    if not candidate_num_str:
        m = re.search(rf"({num_pattern})\s*x\b", lt)
        if m:
            candidate_num_str = m.group(1)
    if not candidate_num_str:
        all_nums = re.findall(num_pattern, lt)
        if all_nums:
            candidate_num_str = all_nums[-1]
    if not candidate_num_str:
        return None
    try:
        return int(float(candidate_num_str))
    except Exception:
        return None


def _parse_user_leverage_int(raw: Any) -> Optional[int]:
    """Normalizza la leva da valore già isolato (int / float / stringa)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    return _extract_leverage_int_from_text(str(raw))


# Modalità operative supportate nel piano FREE
OPERATING_MODE_CANONICAL = ["aggressiva", "equilibrata", "selettiva"]

# Mappatura operating_mode -> id blocco descrittivo (solo riferimento / coerenza nomi)
OPERATING_MODE_TO_BLOCK_ID = {
    "aggressiva": "rsi_only_free",
    "equilibrata": "ema_rsi_free",
    "selettiva": "ema_rsi_atr_free",
}

# Preset di fallback locali per operating_mode → (strategy_id, strategy_params)
# Usati SOLO se idith.plans.free_strategies non è disponibile o non restituisce un preset.
OPERATING_MODE_FALLBACK_PRESETS = {
    "aggressiva": (
        "1",
        {"rsi_buy": 45, "rsi_sell": 55, "rsi_period": 5},
    ),
    "equilibrata": (
        "2",
        {"rsi_buy": 45, "rsi_sell": 55, "rsi_period": 7, "ema_period": 7},
    ),
    "selettiva": (
        "3",
        {
            "rsi_buy": 45,
            "rsi_sell": 55,
            "rsi_period": 7,
            "ema_period": 10,
            "atr_period": 7,
            "atr_min_threshold": 0.05,
        },
    ),
}


def _parse_operating_mode(user_text: str) -> Optional[str]:
    """
    Riconosce la modalità operativa dall'input utente.

    Regola: operating_mode viene aggiornato SOLO su match testuale esplicito:
    - "aggressiva", "aggressivo", "aggressive"
    - "equilibrata", "equilibrato", "balanced"
    - "selettiva", "selettivo", "selective"
    Non usa mai deduzioni numeriche/percentuali (es. 1/2/3, sl/tp/risk).
    """
    t = (user_text or "").strip().lower()
    if not t:
        logger.info("[OPERATING_MODE_EXTRACT] explicit=%s text=%r", None, user_text)
        return None

    explicit_mode: Optional[str] = None
    if re.search(r"\b(aggressiva|aggressivo|aggressive)\b", t, re.I):
        explicit_mode = "aggressiva"
    elif re.search(r"\b(equilibrata|equilibrato|balanced)\b", t, re.I):
        explicit_mode = "equilibrata"
    elif re.search(r"\b(selettiva|selettivo|selective)\b", t, re.I):
        explicit_mode = "selettiva"

    logger.info("[OPERATING_MODE_EXTRACT] explicit=%s text=%r", explicit_mode, user_text)
    return explicit_mode


def _apply_operating_mode_preset(params: Dict[str, Any], operating_mode: str) -> Dict[str, Any]:
    """
    Applica il preset di strategia per la modalità operativa scelta (FREE v2).

    Imposta SEMPRE:
    - params["operating_mode"] = operating_mode canonico (lowercase)
    - params["strategy_id"] = id stringa coerente con la modalità
    - params["strategy_params"] = copia profonda del preset (solo chiavi della nuova modalità, nessun merge)

    Ordine di priorità:
    1. Usa idith.plans.free_strategies.get_preset_by_operating_mode(mode) se disponibile.
    2. In caso di errore o preset mancante, usa OPERATING_MODE_FALLBACK_PRESETS locale.
    """
    if not isinstance(params, dict):
        params = {}

    # Normalizza e valida la modalità
    mode = (operating_mode or "").strip().lower()
    if mode not in OPERATING_MODE_CANONICAL:
        return params

    prev_mode = params.get("operating_mode")
    prev_strategy_id = params.get("strategy_id")
    prev_strategy_params = copy.deepcopy(params.get("strategy_params"))

    strategy_id: Optional[str] = None
    strategy_params: Dict[str, Any] = {}

    # 1) Tentativo con preset da free_strategies (se disponibile)
    try:
        if free_strategies is not None and hasattr(free_strategies, "get_preset_by_operating_mode"):
            preset = free_strategies.get_preset_by_operating_mode(mode)  # type: ignore[attr-defined]
            if preset:
                sid, sparams = preset
                if sid is not None:
                    strategy_id = str(sid)
                if isinstance(sparams, dict):
                    strategy_params = dict(sparams)
    except Exception:
        logger.exception("[OPERATING_MODE] Impossibile leggere i preset per operating_mode=%s", mode)

    # 2) Fallback locale se non abbiamo ottenuto un preset valido
    if strategy_id is None or not strategy_params:
        fallback = OPERATING_MODE_FALLBACK_PRESETS.get(mode)
        if fallback is not None:
            fid, fparams = fallback
            strategy_id = str(fid) if fid is not None else None
            strategy_params = dict(fparams) if isinstance(fparams, dict) else {}

    # Aggiorna params ad alto livello (strategy_params: solo preset, mai merge con dict precedente)
    new_sp = copy.deepcopy(strategy_params) if strategy_params else {}
    params["operating_mode"] = mode
    params["strategy_id"] = strategy_id
    params["strategy_params"] = new_sp

    # Legacy: assicura che il campo strategy esista sempre come lista (non usato nel FREE)
    if not isinstance(params.get("strategy"), list):
        params["strategy"] = []

    logger.info(
        "[OPERATING_MODE_PRESET] previous_operating_mode=%s new_operating_mode=%s "
        "previous_strategy_id=%s new_strategy_id=%s "
        "previous_strategy_params=%s new_strategy_params_rebuilt_from_preset=%s",
        prev_mode,
        mode,
        prev_strategy_id,
        strategy_id,
        prev_strategy_params,
        new_sp,
    )

    return params


def derive_strategy(params: Dict[str, Any]) -> List[str]:
    """
    Helper legacy: restituisce il contenuto di params["strategy"] se è una lista.
    Nel piano FREE v2 questo campo è mantenuto solo per retro‑compatibilità.
    """
    s = params.get("strategy")
    return list(s) if isinstance(s, list) else []


def derive_strategy_label(params: Dict[str, Any]) -> Optional[str]:
    """
    Legacy stub: nel piano FREE v2 non esiste più una tassonomia di strategie
    basata su combinazioni di indicatori, quindi non viene restituita alcuna label.
    """
    return None


def is_valid_strategy_combination(params: Dict[str, Any]) -> bool:
    """
    Legacy stub: nel piano FREE v2 non vengono più validate combinazioni di indicatori.
    Restituisce sempre True per non bloccare eventuali stati legacy.
    """
    return True


def _parse_strategy_choice(user_text: str) -> Optional[int]:
    """
    FREE v2: la scelta strategia legacy (1‑4, combinazioni indicatori) è disattivata.
    Ritorna sempre None per non attivare mai il flusso di cambio strategia legacy.
    """
    return None


def detect_strategy_change(user_text: str) -> Optional[Dict[str, Any]]:
    """
    FREE v2: il cambio strategia legacy (choice/toggle) è disattivato.
    Ritorna sempre None per non attivare mai il flusso legacy basato su indicatori/ATR.
    """
    return None


def _field_to_indicator(field: str) -> str:
    """Mappa ema_period -> EMA, rsi_period -> RSI, atr_period -> ATR."""
    if field == "ema_period":
        return "EMA"
    if field == "rsi_period":
        return "RSI"
    if field == "atr_period":
        return "ATR"
    return ""


def apply_strategy_change(params: Dict[str, Any], change: Dict[str, Any]) -> Tuple[bool, Any, Optional[set]]:
    """
    FREE v2: il cambio strategia legacy non è più supportato.
    Questa funzione esiste solo per compatibilità e ritorna sempre un errore non‑bloccante.
    """
    return (False, "Il cambio strategia manuale non è disponibile nel piano FREE v2.", None)


def _infer_required_indicators(params: Dict[str, Any]) -> Optional[set]:
    """
    Legacy stub: nel piano FREE v2 non viene più inferita alcuna combinazione
    di indicatori a partire da periodi. Restituisce sempre None.
    """
    return None


def first_missing_required_period(
    params: Dict[str, Any],
    required_indicators: Optional[set] = None
) -> Optional[Tuple[str, str]]:
    """
    Legacy stub: nel piano FREE v2 non vengono mai richiesti periodi di indicatori.
    Ritorna sempre None per disattivare il flusso legacy basato sui periodi.
    """
    return None


def _coerce_params(p: Any) -> Dict[str, Any]:
    """
    Helper per assicurare che params sia sempre un dict valido con tutte le chiavi.
    - Se p non è dict -> return deepcopy di DEFAULT_PARAMS
    - Se p è dict -> aggiungi chiavi mancanti da DEFAULT_PARAMS usando il valore di default
    - Non rimuove mai chiavi esistenti né cambia il tipo di campi extra
    """
    if not isinstance(p, dict):
        return copy.deepcopy(DEFAULT_PARAMS)
    
    # Crea una copia per non modificare l'originale
    result = p.copy()
    
    # Aggiungi chiavi mancanti da DEFAULT_PARAMS con il valore di default
    for key, default_value in DEFAULT_PARAMS.items():
        if key not in result:
            # Copia profonda solo per tipi mutabili (dict/list), altrimenti assegna il valore direttamente
            if isinstance(default_value, (dict, list)):
                result[key] = copy.deepcopy(default_value)
            else:
                result[key] = default_value
    
    return result


def deep_merge_config(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge deterministica: existing come base, incoming sovrascrive solo dove ha valore non-None.
    - Per ogni chiave in incoming:
      - se incoming[key] è dict e existing[key] è dict: deep merge ricorsivo
      - se incoming[key] è None: NON sovrascrivere (trattalo come "non aggiornare")
      - altrimenti (stringa/numero/bool/list/dict non-None): SOVRASCRIVI sempre existing[key]
    - Chiavi non presenti in incoming: lascia existing invariato.
    Usata nel percorso di salvataggio config_state su Supabase per evitare di aggiornare
    solo quando il valore esistente è null.
    """
    if not isinstance(existing, dict):
        existing = {}
    if not isinstance(incoming, dict):
        return copy.deepcopy(existing)
    result = copy.deepcopy(existing)
    for key, inval in incoming.items():
        if inval is None:
            continue
        if isinstance(inval, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_config(result[key], inval)
        else:
            result[key] = copy.deepcopy(inval) if isinstance(inval, dict) else inval
    return result


def _apply_strategy_to_params(
    params: Dict[str, Any],
    strategy_list: List[str],
    free_strategy_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Centralizza l'aggiornamento di strategy/free_strategy_id e dei periodi indicatori.
    
    BUGFIX: in precedenza venivano aggiornati dict di lavoro/snapshot basati sui periodi
    senza riallineare sempre free_strategy_id e params["strategy"], causando desincronizzazioni
    tra ciò che veniva mostrato in chat e ciò che veniva salvato in Supabase.
    
    Non tocca altri parametri (symbol, timeframe, leverage, risk, sl, tp).
    """
    params = _coerce_params(params)
    # Normalizza solo la lista passata esplicitamente: strategy è sempre un campo derivato.
    normalized_strategy = _normalize_strategy_list(strategy_list or [])
    
    # Se non viene passato free_strategy_id, derivarlo dalla combinazione indicatori
    if free_strategy_id is None:
        free_strategy_id = _strategy_list_to_free_strategy_id(normalized_strategy)
    
    # Aggiorna sempre la lista strategy normalizzata (anche se vuota, per coerenza)
    params["strategy"] = normalized_strategy
    
    # Aggiorna free_strategy_id solo come memo UI opzionale
    if free_strategy_id is not None:
        params["free_strategy_id"] = free_strategy_id
    
    # IMPORTANTE: i periodi (ema_period, rsi_period, atr_period) restano l'unica fonte di verità
    # e NON vengono modificati da questa funzione.
    return params


def _strategy_list_to_free_strategy_id(strategy_list: List[str]) -> Optional[int]:
    """
    Legacy stub: nel piano FREE v2 non esiste più una mappatura rigida tra
    combinazioni di indicatori e ID numerici di strategia.
    Manteniamo la firma per retro‑compatibilità, ma restituiamo sempre None.
    """
    return None

def _ensure_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """Inizializza lo state con la struttura corretta compatibile con app.py."""
    # app.py si aspetta: { "config_status": "...", "config_state": {...}, "active_bot_id": "..." }
    # La nuova struttura interna è: config_state = { "step": "...", "params": {...} }
    
    # Normalizza config_status: "new" o assente -> "in_progress"
    current_status = state.get("config_status")
    if current_status is None or current_status == "new":
        if current_status == "new":
            chat_id = state.get("chat_id") or state.get("session_id")
            if chat_id:
                logger.info(f"[_ensure_state] Converting config_status 'new' -> 'in_progress' for chat_id={chat_id}")
            else:
                logger.info(f"[_ensure_state] Converting config_status 'new' -> 'in_progress'")
        state["config_status"] = "in_progress"
    # Se già "in_progress", "complete" o "ready", non toccare
    
    # NORMALIZZAZIONE OBBLIGATORIA: Se config_state è una lista con un solo elemento, trasformala in dict
    cs = state.get("config_state")
    if isinstance(cs, list) and len(cs) == 1 and isinstance(cs[0], dict):
        cs = cs[0]
    # Se cs è None dopo normalizzazione, crea sempre la struttura default per nuove chat
    if cs is None:
        # Se esiste vecchia struttura con step/params a livello root, migra
        if "step" in state and "params" in state:
            cs = {
                "step": state.get("step", "market_type"),
                "params": state.get("params", {}),
            }
        else:
            # Nuova struttura default (FREE: market_type primo step)
            cs = {
                "step": "market_type",
                "params": copy.deepcopy(DEFAULT_PARAMS),
                "error_count": {},
            }
    # Assegna config_state normalizzato - questa è l'unica assegnazione, non verrà sovrascritta da default/init
    # IMPORTANTE: dopo questa assegnazione, config_state non deve essere resettato da default/init
    if not isinstance(cs, dict):
        # Fallback: se dopo normalizzazione non è un dict valido, crea default
        cs = {"step": "market_type", "params": copy.deepcopy(DEFAULT_PARAMS), "error_count": {}}
    state["config_state"] = cs
    
    cs = state["config_state"]
    
    # Assicura che cs["params"] sia sempre un dict valido con tutte le chiavi
    cs["params"] = _coerce_params(cs.get("params"))
    
    # Migra vecchi nomi se presenti (preserva periodi per evitare overwrite)
    old_params = cs.get("params", {})
    if "pair" in old_params or "mode" in old_params:
        param_mapping = {
            "pair": "symbol",
            "mode": "market_type"
        }
        new_params = {}
        for key in ["symbol", "market_type", "strategy", "timeframe", "risk_pct", "leverage", "sl", "tp"]:
            old_key = param_mapping.get(key, key)
            new_params[key] = old_params.get(old_key) if old_key in old_params else None
        # Preserva periodi esistenti: new vince su old solo per chiavi migrate, non sovrascrivere periodi
        for key in ["ema_period", "rsi_period", "atr_period"]:
            if key in old_params:
                new_params[key] = old_params[key]
        cs["params"] = new_params
        old_params = new_params
    
    # Migra vecchio step se presente
    old_step = cs.get("step", "market_type")
    if old_step == "pair":
        cs["step"] = "symbol"
    elif old_step == "mode":
        cs["step"] = "market_type"
    
    # Assicura che tutti i parametri esistano (inclusi operating_mode, strategy_id, strategy_params per FREE)
    params = cs.setdefault("params", {})
    for key in list(DEFAULT_PARAMS.keys()):
        if key not in params:
            params[key] = None
    
    # Se market_type=spot, forza leverage=null e azzera pending_leverage_confirmation (BUG 1)
    if params.get("market_type") == "spot":
        params["leverage"] = None
        cs["pending_leverage_confirmation"] = None
    
    # Inizializza pending_risk_confirmation se manca
    if "pending_risk_confirmation" not in cs:
        cs["pending_risk_confirmation"] = None
    
    # Inizializza pending_sl_confirmation e suggested_sl se mancano (per gating stop loss)
    if "pending_sl_confirmation" not in cs:
        cs["pending_sl_confirmation"] = None
    if "suggested_sl" not in cs:
        cs["suggested_sl"] = None
    # BUG3: Inizializza pending_leverage_confirmation per leva alta
    if "pending_leverage_confirmation" not in cs:
        cs["pending_leverage_confirmation"] = None
    
    # Coerenza: pending_* che duplica già params (merge DB / stato incoerente) → azzera pending
    pr = cs.get("pending_risk_confirmation")
    rp = params.get("risk_pct")
    if pr is not None and rp is not None:
        try:
            prf = float(pr)
            rpf = float(str(rp).strip().rstrip("%").replace(",", "."))
            if abs(prf - rpf) < 1e-3:
                cs["pending_risk_confirmation"] = None
        except (TypeError, ValueError):
            pass
    pl = cs.get("pending_leverage_confirmation")
    lev = params.get("leverage")
    if pl is not None and lev is not None:
        try:
            if int(pl) == int(float(lev)):
                cs["pending_leverage_confirmation"] = None
        except (TypeError, ValueError):
            pass
    
    # Inizializza error_count per tracciare errori per step (per messaggi variati)
    if "error_count" not in cs:
        cs["error_count"] = {}  # {step: count}
    
    # Inizializza last_greeting_variant per anti-ripetizione saluti
    if "last_greeting_variant" not in cs:
        cs["last_greeting_variant"] = None  # 0, 1, o 2
    
    # Migrazione: se free_strategy_id è presente in params, assicurati che sia persistito
    # (non serve più _preset_id, usiamo direttamente free_strategy_id in params)
    # Rimuoviamo _preset_id se presente (cleanup legacy)
    cs.pop("_preset_id", None)
    
    # Step corrente = primo campo mancante nella sequenza free_plan.FREE_WIZARD_SEQUENCE
    missing = free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs)
    if missing is not None:
        cs["step"] = missing
    elif is_config_complete(params):
        cs["step"] = None
    else:
        cs["step"] = _free_wizard_terminal_step(params)
    state["config_state"] = cs

    # Coerenza: mai restare "complete" se i params non soddisfano is_config_complete (es. reset DB incompleto).
    if state.get("config_status") == "complete" and not is_config_complete(params):
        logger.info(
            "[_ensure_state] Downgrading config_status complete -> in_progress (params incomplete vs is_config_complete)"
        )
        state["config_status"] = "in_progress"

    return state


def _free_wizard_terminal_step(params: Dict[str, Any]) -> str:
    """Step memorizzato quando il wizard FREE non ha campi mancanti. Spot non usa mai leverage."""
    if params.get("market_type") == "spot":
        return "risk_pct"
    return STEPS[-1]


def _recompute_step(cs: Dict[str, Any]) -> None:
    """
    Ricalcola lo step in base al primo parametro mancante nella sequenza STEPS.
    Usato dopo cambio market_type per evitare step incoerenti (es. leverage con spot).
    """
    params = cs.get("params", {})
    if not isinstance(params, dict):
        return
    missing = free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs)
    if missing is not None:
        cs["step"] = missing
    else:
        cs["step"] = _free_wizard_terminal_step(params)


def format_percent(value: Any) -> str:
    return f"{float(str(value).replace('%', '').replace(',', '.')):.1f}%"


def _sync_state(state: Dict[str, Any], cs: Dict[str, Any], params: Dict[str, Any]) -> tuple:
    """Sincronizza params in cs e cs in state. Unico punto per aggiornare cs["params"] e state["config_state"]."""
    # Assicura che params sia sempre un dict valido con tutte le chiavi
    params = _coerce_params(params)
    for key in ("sl", "tp"):
        if params.get(key) is not None:
            try:
                params[key] = format_percent(params[key])
            except (TypeError, ValueError):
                pass
    cs["params"] = params
    state["config_state"] = cs
    return state, cs, params


def _cleanup_config_state_when_complete(cs: Dict[str, Any]) -> None:
    """Pulizia pending conferme e error_count quando la config è marchiata complete (stato persistito coerente)."""
    if not isinstance(cs, dict):
        return
    cs["pending_risk_confirmation"] = None
    cs["pending_leverage_confirmation"] = None
    cs["pending_sl_confirmation"] = None
    cs["error_count"] = {}
    p = cs.get("params")
    if isinstance(p, dict):
        cs["step"] = _free_wizard_terminal_step(p)


def _is_step_filled(step: str, params: Dict[str, Any]) -> bool:
    """Verifica se lo step è completato."""
    if step == "operating_mode":
        return params.get("operating_mode") in OPERATING_MODE_CANONICAL
    elif step == "symbol":
        return params.get("symbol") is not None and params.get("symbol") != ""
    elif step == "market_type":
        return params.get("market_type") in ["spot", "futures"]
    elif step == "strategy":
        # FREE: strategy è filled quando operating_mode è scelto (parametri preset)
        if params.get("operating_mode") in OPERATING_MODE_CANONICAL:
            return True
        # Legacy: combinazione indicatori (periodi non-null) deve essere permessa
        return is_valid_strategy_combination(params)
    elif step == "timeframe":
        return params.get("timeframe") is not None and params.get("timeframe") != ""
    elif step == "leverage":
        # Per spot, leverage è sempre null (considerato filled)
        if params.get("market_type") == "spot":
            return True
        return params.get("leverage") is not None
    elif step == "risk_pct":
        return params.get("risk_pct") is not None
    elif step == "sl":
        return params.get("sl") is not None and params.get("sl") != ""
    elif step == "tp":
        return params.get("tp") is not None and params.get("tp") != ""
    return False

def _get_current_step_index(step: str) -> int:
    """Restituisce l'indice dello step nella sequenza. Legacy: strategy/strategy_params → operating_mode."""
    if step in ("strategy", "strategy_params") and "operating_mode" in STEPS:
        return STEPS.index("operating_mode")
    try:
        return STEPS.index(step)
    except ValueError:
        return 0

# PATCH: indicator periods - helper functions
def _get_required_indicators(strategy: Any) -> List[str]:
    """Determina quali indicatori sono richiesti dalla strategia."""
    if strategy is None:
        return []
    indicators = []
    strat_str = ""
    if isinstance(strategy, list):
        strat_str = " ".join([s.upper() for s in strategy])
    else:
        strat_str = str(strategy).upper()
    
    if "RSI" in strat_str:
        indicators.append("RSI")
    if "ATR" in strat_str:
        indicators.append("ATR")
    if "EMA" in strat_str:
        indicators.append("EMA")
    return indicators

def _first_missing_step(params: Dict[str, Any]) -> Optional[str]:
    """
    Restituisce quale periodo manca (EMA/RSI/ATR). Nel piano FREE con operating_mode
    i parametri sono PRESET: non si chiedono periodi, quindi ritorna None.
    """
    # FREE: se operating_mode è scelto, i parametri sono preset → nessuna domanda periodi
    if params.get("operating_mode") in OPERATING_MODE_CANONICAL:
        return None
    # Legacy: se è presente free_strategy_id, usa il preset FREE come source of truth
    strategy_id = params.get("free_strategy_id")
    preset = free_plan.get_free_preset(strategy_id) if strategy_id is not None else None
    if preset is not None:
        required_fields = set(preset.required_period_fields)
        # Ordine fisso: EMA → RSI → ATR (coerente con le domande/frasi)
        ordered_fields = ["ema_period", "rsi_period", "atr_period"]
        for field in ordered_fields:
            if field in required_fields and params.get(field) is None:
                return field
        return None
    
    # Fallback legacy: deduci dagli indicatori già presenti (derive_strategy)
    strategy = set(derive_strategy(params))
    
    # Ordine OBBLIGATORIO legacy: EMA → ATR → RSI
    if "EMA" in strategy and params.get("ema_period") is None:
        return "ema_period"
    
    if "ATR" in strategy and params.get("atr_period") is None:
        return "atr_period"
    
    if "RSI" in strategy and params.get("rsi_period") is None:
        return "rsi_period"
    
    return None

def _get_missing_indicator_period(params: Dict[str, Any]) -> Optional[str]:
    """Wrapper per compatibilità: restituisce l'indicatore mancante (EMA/ATR/RSI) invece del step."""
    step = _first_missing_step(params)
    if step == "ema_period":
        return "EMA"
    elif step == "atr_period":
        return "ATR"
    elif step == "rsi_period":
        return "RSI"
    return None

def _get_next_step(
    current_step: str,
    params: Dict[str, Any],
    cs: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """
    Prossimo step del wizard FREE: primo campo mancante nella sequenza free_plan.FREE_WIZARD_SEQUENCE.
    `current_step` è ignorato (compatibilità chiamate); la sequenza è sempre ricalcolata da params.
    """
    _ = current_step
    return free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs)

# Campi minimi piano FREE: senza questi non si marca mai config_status="complete".
REQUIRED_FIELDS = ["market_type", "symbol", "timeframe", "operating_mode"]


def is_config_complete(params: Dict[str, Any]) -> bool:
    """
    Configurazione FREE considerata completa solo se i campi core sono valorizzati
    e vale anche il controllo esteso (_all_params_filled).
    """
    if not all(params.get(k) is not None for k in REQUIRED_FIELDS):
        return False
    return _all_params_filled(params)


def _all_params_filled(params: Dict[str, Any]) -> bool:
    """Verifica se tutti i parametri necessari sono compilati."""
    market_type = params.get("market_type")
    
    # operating_mode è sempre richiesto nel piano FREE v2
    if params.get("operating_mode") not in OPERATING_MODE_CANONICAL:
        return False
    
    # FREE: strategy_id e strategy_params sono impostati dal preset operating_mode
    required = ["symbol", "market_type", "timeframe", "risk_pct", "sl", "tp"]
    for key in required:
        value = params.get(key)
        if value is None or value == "":
            return False
    
    # operating_mode + strategy_id + strategy_params (preset FREE)
    if params.get("operating_mode") not in OPERATING_MODE_CANONICAL:
        return False
    if not params.get("strategy_id"):
        return False
    sp = params.get("strategy_params")
    # Non imporre più una struttura specifica: basta che esista un dict non vuoto
    if not isinstance(sp, dict) or not sp:
        return False
    
    # Se futures, leverage è richiesto
    if market_type == "futures":
        if params.get("leverage") is None:
            return False
    
    return True

# -----------------------
# Parsing helpers - estrae SOLO il valore dello step corrente
# -----------------------

SYMBOL_RE = re.compile(r"\b([A-Z]{2,10}(?:USDT|/USDT|-USDT))\b", re.I)
TF_RE = re.compile(r"\b(\d{1,2}\s*[mhMd])\b", re.I)

# Usa normalize_symbol_strict da validators (STRICT, nessuna interpretazione)
def _normalize_symbol(raw: str) -> Optional[str]:
    """Normalizza symbol usando validators.normalize_symbol_strict (STRICT)."""
    return validators.normalize_symbol_strict(raw)


# Pattern per estrazione ticker: 2-15 caratteri alfanumerici + USDT (case-insensitive)
_TICKER_PATTERN = re.compile(r"[A-Za-z0-9]{2,15}USDT\b", re.I)


def extract_symbol(text: str) -> Optional[str]:
    """
    Parsing deterministico: estrae un simbolo ticker dal testo utente.
    Pattern: ^[A-Z0-9]{2,15}(USDT)$ o token dentro una frase (es. "cambia coppia con ETHUSDT").
    Ritorna il simbolo normalizzato (uppercase) o None se non trovato/non valido.
    """
    if not text or not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    candidate = validators.normalize_symbol_strict(raw)
    if candidate is not None:
        return candidate
    for token in raw.split():
        token = token.strip()
        if not token:
            continue
        candidate = validators.normalize_symbol_strict(token)
        if candidate is not None:
            return candidate
    for m in _TICKER_PATTERN.finditer(raw):
        candidate = validators.normalize_symbol_strict(m.group(0))
        if candidate is not None:
            return candidate
    return None


# ============================================================
# FUNZIONI DI NORMALIZZAZIONE E VALIDAZIONE PARAMETRI
# ============================================================

def normalize_percent(value: Any) -> Optional[str]:
    """
    Normalizza un valore percentuale in una stringa percentuale standardizzata.
    
    Accetta: "4", 4, "4%", "4.0%", 4.5, "4.5%"
    Ritorna: "4.0%", "4.5%" oppure None se il valore non è valido
    
    Args:
        value: Valore da normalizzare (int, float, str)
    
    Returns:
        Stringa percentuale normalizzata (es. "4.0%") oppure None se invalido
    """
    if value is None:
        return None
    
    # Rimuovi spazi e caratteri non necessari
    if isinstance(value, str):
        value = value.strip().replace("%", "").replace(",", ".")
    
    try:
        # Converti in float
        num_value = float(value)
        # Formatta con 1 decimale e aggiungi %
        return f"{num_value:.1f}%"
    except (ValueError, TypeError):
        return None

def normalize_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """
    Normalizza un valore in un intero.
    
    Args:
        value: Valore da normalizzare (int, float, str)
        default: Valore di default se la conversione fallisce
    
    Returns:
        Intero normalizzato oppure default se invalido
    """
    if value is None:
        return default
    
    if isinstance(value, str):
        value = value.strip()
        # Rimuovi caratteri non numerici (tranne il segno meno iniziale)
        value = re.sub(r"[^\d-]", "", value)
    
    try:
        int_value = int(float(value))  # Usa float prima per gestire "4.0"
        return int_value if int_value > 0 else default
    except (ValueError, TypeError):
        return default

def normalize_timeframe(value: Any) -> Optional[str]:
    """
    Normalizza un timeframe in formato standard.
    
    Accetta: "1m", "5m", "10m", "15m", "1h", "4h", "1d", "1M", "15min", "1 hour"
    Ritorna: "1m", "5m", "10m", "15m", "1h", "4h", "1d", "1M" oppure None se invalido
    
    Args:
        value: Valore da normalizzare (str)
    
    Returns:
        Timeframe normalizzato (es. "1m") oppure None se invalido
    """
    if value is None:
        return None
    
    if not isinstance(value, str):
        value = str(value)
    
    raw_value = value.strip().lower()

    verbal_match = re.fullmatch(
        r"(\d+|un)\s*(minuto|minuti|min|minute|minutes|ora|ore|hour|hours|h)",
        raw_value,
    )
    if verbal_match:
        num, unit = verbal_match.groups()
        if num == "un":
            num = "1"
        if unit in {"minuto", "minuti", "min", "minute", "minutes"}:
            return f"{num}m"
        if unit in {"ora", "ore", "hour", "hours", "h"}:
            return f"{num}h"

    value = raw_value.replace(" ", "")
    
    # Normalizza "min" -> "m"
    if value.endswith("min"):
        value = value[:-3] + "m"
    
    # Normalizza "hour" -> "h"
    if value.endswith("hour"):
        value = value[:-4] + "h"
    
    # Pattern: numero seguito da m/h/d/M
    match = re.match(r"^(\d+)([mhdM])$", value)
    if match:
        num, unit = match.groups()
        return f"{num}{unit}"
    
    return None


def _leverage_max_for_params(params: Dict[str, Any]) -> int:
    """
    Massimo leva consentito: limiti Bybit se la coppia in params è valida e listata,
    altrimenti 125 (stima generica per salvataggi parziali senza coppia valida).
    """
    market_type = params.get("market_type") or "futures"
    if market_type != "futures":
        return 125
    symbol = params.get("symbol")
    sym_norm = None
    if symbol:
        try:
            sym_norm = validators.normalize_symbol_strict(str(symbol))
        except Exception:
            sym_norm = None
    if sym_norm:
        try:
            if validators.is_symbol_listed(None, market_type, sym_norm):
                _, max_lev = validators.get_leverage_limits(None, sym_norm, "futures")
                return int(max_lev) if max_lev is not None else 125
        except Exception:
            pass
    return 125


def apply_config_patch(config_state: Dict[str, Any], patch_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Applica una patch di aggiornamenti a config_state["params"] con normalizzazione e logging.
    
    Supporta alias per le chiavi più comuni:
    - stoploss/stop_loss/sl -> "sl"
    - takeprofit/take_profit/tp -> "tp"

    Non sovrascrive con None a meno che l'utente chieda esplicitamente la rimozione.
    
    Args:
        config_state: Dict con struttura {"step": "...", "params": {...}}
        patch_dict: Dict con gli aggiornamenti da applicare (può contenere alias)
    
    Returns:
        Dict con le modifiche applicate: {"ok": bool, "message": str|None, "changed": {...}, "warnings": [...]}
        ok: True se patch applicata con successo, False se errore bloccante
        message: Messaggio di errore se ok=False, None altrimenti
        changed: {param_name: (old_value, new_value)}
        warnings: [lista di warning per chiavi non riconosciute]
    """
    if not isinstance(config_state, dict) or "params" not in config_state:
        logger.warning(f"[CONFIG_PATCH] config_state non valido: {config_state}")
        return {"ok": False, "message": "config_state non valido", "changed": {}, "warnings": ["config_state non valido"]}
    
    params = config_state.get("params", {})
    if not isinstance(params, dict):
        params = {}
        config_state["params"] = params
    
    # Leggi market_type per validazioni (default "futures")
    market_type = params.get("market_type", "futures")
    
    # Mappa alias -> chiave canonica
    alias_map = {
        "stoploss": "sl",
        "stop_loss": "sl",
        "sl": "sl",
        "takeprofit": "tp",
        "take_profit": "tp",
        "tp": "tp",
    }
    
    # Normalizza patch_dict: converte alias in chiavi canoniche
    # "strategy" con valore modalità valida (aggressiva/equilibrata/selettiva) → operating_mode (tramite preset)
    normalized_patch = {}
    for key, value in patch_dict.items():
        canonical_key = alias_map.get(key.lower(), key)
        if canonical_key == "strategy":
            # Se strategy ha valore operating_mode valido, mappalo a operating_mode (preset applicato sotto)
            mode_val = (str(value).strip().lower() if value is not None else "") or ""
            if mode_val in OPERATING_MODE_CANONICAL:
                normalized_patch["operating_mode"] = mode_val
            # Altrimenti ignora (strategy legacy non usata nel FREE v2)
            continue
        normalized_patch[canonical_key] = value
    
    # Chiavi valide per params (rimossa "strategy" perché non deve essere modificata direttamente)
    valid_keys = {
        "symbol",
        "market_type",
        "timeframe",
        "operating_mode",
        "leverage",
        "sl",
        "tp",
        "risk_pct",
    }
    
    changed = {}
    warnings = []
    
    # Log stato prima
    logger.info(f"[CONFIG_PATCH] BEFORE: params={params}")
    logger.info(f"[CONFIG_PATCH] PATCH: {patch_dict} -> normalized: {normalized_patch}")
    
    # Applica ogni aggiornamento
    for param_name, new_value in normalized_patch.items():
        # Verifica se la chiave è valida
        if param_name not in valid_keys:
            warnings.append(f"Chiave non riconosciuta: '{param_name}' (ignorata)")
            logger.warning(f"[CONFIG_PATCH] Chiave non riconosciuta: '{param_name}'")
            continue
        
        old_value = params.get(param_name)
        
        # Gestione None: trattalo come "non aggiornare" (non sovrascrivere il valore esistente)
        if new_value is None:
            logger.debug(f"[CONFIG_PATCH] Skip {param_name}: incoming None, keep existing={old_value}")
            continue
        
        # GUARD RAIL: validazione symbol
        if param_name == "symbol":
            # Leggi market_type da params (può essere aggiornato nella stessa patch)
            current_market_type = params.get("market_type", market_type)
            if current_market_type not in ["spot", "futures"]:
                current_market_type = "futures"
            
            # Normalizza symbol
            symbol_normalized = validators.normalize_symbol_strict(str(new_value))
            if symbol_normalized is None:
                error_msg = f"Il simbolo '{new_value}' non è nel formato corretto. Deve essere una coppia USDT (es. BTCUSDT, ETHUSDT)."
                logger.warning(f"[CONFIG_PATCH] {error_msg}")
                logger.info(f"[PATCH] keys={list(patch_dict.keys())} ok=False step={config_state.get('step')}")
                return {"ok": False, "message": error_msg, "changed": changed, "warnings": warnings}
            
            # Verifica se è listato
            if not validators.is_symbol_listed(None, current_market_type, symbol_normalized):
                error_msg = f"La coppia '{symbol_normalized}' non esiste su Bybit {current_market_type.capitalize()}. Ricontrolla il simbolo e riprova."
                logger.warning(f"[CONFIG_PATCH] {error_msg}")
                logger.info(f"[PATCH] keys={list(patch_dict.keys())} ok=False step={config_state.get('step')}")
                return {"ok": False, "message": error_msg, "changed": changed, "warnings": warnings}
            
            # Symbol valido, usa il valore normalizzato
            normalized_value = symbol_normalized
        
        # GUARD RAIL: validazione timeframe
        elif param_name == "timeframe":
            # Leggi market_type da params (può essere aggiornato nella stessa patch)
            current_market_type = params.get("market_type", market_type)
            if current_market_type not in ["spot", "futures"]:
                current_market_type = "futures"
            
            # Ottieni timeframe validi
            valid_tfs = validators.get_valid_timeframes(None, current_market_type)
            
            # Normalizza timeframe prima della validazione Bybit/config
            tf_for_validation = normalize_timeframe(new_value) or str(new_value)

            # Valida timeframe
            is_valid, error_msg = validators.validate_timeframe(tf_for_validation, valid_tfs)
            if not is_valid:
                logger.warning(f"[CONFIG_PATCH] {error_msg}")
                logger.info(f"[PATCH] keys={list(patch_dict.keys())} ok=False step={config_state.get('step')}")
                return {"ok": False, "message": error_msg, "changed": changed, "warnings": warnings}
            
            # Timeframe valido, normalizza formato
            normalized_value = normalize_timeframe(tf_for_validation)
            if normalized_value is None:
                error_msg = f"Valore non valido per {param_name}: {new_value} (ignorato)"
                warnings.append(error_msg)
                logger.warning(f"[CONFIG_PATCH] {error_msg}")
                continue
        
        # GUARD RAIL: validazione leverage (max Bybit se coppia valida, altrimenti cap generico)
        elif param_name == "leverage":
            current_market_type = params.get("market_type", market_type)
            if current_market_type not in ["spot", "futures"]:
                current_market_type = "futures"
            if current_market_type == "spot":
                error_msg = "La leva non è disponibile per il trading spot. La leva è disponibile solo per futures."
                logger.warning(f"[CONFIG_PATCH] {error_msg}")
                logger.info(f"[PATCH] keys={list(patch_dict.keys())} ok=False step={config_state.get('step')}")
                return {"ok": False, "message": error_msg, "changed": changed, "warnings": warnings}
            max_leverage = _leverage_max_for_params(params)
            try:
                lev = validators.parse_positive_int(str(new_value).strip(), "Leva", 1, max_leverage)
                validators.validate_leverage_range(lev, max_leverage)
                normalized_value = lev
                logger.debug("[CONFIG_PATCH] leverage max_leverage=%s chosen=%s", max_leverage, lev)
            except ValueError as e:
                logger.warning(f"[CONFIG_PATCH] {e}")
                return {"ok": False, "message": str(e), "code": "invalid_leverage", "changed": changed, "warnings": warnings}
        
        # operating_mode: applica preset (strategy_id + strategy_params coerenti con la modalità)
        elif param_name == "operating_mode":
            mode = (str(new_value).strip().lower() if new_value is not None else "") or ""
            if mode not in OPERATING_MODE_CANONICAL:
                warnings.append(f"Valore non valido per operating_mode: {new_value} (ignorato)")
                logger.warning(f"[CONFIG_PATCH] operating_mode non valido: {new_value}")
                continue
            old_mode = params.get("operating_mode")
            _apply_operating_mode_preset(params, mode)
            if old_mode != mode:
                changed["operating_mode"] = (old_mode, mode)
                logger.info(
                    "[CONFIG_PATCH] operating_mode %s -> %s strategy_id=%s strategy_params=%s (preset rebuilt)",
                    old_mode,
                    mode,
                    params.get("strategy_id"),
                    params.get("strategy_params"),
                )
            continue

        # Normalizza il valore in base al tipo di parametro (per altri parametri)
        else:
            normalized_value = None
            
            if param_name in ["sl", "tp"]:
                # Percentuali: normalizza a formato "X.X%"
                normalized_value = normalize_percent(new_value)
                if normalized_value is None:
                    warnings.append(f"Valore non valido per {param_name}: {new_value} (ignorato)")
                    logger.warning(f"[CONFIG_PATCH] Valore non valido per {param_name}: {new_value}")
                    continue
            
            elif param_name == "risk_pct":
                # Risk percent: normalizza a float
                try:
                    if isinstance(new_value, str):
                        normalized_value = float(new_value.strip().replace("%", "").replace(",", "."))
                    else:
                        normalized_value = float(new_value)
                except (ValueError, TypeError):
                    warnings.append(f"Valore non valido per {param_name}: {new_value} (ignorato)")
                    logger.warning(f"[CONFIG_PATCH] Valore non valido per {param_name}: {new_value}")
                    continue
            
            else:
                # Per altri parametri (market_type, ecc.), usa il valore così com'è
                normalized_value = new_value
        
        if normalized_value != old_value:
            params[param_name] = normalized_value
            changed[param_name] = (old_value, normalized_value)
            logger.info(f"[CONFIG_PATCH] Changed {param_name}: {old_value} -> {normalized_value}")
        else:
            logger.info(f"[CONFIG_PATCH] No change for {param_name}: {old_value} (unchanged)")
    
    # BUG 1 + BUG 4: Se market_type=spot, rimuovi leverage e pending incompatibili (merge pulito)
    if params.get("market_type") == "spot":
        old_lev = params.get("leverage")
        if old_lev is not None:
            params["leverage"] = None
            changed["leverage"] = (old_lev, None)
        config_state["pending_leverage_confirmation"] = None
    
    # BUG 3: Se market_type è cambiato, ricalcola step coerente (non lasciare step leverage con spot)
    if "market_type" in changed:
        _recompute_step(config_state)
        logger.info(f"[CONFIG_PATCH] market_type changed -> recomputed step={config_state.get('step')}")
    
    # Log stato dopo
    logger.info(f"[CONFIG_PATCH] AFTER: params={params}")
    logger.info(f"[CONFIG_PATCH] CHANGED: {changed}")
    if warnings:
        logger.warning(f"[CONFIG_PATCH] WARNINGS: {warnings}")
    
    # Log finale con keys applicate
    ok = True
    logger.info(f"[PATCH] keys={list(patch_dict.keys())} ok={ok} step={config_state.get('step')}")
    
    return {
        "ok": ok,
        "message": None,
        "changed": changed,
        "warnings": warnings
    }

# PATCH: indicator periods - estrazione periodo
def _extract_indicator_period(user_text: str, indicator: str) -> Optional[int]:
    """Estrae il periodo per un indicatore dal testo utente."""
    text = user_text.strip()
    lt = text.lower()
    ind_lower = indicator.lower()
    
    # Se l'utente dice "default" o "ok", restituisci None (sarà usato il default)
    if lt in ["default", "ok", "si", "sì", "yes", "y"]:
        return None
    
    # Pattern: "14", "rsi 14", "RSI=14", "ema 200", "atr(14)"
    # Cerca numero con o senza indicatore
    patterns = [
        rf"{ind_lower}\s*[:=\(]?\s*(\d+)",  # rsi 14, rsi=14, rsi(14)
        rf"(\d+)\s*{ind_lower}",  # 14 rsi
        r"^(\d+)$",  # solo numero
    ]
    
    for pattern in patterns:
        m = re.search(pattern, lt)
        if m:
            try:
                period = int(m.group(1))
                if period > 0:
                    return period
            except:
                pass
    
    return None

def _extract_step_value(user_text: str, step: str, params: Dict[str, Any]) -> Optional[Any]:
    """
    Estrae SOLO il valore per lo step corrente dal testo utente.
    PROTEZIONE: Se strategy è già filled e siamo su step successivi, NON estrarre mai una nuova strategy.
    """
    # ============================================================
    # LOG EXTRACT_IN - All'ingresso della funzione
    # ============================================================
    logger.info(f"[EXTRACT_IN] step={step} user_text={user_text!r}")
    
    text = user_text.strip()
    lt = text.lower()
    
    # PROTEZIONE: Se strategy è già filled e NON siamo sullo step strategy, NON estrarre strategy
    strategy_filled = _is_step_filled("strategy", params)
    if strategy_filled and step != "strategy":
        # Se siamo su step successivi a strategy, ignora qualsiasi estrazione di strategy
        # Questo previene che valori come "rsi" in altri contesti vengano interpretati come strategy
        pass  # Continua con l'estrazione normale per lo step corrente
    
    # PATCH: indicator periods - se siamo su strategy e manca un periodo, estrai il periodo
    if step == "strategy" and strategy_filled:
        missing_indicator = _get_missing_indicator_period(params)
        if missing_indicator:
            period = _extract_indicator_period(user_text, missing_indicator)
            if period is not None:
                # Restituisci un dict speciale per indicare che è un periodo
                return {"indicator": missing_indicator, "period": period}
            # Se l'utente ha scritto "default" o "ok", usa il default
            if lt in ["default", "ok", "si", "sì", "yes", "y"]:
                return {"indicator": missing_indicator, "period": None}  # None = usa default
    
    if step == "operating_mode":
        mode = _parse_operating_mode(user_text)
        extracted_value = mode
        logger.info(
            f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}"
        )
        return mode
    
    if step == "symbol":
        m = SYMBOL_RE.search(text)
        if m:
            normalized = _normalize_symbol(m.group(1))
            # STRICT: se normalize_symbol_strict ritorna None, il formato è invalido
            # NON procedere con autocorrezione
            extracted_value = normalized
            logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
            return normalized
        extracted_value = None
        logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
        return None
    
    elif step == "market_type":
        if "spot" in lt and "futures" not in lt:
            extracted_value = "spot"
            logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
            return "spot"
        if "futures" in lt or "perpetual" in lt:
            extracted_value = "futures"
            logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
            return "futures"
        extracted_value = None
        logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
        return None
    
    elif step == "timeframe":
        m = TF_RE.search(text)
        if m:
            tf = m.group(1).lower().replace(" ", "")
            # Rimuovi solo "min" se presente (per "15min" → "15m"), ma NON convertiamo "minuti" → "m"
            # Questo è solo per pulizia, non per interpretazione
            if tf.endswith("min"):
                tf = tf[:-3] + "m"
            # Verifica che sia un valore valido (la validazione finale sarà in _validate_step_value)
            extracted_value = tf
            logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
            return tf
        tf_normalized = normalize_timeframe(text)
        if tf_normalized is not None:
            extracted_value = tf_normalized
            logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
            return tf_normalized
        extracted_value = None
        logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
        return None
    
    elif step == "strategy" or step == "strategy_choice":
        # PROTEZIONE: Se strategy è già filled, NON estrarre una nuova strategy
        # (questo non dovrebbe mai accadere se la logica è corretta, ma è una protezione extra)
        if strategy_filled:
            # Se strategy è già filled, non estrarre una nuova strategy
            # Questo può accadere solo se siamo ancora su strategy per i periodi
            # In quel caso, l'estrazione dei periodi è già gestita sopra
            extracted_value = None
            logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
            return None
        
        # Usa _parse_strategy_choice per riconoscere scelte 1-4 e parole chiave
        strategy_id = _parse_strategy_choice(user_text)
        if strategy_id is not None:
            # Restituisci un dict con strategy_id (1-4) come source of truth
            extracted_value = {"strategy_id": strategy_id, "type": "strategy_choice"}
            logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
            return extracted_value
        
        # Se l'input non è riconosciuto, ritorna None
        extracted_value = None
        logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
        return None
    
    elif step == "leverage":
        # Solo per futures
        if params.get("market_type") == "spot":
            return None
        lev_val_int = _extract_leverage_int_from_text(text)
        if lev_val_int is not None:
            logger.info(
                f"[EXTRACT_OUT] step={step} extracted_type=int extracted_value={lev_val_int!r}"
            )
            return lev_val_int
        extracted_value = None
        logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
        return None
    
    elif step == "risk_pct":
        # Cerca numeri con o senza %
        m = re.search(r"(\d+(?:\.\d+)?)\s*%?", lt)
        if m:
            try:
                return float(m.group(1))
            except:
                pass
        return None
    
    elif step == "sl":
        # Cerca pattern SL
        m = re.search(r"(?:stop\s*loss|stoploss|sl)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%?", lt, re.I)
        if m:
            val = m.group(1).strip()
            return f"{val}%" if "%" not in val else val
        # Se è solo un numero, assumi sia SL
        m = re.search(r"^(\d+(?:\.\d+)?)\s*%?$", text.strip())
        if m:
            val = m.group(1).strip()
            return f"{val}%" if "%" not in val else val
        return None
    
    elif step == "tp":
        # Cerca pattern TP
        m = re.search(r"(?:take\s*profit|takeprofit|tp)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%?", lt, re.I)
        if m:
            val = m.group(1).strip()
            return f"{val}%" if "%" not in val else val
        # Se è solo un numero, assumi sia TP
        m = re.search(r"^(\d+(?:\.\d+)?)\s*%?$", text.strip())
        if m:
            val = m.group(1).strip()
            return f"{val}%" if "%" not in val else val
        return None
    
    # ============================================================
    # LOG EXTRACT_OUT - Prima del return finale (fallback)
    # ============================================================
    extracted_value = None
    logger.info(f"[EXTRACT_OUT] step={step} extracted_type={type(extracted_value).__name__ if extracted_value is not None else None} extracted_value={extracted_value!r}")
    return None

def _check_risk_warning(risk_pct: float, market_type: str) -> tuple[bool, Optional[str]]:
    """
    Verifica se il rischio richiede warning o conferma.
    Ritorna (richiede_conferma, messaggio_warning)
    REGOLA BUG3: >=HIGH_RISK_PCT_WARNING_THRESHOLD% → warning + conferma obbligatoria; <soglia → accettato normalmente
    """
    if risk_pct >= HIGH_RISK_PCT_WARNING_THRESHOLD:
        return (True, f"⚠️ Attenzione: rischiare il {risk_pct}% per trade è molto aggressivo. Confermi di volerlo impostare?")
    return (False, None)

def _check_leverage_warning(leverage_int: int, symbol: str) -> tuple[bool, Optional[str]]:
    """
    Verifica se la leva richiede warning o conferma.
    Ritorna (richiede_conferma, messaggio_warning)
    REGOLA BUG3: >=HIGH_LEVERAGE_WARNING_THRESHOLD → warning + conferma obbligatoria; <soglia → accettato normalmente
    """
    if leverage_int >= HIGH_LEVERAGE_WARNING_THRESHOLD:
        sym = symbol or "questa coppia"
        return (True, f"⚠️ Attenzione: una leva di {leverage_int}x aumenta molto il rischio. Confermi di volerla impostare?")
    return (False, None)

def _check_sl_warning(sl_pct: float) -> tuple[bool, Optional[str], Optional[float]]:
    """
    Verifica se lo stop loss richiede warning o conferma.
    Ritorna (richiede_conferma, messaggio_warning, valore_suggerito)
    REGOLA: 
    - >10% → sempre avviso + proposta valore prudente (2%) + conferma obbligatoria
    - >5% e <=10% → avviso soft ma tecnicamente accettabile, richiede conferma
    - ≤5% → accettato normalmente
    """
    if sl_pct > 10:
        # Stop loss molto alto (>10%): richiede conferma + proposta alternativa
        suggested_sl = 2.0
        return (True, f"⚠️ Attenzione: stai impostando uno stop loss del {sl_pct}%, che è molto alto e rischioso. Ti suggerisco un valore più prudente del {suggested_sl}%. Vuoi usare {suggested_sl}% o preferisci confermare {sl_pct}%?", suggested_sl)
    elif sl_pct > 5:
        # Stop loss alto ma tecnicamente accettabile (5-10%): avviso + conferma
        return (True, f"⚠️ Attenzione: stai impostando uno stop loss del {sl_pct}%, che è alto. Assicurati di comprendere i rischi. Vuoi confermare {sl_pct}% o preferisci un valore più prudente?", None)
    return (False, None, None)

def _extract_confirmation(user_text: str) -> Optional[bool]:
    """Estrae conferma (si/no) dal testo utente."""
    lt = user_text.strip().lower()
    confirm_words = ["si", "sì", "s", "yes", "y", "ok", "confermo", "conferma"]
    deny_words = ["no", "n", "non", "niente"]
    
    for word in confirm_words:
        if word in lt:
            return True
    for word in deny_words:
        if word in lt:
            return False
    return None


def _analyze_pending_resolve_input(
    user_text: str,
    merged_params: Dict[str, Any],
    merged_pending: Dict[str, Any],
) -> Tuple[Dict[str, Any], set[str], set[str]]:
    """
    Rileva modifiche esplicite (sl/tp/leverage/risk_pct) e conferme esplicite per campo (come resolve_input).

    Ritorna anche ``explicit_keys_for_ambiguity``: solo chiavi provenienti da _extract_modification_requests,
    senza il fallback lev_guess (che può leggere numeri destinati ad altri campi, es. SL 3%).
    """
    lt = (user_text or "").strip().lower()
    extracted = _extract_modification_requests(user_text, merged_params)
    explicit_updates: Dict[str, Any] = {}
    for key in ("sl", "tp", "leverage", "risk_pct"):
        if key in extracted:
            explicit_updates[key] = extracted[key]

    explicit_keys_for_ambiguity = set(explicit_updates.keys())

    # Nuova leva nel messaggio mentre c'è pending: aggiorna sempre il pending (non ripetere vecchio warning)
    if (
        "leverage" in merged_pending
        and "leverage" not in explicit_updates
        and merged_params.get("market_type") == "futures"
    ):
        lev_guess = _extract_step_value(user_text, "leverage", merged_params)
        if lev_guess is not None:
            explicit_updates["leverage"] = lev_guess

    confirmed_fields: set[str] = set()
    confirmation_patterns = {
        "leverage": r"\bconferm\w*\s+(?:la\s+)?(?:leva|leverage|lev)\b",
        "sl": r"\bconferm\w*\s+(?:lo\s+)?(?:sl|stop\s*loss|stoploss)\b",
        "risk_pct": r"\bconferm\w*\s+(?:il\s+)?(?:rischio|risk)\b",
        "tp": r"\bconferm\w*\s+(?:il\s+)?(?:tp|take\s*profit|takeprofit)\b",
    }
    for field, pattern in confirmation_patterns.items():
        if re.search(pattern, lt, re.I):
            confirmed_fields.add(field)
    return explicit_updates, confirmed_fields, explicit_keys_for_ambiguity


def _ambiguous_modify_confirm_clarification(
    user_text: str,
    params: Dict[str, Any],
    pending: Dict[str, Any],
) -> Optional[str]:
    """
    Messaggio misto ambiguo: stesso campo con modifica esplicita e conferma esplicita (es. “metti leva 3x e confermo leva”).
    Da chiamare prima di resolve_input / commit pending.
    """
    merged_params = dict(params or {})
    merged_pending = dict(pending or {})
    _, confirmed_fields, explicit_keys_for_ambiguity = _analyze_pending_resolve_input(
        user_text, merged_params, merged_pending
    )
    ambiguous = explicit_keys_for_ambiguity & confirmed_fields
    if not ambiguous:
        return None
    logger.info("[PENDING_AMBIGUITY] fields=%s input=%r", sorted(ambiguous), user_text)
    order = ("sl", "tp", "leverage", "risk_pct")
    labels = {
        "sl": "lo stop loss",
        "tp": "il take profit",
        "leverage": "la leva",
        "risk_pct": "il rischio per trade",
    }
    ordered = [f for f in order if f in ambiguous]
    if len(ordered) == 1:
        detail = labels[ordered[0]]
    elif len(ordered) == 2:
        detail = f"{labels[ordered[0]]} e {labels[ordered[1]]}"
    else:
        detail = ", ".join(labels[f] for f in ordered[:-1]) + f" e {labels[ordered[-1]]}"
    return (
        f"Nel messaggio chiedi sia di modificare sia di confermare {detail}: non è chiaro cosa preferisci. "
        "Rispondi con un'unica azione (solo la modifica che vuoi applicare oppure solo la conferma per quel parametro)."
    )


def _append_pending_confirmation_remnant_to_error_reply(base_reply: str, cs: Dict[str, Any]) -> str:
    """Dopo un errore di validazione, ricorda eventuali conferme ancora in attesa (non sovrascritte)."""
    extras: List[str] = []
    if cs.get("pending_sl_confirmation") is not None:
        p = _fmt_pending_percent(cs["pending_sl_confirmation"])
        extras.append(
            f"Rimane in attesa la conferma dello Stop Loss {p}%. "
            f"Confermi {p}% o vuoi indicare un altro valore valido?"
        )
    if cs.get("pending_risk_confirmation") is not None:
        p = _fmt_pending_percent(cs["pending_risk_confirmation"])
        extras.append(
            f"Rimane in attesa la conferma del rischio {p}%. "
            f"Confermi {p}% o vuoi indicare un altro valore valido?"
        )
    if cs.get("pending_leverage_confirmation") is not None:
        try:
            lev = int(float(cs["pending_leverage_confirmation"]))
        except (TypeError, ValueError):
            lev = cs["pending_leverage_confirmation"]
        extras.append(
            f"Rimane in attesa la conferma della leva {lev}x. "
            f"Confermi {lev}x o vuoi indicare un altro valore valido?"
        )
    if not extras:
        return (base_reply or "").strip()
    base = (base_reply or "").strip()
    if base:
        return f"{base} {' '.join(extras)}".strip()
    return " ".join(extras).strip()


def resolve_input(
    params: Dict[str, Any],
    pending: Dict[str, Any],
    user_text: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """
    Merge deterministico tra modifiche esplicite e pending.
    Regole:
    1) applica prima i valori espliciti (sl/tp/leverage/risk_pct) solo se validi per quel campo
    2) applica poi solo le conferme esplicite per campo
    3) "confermo" generico non accetta nulla
    4) pulisce pending solo per campi toccati (mai per un explicit update respinto dalla validazione)
    """
    merged_params: Dict[str, Any] = dict(params or {})
    merged_pending: Dict[str, Any] = dict(pending or {})

    explicit_updates, confirmed_fields, _ = _analyze_pending_resolve_input(
        user_text, merged_params, merged_pending
    )

    touched_fields: set[str] = set()
    validation_errors: List[str] = []

    for field, value in explicit_updates.items():
        is_valid, error_msg, _ = _validate_step_value(field, value, merged_params)
        if not is_valid:
            if error_msg:
                validation_errors.append(error_msg)
            else:
                validation_errors.append(f"Valore non valido per {field}.")
            continue
        merged_params[field] = value
        touched_fields.add(field)

    for field in confirmed_fields:
        if field in merged_pending:
            merged_params[field] = merged_pending[field]
            touched_fields.add(field)

    for field in touched_fields:
        merged_pending.pop(field, None)

    return merged_params, merged_pending, validation_errors


def _flush_all_high_risk_pending_to_params(cs: Dict[str, Any], params: Dict[str, Any]) -> None:
    """
    Scrive in params tutti i valori ancora in pending_leverage_confirmation / pending_risk_confirmation /
    pending_sl_confirmation e azzera i tre pending. Chiamare solo dopo aver verificato la conferma utente.
    """
    if cs.get("pending_leverage_confirmation") is not None:
        try:
            params["leverage"] = int(float(cs.get("pending_leverage_confirmation")))
        except (TypeError, ValueError):
            params["leverage"] = cs.get("pending_leverage_confirmation")
        cs["pending_leverage_confirmation"] = None
    if cs.get("pending_risk_confirmation") is not None:
        pending_risk = cs.get("pending_risk_confirmation")
        try:
            params["risk_pct"] = float(pending_risk)
        except (TypeError, ValueError):
            params["risk_pct"] = pending_risk
        cs["pending_risk_confirmation"] = None
        _ec = cs.get("error_count")
        if isinstance(_ec, dict):
            _ec2 = dict(_ec)
            _ec2.pop("risk_pct", None)
            cs["error_count"] = _ec2
    if cs.get("pending_sl_confirmation") is not None:
        pending_sl = cs.get("pending_sl_confirmation")
        try:
            ps = float(pending_sl)
            params["sl"] = f"{ps}%"
        except (TypeError, ValueError):
            pass
        cs["pending_sl_confirmation"] = None
        cs["suggested_sl"] = None


def _pending_batch_snapshot(cs: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot ordinato dei pending conferma ad alto rischio."""
    pending: Dict[str, Any] = {}
    if cs.get("pending_sl_confirmation") is not None:
        pending["sl"] = cs.get("pending_sl_confirmation")
    if cs.get("pending_risk_confirmation") is not None:
        pending["risk_pct"] = cs.get("pending_risk_confirmation")
    if cs.get("pending_leverage_confirmation") is not None:
        pending["leverage"] = cs.get("pending_leverage_confirmation")
    return pending


def _clear_pending_confirmation_batch(cs: Dict[str, Any]) -> Dict[str, Any]:
    """Azzera sempre tutti i pending e ritorna lo snapshot precedente."""
    before = _pending_batch_snapshot(cs)
    cs["pending_risk_confirmation"] = None
    cs["pending_leverage_confirmation"] = None
    cs["pending_sl_confirmation"] = None
    cs["suggested_sl"] = None
    logger.info("[PENDING_BATCH_CLEAR] cleared=%s", before)
    return before


def _fmt_pending_percent(value: Any) -> str:
    try:
        f = float(str(value).replace("%", "").replace(",", "."))
    except Exception:
        return str(value)
    return str(int(f)) if f.is_integer() else str(f)


def _build_pending_batch_confirmation_prompt(cs: Dict[str, Any]) -> str:
    """Costruisce una sola richiesta di conferma cumulativa."""
    pending = _pending_batch_snapshot(cs)
    pieces: List[str] = []
    if "sl" in pending:
        pieces.append(f"Stop Loss {_fmt_pending_percent(pending['sl'])}%")
    if "risk_pct" in pending:
        pieces.append(f"rischio {_fmt_pending_percent(pending['risk_pct'])}%")
    if "leverage" in pending:
        try:
            lev = int(float(pending["leverage"]))
        except Exception:
            lev = pending["leverage"]
        pieces.append(f"leva {lev}x")
    if not pieces:
        return "Confermi i valori proposti?"
    if len(pieces) == 1:
        details = pieces[0]
    elif len(pieces) == 2:
        details = f"{pieces[0]} e {pieces[1]}"
    else:
        details = f"{pieces[0]}, {pieces[1]} e {pieces[2]}"
    return f"Stai impostando {details}. Confermi?"


def _commit_pending_risk_or_leverage_on_confirm(
    user_text: str, cs: Dict[str, Any], params: Dict[str, Any]
) -> Optional[str]:
    """
    Applica merge deterministico tra pending e testo utente:
    - valori espliciti prima
    - conferme solo per campo menzionato
    - nessun "accept all pending" su "confermo" generico

    Ritorna messaggio da mostrare all'utente se il merge è bloccato (ambiguità modifica+conferma stesso campo).
    """
    pending_batch = _pending_batch_snapshot(cs)
    if not pending_batch:
        return None
    amb = _ambiguous_modify_confirm_clarification(user_text, params, pending_batch)
    if amb:
        return amb
    merged_params, merged_pending, resolve_errors = resolve_input(params, pending_batch, user_text)
    params.update(merged_params)
    cs["pending_sl_confirmation"] = merged_pending.get("sl")
    cs["pending_risk_confirmation"] = merged_pending.get("risk_pct")
    cs["pending_leverage_confirmation"] = merged_pending.get("leverage")
    if "sl" not in merged_pending:
        cs["suggested_sl"] = None
    if resolve_errors:
        return _append_pending_confirmation_remnant_to_error_reply(" ".join(resolve_errors), cs)
    return None


def _detect_empathetic_phrase(user_text: str) -> Optional[str]:
    """
    Rileva frasi empatiche di prudenza/cautela.
    Ritorna una frase empatica breve se rilevata, None altrimenti.
    """
    lt = user_text.strip().lower()
    
    # Pattern per rilevare frasi di prudenza
    cautious_patterns = [
        r"voglio stare tranquillo",
        r"preferisco andare piano",
        r"meglio essere prudent",
        r"voglio essere prudent",
        r"preferisco la prudenza",
        r"vado piano",
        r"meglio piano",
        r"partire con cautela",
        r"essere caut",
        r"preferisco cautela",
        r"meglio cautela",
        r"non voglio rischiare troppo",
        r"poco rischioso",
        r"basso rischio",
    ]
    
    for pattern in cautious_patterns:
        if re.search(pattern, lt):
            # Frasi empatiche brevi (una sola frase)
            empathetic_responses = [
                "Sono d'accordo, partire con cautela è sempre una buona scelta.",
                "Scelta sensata, soprattutto all'inizio.",
                "Va bene, meglio procedere gradualmente.",
                "Perfetto, la prudenza è importante.",
                "Saggio, è meglio partire piano.",
            ]
            import random
            return random.choice(empathetic_responses)
    
    return None

def _validate_step_value(step: str, value: Any, params: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validazione rigorosa per il valore dello step usando validators.py.
    
    Returns:
        (is_valid, error_message, warning_message)
        - Se valido: (True, None, warning_opzionale)
        - Se invalido: (False, messaggio_errore_umano, None)
    """
    if value is None:
        return (False, "Il valore non può essere vuoto.", None)
    
    # Validazione operating_mode (piano FREE v2)
    if step == "operating_mode":
        if value not in OPERATING_MODE_CANONICAL:
            return (
                False,
                "Modalità non valida. Scegli tra Aggressiva, Equilibrata o Selettiva.",
                None,
            )
        return (True, None, None)
    
    # Validazione market_type (semplice, non richiede Bybit)
    if step == "market_type":
        if value not in ["spot", "futures"]:
            return (
                False,
                f"Tipo di mercato non valido: {value}. Deve essere 'spot' o 'futures'.",
                None
            )
        return (True, None, None)
    
    # Validazione symbol usando Bybit come source of truth (STRICT)
    elif step == "symbol":
        market_type = params.get("market_type")
        if not market_type:
            return (
                False,
                "Devi prima scegliere il tipo di mercato (Spot o Futures) prima di selezionare la coppia.",
                None
            )
        
        # STRICT: prima normalizza, poi verifica se è listato
        symbol_normalized = validators.normalize_symbol_strict(str(value))
        if symbol_normalized is None:
            return (
                False,
                f"Il simbolo '{value}' non è nel formato corretto. "
                "Deve essere una coppia USDT (es. BTCUSDT, ETHUSDT).",
                None
            )
        
        # Verifica se è listato (STRICT: nessuna interpretazione)
        is_listed = validators.is_symbol_listed(None, market_type, symbol_normalized)
        if not is_listed:
            # Recupera esempi validi reali
            try:
                valid_symbols = validators.fetch_valid_symbols(market_type)
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
                    f"Ricontrolla il simbolo e riprova (esempi validi: {examples_str}).",
                    None
                )
            except Exception as e:
                return (
                    False,
                    f"Errore nel recupero dei simboli da Bybit: {str(e)}",
                    None
                )
        
        return (True, None, None)
    
    # Validazione timeframe usando lista supportata da Bybit (STRICT)
    elif step == "timeframe":
        v = normalize_timeframe(value) or str(value).strip().lower()
        is_valid, error_msg = validators.validate_timeframe(v)
        if is_valid:
            return (True, None, None)

        # Fallback: se i valid_tfs sono in formato Bybit numerico (minuti),
        # converti 15m/1h/1d/1w -> 15/60/1440/10080 e riprova.
        v2 = None
        try:
            if v.endswith("m") and v[:-1].isdigit():
                v2 = v[:-1]
            elif v.endswith("h") and v[:-1].isdigit():
                v2 = str(int(v[:-1]) * 60)
            elif v.endswith("d") and v[:-1].isdigit():
                v2 = str(int(v[:-1]) * 1440)
            elif v.endswith("w") and v[:-1].isdigit():
                v2 = str(int(v[:-1]) * 10080)
        except Exception:
            v2 = None

        if v2:
            is_valid2, error_msg2 = validators.validate_timeframe(v2)
            if is_valid2:
                return (True, None, None)
            return (False, error_msg2, None)

        return (False, error_msg, None)
    
    # Validazione strategy (alias legacy per operating_mode nel piano FREE v2)
    elif step == "strategy":
        # Tratta "strategy" come alias di operating_mode per retro‑compatibilità
        return _validate_step_value("operating_mode", value, params)
    
    # Validazione leverage: limiti Bybit se coppia valida, altrimenti range generico (salvataggi parziali)
    elif step == "leverage":
        market_type = params.get("market_type")
        symbol = params.get("symbol")
        
        if market_type != "futures":
            return (
                False,
                "La leva non è disponibile per il trading spot. La leva è disponibile solo per futures.",
                None
            )
        
        leverage_int = _parse_user_leverage_int(value)
        if leverage_int is None:
            return (
                False,
                "La leva deve essere un numero intero (es. 1, 5, 10x).",
                None,
            )
        
        maxLev = _leverage_max_for_params(params)
        minLev = 1
        is_valid, error_msg = validators.validate_leverage(
            float(leverage_int), market_type, float(minLev), float(maxLev)
        )

        if not is_valid:
            return (False, error_msg or "Valore di leva non valido.", None)
        
        sym_display = symbol or "questa coppia"
        warning_msg = None
        if leverage_int >= 51:
            warning_msg = (
                f"⚠️ Attenzione: stai usando una leva alta ({leverage_int}x) per {sym_display}. "
                "Le leve elevate aumentano significativamente il rischio. "
                "Assicurati di comprendere i rischi prima di procedere."
            )
        
        return (True, None, warning_msg)
    
    # Validazione risk_pct (semplice, non richiede Bybit)
    elif step == "risk_pct":
        try:
            val = float(value)
            if val <= 0:
                return (
                    False,
                    f"La percentuale di rischio deve essere un numero positivo.",
                    None
                )
            return (True, None, None)
        except (ValueError, TypeError):
            return (
                False,
                f"La percentuale di rischio deve essere un numero.",
                None
            )
    
    # Validazione sl (semplice, non richiede Bybit)
    elif step == "sl":
        is_valid, error_msg = validators.validate_stop_loss(value)
        if not is_valid:
            return (False, error_msg, None)
        return (True, None, None)
    
    # Validazione tp (semplice, non richiede Bybit)
    elif step == "tp":
        is_valid, error_msg = validators.validate_take_profit(value)
        if not is_valid:
            return (False, error_msg, None)
        return (True, None, None)
    
    return (True, None, None)


def _message_looks_like_mixed_config(user_text: str) -> bool:
    """Messaggio con più segnali di configurazione: non bloccare tutto su un solo errore locale."""
    lt = user_text.strip().lower()
    if len(user_text.split()) > 1:
        return True
    if re.search(
        r"%|x\b|leva|leverage|stop|take|rischio|tp\b|sl\b|tf\b|timeframe|futures|spot|capitale",
        lt,
    ):
        return True
    return False


def _apply_wizard_parallel_optional_params(
    user_text: str,
    current_step: str,
    state: Dict[str, Any],
    cs: Dict[str, Any],
    params: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, str], List[str]]:
    """
    Messaggio misto nel wizard: per ogni step in STEPS diverso da current_step,
    estrae e applica i valori validi; errori incrementano error_count e non bloccano gli altri.
    """
    errors: Dict[str, str] = {}
    success_msgs: List[str] = []
    if current_step not in STEPS:
        return params, cs, errors, success_msgs

    for step in STEPS:
        if step == current_step:
            continue
        ev = _extract_step_value(user_text, step, params)
        if ev is None:
            continue
        vr = _validate_step_value(step, ev, params)
        if not vr[0]:
            err_dict = cs.get("error_count", {})
            err_dict[step] = err_dict.get(step, 0) + 1
            cs["error_count"] = err_dict
            errors[step] = vr[1] or ""
            continue

        patch_key: Optional[Dict[str, Any]] = None

        if step == "leverage":
            lev_int = int(ev) if isinstance(ev, int) else int(float(ev))
            req_c, _ = _check_leverage_warning(lev_int, params.get("symbol") or "questa coppia")
            if req_c:
                continue
            patch_key = {"leverage": lev_int}
            label = f"leva {lev_int}x"
        elif step == "risk_pct":
            req_c, _ = _check_risk_warning(ev, params.get("market_type", "futures"))
            if req_c:
                continue
            patch_key = {"risk_pct": ev}
            label = f"rischio {ev}%"
        elif step == "sl":
            sl_val = float(str(ev).replace("%", ""))
            req_c, _, _ = _check_sl_warning(sl_val)
            if req_c:
                continue
            patch_key = {"sl": sl_val}
            label = f"stop loss {sl_val}%"
        elif step == "market_type":
            patch_key = {"market_type": ev}
            label = f"mercato {ev}"
        elif step == "symbol":
            patch_key = {"symbol": ev}
            label = f"coppia {ev}"
        elif step == "timeframe":
            patch_key = {"timeframe": ev}
            label = f"timeframe {ev}"
        elif step == "operating_mode":
            patch_key = {"operating_mode": ev}
            label = f"modalità {ev}"
        elif step == "tp":
            patch_key = {"tp": ev}
            label = f"take profit {ev}"
        else:
            continue

        if not patch_key:
            continue
        patch_result = apply_config_patch(cs, patch_key)
        if not patch_result.get("ok", True):
            err_dict = cs.get("error_count", {})
            err_dict[step] = err_dict.get(step, 0) + 1
            cs["error_count"] = err_dict
            errors[step] = patch_result.get("message") or "Valore non applicabile."
            continue
        params = cs["params"].copy()
        state, cs, params = _sync_state(state, cs, params)
        success_msgs.append(label)

    return params, cs, errors, success_msgs


# -----------------------
# Question templates - UNA DOMANDA ALLA VOLTA
# -----------------------

def prompt_retry(step_name: str, error_count: int, market_type: Optional[str] = None, 
                 symbol: Optional[str] = None, tf_examples: Optional[str] = None,
                 minLev: Optional[float] = None, maxLev: Optional[float] = None) -> str:
    """
    Genera messaggi di errore variati per evitare ripetizioni.
    
    Args:
        step_name: Nome dello step (es. "symbol", "timeframe", "leverage")
        error_count: Numero di errori consecutivi per questo step
        market_type: Tipo di mercato (per messaggi symbol)
        symbol: Simbolo (per messaggi leverage)
        tf_examples: Esempi di timeframe validi (per messaggi timeframe)
        minLev, maxLev: Limiti leverage (per messaggi leverage)
    
    Returns:
        Messaggio di errore variato (senza "Perfetto/Ottimo")
    """
    # Template per SYMBOL (errore)
    symbol_templates = [
        "Quel simbolo non risulta su Bybit {market_type}. Riprova inserendolo esattamente (es. BTCUSDT).",
        "Non lo trovo su Bybit {market_type}. Controlla le lettere e reinserisci la coppia (es. ETHUSDT).",
        "Sembra non valido per {market_type}. Inserisci una coppia USDT corretta (es. SUIUSDT).",
        "Ok, questo non è un simbolo listato. Scrivimi la coppia esatta in formato tipo BTCUSDT.",
        "Il simbolo non è disponibile su Bybit {market_type}. Verifica la scrittura e riprova (es. ADAUSDT).",
        "Non risulta tra le coppie disponibili su Bybit {market_type}. Inserisci il simbolo esatto (es. SOLUSDT)."
    ]
    
    # Template per TIMEFRAME (errore)
    timeframe_templates = [
        "Quel timeframe non è supportato. Scegline uno tra: {tf_examples}.",
        "Timeframe non valido per Bybit. Valori accettati: {tf_examples}.",
        "Non posso usare '{tf}'. Prova con uno tra: {tf_examples}.",
        "Bybit non supporta quel timeframe. Usa uno di questi: {tf_examples}.",
        "Timeframe non disponibile. Scegli tra: {tf_examples}.",
        "Quel valore non è un timeframe valido. Prova con: {tf_examples}."
    ]
    
    # Template per LEVERAGE (errore hard)
    leverage_templates = [
        "Quella leva non è consentita per {symbol}. Inserisci un valore tra {minLev}x e {maxLev}x.",
        "Leva fuori range. Per {symbol} puoi usare {minLev}x–{maxLev}x. Che leva scegli?",
        "Il valore inserito non è valido per {symbol}. La leva deve essere tra {minLev}x e {maxLev}x.",
        "Leva non consentita. Per {symbol} il range è {minLev}x–{maxLev}x. Inserisci un valore valido.",
        "Valore fuori dai limiti per {symbol}. Usa una leva tra {minLev}x e {maxLev}x."
    ]
    
    # Template per domande finali (variati)
    question_templates = {
        "symbol": [
            "Che coppia vuoi usare?",
            "Quale coppia USDT inserisci?",
            "Scrivimi la coppia in formato BTCUSDT:",
            "Ok, riproviamo: quale symbol USDT vuoi?",
            "Inserisci la coppia di trading:",
            "Quale coppia scegli?"
        ],
        "timeframe": [
            "Quale timeframe?",
            "Scegli un timeframe:",
            "Che timeframe vuoi usare?",
            "Inserisci il timeframe:",
            "Quale timeframe preferisci?",
            "Ok, riproviamo: quale timeframe?"
        ],
        "leverage": [
            "Che leva vuoi utilizzare?",
            "Quale leva scegli?",
            "Inserisci la leva:",
            "Che leva preferisci?",
            "Ok, riproviamo: quale leva?"
        ]
    }
    
    # Seleziona template basato su step e error_count (pseudo-casuale ma deterministico)
    idx = error_count % 6  # Cicla tra 0-5
    
    if step_name == "symbol":
        template = symbol_templates[idx % len(symbol_templates)]
        market = market_type or "Bybit"
        return template.format(market_type=market)
    
    elif step_name == "timeframe":
        template = timeframe_templates[idx % len(timeframe_templates)]
        examples = tf_examples or "1m, 5m, 15m, 1h, 4h, 1d"
        return template.format(tf_examples=examples)
    
    elif step_name == "leverage":
        template = leverage_templates[idx % len(leverage_templates)]
        sym = symbol or "questa coppia"
        min_l = minLev or 1
        max_l = maxLev or 125
        return template.format(symbol=sym, minLev=int(min_l), maxLev=int(max_l))
    
    # Fallback: domanda generica
    return "Riprova inserendo un valore valido."


def _step_question(step: str, params: Dict[str, Any], error_count: int = 0, is_error: bool = False, greeting_variant: Optional[int] = None) -> str:
    """
    Restituisce la domanda per lo step corrente (UNA SOLA domanda).
    
    Args:
        step: Nome dello step
        params: Parametri correnti
        error_count: Numero di errori consecutivi (per variare messaggi)
        is_error: Se True, NON usa "Perfetto/Ottimo"
        greeting_variant: Indice della variante da usare per market_type (0, 1, o 2). Se None, usa la prima.
    """
    # PATCH: indicator periods - se strategy/strategy_params e manca un periodo, chiedilo
    if step in ("strategy", "strategy_params"):
        missing = first_missing_required_period(params)
        if missing:
            return missing[1]
    
    # Template per domande finali (variati)
    question_templates = {
        "symbol": [
            "Che coppia vuoi usare?",
            "Quale coppia USDT inserisci?",
            "Scrivimi la coppia in formato BTCUSDT:",
            "Ok, riproviamo: quale symbol USDT vuoi?",
            "Inserisci la coppia di trading:",
            "Quale coppia scegli?"
        ],
        "timeframe": [
            "Quale timeframe?",
            "Scegli un timeframe:",
            "Che timeframe vuoi usare?",
            "Inserisci il timeframe:",
            "Quale timeframe preferisci?",
            "Ok, riproviamo: quale timeframe?"
        ],
        "leverage": [
            "Che leva vuoi utilizzare?",
            "Quale leva scegli?",
            "Inserisci la leva:",
            "Che leva preferisci?",
            "Ok, riproviamo: quale leva?"
        ]
    }
    
    if step == "operating_mode":
        # Domanda iniziale per il piano FREE v2
        return "Scegli la modalità operativa: Aggressiva, Equilibrata o Selettiva."
    
    if step == "symbol":
        market_type = params.get("market_type")
        if is_error:
            # Dopo errore: usa messaggio variato senza "Perfetto"
            return phrases.get_ask_symbol(error_count)
        else:
            # Prima volta o dopo successo: usa variante senza "Perfetto"
            transition = phrases.get_positive_transition(error_count)
            return f"{transition}. {phrases.get_ask_symbol(error_count)} (es. BTCUSDT)"
    
    elif step == "market_type":
        # Varianti per la domanda market_type con saluto
        greeting_variants = [
            "Iniziamo! Vuoi operare in Spot o in Futures? ⚠️ Nota: visti i recenti aggiornamenti normativi, per alcuni account europei i Futures su Bybit potrebbero non essere disponibili. Se scegli Futures, il bot proverà comunque a tradare.",
            "Partiamo dalla modalità: Spot o Futures? ⚠️ Nota importante: per alcuni account europei i Futures su Bybit potrebbero non essere disponibili a causa di recenti aggiornamenti normativi. Se scegli Futures, il bot proverà comunque a operare.",
            "Prima scelta: preferisci Spot o Futures? ⚠️ Attenzione: a causa delle recenti normative, per alcuni account europe i Futures su Bybit potrebbero non essere disponibili. Se scegli Futures, il bot proverà comunque a tradare."
        ]
        
        # Se è specificata una variante, usala
        if greeting_variant is not None and 0 <= greeting_variant < len(greeting_variants):
            return greeting_variants[greeting_variant]
        
        # Altrimenti usa la prima variante (comportamento di default)
        return greeting_variants[0]
    
    elif step == "timeframe":
        if is_error:
            return phrases.get_ask_timeframe(error_count)
        return phrases.get_ask_timeframe(error_count)
    
    elif step == "strategy":
        # Nel piano FREE v2 lo step strategy è concettualmente la modalità operativa
        return "Che modalità preferisci: aggressiva, equilibrata o selettiva?"
    
    elif step == "leverage":
        if is_error:
            return phrases.get_ask_leverage(error_count)
        return phrases.get_ask_leverage(error_count)
    
    elif step == "risk_pct":
        return "Che percentuale del capitale vuoi rischiare per trade?"
    
    elif step == "sl":
        return "Quale stop loss in percentuale?"
    
    elif step == "tp":
        return "Quale take profit in percentuale?"
    
    # Fallback: se lo step non è riconosciuto, riparti da operating_mode
    return "Che modalità vuoi usare? Aggressiva / Equilibrata / Selettiva"

def _extract_remove_indicators(user_text: str) -> list[str]:
    """Estrae gli indicatori da rimuovere dal testo utente."""
    t = (user_text or "").lower()
    if not re.search(r"\b(elimina|rimuovi|cancella|togli)\b", t):
        return []
    remove = []
    if re.search(r"\brsi\b", t): remove.append("RSI")
    if re.search(r"\batr\b", t): remove.append("ATR")
    if re.search(r"\bema\b", t): remove.append("EMA")
    return remove

def recompute_strategy_from_periods(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Legacy helper: nel piano FREE v2 la strategia NON viene più derivata da periodi
    di indicatori. Manteniamo solo che `strategy` esista sempre come lista.
    """
    if params is None or not isinstance(params, dict):
        params = _coerce_params(params)

    if not isinstance(params.get("strategy"), list):
        params["strategy"] = []
    return params


def _sync_strategy_from_periods(params):
    """
    Legacy helper: per FREE v2 non deve più derivare nulla da periodi indicatori.
    Garantisce solo che `strategy` sia una lista (vuota per default).
    """
    params = _coerce_params(params)
    if not isinstance(params.get("strategy"), list):
        params["strategy"] = []
    return params

def _build_summary(params: Dict[str, Any]) -> str:
    """Costruisce il riepilogo ordinato della configurazione."""
    lines = []
    
    if params.get("symbol"):
        lines.append(f"Coppia: {params.get('symbol')}")
    
    if params.get("market_type"):
        lines.append(f"Tipo di mercato: {params.get('market_type')}")
    
    if params.get("timeframe"):
        lines.append(f"Timeframe: {params.get('timeframe')}")
    
    # Modalità operativa e strategia ad alto livello (FREE v2)
    operating_mode = params.get("operating_mode")
    if operating_mode in OPERATING_MODE_CANONICAL:
        lines.append(f"Modalità operativa: {operating_mode}")

    # Non mostriamo più dettagli interni di strategy_params (indicator‑level).
    
    if params.get("market_type") == "futures" and params.get("leverage"):
        lines.append(f"Leva: {params.get('leverage')}x")
    
    if params.get("risk_pct"):
        lines.append(f"Rischio per trade: {params.get('risk_pct')}%")
    
    if params.get("sl"):
        lines.append(f"Stop Loss: {params.get('sl')}")
    
    if params.get("tp"):
        lines.append(f"Take Profit: {params.get('tp')}")
    
    return "\n".join(lines)

# -----------------------
# Main entry - FSM SEQUENZIALE
# -----------------------

def _is_configuration_complete(state: Dict[str, Any]) -> bool:
    """Verifica se la configurazione è completa (config_status='complete')."""
    return state.get("config_status") == "complete"

def _is_generic_question(user_text: str) -> bool:
    """Rileva se l'utente sta facendo una domanda generica/informativa."""
    lt = user_text.strip().lower()
    generic_patterns = [
        "posso usare", "posso cambiare", "e se volessi", "che differenza", "cosa significa",
        "spiegami", "dimmi", "raccontami", "parlami", "come funziona", "cosa fa",
        "quali sono", "qual è la differenza", "cosa cambia", "può", "puoi spiegare"
    ]
    return any(pattern in lt for pattern in generic_patterns)

def is_informational_question(text: str) -> bool:
    """
    Rileva domande informative durante il wizard.

    Quando True, non si applica patch né si avanza lo step: si risponde (LLM + contesto)
    e si ripropone la domanda dello step corrente. I comandi diretti restano esclusi
    (es. "metti futures", "BTCUSDT", "1m", "leva 10x", "sl 3%").
    """
    raw = (text or "").strip()
    if not raw:
        return False
    lt = raw.lower()

    # Non trattare come domanda informativa input chiaramente operativi.
    # 1) Ticker "puro" (es. BTCUSDT, ETH/USDT)
    if re.fullmatch(r"\s*[a-z0-9]{2,12}\s*(?:/|-)?\s*usdt\s*\b", lt, re.I):
        return False
    # 2) Timeframe "puro" (es. 1m, 15m, 1h, 4h, 1d)
    if re.fullmatch(r"\s*\d+\s*[mhdw]\s*\b", lt, re.I) or normalize_timeframe(raw) is not None:
        return False
    # 3) Market type diretto
    if lt in ("spot", "futures", "perpetual"):
        return False
    # 4) Comandi espliciti di set
    if re.search(r"\b(?:metti|imposta|voglio|vorrei|usa|passa|torna|rimetti)\b.*\b(?:spot|futures)\b", lt):
        return False
    if re.search(r"(?:\bleva\b|\bleverage\b|\blev\b)\s*[:=]?\s*\d+(?:\.\d+)?\s*x?\b", lt):
        return False
    if re.search(r"\b\d+(?:\.\d+)?\s*x\b", lt):
        return False
    if re.search(r"\b(?:sl|stop\s*loss|tp|take\s*profit)\b\s*[:=]?\s*\d+(?:\.\d+)?\s*%?\b", lt):
        return False
    if re.search(r"\b(?:rischio|risk)\b\s*[:=]?\s*\d+(?:\.\d+)?\s*%?\b", lt):
        return False

    informational_triggers = [
        "?",
        "cosa",
        "che cosa",
        "qual è",
        "differenza",
        "cambia",
        "spiegami",
        "cos'è",
        "cosa significa",
        "conviene",
        "meglio",
        "perché",
        "perche",
        "come funziona",
        "che coppia",
        "quale coppia",
        "coppia mi consigli",
        "cosa mi consigli",
        "mi consigli",
        "non ho capito",
        "aiutami",
        "non saprei",
        "non lo so",
        "consigliami",
        "cosa scelgo",
        "aiutami a scegliere",
        "consigli",
    ]
    if any(trigger in lt for trigger in informational_triggers):
        return True
    # Parole corte: word boundary per limitare falsi positivi
    if re.search(r"\b(?:che|come)\b", lt):
        return True
    return False

def _informational_answer_fallback(user_text: str) -> str:
    """Fallback breve per risposte informative durante il wizard."""
    lt = (user_text or "").strip().lower()
    if any(k in lt for k in ["spot", "futures", "differenza", "cosa cambia"]):
        return (
            "Spot significa comprare/vendere direttamente la crypto. "
            "Futures usa contratti e puo includere leva, quindi espone a piu rischio."
        )
    if any(k in lt for k in ["leva", "leverage", "cosa significa leva"]):
        return (
            "La leva amplifica guadagni e perdite: con 10x, una variazione dell'1% sul prezzo "
            "vale circa 10% sul margine."
        )
    if "timeframe" in lt:
        return (
            "In generale timeframe bassi sono piu rapidi ma piu rumorosi, "
            "timeframe alti sono piu stabili ma meno frequenti."
        )
    if any(k in lt for k in ["rischio", "risk", "quanto rischio"]):
        return (
            "Per molti profili prudenti si usa spesso un rischio contenuto per trade; "
            "l'importante e mantenere coerenza con il tuo piano."
        )
    return (
        "Ti aiuto volentieri su questo punto; la spiegazione dettagliata non è disponibile in questo momento. "
        "Se riformuli la domanda o riprovi tra poco, posso essere più preciso."
    )


def _normalize_faq_input(user_text: str) -> str:
    t = (user_text or "").strip().lower()
    for ch in ("\u2019", "\u2018", "`", "\xb4"):
        t = t.replace(ch, "'")
    t = re.sub(r"\s+", " ", t)
    return t


def _faq_info_complete_guard_response(
    body: str,
    state: Dict[str, Any],
    cs: Dict[str, Any],
    params: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Stesso criterio dei log [CONFIG_CHECK]: is_config_complete(params).
    Se la config è già completa, niente wizard/repeat step: solo ask-start-bot.
    """
    if not is_config_complete(params):
        return None
    cs["step"] = None
    state["step"] = None
    state["config_status"] = "complete"
    logger.info("[FAQ_COMPLETE_GUARD] complete=True step forced None ask_start_bot")
    reply = (body or "").strip() + "\n\nVuoi avviare il bot adesso?"
    state, cs, params = _sync_state(state, cs, params)
    return {"reply": reply, "state": state, "skip_llm": True}


def _faq_repeat_suffix(
    state: Dict[str, Any],
    cs: Dict[str, Any],
    params: Dict[str, Any],
    current_step: str,
) -> Tuple[str, str, str]:
    """Testo da appendere dopo la FAQ, label step, tipo di ripetizione (solo log)."""
    if not _is_step_filled("market_type", params):
        invite = "Per configurare Idith rispondi alla domanda qui sotto.\n\n"
        return invite + _step_question("market_type", params), "market_type", "wizard_not_started_invite"
    step_eff = current_step or "market_type"
    return _step_question(step_eff, params), step_eff, "repeat_current_step"


def _faq_text_has_trading_param_signals(user_text: str) -> bool:
    """Evita FAQ su frasi che impostano anche SL/TP/leva/timeframe (senza usare _message_looks_like_mixed_config)."""
    lt = user_text.strip().lower()
    return bool(
        re.search(
            r"%|x\b|leva|leverage|stop|take|rischio|tp\b|sl\b|tf\b|timeframe|futures|spot|capitale",
            lt,
        )
    )


def _match_practical_faq(user_text: str) -> Optional[Tuple[str, str]]:
    """
    Domande pratiche su runner/API/status: risposta fissa (intent, testo).
    Ordine delle regole: più specifiche prima.
    """
    lt = _normalize_faq_input(user_text)
    if not lt or len(lt) > 320:
        return None
    if _faq_text_has_trading_param_signals(user_text):
        return None
    if _is_explicit_modification_request(user_text):
        return None

    # --- Runner (ordine: problemi → post-install → collegamento → stato → download → avvio → cos'è) ---
    if "runner" in lt:
        runner_connect_msg = (
            "Per collegare il runner: fai doppio clic sull'icona Idith Runner, aspetta circa 5-6 secondi finché appare la finestra con il codice, clicca 'Copia code', torna nella chat di Idith, clicca 'Collega runner' e incolla il codice. Se tutto è corretto, nella tabella a sinistra lo stato del runner passerà da 'non connesso' a 'connesso'."
        )
        if (
            "non connesso" in lt
            or "non collegato" in lt
            or "runner non" in lt
            or ("cosa fare" in lt and "non" in lt)
        ):
            return (
                "faq_runner_not_connected",
                "Se il runner non risulta connesso, apri il runner sul PC e controlla di averlo collegato con il codice tramite 'Collega runner'.",
            )
        # 1) Dopo installazione → passo successivo = collegamento (prima di qualsiasi match generico "runner")
        if any(
            p in lt
            for p in (
                "ho installato",
                "installato il runner",
                "runner installato",
                "installata",
                "installazione",
                "dopo l'install",
                "dopo install",
                "dopo aver installato",
                "dopo aver installato il runner",
                "appena installato",
                "appena installata",
                "finito di installare",
                "finita l'installazione",
                "installazione completata",
                "installazione finita",
            )
        ) or (
            "install" in lt
            and "runner" in lt
            and any(
                w in lt
                for w in (
                    "ora cosa",
                    "cosa faccio",
                    "che faccio",
                    "prossimo passo",
                    "passo successivo",
                    "adesso cosa",
                    "e adesso",
                    "e ora",
                )
            )
        ):
            return ("faq_runner_post_install", runner_connect_msg)
        # 2) Collegamento esplicito
        if any(
            p in lt
            for p in (
                "come collego il runner",
                "come collego runner",
                "collegare il runner",
                "collego il runner",
                "collega runner",
                "collego runner",
                "copia code",
                "copia il code",
                "incolla il codice",
                "incolla il code",
            )
        ):
            return ("faq_runner_connect", runner_connect_msg)
        # 3) Avvio runner (prima del fallback "cos'è", così "come avvio" non finisce nel definizione)
        if any(
            p in lt
            for p in (
                "come avvio",
                "avvio il runner",
                "avviare il runner",
                "avvi il runner",
                "come si avvia",
                "come faccio ad avviare",
                "doppio clic",
            )
        ):
            return (
                "faq_runner_start",
                "Per avviare il runner fai doppio clic sull'icona Idith Runner sul desktop. In alternativa, se è già installato, puoi controllare anche nella barra di Windows in basso a destra, vicino alle icone di volume o Wi-Fi. Dopo l'avvio aspetta qualche secondo.",
            )
        if any(
            p in lt
            for p in (
                "come vedo",
                "vedo se",
                "e connesso",
                "è connesso",
                "stato runner",
                "runner connesso",
                "runner collegato",
                "tabella",
            )
        ):
            return (
                "faq_runner_status",
                "Lo vedi dalla tabella di stato a sinistra: se il runner è connesso, lo stato runner risulta attivo/connesso. Se non è collegato, Idith non può avviare il bot.",
            )
        if "scaric" in lt or "download" in lt:
            return (
                "faq_runner_download",
                "Puoi scaricare il runner dal pulsante 'Download runner'. Dopo l'installazione, avvialo sul tuo PC e poi torna su Idith per collegarlo.",
            )
        # 4) Fallback stretto: solo definizione "cos'è / che cos'è il runner" (niente match generico runner+?)
        if re.search(r"(cos['']?\s*[eè]\s+il\s+runner|che\s+cos['']?\s*[eè]\s+il\s+runner|cosa\s+[eè]\s+il\s+runner)", lt) or any(
            p in lt
            for p in (
                "cos'e il runner",
                "cos'è il runner",
                "che cos'e il runner",
                "che cos'è il runner",
                "cosa e il runner",
                "cosa è il runner",
                "cosè il runner",
            )
        ):
            return (
                "faq_runner_what",
                "Il runner è il piccolo programma che gira sul tuo PC e permette a Idith di comunicare con Bybit. Serve perché le API restano salvate localmente sul tuo dispositivo: Idith non le memorizza sul server.",
            )

    # --- API / exchange ---
    if "api" in lt or "exchange" in lt:
        if "non valid" in lt or "non valide" in lt or "invalid" in lt:
            return (
                "faq_api_invalid",
                "Se le API non sono valide, crea nuove API su Bybit Testnet e reinseriscile da 'Collega Exchange'. Controlla di usare API Testnet, non API live.",
            )
        if ("dove" in lt or "vanno" in lt) and any(x in lt for x in ("salv", "memorizz", "tenut")) and "colleg" not in lt:
            return (
                "faq_api_where",
                "Le API vengono salvate localmente nel runner sul tuo PC. Idith non le memorizza sul server.",
            )
        if ("cre" in lt or "gener" in lt) and ("api" in lt or "chiavi" in lt or "chiave" in lt):
            return (
                "faq_api_create",
                "Per creare le API devi entrare su Bybit Testnet, andare nella sezione API/API Management, creare una nuova API Key e copiare API Key e API Secret. Usa solo API Testnet.",
            )
        if (
            ("colleg" in lt or "verifica e salva" in lt)
            and ("api" in lt or "exchange" in lt)
            and "runner" not in lt
        ):
            return (
                "faq_api_connect",
                "Per collegare le API clicca 'Collega Exchange', incolla API Key e API Secret e poi premi 'Verifica e salva'. Le chiavi vengono salvate localmente nel runner sul tuo PC, non su Supabase/server.",
            )

    # --- Testnet / soldi reali ---
    if "testnet" in lt and any(x in lt for x in ("cos'", "cos'e", "che ", "signif", "cosa ")):
        return (
            "faq_testnet",
            "La testnet è un ambiente di prova: permette di testare il bot senza usare fondi reali.",
        )
    if (
        any(x in lt for x in ("soldi veri", "soldi reali", "denaro reale", "fondi reali"))
        or ("soldi" in lt and ("veri" in lt or "reali" in lt))
        or ("posso usare" in lt and ("reali" in lt or "veri" in lt or "live" in lt))
    ):
        return (
            "faq_real_money",
            "Nel piano Free Idith usa solo testnet. Non opera con soldi reali.",
        )

    # --- P/L e ordini ---
    plish = "p/l" in lt or bool(re.search(r"\bpl\b", lt))
    if plish and ("realizz" in lt or "realizzato" in lt):
        return (
            "faq_pl_realized",
            "Il P/L realizzato indica il profitto o la perdita già chiusa da operazioni completate.",
        )
    if plish and "apert" in lt:
        return (
            "faq_pl_open",
            "Il P/L aperto indica il profitto o la perdita momentanea delle operazioni ancora aperte. Può cambiare finché l'ordine non viene chiuso.",
        )
    if "analizz" in lt and ("idith" in lt or "sta analizz" in lt):
        return (
            "faq_idith_analyzing",
            "'Idith sta analizzando' significa che il bot sta controllando il mercato secondo la strategia scelta. Non vuol dire per forza che aprirà subito un ordine.",
        )
    if any(
        p in lt
        for p in (
            "come avvio il bot",
            "come avvio bot",
            "come faccio ad avviare il bot",
            "come faccio a avviare il bot",
            "come si avvia il bot",
            "come avviare il bot",
            "come faccio a farlo partire",
            "come faccio farlo partire",
            "come faccio partire il bot",
            "come faccio a far partire il bot",
            "avvio il bot",
            "avviare il bot",
            "far partire il bot",
            "come fermo il bot",
            "come fermare il bot",
            "come faccio a fermare il bot",
            "fermare il bot",
        )
    ):
        return (
            "faq_bot_start_stop_chat",
            "Per avviare il bot non ci sono pulsanti.\n\n"
            "Devi usare direttamente la chat.\n\n"
            "Prima assicurati di aver completato questi passaggi:\n\n"
            '- Hai installato e avviato il runner sul tuo PC\n'
            '- Hai collegato il runner tramite codice (cliccando "Collega runner")\n'
            '- Hai inserito le API di Bybit (tramite "Collega Exchange")\n'
            '- Hai completato la configurazione del bot\n\n'
            "Se tutto è pronto, per avviare il bot scrivi:\n"
            "👉 avvia bot\n\n"
            "Per fermarlo in qualsiasi momento scrivi:\n"
            "👉 ferma bot\n\n"
            "Quando il bot è attivo, vedrai lo stato aggiornarsi nella tabella a sinistra.",
        )
    if "bot attivo" in lt or (
        "bot" in lt and "attivo" in lt and any(x in lt for x in ("signif", "cosa ", "che cos", "cos'e", "cos'è"))
    ):
        return (
            "faq_bot_active",
            "'Bot attivo' significa che la configurazione è stata completata e il runner ha ricevuto il comando per avviare il bot.",
        )
    if "ordini complet" in lt or ("completat" in lt and "ordin" in lt):
        return (
            "faq_orders_completed",
            "'Ordini completati' indica quante operazioni sono state chiuse dal bot in quella sessione.",
        )
    if (
        ("non apre" in lt and "ordin" in lt)
        or ("nessun ordine" in lt)
        or ("perch" in lt and "ordin" in lt and "non" in lt)
    ):
        return (
            "faq_bot_no_orders",
            "Il bot apre ordini solo quando la strategia trova condizioni valide. Se il runner è scollegato, le API non sono valide, il saldo è insufficiente o il mercato non dà segnali, potrebbe non aprire nulla.",
        )

    return None


def _is_explicit_modification_request(user_text: str) -> bool:
    """Rileva se l'utente chiede esplicitamente di modificare un parametro."""
    lt = user_text.strip().lower()
    if lt in ("spot", "futures"):
        return True
    modification_patterns = [
        "cambia", "modifica", "voglio cambiare", "vorrei cambiare", "preferirei",
        "metti", "imposta", "voglio", "vorrei", "preferisco"
    ]
    if any(pattern in lt for pattern in modification_patterns):
        return True
    # Cambio market_type: azione + spot/futures (es. "passa a futures", "torna a spot", "metti spot")
    market_type_actions = (
        "passa", "passiamo", "cambia", "cambiamo", "metti", "mettiamo",
        "vai", "andiamo", "voglio", "torna", "rimetti"
    )
    if ("spot" in lt or "futures" in lt) and any(a in lt for a in market_type_actions):
        return True
    # "strategia aggressiva/equilibrata/selettiva", "modalità ...", "operativa ..." → cambio operating_mode
    operating_mode_trigger_patterns = [
        "strategia aggressiva", "strategia equilibrata", "strategia selettiva",
        "modalità aggressiva", "modalità equilibrata", "modalità selettiva",
        "operativa aggressiva", "operativa equilibrata", "operativa selettiva",
        "quella aggressiva", "quella equilibrata", "quella selettiva",
    ]
    return any(p in lt for p in operating_mode_trigger_patterns)

def _extract_modification_requests(user_text: str, params: Dict[str, Any], current_step: Optional[str] = None) -> Dict[str, Any]:
    """
    Estrae tutti i parametri che l'utente vuole modificare e i nuovi valori proposti.
    
    IMPORTANTE: symbol viene estratto SOLO se:
    - current_step == "symbol" (flusso normale FSM)
    - OPPURE se è una modifica esplicita (es. "cambia symbol a BTCUSDT")
      In questo caso, la validazione verrà fatta successivamente in handle_message.
    
    Args:
        user_text: Testo dell'utente
        params: Parametri correnti
        current_step: Step corrente della FSM (opzionale, per controllare se siamo nello step "symbol")
    
    Returns:
        Dict[str, Any] con tutti gli aggiornamenti trovati (es. {"symbol": "BTCUSDT", "timeframe": "15m"}).
        Le chiavi possono essere: "symbol", "timeframe", "leverage", "ema_period", "rsi_period", 
        "atr_period", "sl", "tp", "risk_pct", "market_type", "strategy"
        Per la rimozione di indicatori, usa None come valore (es. {"ema_period": None})
    """
    text = user_text.strip()
    lt = text.lower()
    updates = {}
    
    # Mappa parametri a pattern di riconoscimento (ordine importante: più specifici prima)
    param_patterns = {
        "ema_period": ["ema"],
        "rsi_period": ["rsi"],
        "atr_period": ["atr"],
        "sl": ["stop loss", "stoploss", "sl"],
        "tp": ["take profit", "takeprofit", "tp"],
        "symbol": ["coppia", "symbol", "pair", "simbolo"],
        "timeframe": ["timeframe", "tf", "tempo"],
        "leverage": ["leva", "leverage", "lev"],
        "risk_pct": ["rischio", "risk", "percentuale rischio"],
        "market_type": ["spot", "futures", "mercato", "tipo mercato"],
        "strategy": ["strategia", "strategy"]
    }
    
    # Pattern per rimozione indicatori
    removal_patterns = ["rimuovi", "cancella", "elimina", "togli", "remove", "delete"]
    
    # Controlla rimozione indicatori PRIMA di cercare valori
    indicator_mapping = [("ema_period", ["ema"]), ("rsi_period", ["rsi"]), ("atr_period", ["atr"])]
    
    for indicator_key, indicator_patterns in indicator_mapping:
        # Controlla rimozione
        for removal in removal_patterns:
            for ind_pattern in indicator_patterns:
                pattern_re = re.compile(rf"\b{re.escape(removal)}\s+{re.escape(ind_pattern)}\b", re.I)
                if pattern_re.search(lt):
                    updates[indicator_key] = None  # None = rimuovi
                    break
            if indicator_key in updates:
                break

    # Estrai operating_mode: "aggressiva/equilibrata/selettiva" anche se precedute da "strategia" / "modalità" / "operativa"
    # Così "strategia aggressiva", "modifica la strategia con quella aggressiva" → operating_mode, non strategy_id
    parsed_mode = _parse_operating_mode(text)
    if parsed_mode is not None:
        updates["operating_mode"] = parsed_mode

    # Estrai symbol SOLO se:
    # 1. Siamo nello step "symbol" (flusso normale FSM)
    # 2. OPPURE se è una modifica esplicita (es. "cambia symbol a BTCUSDT")
    #    In questo caso, la validazione verrà fatta successivamente in handle_message
    should_extract_symbol = False
    if current_step == "symbol":
        # Flusso normale FSM: estrai symbol
        should_extract_symbol = True
    else:
        # Modifica esplicita: estrai symbol solo se c'è un pattern esplicito
        # Verifica se c'è un pattern tipo "cambia symbol a XXX" o "symbol XXX"
        symbol_explicit_patterns = [
            r"(?:cambia|modifica|imposta|metti|voglio|vorrei|preferisco)\s+(?:coppia|symbol|pair|simbolo)\s+(?:a|a|in|su)?\s*",
            r"(?:coppia|symbol|pair|simbolo)\s+(?:è|sia|diventa|diventi)\s*",
        ]
        for pattern in symbol_explicit_patterns:
            if re.search(pattern, lt, re.I):
                should_extract_symbol = True
                break
        # BUG1 FIX: "coppia X" / "symbol X" in messaggi misti (es. "metti sl 5%, tp 5%, coppia bbbusdt")
        # Se c'è un ticker nel testo E parole coppia/symbol/pair/simbolo, estrai per validare e includere eventuali errori
        if not should_extract_symbol and SYMBOL_RE.search(text):
            if any(re.search(r"\b" + re.escape(p) + r"\b", lt) for p in ["coppia", "symbol", "pair", "simbolo"]):
                should_extract_symbol = True

    if should_extract_symbol:
        symbol_match = SYMBOL_RE.search(text)
        if symbol_match:
            normalized = _normalize_symbol(symbol_match.group(1))
            if normalized:
                updates["symbol"] = normalized
    
    # Estrai timeframe (primo match ok)
    tf_match = TF_RE.search(text)
    if tf_match:
        tf = tf_match.group(1).lower().replace(" ", "")
        # Rimuovi "min" se presente (per "15min" → "15m")
        if tf.endswith("min"):
            tf = tf[:-3] + "m"
        updates["timeframe"] = tf
    else:
        # Supporta forme testuali come "5 minuti" / "1 ora"
        tf_words_match = re.search(
            r"\b(\d{1,2})\s*(minuto|minuti|ora|ore)\b",
            lt,
            re.I,
        )
        if tf_words_match:
            tf_num = tf_words_match.group(1)
            tf_unit = tf_words_match.group(2).lower()
            if tf_unit.startswith("minut"):
                updates["timeframe"] = f"{tf_num}m"
            elif tf_unit in ("ora", "ore"):
                updates["timeframe"] = f"{tf_num}h"
    
    # Estrai leverage: pattern (\d+(\.\d+)?)\s*x oppure "leva 3"
    leverage_patterns = [
        r"(\d+(?:\.\d+)?)\s*x\b",  # "3x", "2.5x"
        r"leva\s+(\d+(?:\.\d+)?)",  # "leva 3"
        r"leverage\s+(\d+(?:\.\d+)?)",  # "leverage 3"
    ]
    for pattern in leverage_patterns:
        m = re.search(pattern, lt, re.I)
        if m:
            try:
                lev_val = float(m.group(1))
                updates["leverage"] = lev_val
                break
            except:
                pass
    
    # Estrai sl/tp/risk_pct distinguendo il contesto
    # Stop loss / SL
    sl_patterns = [
        r"stop\s+loss[:\s]+(\d+(?:\.\d+)?)\s*%?",
        r"sl[:\s]+(\d+(?:\.\d+)?)\s*%?",
        r"stoploss[:\s]+(\d+(?:\.\d+)?)\s*%?",
    ]
    for pattern in sl_patterns:
        m = re.search(pattern, lt, re.I)
        if m:
            try:
                sl_val = float(m.group(1))
                updates["sl"] = sl_val
                break
            except:
                pass
    
    # Take profit / TP
    tp_patterns = [
        r"take\s+profit[:\s]+(\d+(?:\.\d+)?)\s*%?",
        r"tp[:\s]+(\d+(?:\.\d+)?)\s*%?",
        r"takeprofit[:\s]+(\d+(?:\.\d+)?)\s*%?",
    ]
    for pattern in tp_patterns:
        m = re.search(pattern, lt, re.I)
        if m:
            try:
                tp_val = float(m.group(1))
                updates["tp"] = tp_val
                break
            except:
                pass
    
    # Risk percentage
    risk_patterns = [
        r"rischio[:\s]+(\d+(?:\.\d+)?)\s*%?",
        r"risk[:\s]+(\d+(?:\.\d+)?)\s*%?",
        r"percentuale\s+rischio[:\s]+(\d+(?:\.\d+)?)\s*%?",
    ]
    for pattern in risk_patterns:
        m = re.search(pattern, lt, re.I)
        if m:
            try:
                risk_val = float(m.group(1))
                updates["risk_pct"] = risk_val
                break
            except:
                pass
    
    # Estrai periodi indicatori: ema/rsi/atr periodo
    # Pattern: "rsi 14", "ema=20", "atr periodo 7", "ema 200", "aggiungi rsi 20"
    indicator_mapping_periods = [("ema_period", "EMA", "ema"), ("rsi_period", "RSI", "rsi"), ("atr_period", "ATR", "atr")]
    for indicator_key, indicator_name, indicator_lower in indicator_mapping_periods:
        # Skip se già impostato per rimozione
        if indicator_key in updates and updates[indicator_key] is None:
            continue
        
        # Cerca pattern con indicatore esplicito (incluso "aggiungi rsi 20")
        patterns = [
            rf"{indicator_lower}\s+(\d+)",  # "rsi 14", "ema 200"
            rf"{indicator_lower}[=:](\d+)",  # "rsi=14", "ema:200"
            rf"{indicator_lower}\s+periodo\s+(\d+)",  # "rsi periodo 14"
            rf"periodo\s+{indicator_lower}\s+(\d+)",  # "periodo rsi 14"
            rf"aggiungi\s+{indicator_lower}\s+(\d+)",  # "aggiungi rsi 20"
            rf"metti\s+{indicator_lower}\s+(\d+)",  # "metti rsi 20"
        ]
        for pattern in patterns:
            m = re.search(pattern, lt, re.I)
            if m:
                try:
                    period = int(m.group(1))
                    if period > 0:
                        updates[indicator_key] = period
                        break
                except:
                    pass
            if indicator_key in updates:
                break
    
    # Estrai market_type
    if "spot" in lt and "futures" not in lt:
        updates["market_type"] = "spot"
    elif "futures" in lt or "perpetual" in lt:
        updates["market_type"] = "futures"
    
    # Se non abbiamo trovato nulla tramite pattern specifici, prova deduzione contestuale
    # (solo se non abbiamo già trovato qualcosa)
    if not updates:
        # Se contiene un numero con x, potrebbe essere leverage
        if re.search(r"\d+\s*x", lt):
            m = re.search(r"(\d+(?:\.\d+)?)\s*x", lt)
            if m:
                try:
                    updates["leverage"] = float(m.group(1))
                except:
                    pass
        # Se contiene un numero con %, prova a capire quale in base al contesto
        elif re.search(r"\d+(?:\.\d+)?\s*%", text):
            if any(p in lt for p in ["stop", "sl"]):
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
                if m:
                    try:
                        updates["sl"] = float(m.group(1))
                    except:
                        pass
            elif any(p in lt for p in ["take", "profit", "tp"]):
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
                if m:
                    try:
                        updates["tp"] = float(m.group(1))
                    except:
                        pass
            elif any(p in lt for p in ["rischio", "risk"]):
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
                if m:
                    try:
                        updates["risk_pct"] = float(m.group(1))
                    except:
                        pass
    
    return updates


def _is_greeting(user_text: str) -> bool:
    """Rileva se il messaggio dell'utente è un saluto."""
    lt = user_text.strip().lower()
    greeting_patterns = [
        "ciao", "hey", "buongiorno", "buonasera", "salve", "buon pomeriggio",
        "buonasera", "buondì", "ciau", "ehi", "hello", "hi"
    ]
    # Controlla se il testo è solo un saluto (senza altre parole significative)
    words = lt.split()
    # Se ci sono più di 2 parole, probabilmente non è solo un saluto
    if len(words) > 2:
        return False
    # Verifica se contiene un pattern di saluto
    return any(pattern in lt for pattern in greeting_patterns)

def _log_final_report(state: Dict[str, Any], context: str = ""):
    """Helper per loggare REPORT finale prima di return"""
    final_params = state.get("config_state", {}).get("params", {})
    final_step = state.get("config_state", {}).get("step", "unknown")
    logger.info(
        f"[REPORT_FINAL{('_' + context) if context else ''}] step={final_step}, strategy={final_params.get('strategy')}, "
        f"rsi_period={final_params.get('rsi_period')}, atr_period={final_params.get('atr_period')}, "
        f"ema_period={final_params.get('ema_period')}, timeframe={final_params.get('timeframe')}, "
        f"leverage={final_params.get('leverage')}, risk_pct={final_params.get('risk_pct')}, "
        f"sl={final_params.get('sl')}, tp={final_params.get('tp')}"
    )


def _wizard_seq_missing_required_fields(params: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    required = ["market_type", "symbol", "timeframe", "operating_mode", "sl", "tp", "risk_pct"]
    for field in required:
        value = params.get(field)
        if value is None or value == "":
            missing.append(field)
    if params.get("market_type") == "futures" and params.get("leverage") is None:
        missing.append("leverage")
    return missing


def _wizard_seq_pending_snapshot(cs: Dict[str, Any]) -> Dict[str, Any]:
    pending: Dict[str, Any] = {}
    if cs.get("pending_risk_confirmation") is not None:
        pending["risk_pct"] = cs.get("pending_risk_confirmation")
    if cs.get("pending_leverage_confirmation") is not None:
        pending["leverage"] = cs.get("pending_leverage_confirmation")
    if cs.get("pending_sl_confirmation") is not None:
        pending["sl"] = cs.get("pending_sl_confirmation")
    return pending


def _wizard_seq_tail_after_save(
    state: Dict[str, Any],
    cs: Dict[str, Any],
    params: Dict[str, Any],
    current_step: str,
    *,
    summary_followup: str,
) -> Dict[str, Any]:
    """After a wizard field is persisted: advance or mark configuration complete."""
    state, cs, params = _sync_state(state, cs, params)
    next_step = _get_next_step(current_step, params, cs)
    logger.info("[WIZARD_SEQ_NEXT] from=%s to=%s", current_step, next_step)
    if next_step is not None:
        cs["step"] = next_step
        state, cs, params = _sync_state(state, cs, params)
        return {"reply": _step_question(next_step, params), "state": state}

    missing = _wizard_seq_missing_required_fields(params)
    pending = _wizard_seq_pending_snapshot(cs)
    if missing or pending:
        logger.info("[WIZARD_COMPLETE_BLOCKED] missing=%s pending=%s", missing, pending)
        if missing:
            cs["step"] = missing[0]
        state, cs, params = _sync_state(state, cs, params)
        return {"reply": _step_question(cs.get("step", current_step), params), "state": state}

    if not is_config_complete(params):
        logger.warning("[WIZARD_SEQ] refuse complete: is_config_complete=False after wizard tail")
        mw = free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs)
        if mw is not None:
            cs["step"] = mw
            state["config_status"] = "in_progress"
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": _step_question(mw, params), "state": state}

    state["config_status"] = "complete"
    _cleanup_config_state_when_complete(cs)
    cs["step"] = None
    state["step"] = None
    state, cs, params = _sync_state(state, cs, params)
    return {
        "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + summary_followup,
        "state": state,
    }


def _wizard_seq_handle_message(
    user_text: str,
    state: Dict[str, Any],
    history: List[Dict[str, str]],
    system_prompt: str = "",
) -> Dict[str, Any]:
    _ = history
    _ = system_prompt
    state = _ensure_state(state)
    cs = state["config_state"]
    params = _coerce_params(cs.get("params"))
    cs["params"] = params

    current_step = cs.get("step") or "market_type"
    logger.info("[WIZARD_SEQ] step=%s input=%r", current_step, user_text)
    print("[CONFIG_CHECK]", dict(params), "complete:", is_config_complete(params))
    logger.info("[CONFIG_CHECK] params=%s complete=%s", dict(params), is_config_complete(params))

    faq_hit = _match_practical_faq(user_text)
    if faq_hit is not None:
        intent_id, faq_answer = faq_hit
        logger.info("[FAQ_MATCH] intent=%s", intent_id)
        guarded = _faq_info_complete_guard_response(faq_answer, state, cs, params)
        if guarded is not None:
            return guarded
        suffix, reconciled_step, ask_repeat_question = _faq_repeat_suffix(state, cs, params, current_step)
        logger.info(
            "[FAQ_REPLY] reconciled_step=%s ask_repeat_question=%s",
            reconciled_step,
            ask_repeat_question,
        )
        state, cs, params = _sync_state(state, cs, params)
        return {"reply": f"{faq_answer}\n\n{suffix}", "state": state, "skip_llm": True}

    # Priorità modifiche dirette su configurazione completa:
    # Solo se il flag persisted è complete E i params passano il controllo stretto is_config_complete.
    config_is_complete = _is_configuration_complete(state) and is_config_complete(params)
    if config_is_complete:
        pending_batch = _pending_batch_snapshot(cs)
        if pending_batch:
            amb = _ambiguous_modify_confirm_clarification(user_text, params, pending_batch)
            if amb:
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": amb, "state": state}
            merged_params, merged_pending, resolve_errors = resolve_input(params, pending_batch, user_text)
            applied_something = merged_params != params or merged_pending != pending_batch
            if resolve_errors:
                params = merged_params
                cs["pending_sl_confirmation"] = merged_pending.get("sl")
                cs["pending_risk_confirmation"] = merged_pending.get("risk_pct")
                cs["pending_leverage_confirmation"] = merged_pending.get("leverage")
                if "sl" not in merged_pending:
                    cs["suggested_sl"] = None
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                reply = _append_pending_confirmation_remnant_to_error_reply(" ".join(resolve_errors), cs)
                return {"reply": reply, "state": state}
            if applied_something:
                logger.info(
                    "[PENDING_RESOLVE_APPLY] pending_before=%s pending_after=%s",
                    pending_batch,
                    merged_pending,
                )
                params = merged_params
                cs["pending_sl_confirmation"] = merged_pending.get("sl")
                cs["pending_risk_confirmation"] = merged_pending.get("risk_pct")
                cs["pending_leverage_confirmation"] = merged_pending.get("leverage")
                if "sl" not in merged_pending:
                    cs["suggested_sl"] = None

                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                step = cs.get("step")
                complete_ok = is_config_complete(params)
                print("[POST_CONFIRM_CHECK]", dict(params), "complete:", complete_ok, "step:", step)
                logger.info(
                    "[POST_CONFIRM_CHECK] params=%s complete=%s step=%s",
                    dict(params),
                    complete_ok,
                    step,
                )
                if complete_ok:
                    state["config_status"] = "complete"
                    _cleanup_config_state_when_complete(cs)
                    cs["step"] = None
                    state["step"] = None
                    state, cs, params = _sync_state(state, cs, params)
                    return {
                        "reply": "Configurazione completata ✅\n\n"
                        + _build_summary(params)
                        + "\n\nVuoi modificare qualcosa o avviare il bot adesso?",
                        "state": state,
                    }
                state["config_status"] = "in_progress"
                _recompute_step(cs)
                state, cs, params = _sync_state(state, cs, params)
                next_ask = cs.get("step") or free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs) or "market_type"
                cs["step"] = next_ask
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": _step_question(next_ask, params), "state": state}
            if re.search(r"\bconferm\w*\b", user_text.strip().lower(), re.I):
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Mi dici cosa vuoi confermare esattamente? (es. 'confermo leva' o 'confermo sl')",
                    "state": state,
                }
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": _build_pending_batch_confirmation_prompt(cs), "state": state}

        updates = _extract_modification_requests(user_text, params, current_step=current_step)
        is_explicit_modification = _is_explicit_modification_request(user_text)
        lt_user = user_text.strip().lower()

        # Supporta anche input tipo "metti ethusdt" (senza keyword "coppia/symbol")
        # quando la richiesta è chiaramente una modifica esplicita.
        if "symbol" not in updates and is_explicit_modification:
            symbol_match = SYMBOL_RE.search(user_text.strip())
            if symbol_match:
                normalized_symbol = _normalize_symbol(symbol_match.group(1))
                if normalized_symbol:
                    updates["symbol"] = normalized_symbol

        if updates:
            # "aggressiv/equilibrat/selettiv" devono valere come alias espliciti.
            if "operating_mode" in updates and _parse_operating_mode(lt_user) is None:
                updates.pop("operating_mode", None)

            allowed_complete_updates = {
                "symbol",
                "timeframe",
                "tp",
                "sl",
                "risk_pct",
                "operating_mode",
                "market_type",
                "leverage",
            }
            selected_updates = {k: v for k, v in updates.items() if k in allowed_complete_updates}
            immediate_patch: Dict[str, Any] = {}
            risky_updates: Dict[str, Any] = {}

            # Multi-parametro: validare ogni campo prima di pending / apply (nessun valore invalido in pending).
            batch_validation_errors: List[str] = []
            for key, value in selected_updates.items():
                is_ok, err_msg, _ = _validate_step_value(key, value, params)
                if not is_ok and err_msg:
                    batch_validation_errors.append(err_msg)
            if batch_validation_errors:
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": " ".join(batch_validation_errors), "state": state}

            # Regola post-config: sl/risk numerici alti e leva con warning → pending batch.
            for key, value in selected_updates.items():
                if key == "leverage":
                    lev_int = _parse_user_leverage_int(value)
                    if lev_int is None:
                        immediate_patch[key] = value
                        continue
                    sym = params.get("symbol") or "questa coppia"
                    req_confirm, _ = _check_leverage_warning(lev_int, sym)
                    if req_confirm:
                        risky_updates[key] = float(lev_int)
                        continue
                    immediate_patch[key] = lev_int
                    continue
                if key in ("sl", "risk_pct"):
                    try:
                        numeric_val = float(str(value).replace("%", "").replace(",", "."))
                    except Exception:
                        immediate_patch[key] = value
                        continue
                    if numeric_val >= 4:
                        risky_updates[key] = numeric_val
                        continue
                immediate_patch[key] = value

            if immediate_patch:
                patch_result = apply_config_patch(cs, immediate_patch)
                if not patch_result.get("ok", True):
                    params = cs.get("params", {})
                    state, cs, params = _sync_state(state, cs, params)
                    reply = patch_result.get("message") or "Non sono riuscito ad applicare la modifica richiesta."
                    return {"reply": reply, "state": state}
                params = cs.get("params", {})

            if risky_updates:
                _clear_pending_confirmation_batch(cs)
                if "sl" in risky_updates:
                    cs["pending_sl_confirmation"] = float(risky_updates["sl"])
                if "risk_pct" in risky_updates:
                    cs["pending_risk_confirmation"] = float(risky_updates["risk_pct"])
                if "leverage" in risky_updates:
                    cs["pending_leverage_confirmation"] = int(float(risky_updates["leverage"]))
                logger.info("[PENDING_BATCH_SET] pending=%s", _pending_batch_snapshot(cs))
                state["config_status"] = "complete" if is_config_complete(params) else "in_progress"
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": _build_pending_batch_confirmation_prompt(cs), "state": state}

            if immediate_patch:
                if is_config_complete(params):
                    state["config_status"] = "complete"
                    _cleanup_config_state_when_complete(cs)
                    cs["step"] = None
                    state["step"] = None
                    state, cs, params = _sync_state(state, cs, params)
                    reply = "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi modificare qualcosa o avviare il bot adesso?"
                    return {"reply": reply, "state": state}
                state["config_status"] = "in_progress"
                _recompute_step(cs)
                state, cs, params = _sync_state(state, cs, params)
                next_ask = cs.get("step") or free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs) or "symbol"
                cs["step"] = next_ask
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": _step_question(next_ask, params), "state": state}

    if is_informational_question(user_text):
        logger.info(
            "[WIZARD_INFO_Q] step=%s text=%r ai_then_repeat_step=True",
            current_step,
            user_text,
        )
        step_reply = _step_question(current_step, params)
        ai_reply = ""
        try:
            from . import llm_client

            ai_reply = (llm_client.wizard_config_question_answer(user_text, current_step, params) or "").strip()
        except Exception as exc:
            logger.warning("[WIZARD_INFO_Q] llm_failed: %s", exc, exc_info=True)
        if not ai_reply:
            ai_reply = _informational_answer_fallback(user_text)
        guarded = _faq_info_complete_guard_response(ai_reply, state, cs, params)
        if guarded is not None:
            return guarded
        state, cs, params = _sync_state(state, cs, params)
        return {"reply": f"{ai_reply}\n\n{step_reply}", "state": state}

    pending_key_map = {
        "risk_pct": "pending_risk_confirmation",
        "leverage": "pending_leverage_confirmation",
        "sl": "pending_sl_confirmation",
    }
    pending_key = pending_key_map.get(current_step)
    if pending_key and cs.get(pending_key) is not None:
        # Leva: priorità al nuovo valore numerico rispetto a ripetere warning o a "ok/sì" ambigui
        if current_step == "leverage" and cs.get("pending_leverage_confirmation") is not None:
            new_lev_value = _extract_step_value(user_text, "leverage", params)
            if new_lev_value is not None:
                is_valid, error_msg, _ = _validate_step_value("leverage", new_lev_value, params)
                if not is_valid:
                    logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": (error_msg or _step_question(current_step, params)), "state": state}
                lev_int = int(new_lev_value)
                sym = params.get("symbol") or "questa coppia"
                cs["pending_leverage_confirmation"] = lev_int
                requires_confirm, warning_msg = _check_leverage_warning(lev_int, sym)
                if requires_confirm:
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": warning_msg or _step_question(current_step, params), "state": state}
                params["leverage"] = lev_int
                cs["pending_leverage_confirmation"] = None
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return _wizard_seq_tail_after_save(
                    state,
                    cs,
                    params,
                    current_step,
                    summary_followup="\n\nVuoi avviare il bot adesso?",
                )

        if current_step == "risk_pct" and cs.get("pending_risk_confirmation") is not None:
            new_risk_value = _extract_step_value(user_text, "risk_pct", params)
            if new_risk_value is not None:
                is_valid, error_msg, _ = _validate_step_value("risk_pct", new_risk_value, params)
                if not is_valid:
                    logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": (error_msg or _step_question(current_step, params)), "state": state}
                risk_value = float(str(new_risk_value).replace("%", "").replace(",", "."))
                requires_confirm, warning_msg = _check_risk_warning(
                    risk_value, params.get("market_type", "futures")
                )
                if requires_confirm:
                    cs["pending_risk_confirmation"] = risk_value
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": warning_msg or _step_question(current_step, params), "state": state}
                params["risk_pct"] = risk_value
                cs["pending_risk_confirmation"] = None
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return _wizard_seq_tail_after_save(
                    state,
                    cs,
                    params,
                    current_step,
                    summary_followup="\n\nVuoi avviare il bot adesso?",
                )

        if current_step == "sl" and cs.get("pending_sl_confirmation") is not None:
            new_sl_value = _extract_step_value(user_text, "sl", params)
            if new_sl_value is None:
                lt_pending = user_text.strip().lower()
                if re.search(r"\d+(?:\.\d+)?\s*%", user_text):
                    has_other_param_context = any(
                        token in lt_pending
                        for token in ["take profit", "tp", "rischio", "risk", "leva", "leverage"]
                    )
                    if not has_other_param_context:
                        m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", user_text)
                        if m_pct:
                            new_sl_value = f"{m_pct.group(1)}%"
            if new_sl_value is not None:
                sl_val = float(str(new_sl_value).replace("%", "").replace(",", "."))
                is_valid, error_msg, _ = _validate_step_value("sl", new_sl_value, params)
                if not is_valid:
                    logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": (error_msg or _step_question(current_step, params)), "state": state}
                requires_confirm, warning_msg, suggested_sl = _check_sl_warning(sl_val)
                if requires_confirm:
                    cs["pending_sl_confirmation"] = sl_val
                    cs["suggested_sl"] = suggested_sl
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": warning_msg or _step_question(current_step, params), "state": state}
                params["sl"] = f"{sl_val}%"
                cs["pending_sl_confirmation"] = None
                cs["suggested_sl"] = None
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return _wizard_seq_tail_after_save(
                    state,
                    cs,
                    params,
                    current_step,
                    summary_followup="\n\nVuoi avviare il bot adesso?",
                )

        confirmation = _extract_confirmation(user_text)
        if confirmation is True:
            pending_value = cs.get(pending_key)
            logger.info("[WIZARD_SEQ_SAVE] step=%s value=%r", current_step, pending_value)
            if current_step == "risk_pct":
                params["risk_pct"] = float(str(pending_value).replace("%", "").replace(",", "."))
                cs["pending_risk_confirmation"] = None
            elif current_step == "leverage":
                params["leverage"] = int(float(pending_value))
                cs["pending_leverage_confirmation"] = None
            elif current_step == "sl":
                sl_val = float(str(pending_value).replace("%", "").replace(",", "."))
                params["sl"] = f"{sl_val}%"
                cs["pending_sl_confirmation"] = None
                cs["suggested_sl"] = None
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            step = cs.get("step")
            print(
                "[POST_CONFIRM_CHECK]",
                dict(params),
                "complete:",
                is_config_complete(params),
                "step:",
                step,
            )
            logger.info(
                "[POST_CONFIRM_CHECK] params=%s complete=%s step=%s",
                dict(params),
                is_config_complete(params),
                step,
            )
            return _wizard_seq_tail_after_save(
                state,
                cs,
                params,
                current_step,
                summary_followup="\n\nVuoi avviare il bot adesso?",
            )
        elif confirmation is False:
            cs[pending_key] = None
            if current_step == "sl":
                cs["suggested_sl"] = None
            logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": _step_question(current_step, params), "state": state}
        else:
            if current_step == "risk_pct":
                _, warning_msg = _check_risk_warning(float(cs.get("pending_risk_confirmation")), params.get("market_type", "futures"))
            elif current_step == "leverage":
                _, warning_msg = _check_leverage_warning(int(float(cs.get("pending_leverage_confirmation"))), params.get("symbol") or "questa coppia")
            else:
                _, warning_msg, _ = _check_sl_warning(float(cs.get("pending_sl_confirmation")))
            logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": warning_msg or _step_question(current_step, params), "state": state}

    extracted_value = _extract_step_value(user_text, current_step, params)
    if extracted_value is None:
        logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
        state, cs, params = _sync_state(state, cs, params)
        return {"reply": _step_question(current_step, params), "state": state}

    is_valid, error_msg, _warning_msg = _validate_step_value(current_step, extracted_value, params)
    if not is_valid:
        logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
        state, cs, params = _sync_state(state, cs, params)
        return {"reply": (error_msg or _step_question(current_step, params)), "state": state}

    if current_step == "risk_pct":
        risk_value = float(str(extracted_value).replace("%", "").replace(",", "."))
        requires_confirm, warning_msg = _check_risk_warning(risk_value, params.get("market_type", "futures"))
        if requires_confirm:
            cs["pending_risk_confirmation"] = risk_value
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": warning_msg or _step_question(current_step, params), "state": state}
        params["risk_pct"] = risk_value
    elif current_step == "leverage":
        lev_int = int(float(extracted_value))
        requires_confirm, warning_msg = _check_leverage_warning(lev_int, params.get("symbol") or "questa coppia")
        if requires_confirm:
            cs["pending_leverage_confirmation"] = lev_int
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": warning_msg or _step_question(current_step, params), "state": state}
        params["leverage"] = lev_int
    elif current_step == "sl":
        sl_val = float(str(extracted_value).replace("%", "").replace(",", "."))
        requires_confirm, warning_msg, suggested_sl = _check_sl_warning(sl_val)
        if requires_confirm:
            cs["pending_sl_confirmation"] = sl_val
            cs["suggested_sl"] = suggested_sl
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": warning_msg or _step_question(current_step, params), "state": state}
        params["sl"] = f"{sl_val}%"
        cs["pending_sl_confirmation"] = None
        cs["suggested_sl"] = None
    elif current_step == "tp":
        patch_result = apply_config_patch(cs, {"tp": extracted_value})
        if not patch_result.get("ok", True):
            logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": patch_result.get("message", _step_question(current_step, params)), "state": state}
        params = cs["params"].copy()
    elif current_step == "symbol":
        patch_result = apply_config_patch(cs, {"symbol": extracted_value})
        if not patch_result.get("ok", True):
            logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": patch_result.get("message", _step_question(current_step, params)), "state": state}
        params = cs["params"].copy()
    elif current_step == "timeframe":
        patch_result = apply_config_patch(cs, {"timeframe": extracted_value})
        if not patch_result.get("ok", True):
            logger.info("[WIZARD_SEQ_INVALID] step=%s repeat", current_step)
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": patch_result.get("message", _step_question(current_step, params)), "state": state}
        params = cs["params"].copy()
    elif current_step == "operating_mode":
        params = _apply_operating_mode_preset(params, str(extracted_value))
    elif current_step == "market_type":
        params["market_type"] = extracted_value
        if extracted_value == "spot":
            params["leverage"] = None
            cs["pending_leverage_confirmation"] = None
    else:
        params[current_step] = extracted_value

    logger.info("[WIZARD_SEQ_SAVE] step=%s value=%r", current_step, extracted_value)
    state, cs, params = _sync_state(state, cs, params)

    return _wizard_seq_tail_after_save(
        state,
        cs,
        params,
        current_step,
        summary_followup="\n\nVuoi avviare il bot adesso?",
    )

def handle_message(user_text: str, state: Dict[str, Any], history: List[Dict[str, str]], system_prompt: str = "") -> Dict[str, Any]:
    """
    FSM sequenziale: garantisce SEMPRE la stessa sequenza e UNA domanda alla volta.
    Sequenza: market_type → symbol → timeframe → operating_mode → sl → tp → risk_pct → leverage (solo futures)
    """
    user_lower_global = user_text.strip().lower()
    global_reset_commands = {
        "resetta",
        "reset",
        "resetta configurazione",
        "reset configurazione",
        "ricomincia",
        "nuova configurazione",
    }
    if user_lower_global in global_reset_commands or "reset configurazione" in user_lower_global:
        state = _ensure_state(state)
        state["config_status"] = "in_progress"
        state["config_state"] = {
            "step": "market_type",
            "params": copy.deepcopy(DEFAULT_PARAMS),
            "error_count": {},
            "pending_risk_confirmation": None,
            "pending_leverage_confirmation": None,
            "pending_sl_confirmation": None,
            "suggested_sl": None,
        }
        state.pop("params", None)
        return {
            "reply": _step_question("market_type", {}),
            "state": state,
        }
    return _wizard_seq_handle_message(user_text, state, history, system_prompt)
    reply = ""
    orch_error_code: Optional[str] = None
    wizard_parallel_errors: Dict[str, str] = {}
    wizard_parallel_success_msgs: List[str] = []
    state = _ensure_state(state)
    cs = state["config_state"]
    # Assicura che params sia sempre un dict valido con tutte le chiavi
    params = _coerce_params(cs.get("params"))
    cs["params"] = params
    current_step = cs.get("step", "market_type")
    if state.get("config_status") == "in_progress":
        logger.info("[WIZARD_STEP] before processing: cs[step]=%s current_step=%s", cs.get("step"), current_step)

    user_message = user_text
    current_config = dict(params)
    new_config, _v2_next = process_message_v2(user_message, current_config)
    v2_applied_keys = set(new_config.keys())
    if "timeframe" in new_config:
        tf = new_config.get("timeframe")
        from idith.validators import validate_timeframe

        tf_ok, _ = validate_timeframe(str(tf)) if tf is not None else (False, None)
        if not tf_ok:
            new_config.pop("timeframe", None)
    stripped_high_risk_pending = False
    popped_risk_while_pending = False
    new_risk = new_config.get("risk_pct")
    if new_risk is not None:
        # Pending rischio già attivo: non far riscrivere pending dal vecchio risk_pct ripassato da v2 (BUG3).
        if cs.get("pending_risk_confirmation") is not None:
            new_config.pop("risk_pct", None)
            popped_risk_while_pending = True
        else:
            mt = new_config.get("market_type") or current_config.get("market_type") or "futures"
            requires_risk_confirm, _ = _check_risk_warning(float(new_risk), mt)
            if requires_risk_confirm:
                new_config.pop("risk_pct", None)
                cs["pending_risk_confirmation"] = float(new_risk)
                stripped_high_risk_pending = True
    skip_blind_v2_params_merge = _is_explicit_modification_request(user_text)
    # BUG3: come risk_pct — non applicare subito leverage da v2 se serve conferma (es. 7x)
    # Solo col merge v2 "cieco": con modifica esplicita il ramo dedicato gestisce conferme
    stripped_high_leverage_pending = False
    popped_leverage_while_pending = False
    if not skip_blind_v2_params_merge:
        new_lev = new_config.get("leverage")
        if new_lev is not None:
            mt_lev = new_config.get("market_type") or current_config.get("market_type") or "futures"
            if mt_lev == "futures":
                try:
                    lev_int = int(new_lev) if isinstance(new_lev, int) else int(float(new_lev))
                except (TypeError, ValueError):
                    lev_int = None
                if lev_int is not None:
                    sym_for_warn = current_config.get("symbol") or params.get("symbol") or "questa coppia"
                    prev_lev_raw = current_config.get("leverage")
                    try:
                        prev_lev_int = (
                            int(prev_lev_raw)
                            if isinstance(prev_lev_raw, int)
                            else int(float(prev_lev_raw))
                            if prev_lev_raw is not None
                            else None
                        )
                    except (TypeError, ValueError):
                        prev_lev_int = None
                    # Stessa leva già committata: non riaprire BUG3 sul solo merge v2 (es. conferma rischio mentre leva è già 10x).
                    if prev_lev_int is not None and prev_lev_int == lev_int:
                        pass
                    # Pending leva già attivo: non sovrascrivere pending col leverage ripassato da v2 (BUG3 conferma).
                    elif cs.get("pending_leverage_confirmation") is not None:
                        new_config.pop("leverage", None)
                        popped_leverage_while_pending = True
                    else:
                        requires_lev_confirm, _ = _check_leverage_warning(lev_int, sym_for_warn)
                        if requires_lev_confirm:
                            new_config.pop("leverage", None)
                            cs["pending_leverage_confirmation"] = lev_int
                            stripped_high_leverage_pending = True
    if not skip_blind_v2_params_merge:
        config_state = new_config
        params.clear()
        params.update(new_config)
        if stripped_high_risk_pending or popped_risk_while_pending:
            params["risk_pct"] = current_config.get("risk_pct")
        if stripped_high_leverage_pending or popped_leverage_while_pending:
            params["leverage"] = current_config.get("leverage")
        # leverage è già in new_config se non richiede conferma; niente re-forzatura extra
    if params.get("operating_mode"):
        params = _apply_operating_mode_preset(params, params["operating_mode"])
    mw_init = free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs)
    if mw_init is not None:
        cs["step"] = mw_init
    state, cs, params = _sync_state(state, cs, params)
    current_step = cs.get("step", STEPS[0])

    # ============================================================
    # LOCAL SYMBOL UPDATE (deterministico, prima di intent/normalized/openai)
    # Se user_text contiene un ticker valido: aggiorna sempre params["symbol"].
    # Return solo se il messaggio è "solo simbolo"; altrimenti continua il flusso (es. sl/tp/coppia).
    # ============================================================
    extracted_symbol = extract_symbol(user_text)
    if extracted_symbol is not None:
        market_type = params.get("market_type") or "futures"
        if validators.is_symbol_listed(None, market_type, extracted_symbol):
            params["symbol"] = extracted_symbol
            if "symbol" in cs:
                cs["symbol"] = extracted_symbol
            state, cs, params = _sync_state(state, cs, params)
            # Messaggio "solo simbolo" = testo normalizzato uguale al symbol oppure un solo token che è il symbol
            clean = user_text.strip().upper()
            tokens = clean.split()
            symbol_only_message = (
                (clean == extracted_symbol)
                or (len(tokens) == 1 and validators.normalize_symbol_strict(tokens[0]) == extracted_symbol)
            )
            if symbol_only_message:
                logger.info("[LOCAL_UPDATE] symbol -> %s", extracted_symbol)
                reply = f"Hai confermato {extracted_symbol}."
                return {"reply": reply, "state": state}
            else:
                logger.info("[LOCAL_UPDATE] symbol -> %s (continuing)", extracted_symbol)
                # Non fare return: lascia proseguire il parsing per SL/TP/risk ecc.
        else:
            # Symbol invalido: se è richiesta modifica esplicita (es. "metti sl 2 tp 2 AAAUSDT"),
            # NON ritornare qui: lascia proseguire a GESTIONE MODIFICA PARAMETRI che applicherà
            # i parametri validi (sl, tp) e includerà symbol negli errori nella reply.
            if (
                not _is_explicit_modification_request(user_text)
                and not _message_looks_like_mixed_config(user_text)
            ):
                reply = (
                    f"La coppia '{extracted_symbol}' non esiste su Bybit {market_type.capitalize()}. "
                    "Ricontrolla il simbolo e riprova."
                )
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": reply, "state": state}
            logger.info(
                "[LOCAL_UPDATE] invalid symbol ignored (mixed or explicit mod): %s",
                extracted_symbol,
            )

    # ============================================================
    # LOG STEP_TRACE - Dopo lettura current_step
    # ============================================================
    config_status = state.get("config_status", "unknown")
    params_snapshot = dict(params)  # Snapshot per il log
    logger.info(f"[STEP_TRACE] step={current_step} user_text={user_text!r} config_status={config_status} params_snapshot={params_snapshot}")
    
    # ============================================================
    # LOG PUNTO A) - Dopo inizializzazione cs, params, cs["params"]
    # ============================================================
    logger.info(
        f"[ANALYSIS_A] After init: current_step={current_step}, cs['step']={cs.get('step')}, "
        f"params_strategy={params.get('strategy')}, params_rsi_period={params.get('rsi_period')}, "
        f"params_atr_period={params.get('atr_period')}, params_ema_period={params.get('ema_period')}, "
        f"error_count={cs.get('error_count', {})}, params_full={params}"
    )
    
    # GLOBAL RESET CONFIG: comando "resetta configurazione" disponibile in qualsiasi stato
    user_lower_global = user_text.strip().lower()
    if "resetta configurazione" in user_lower_global:
        state["config_status"] = "in_progress"
        state["config_state"] = {
            "step": "market_type",
            "params": copy.deepcopy(DEFAULT_PARAMS),
            "error_count": {},
        }
        state.pop("params", None)
        return {
            "reply": "Ho resettato la configurazione. Iniziamo da capo.\n\n" + _step_question("market_type", {}),
            "state": state,
        }
    
    # ============================================================
    # GLOBAL INTERRUPT: CAMBIO STRATEGIA (alta priorità)
    # Se l'utente seleziona 1-4 o chiede di cambiare strategia,
    # applica subito e chiedi periodi mancanti. Funziona da qualsiasi step
    # (timeframe, strategy_params, ecc.) purché market_type e symbol siano già scelti.
    # ============================================================
    # Riconosci richiesta cambio strategia
    strategy_choice = _parse_strategy_choice(user_text)
    change_request = detect_strategy_change(user_text)
    
    # Se riconosci una scelta strategia (1-4 o parole chiave) o richiesta cambio esplicito
    if (strategy_choice is not None or 
        (change_request and change_request.get("mode") == "choice")) and \
        _is_step_filled("market_type", params) and _is_step_filled("symbol", params):
        
        # Usa strategy_choice se disponibile, altrimenti usa change_request
        if strategy_choice is not None:
            target_strategy_id = strategy_choice
        elif change_request and change_request.get("mode") == "choice":
            target_strategy_id = change_request.get("choice")
        else:
            target_strategy_id = None
        
        if target_strategy_id and 1 <= target_strategy_id <= 4:
            # Applica preset: imposta free_strategy_id (memo UI) e azzera periodi non previsti
            old_strategy_id = params.get("free_strategy_id")
            params = free_plan.apply_free_strategy_to_params(params, target_strategy_id)
            # Dopo un cambio strategia la configurazione torna in_progress finché non sono
            # stati raccolti tutti i nuovi parametri richiesti
            prev_status = state.get("config_status")
            state["config_status"] = "in_progress"
            logger.info(
                f"[CONFIG_STATUS_RESET] from={prev_status} to=in_progress "
                f"reason=strategy_change global_interrupt old_strategy_id={old_strategy_id} new_strategy_id={target_strategy_id}"
            )
            # Allinea sempre il campo derivato strategy ai periodi
            params = recompute_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            
            # Chiedi periodi mancanti per la nuova strategia
            period_question = free_plan.next_missing_period_question(params)
            if period_question:
                field, question = period_question
                cs["step"] = "strategy_params"
                state, cs, params = _sync_state(state, cs, params)
                logger.info(
                    f"[STRATEGY_CHANGE] mode=global_interrupt strategy_id={target_strategy_id} "
                    f"next_missing_field={field} params_snapshot={params}"
                )
                return {"reply": question, "state": state}
            else:
                # Tutti i periodi richiesti sono già presenti: avanza al prossimo step normale
                next_step = _get_next_step("strategy", params, cs)
                if next_step and next_step not in ("strategy", "strategy_params"):
                    cs["step"] = next_step
                    state, cs, params = _sync_state(state, cs, params)
                    reply = _build_summary(params) + "\n\n" + _step_question(next_step, params)
                else:
                    # Nessun prossimo step specifico: resta su strategy_params per eventuali micro‑aggiustamenti
                    cs["step"] = "strategy_params"
                    state, cs, params = _sync_state(state, cs, params)
                    reply = _build_summary(params) + "\n\n" + _step_question("strategy_params", params)
                return {"reply": reply, "state": state}
    
    # Gestione cambio strategia tramite aggiungi/togli indicatori (toggle)
    if (
        change_request
        and change_request.get("mode") == "toggle"
        and _is_step_filled("market_type", params)
        and _is_step_filled("symbol", params)
    ):
        ok, result, target_indicators = apply_strategy_change(params, change_request)
        if not ok:
            # Messaggio di errore già pronto in result
            return {"reply": result, "state": state}
        
        # Usa il dict risultante come nuovi params e riallinea strategy/free_strategy_id
        params = result
        # BUGFIX: prima derivavamo la combinazione dagli unici periodi già valorizzati,
        # quindi dopo "aggiungi EMA" ma prima del periodo EMA la strategia rimaneva RSI+ATR
        # con free_strategy_id=1. Ora usiamo la combinazione target esplicita.
        if target_indicators:
            strategy_list = _normalize_strategy_list(list(target_indicators))
        else:
            strategy_list = derive_strategy(params)
        strategy_id = _strategy_list_to_free_strategy_id(strategy_list)
        # Aggiorna memo UI (free_strategy_id) e anche il campo derivato strategy
        # usando direttamente la combinazione target, in modo da includere subito
        # gli indicatori aggiunti (es. EMA) senza attendere il periodo.
        if strategy_id is not None:
            params["free_strategy_id"] = strategy_id
        params["strategy"] = strategy_list
        
        # Dopo un cambio strategia la configurazione torna in_progress finché non sono
        # stati raccolti tutti i nuovi parametri richiesti
        prev_status = state.get("config_status")
        state["config_status"] = "in_progress"
        logger.info(
            f"[CONFIG_STATUS_RESET] from={prev_status} to=in_progress "
            f"reason=strategy_toggle global_interrupt new_strategy_id={strategy_id} strategy_list={strategy_list}"
        )
        
        # Sincronizza state prima di chiedere i periodi mancanti
        state, cs, params = _sync_state(state, cs, params)
        
        # Chiedi periodi mancanti per la nuova strategia
        period_question = free_plan.next_missing_period_question(params)
        if period_question:
            field, question = period_question
            cs["step"] = "strategy_params"
            state, cs, params = _sync_state(state, cs, params)
            logger.info(
                f"[STRATEGY_TOGGLE] mode=global_interrupt strategy_id={strategy_id} "
                f"next_missing_field={field} params_snapshot={params}"
            )
            return {"reply": question, "state": state}
        else:
            # Tutti i periodi richiesti sono già presenti: avanza al prossimo step normale
            next_step = _get_next_step("strategy", params, cs)
            if next_step and next_step not in ("strategy", "strategy_params"):
                cs["step"] = next_step
                state, cs, params = _sync_state(state, cs, params)
                reply = _build_summary(params) + "\n\n" + _step_question(next_step, params)
            else:
                # Nessun prossimo step specifico: resta su strategy_params per eventuali micro‑aggiustamenti
                cs["step"] = "strategy_params"
                state, cs, params = _sync_state(state, cs, params)
                reply = _build_summary(params) + "\n\n" + _step_question("strategy_params", params)
            return {"reply": reply, "state": state}
    
    # Se l'utente chiede solo "cambia strategia"/"strategie disponibili" senza specificare quale
    if change_request and change_request.get("mode") == "prompt" and \
       _is_step_filled("market_type", params) and _is_step_filled("symbol", params):
        # Riporta a step strategy per scegliere
        cs["step"] = "strategy"
        state, cs, params = _sync_state(state, cs, params)
        return {
            "reply": _step_question("strategy", params),
            "state": state
        }
    
    # ============================================================
    # GESTIONE RIMOZIONE INDICATORI (direttamente dal testo utente)
    # ============================================================
    remove_list = _extract_remove_indicators(user_text)
    # Evita doppio handling se la stessa frase è già stata gestita come cambio strategia toggle
    if remove_list and not (change_request and change_request.get("mode") == "toggle"):
        # Usa la funzione unificata per:
        # - azzerare i periodi degli indicatori rimossi
        # - aggiornare params["strategy"] e free_strategy_id in modo coerente
        params = apply_strategy_update(params, remove=remove_list)
        
        logger.info(f"[REMOVE_IND] remove={remove_list} strategy_after={params.get('strategy')} "
                    f"rsi_period={params.get('rsi_period')} atr_period={params.get('atr_period')} ema_period={params.get('ema_period')}")
        
        # Sincronizza state dopo rimozione indicatori
        state, cs, params = _sync_state(state, cs, params)
    
    # ============================================================
    # GESTIONE MODIFICA PARAMETRI (PRIMA DI TUTTO)
    # ============================================================
    # Se l'utente chiede di modificare un parametro, gestiscilo qui
    # Funziona sia durante la configurazione che quando è completa
    if _is_explicit_modification_request(user_text):
        updates = _extract_modification_requests(user_text, params, current_step)
        if cs.get("pending_sl_confirmation") is not None and isinstance(updates, dict):
            # Caso reale: con pending SL attivo, frasi tipo "metti 3%" devono valere come nuovo SL
            # anche se il parser di modifica esplicita non rileva "sl" o inferisce altro (es. operating_mode).
            lt_updates = user_text.strip().lower()
            has_sl_update = "sl" in updates
            if not has_sl_update and re.search(r"\d+(?:\.\d+)?\s*%", user_text):
                has_other_param_context = any(
                    token in lt_updates
                    for token in ["take profit", "tp", "rischio", "risk", "leva", "leverage"]
                )
                if not has_other_param_context:
                    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", user_text)
                    if m_pct:
                        try:
                            updates.pop("operating_mode", None)
                            updates["sl"] = float(m_pct.group(1))
                        except Exception:
                            pass
        if cs.get("pending_risk_confirmation") is not None and isinstance(updates, dict):
            # Pending rischio attivo: frasi tipo "metti 1%" devono aggiornare il rischio,
            # evitando che "1" venga interpretato come operating_mode.
            lt_updates = user_text.strip().lower()
            has_risk_update = "risk_pct" in updates
            if not has_risk_update and re.search(r"\d+(?:\.\d+)?\s*%", user_text):
                has_other_param_context = any(
                    token in lt_updates
                    for token in ["stop loss", "stoploss", "sl", "take profit", "tp", "leva", "leverage"]
                )
                if not has_other_param_context:
                    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", user_text)
                    if m_pct:
                        try:
                            updates.pop("operating_mode", None)
                            updates["risk_pct"] = float(m_pct.group(1))
                        except Exception:
                            pass
        
        if updates:
            # Raccogli errori, conferme e update validi
            errors = {}  # {param_name: error_message}
            requires_confirmation_list = {}  # {param_name: (confirmation_msg, suggested_value)}
            applied_updates = {}  # {param_name: (old_value, new_value)}
            old_values = {}  # Per tracciare i valori precedenti
            
            # Prima passata: valida tutti gli update e raccogli errori/conferme
            for param_name, new_value in updates.items():
                old_values[param_name] = params.get(param_name)
                
                # Gestione rimozione indicatori
                if param_name in ["ema_period", "rsi_period", "atr_period"] and new_value is None:
                    # Rimozione: non serve validazione, procediamo direttamente
                    continue
                
                # Valida il nuovo valore usando le stesse validazioni del flusso normale
                step_for_validation = param_name
                if param_name in ["ema_period", "rsi_period", "atr_period"]:
                    step_for_validation = param_name
                
                # Valida il valore
                is_valid, error_msg, warning_msg = _validate_step_value(step_for_validation, new_value, params)
                
                if not is_valid:
                    # Costruisci messaggio di errore con alternative valide
                    error_reply = ""
                    
                    # Messaggi specifici per tipo di parametro
                    if param_name == "ema_period":
                        error_reply = f"Ok 👍 però {new_value} è fuori range: per EMA accetto solo valori tra 5 e 500 (es. 5–500).\nVuoi usare 200 (consigliato) oppure dimmi tu un numero tra 5 e 500?"
                    elif param_name == "rsi_period":
                        error_reply = f"Ok 👍 però {new_value} è fuori range: per RSI accetto solo valori tra 5 e 100 (es. 5–100).\nVuoi usare 14 (consigliato) oppure dimmi tu un numero tra 5 e 100?"
                    elif param_name == "atr_period":
                        error_reply = f"Ok 👍 però {new_value} è fuori range: per ATR accetto solo valori tra 5 e 100 (es. 5–100).\nVuoi usare 14 (consigliato) oppure dimmi tu un numero tra 5 e 100?"
                    elif param_name == "timeframe":
                        market_type = params.get("market_type", "futures")
                        valid_tfs = validators.get_valid_timeframes(None, market_type)
                        tf_list = ", ".join(sorted(valid_tfs, key=lambda x: (
                            int(x[:-1]) if x[:-1].isdigit() else 999,
                            x[-1]
                        )))
                        error_reply = f"{new_value} non è disponibile su Bybit {market_type.capitalize()}. Puoi scegliere tra: {tf_list}. Quale preferisci?"
                    elif param_name == "symbol":
                        # LOGGING DIAGNOSTICO: symbol rifiutato durante modifica esplicita
                        logger.info(
                            f"[SYMBOL_REJECT] file={__file__} function=handle_message "
                            f"step=explicit_modification config_status={state.get('config_status', 'N/A')} "
                            f"symbol_received={new_value} decision=rejected error_msg={error_msg}"
                        )
                        market_type = params.get("market_type", "futures")
                        try:
                            valid_symbols = validators.fetch_valid_symbols(market_type)
                            import random
                            examples_list = list(valid_symbols)
                            if len(examples_list) > 6:
                                examples = random.sample(examples_list, 6)
                            else:
                                examples = examples_list[:6]
                            examples_str = ", ".join(examples) if examples else "Nessun esempio disponibile"
                            error_reply = f"La coppia '{new_value}' non esiste su Bybit {market_type.capitalize()}. Ricontrolla il simbolo e riprova (esempi validi: {examples_str})."
                        except:
                            error_reply = error_msg or f"La coppia '{new_value}' non è valida. Inserisci una coppia USDT valida (es. BTCUSDT)."
                    elif param_name == "leverage":
                        symbol = params.get("symbol", "questa coppia")
                        market_type = params.get("market_type", "futures")
                        minLev, maxLev = validators.get_leverage_limits(None, symbol, market_type)
                        if minLev is not None and maxLev is not None:
                            error_reply = f"La leva {new_value}x non è consentita per {symbol}. Inserisci un valore tra {int(minLev)}x e {int(maxLev)}x."
                        else:
                            error_reply = error_msg or f"La leva {new_value}x non è valida. Inserisci un valore tra 1x e 100x."
                    else:
                        # Per altri parametri, usa il messaggio di errore standard
                        error_reply = error_msg or f"Il valore {new_value} non è valido per {param_name}."
                    
                    errors[param_name] = error_reply
                    continue
                
                # Valore VALIDO: verifica se richiede conferma (per risk_pct e sl)
                requires_confirmation = False
                confirmation_msg = None
                suggested_value = None
                
                if param_name == "risk_pct":
                    market_type = params.get("market_type", "futures")
                    requires_confirmation, confirmation_msg = _check_risk_warning(new_value, market_type)
                elif param_name == "leverage":
                    lev_int = int(float(new_value)) if new_value is not None else 0
                    symbol = params.get("symbol") or "questa coppia"
                    requires_confirmation, confirmation_msg = _check_leverage_warning(lev_int, symbol)
                elif param_name == "sl":
                    sl_val = float(str(new_value).replace("%", ""))
                    requires_confirmation, confirmation_msg, suggested_value = _check_sl_warning(sl_val)
                
                if requires_confirmation:
                    # Se richiede conferma, salva per gestirlo dopo
                    requires_confirmation_list[param_name] = (confirmation_msg, suggested_value, new_value)
                    continue
                
                # Valore VALIDO e non richiede conferma: aggiungi agli update da applicare
                applied_updates[param_name] = (old_values[param_name], new_value)
            
            # Qualsiasi errore di validazione: nessun apply, nessun pending (evita conferma con valori invalidi).
            if errors:
                state, cs, params = _sync_state(state, cs, params)
                reply = _append_pending_confirmation_remnant_to_error_reply(" ".join(errors.values()), cs)
                return {"reply": reply, "state": state}

            # 1) Applica subito tutti gli update che non richiedono conferma (stesso messaggio).
            patch_dict = {param_name: new_value for param_name, (_, new_value) in applied_updates.items()}
            patch_result: Dict[str, Any] = {"ok": True, "changed": {}, "warnings": []}

            if patch_dict:
                logger.info("[STRATEGY_UPDATE] patch_dict=%s (before apply_config_patch)", patch_dict)
                patch_result = apply_config_patch(cs, patch_dict)
                if not patch_result.get("ok", True):
                    state, cs, params = _sync_state(state, cs, cs["params"])
                    return {
                        "reply": patch_result.get("message", "Modifica non applicabile."),
                        "state": state
                    }

                params = cs["params"].copy()

                for param_name, (old_value, new_value) in applied_updates.items():
                    if param_name == "symbol":
                        market_type = params.get("market_type", "futures")
                        logger.info(f"[SYMBOL_OK] saved symbol=%s market_type=%s", new_value, market_type)
                    elif param_name == "leverage":
                        cs["pending_leverage_confirmation"] = None
                    elif param_name == "operating_mode":
                        logger.info("[LOCAL_UPDATE] operating_mode -> %s", new_value)
                    elif param_name == "sl":
                        cs["pending_sl_confirmation"] = None
                        cs["suggested_sl"] = None
                    elif param_name == "risk_pct":
                        cs["pending_risk_confirmation"] = None
                    elif param_name == "market_type":
                        if new_value == "spot":
                            params["leverage"] = None
                            cs["pending_leverage_confirmation"] = None

                if patch_result.get("changed"):
                    logger.info(f"[MOD_UPDATES] Applied changes: {patch_result['changed']}")
                if patch_result.get("warnings"):
                    logger.warning(f"[MOD_UPDATES] Warnings: {patch_result['warnings']}")
                logger.info(f"[MOD_UPDATES] updates={updates} final_params={params}")

                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)

            # 2) Registra TUTTI i parametri rischiosi in pending (nessun return nel loop).
            if requires_confirmation_list:
                _clear_pending_confirmation_batch(cs)
                for param_name in list(updates.keys()):
                    if param_name not in requires_confirmation_list:
                        continue
                    _, suggested_value, new_value = requires_confirmation_list[param_name]
                    if param_name == "risk_pct":
                        try:
                            cs["pending_risk_confirmation"] = float(
                                str(new_value).replace("%", "").replace(",", ".")
                            )
                        except (TypeError, ValueError):
                            cs["pending_risk_confirmation"] = new_value
                    elif param_name == "leverage":
                        cs["pending_leverage_confirmation"] = (
                            int(float(new_value)) if new_value is not None else None
                        )
                    elif param_name == "sl":
                        cs["pending_sl_confirmation"] = float(str(new_value).replace("%", "").replace(",", "."))
                        cs["suggested_sl"] = suggested_value

                logger.info("[PENDING_BATCH_SET] pending=%s", _pending_batch_snapshot(cs))

                state, cs, params = _sync_state(state, cs, params)
                reply = _build_pending_batch_confirmation_prompt(cs)
                return {"reply": reply, "state": state}
            
            # REGOLA CRITICA: Gestione speciale per cambio market_type
            market_type_changed = "market_type" in applied_updates
            if market_type_changed:
                old_market, new_market = applied_updates["market_type"]
                
                # Spot → Futures: chiedi leva SOLO se leverage mancante (BUG 2 - step già ricalcolato)
                if old_market == "spot" and new_market == "futures":
                    if params.get("leverage") is None and cs.get("step") == "leverage":
                        state, cs, params = _sync_state(state, cs, params)
                        return {
                            "reply": "Ok, passiamo da Spot a Futures 👍\nNei Futures è necessario impostare una leva.\nChe leva vuoi usare?",
                            "state": state
                        }
                    # Altrimenti step già coerente da _recompute_step, continua flusso normale
                
                # Futures → Spot: leverage già rimosso, pending azzerato da apply_config_patch
                elif old_market == "futures" and new_market == "spot":
                    # La leva è già stata rimossa sopra
                    if _is_configuration_complete(state):
                        reply = "Ok, passiamo da Futures a Spot 👍\nIn modalità Spot la leva non si utilizza, quindi la rimuovo.\n\n" + _build_summary(params) + "\n\nVuoi modificare altro o avviare il bot?"
                        state, cs, params = _sync_state(state, cs, params)
                        return {
                            "reply": reply,
                            "state": state
                        }
            
            # Se la configurazione era completa, rimane completa
            # Se era in progress, continua dal punto in cui si era
            if _is_configuration_complete(state):
                # REGOLA OBBLIGATORIA: se c'è pending_leverage_confirmation, reply SOLO warning conferma rischio
                if cs.get("pending_leverage_confirmation") is not None:
                    pending_lev = cs.get("pending_leverage_confirmation")
                    sym = params.get("symbol") or "questa coppia"
                    _, warning_msg = _check_leverage_warning(int(pending_lev), sym)
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": warning_msg or "Conferma leva.", "state": state}
                # REGOLA: Se Futures senza leva, NON mostrare riepilogo, chiedi leva
                if params.get("market_type") == "futures" and params.get("leverage") is None:
                    cs["step"] = "leverage"
                    state, cs, params = _sync_state(state, cs, params)
                    return {
                        "reply": "Nei Futures è necessario impostare una leva.\nChe leva vuoi usare?",
                        "state": state
                    }
                
                # Configurazione completa: conferma modifiche e mostra summary
                # Costruisci messaggio che elenca tutte le modifiche effettivamente applicate
                param_display_names = {
                    "timeframe": "il timeframe",
                    "leverage": "la leva",
                    "sl": "lo stop loss",
                    "tp": "il take profit",
                    "risk_pct": "la percentuale di rischio",
                    "symbol": "la coppia",
                    "market_type": "il tipo di mercato",
                    "strategy": "la strategia",
                    "operating_mode": "la modalità operativa",
                }
                
                # Costruisci lista delle modifiche
                modifications_list = []
                for param_name, (old_value, new_value) in applied_updates.items():
                    param_display = param_display_names.get(param_name, param_name)
                    
                    # Formatta il nuovo valore per il messaggio
                    new_value_display = new_value
                    if param_name == "leverage":
                        new_value_display = f"{new_value}x"
                    elif param_name in ["sl", "tp", "risk_pct"]:
                        if isinstance(new_value, (int, float)):
                            new_value_display = f"{new_value}%"
                        elif new_value and "%" not in str(new_value):
                            new_value_display = f"{new_value}%"
                    # Per eventuali altri parametri usa la stringa così com'è
                    
                    # Formatta il vecchio valore per il messaggio
                    old_value_display = old_value
                    if old_value is not None:
                        if param_name == "leverage":
                            old_value_display = f"{old_value}x"
                        elif param_name in ["sl", "tp"]:
                            old_value_display = str(old_value)
                        elif param_name == "risk_pct":
                            old_value_display = f"{old_value}%"
                        elif param_name in ["ema_period", "rsi_period", "atr_period"]:
                            old_value_display = str(old_value)
                    
                    # Costruisci messaggio per questa modifica
                    if old_value is not None and new_value is not None:
                        modifications_list.append(f"{param_display} da {old_value_display} a {new_value_display}")
                    elif new_value is None:
                        modifications_list.append(f"{param_display} rimosso")
                    else:
                        modifications_list.append(f"{param_display} impostato a {new_value_display}")
                
                # Costruisci reply finale usando params (già sincronizzato con _sync_state alla riga 2013)
                if len(modifications_list) == 1:
                    reply = f"Ok, aggiorno {modifications_list[0]}.\n\n" + _build_summary(params) + "\n\nVuoi modificare altro o avviare il bot?"
                elif len(modifications_list) > 1:
                    reply = "Ok, aggiorno:\n" + "\n".join(f"• {mod}" for mod in modifications_list) + "\n\n" + _build_summary(params) + "\n\nVuoi modificare altro o avviare il bot?"
                else:
                    reply = "Perfetto 👍 Ho aggiornato la configurazione.\n\n" + _build_summary(params) + "\n\nVuoi modificare altro o avviare il bot?"
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": reply,
                    "state": state
                }
            else:
                # REGOLA OBBLIGATORIA: se c'è pending_leverage_confirmation, reply SOLO warning conferma rischio
                if cs.get("pending_leverage_confirmation") is not None:
                    pending_lev = cs.get("pending_leverage_confirmation")
                    sym = params.get("symbol") or "questa coppia"
                    _, warning_msg = _check_leverage_warning(int(pending_lev), sym)
                    state, cs, params = _sync_state(state, cs, params)
                    return {"reply": warning_msg or "Conferma leva.", "state": state}
                # Configurazione in corso: conferma modifiche e riprendi dal punto in cui eri
                # Non cambiare current_step, continua con la domanda corrente
                state, cs, params = _sync_state(state, cs, params)
                
                # Costruisci risposta: conferma + domanda corrente
                if len(applied_updates) == 1:
                    param_name = list(applied_updates.keys())[0]
                    confirmation = f"Ok 👍 Ho aggiornato {param_name}."
                else:
                    confirmation = f"Ok 👍 Ho aggiornato {len(applied_updates)} parametri."
                next_question = _step_question(current_step, params)
                reply_text = f"{confirmation} {next_question}"
                return {
                    "reply": reply_text,
                    "state": state
                }
        else:
            # Non siamo riusciti a estrarre parametro/valore dalla richiesta
            # Prova a capire quale parametro l'utente vuole modificare anche senza valore esplicito
            # e chiedi il nuovo valore seguendo gli esempi obbligatori
            detected_param_only = None
            lt = user_text.strip().lower()
            
            # Cerca pattern per identificare il parametro anche senza valore
            param_patterns = {
                "timeframe": ["timeframe", "tf", "tempo"],
                "leverage": ["leva", "leverage", "lev"],
                "ema_period": ["ema"],
                "rsi_period": ["rsi"],
                "atr_period": ["atr"],
                "sl": ["stop loss", "stoploss", "sl"],
                "tp": ["take profit", "takeprofit", "tp"],
                "symbol": ["coppia", "symbol", "pair", "simbolo"],
                "risk_pct": ["rischio", "risk", "percentuale rischio"],
                "market_type": ["spot", "futures", "mercato", "tipo mercato"],
                "strategy": ["strategia", "strategy"]
            }
            
            for param_name, patterns in param_patterns.items():
                for pattern in patterns:
                    pattern_re = re.compile(rf"\b{re.escape(pattern)}\b", re.I)
                    if pattern_re.search(lt):
                        detected_param_only = param_name
                        break
                if detected_param_only:
                    break
            
            if detected_param_only and _is_configuration_complete(state):
                # Parametro riconosciuto ma valore non estratto: chiedi il nuovo valore
                # Segui gli esempi obbligatori: "Perfetto 👍 Attualmente il timeframe è 1h. Che timeframe vuoi impostare?"
                current_value = params.get(detected_param_only)
                
                # Formatta il valore corrente per il messaggio
                current_value_display = current_value
                if current_value is not None:
                    if detected_param_only == "leverage":
                        current_value_display = f"{current_value}x"
                    elif detected_param_only in ["sl", "tp"]:
                        current_value_display = str(current_value)
                    elif detected_param_only == "risk_pct":
                        current_value_display = f"{current_value}%"
                    elif detected_param_only == "market_type":
                        # Formatta market_type in modo leggibile
                        if current_value == "spot":
                            current_value_display = "Spot"
                        elif current_value == "futures":
                            current_value_display = "Futures"
                        else:
                            current_value_display = current_value.capitalize() if current_value else "non impostato"
                
                param_questions = {
                    "timeframe": f"Perfetto 👍 Attualmente il timeframe è {current_value_display or 'non impostato'}.\nChe timeframe vuoi impostare?",
                    "leverage": f"Perfetto 👍 Attualmente la leva è {current_value_display or 'non impostata'}.\nChe leva vuoi utilizzare?",
                    "ema_period": f"Nessun problema.\nChe periodo EMA vuoi usare?",
                    "rsi_period": f"Nessun problema.\nChe periodo RSI vuoi usare?",
                    "atr_period": f"Nessun problema.\nChe periodo ATR vuoi usare?",
                    "sl": f"Perfetto 👍 Attualmente lo stop loss è {current_value_display or 'non impostato'}.\nChe stop loss vuoi impostare?",
                    "tp": f"Perfetto 👍 Attualmente il take profit è {current_value_display or 'non impostato'}.\nChe take profit vuoi impostare?",
                    "symbol": f"Perfetto 👍 Attualmente la coppia è {current_value_display or 'non impostata'}.\nChe coppia vuoi usare?",
                    "risk_pct": f"Perfetto 👍 Attualmente il rischio è {current_value_display or 'non impostato'}.\nChe percentuale di rischio vuoi impostare?",
                    "strategy": f"Perfetto 👍 Attualmente la strategia è {current_value_display or 'non impostata'}.\nChe strategia vuoi usare?",
                    "market_type": f"Perfetto 👍 Attualmente stai usando {current_value_display or 'non impostato'}.\nVuoi passare a Spot o Futures?",
                }
                
                question = param_questions.get(detected_param_only, f"Che valore vuoi impostare per {detected_param_only}?")
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": question,
                    "state": state
                }
            
            # Se la configurazione è completa e non abbiamo riconosciuto il parametro
            if _is_configuration_complete(state):
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Non ho capito quale parametro vuoi modificare. Puoi essere più specifico? (es. 'voglio modificare il timeframe', 'cambia leva a 5x', 'voglio cambiare EMA')",
                    "state": state
                }
            # Se la configurazione è in corso, continua normalmente (potrebbe essere una risposta normale)
            # Non fare nulla, lascia che il flusso normale gestisca
    
    # Priorità pending conferma: prima del ramo CONFIG COMPLETE (evita che "sì confermo" finisca nel riepilogo generico)
    empathetic_response = _detect_empathetic_phrase(user_text)
    pending_batch = _pending_batch_snapshot(cs)
    if pending_batch:
        amb = _ambiguous_modify_confirm_clarification(user_text, params, pending_batch)
        if amb:
            state, cs, params = _sync_state(state, cs, params)
            reply = amb
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}
        merged_params, merged_pending, resolve_errors = resolve_input(params, pending_batch, user_text)
        applied_something = merged_params != params or merged_pending != pending_batch
        if resolve_errors:
            params = merged_params
            cs["pending_sl_confirmation"] = merged_pending.get("sl")
            cs["pending_risk_confirmation"] = merged_pending.get("risk_pct")
            cs["pending_leverage_confirmation"] = merged_pending.get("leverage")
            if "sl" not in merged_pending:
                cs["suggested_sl"] = None
            params = _sync_strategy_from_periods(params)
            cs["params"] = params
            state, cs, params = _sync_state(state, cs, params)
            reply = _append_pending_confirmation_remnant_to_error_reply(" ".join(resolve_errors), cs)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply.strip(), "state": state}
        if applied_something:
            logger.info(
                "[PENDING_RESOLVE_APPLY] pending_before=%s pending_after=%s",
                pending_batch,
                merged_pending,
            )
            params = merged_params
            cs["pending_sl_confirmation"] = merged_pending.get("sl")
            cs["pending_risk_confirmation"] = merged_pending.get("risk_pct")
            cs["pending_leverage_confirmation"] = merged_pending.get("leverage")
            if "sl" not in merged_pending:
                cs["suggested_sl"] = None

            params = _sync_strategy_from_periods(params)
            cs["params"] = params
            step = cs.get("step")
            complete_ok = is_config_complete(params)
            print("[POST_CONFIRM_CHECK]", dict(params), "complete:", complete_ok, "step:", step)
            logger.info(
                "[POST_CONFIRM_CHECK] params=%s complete=%s step=%s",
                dict(params),
                complete_ok,
                step,
            )
            if complete_ok:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                cs["step"] = None
                state["step"] = None
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state,
                }
            next_step = _get_next_step(current_step, params, cs)
            if next_step is None:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                cs["step"] = None
                state["step"] = None
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state,
                }
            cs["step"] = next_step
            state, cs, params = _sync_state(state, cs, params)
            reply = _step_question(next_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}
        if re.search(r"\bconferm\w*\b", user_text.strip().lower(), re.I):
            state, cs, params = _sync_state(state, cs, params)
            reply = "Mi dici cosa vuoi confermare esattamente? (es. 'confermo leva' o 'confermo sl')"
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}
        if len(pending_batch) > 1:
            state, cs, params = _sync_state(state, cs, params)
            reply = _build_pending_batch_confirmation_prompt(cs)
            if empathetic_response and empathetic_response.lower() not in reply.lower():
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}

    # Leva prima del rischio: input tipo "7x" (BUG3) non deve restare bloccato da pending_risk stale
    # Spot: nessun flusso leva (né pending né domanda)
    if params.get("market_type") == "spot":
        cs["pending_leverage_confirmation"] = None
    elif cs.get("pending_leverage_confirmation") is not None:
        pending_lev = cs.get("pending_leverage_confirmation")
        # Se arriva una NUOVA leva valida durante pending, sostituisce completamente la precedente.
        new_lev_value = _extract_step_value(user_text, "leverage", params)
        if new_lev_value is not None:
            is_valid, error_msg, _ = _validate_step_value("leverage", new_lev_value, params)
            if not is_valid:
                state, cs, params = _sync_state(state, cs, params)
                reply = (error_msg or "Leva non valida.") + "\n\n" + _step_question(current_step, params)
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}
            lev_int = int(float(new_lev_value))
            sym = params.get("symbol") or "questa coppia"
            requires_confirm, warning_msg = _check_leverage_warning(lev_int, sym)
            if requires_confirm:
                # Pending reale: non promuovere la nuova leva in params prima della conferma esplicita.
                cs["pending_leverage_confirmation"] = lev_int
            else:
                params["leverage"] = lev_int
                cs["pending_leverage_confirmation"] = None
            if requires_confirm and warning_msg:
                state, cs, params = _sync_state(state, cs, params)
                reply = warning_msg
                if empathetic_response and empathetic_response.lower() not in warning_msg.lower():
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}
            next_step = _get_next_step(current_step, params, cs)
            if next_step is None:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state,
                }
            cs["step"] = next_step
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            reply = _step_question(next_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}

        confirmation = _extract_confirmation(user_text)

        if confirmation is True:
            try:
                params["leverage"] = int(float(pending_lev))
            except (TypeError, ValueError):
                params["leverage"] = pending_lev
            cs["pending_leverage_confirmation"] = None
            next_step = _get_next_step(current_step, params, cs)
            if next_step is None:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state,
                }
            cs["step"] = next_step
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            reply = _step_question(next_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}
        elif confirmation is False:
            cs["pending_leverage_confirmation"] = None
            state, cs, params = _sync_state(state, cs, params)
            reply = _step_question(current_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}
        else:
            sym = params.get("symbol") or "questa coppia"
            requires_confirm, warning_msg = _check_leverage_warning(int(pending_lev), sym)
            if warning_msg:
                state, cs, params = _sync_state(state, cs, params)
                reply = warning_msg
                if empathetic_response and empathetic_response.lower() not in warning_msg.lower():
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}

    if cs.get("pending_risk_confirmation") is not None:
        logger.info(
            "[PENDING_RISK_DEBUG] enter branch: params[risk_pct]_before=%s pending_before=%s",
            params.get("risk_pct"),
            cs.get("pending_risk_confirmation"),
        )
        pending_risk = cs.get("pending_risk_confirmation")
        # Se arriva un NUOVO rischio valido durante pending, sostituisce il valore precedente ovunque.
        new_risk_value = _extract_step_value(user_text, "risk_pct", params)
        if new_risk_value is not None:
            is_valid, error_msg, _ = _validate_step_value("risk_pct", new_risk_value, params)
            if not is_valid:
                state, cs, params = _sync_state(state, cs, params)
                reply = (error_msg or "Valore rischio non valido.") + "\n\n" + _step_question(current_step, params)
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}
            risk_float = float(str(new_risk_value).replace("%", ""))
            market_type = params.get("market_type", "futures")
            requires_confirm, warning_msg = _check_risk_warning(risk_float, market_type)
            logger.info(
                "[PENDING_RISK_DEBUG] parsed new risk: risk_float=%s requires_confirm=%s",
                risk_float,
                requires_confirm,
            )
            params["risk_pct"] = risk_float
            cs["pending_risk_confirmation"] = risk_float if requires_confirm else None
            logger.info(
                "[PENDING_RISK_DEBUG] pending after set=%s",
                cs.get("pending_risk_confirmation"),
            )
            if requires_confirm and warning_msg:
                logger.info(
                    "[PENDING_RISK_DEBUG] before _sync_state (confirm path): params[risk_pct]=%s",
                    params.get("risk_pct"),
                )
                state, cs, params = _sync_state(state, cs, params)
                logger.info(
                    "[PENDING_RISK_DEBUG] after _sync_state (confirm path): params[risk_pct]=%s",
                    params.get("risk_pct"),
                )
                reply = warning_msg
                if empathetic_response and not empathetic_response.lower() in warning_msg.lower():
                    reply = empathetic_response + " " + reply
                logger.info(
                    "[PENDING_RISK_DEBUG] pre-return state risk (confirm path)=%s",
                    ((state.get("config_state") or {}).get("params") or {}).get("risk_pct"),
                )
                return {"reply": reply, "state": state}
            _ec = cs.get("error_count")
            if isinstance(_ec, dict):
                _ec2 = dict(_ec)
                _ec2.pop("risk_pct", None)
                cs["error_count"] = _ec2
            next_step = _get_next_step(current_step, params, cs)
            if next_step is None:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                params = _sync_strategy_from_periods(params)
                logger.info(
                    "[PENDING_RISK_DEBUG] before _sync_state (complete path): params[risk_pct]=%s",
                    params.get("risk_pct"),
                )
                state, cs, params = _sync_state(state, cs, params)
                logger.info(
                    "[PENDING_RISK_DEBUG] after _sync_state (complete path): params[risk_pct]=%s",
                    params.get("risk_pct"),
                )
                logger.info(
                    "[PENDING_RISK_DEBUG] pre-return state risk (complete path)=%s",
                    ((state.get("config_state") or {}).get("params") or {}).get("risk_pct"),
                )
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state,
                }
            cs["step"] = next_step
            params = _sync_strategy_from_periods(params)
            logger.info(
                "[PENDING_RISK_DEBUG] before _sync_state (next-step path): params[risk_pct]=%s",
                params.get("risk_pct"),
            )
            state, cs, params = _sync_state(state, cs, params)
            logger.info(
                "[PENDING_RISK_DEBUG] after _sync_state (next-step path): params[risk_pct]=%s",
                params.get("risk_pct"),
            )
            reply = _step_question(next_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            logger.info(
                "[PENDING_RISK_DEBUG] pre-return state risk (next-step path)=%s",
                ((state.get("config_state") or {}).get("params") or {}).get("risk_pct"),
            )
            return {"reply": reply, "state": state}

        confirmation = _extract_confirmation(user_text)

        if confirmation is True:
            try:
                params["risk_pct"] = float(pending_risk)
            except (TypeError, ValueError):
                params["risk_pct"] = pending_risk
            cs["pending_risk_confirmation"] = None
            _ec = cs.get("error_count")
            if isinstance(_ec, dict):
                _ec2 = dict(_ec)
                _ec2.pop("risk_pct", None)
                cs["error_count"] = _ec2
            next_step = _get_next_step(current_step, params, cs)
            if next_step is None:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state,
                }
            cs["step"] = next_step
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            reply = _step_question(next_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}
        elif confirmation is False:
            cs["pending_risk_confirmation"] = None
            state, cs, params = _sync_state(state, cs, params)
            reply = _step_question(current_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {
                "reply": reply,
                "state": state,
            }
        else:
            market_type = params.get("market_type", "futures")
            requires_confirm, warning_msg = _check_risk_warning(pending_risk, market_type)
            if warning_msg:
                state, cs, params = _sync_state(state, cs, params)
                reply = warning_msg
                if empathetic_response and not empathetic_response.lower() in warning_msg.lower():
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}

    if cs.get("pending_sl_confirmation") is not None:
        pending_sl = cs.get("pending_sl_confirmation")
        suggested_sl = cs.get("suggested_sl")

        new_sl_value = _extract_step_value(user_text, "sl", params)
        if new_sl_value is None:
            # Pending SL attivo: accetta anche frasi tipo "metti 3%" senza keyword "sl"
            lt_pending = user_text.strip().lower()
            if re.search(r"\d+(?:\.\d+)?\s*%", user_text):
                has_other_param_context = any(
                    token in lt_pending
                    for token in ["take profit", "tp", "rischio", "risk", "leva", "leverage"]
                )
                if not has_other_param_context:
                    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", user_text)
                    if m_pct:
                        new_sl_value = f"{m_pct.group(1)}%"
        if new_sl_value is not None:
            sl_val = float(str(new_sl_value).replace("%", ""))
            is_valid, error_msg, _ = _validate_step_value("sl", new_sl_value, params)
            if is_valid:
                requires_confirm, confirmation_msg, suggested_value = _check_sl_warning(sl_val)
                if requires_confirm:
                    # Caso B: nuovo valore ancora ad alto rischio -> NON scrivere in params["sl"]
                    cs["pending_sl_confirmation"] = sl_val
                    cs["suggested_sl"] = suggested_value
                    state, cs, params = _sync_state(state, cs, params)
                    reply = confirmation_msg or "Confermi questo stop loss?"
                    if empathetic_response and not empathetic_response.lower() in reply.lower():
                        reply = empathetic_response + " " + reply
                    return {"reply": reply, "state": state}

                # Caso A: nuovo valore definitivo (non richiede conferma) -> commit reale + pulizia pending
                params["sl"] = f"{sl_val}%"
                cs["pending_sl_confirmation"] = None
                cs["suggested_sl"] = None
                next_step = _get_next_step(current_step, params, cs)
                if next_step is None:
                    state["config_status"] = "complete"
                    _cleanup_config_state_when_complete(cs)
                    params = _sync_strategy_from_periods(params)
                    state, cs, params = _sync_state(state, cs, params)
                    return {
                        "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                        "state": state,
                    }
                cs["step"] = next_step
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                reply = _step_question(next_step, params)
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}
            else:
                requires_confirm, warning_msg, suggested = _check_sl_warning(pending_sl)
                state, cs, params = _sync_state(state, cs, params)
                reply = error_msg + "\n\n" + (warning_msg or "Quale stop loss in percentuale?")
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}

        lt = user_text.strip().lower()

        if suggested_sl is not None:
            suggested_str = str(int(suggested_sl)) if suggested_sl == int(suggested_sl) else str(suggested_sl)
            if suggested_str in lt or f"{suggested_str}%" in lt:
                params["sl"] = f"{suggested_sl}%"
                cs["pending_sl_confirmation"] = None
                cs["suggested_sl"] = None
                next_step = _get_next_step(current_step, params, cs)
                if next_step is None:
                    state["config_status"] = "complete"
                    _cleanup_config_state_when_complete(cs)
                    params = _sync_strategy_from_periods(params)
                    state, cs, params = _sync_state(state, cs, params)
                    return {
                        "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                        "state": state,
                    }
                cs["step"] = next_step
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                reply = _step_question(next_step, params)
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}

        if suggested_sl is not None:
            accept_suggested = any(phrase in lt for phrase in ["ok", "va bene", "sì", "si", "yes", "y", "perfetto", "metti", "usa", "va bene così", "accetto"])
            if accept_suggested:
                params["sl"] = f"{suggested_sl}%"
                cs["pending_sl_confirmation"] = None
                cs["suggested_sl"] = None
                next_step = _get_next_step(current_step, params, cs)
                if next_step is None:
                    state["config_status"] = "complete"
                    _cleanup_config_state_when_complete(cs)
                    params = _sync_strategy_from_periods(params)
                    state, cs, params = _sync_state(state, cs, params)
                    return {
                        "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                        "state": state,
                    }
                cs["step"] = next_step
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                reply = _step_question(next_step, params)
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {"reply": reply, "state": state}

        pending_str = str(int(pending_sl)) if pending_sl == int(pending_sl) else str(pending_sl)
        confirm_extreme = any(phrase in lt for phrase in ["confermo", "lascialo così", "mantieni", "tieni", f"confermo {pending_str}", f"{pending_str}%"])
        if confirm_extreme or _extract_confirmation(user_text) is True:
            params["sl"] = f"{pending_sl}%"
            cs["pending_sl_confirmation"] = None
            cs["suggested_sl"] = None
            next_step = _get_next_step(current_step, params, cs)
            if next_step is None:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state,
                }
            cs["step"] = next_step
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            reply = _step_question(next_step, params)
            if empathetic_response:
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}

        requires_confirm, warning_msg, suggested = _check_sl_warning(pending_sl)
        if warning_msg:
            state, cs, params = _sync_state(state, cs, params)
            reply = warning_msg
            if empathetic_response and not empathetic_response.lower() in warning_msg.lower():
                reply = empathetic_response + " " + reply
            return {"reply": reply, "state": state}

    # GESTIONE CONFIGURAZIONE COMPLETA: Anche se completa, permette SEMPRE modifiche
    # La configurazione completata NON è uno stato bloccato
    # ATTENZIONE: entra qui SOLO se la config è davvero completa (tutti i parametri + periodi)
    # e lo step corrente NON richiede ancora parametri (es. strategy_params).
    is_flag_complete = _is_configuration_complete(state)
    is_all_filled = _all_params_filled(params)
    current_step_for_complete = cs.get("step")
    if is_flag_complete and is_all_filled and current_step_for_complete not in ("strategy", "strategy_params"):
        user_lower = user_text.strip().lower()
        
        # Intent "strategie disponibili" - nel piano FREE rimappa a modalità operative
        if "strategie" in user_lower and any(p in user_lower for p in ["disponibili", "quali", "che strategie", "strategie posso"]):
            return {
                "reply": "Nel piano Free puoi scegliere tra tre modalità operative:\n"
                         "- Aggressiva\n- Equilibrata\n- Selettiva\n\n"
                         "Quale modalità preferisci?",
                "state": state
            }
        
        # Permetti: avvio bot, spiegazioni, modifiche (già gestite sopra), nuova configurazione
        if "avvia" in user_lower and "bot" in user_lower:
            state["config_status"] = "ready"
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "Bot avviato con la seguente configurazione:\n\n" + _build_summary(params),
                "state": state
            }
        
        # Richiesta di nuova configurazione (reset completo)
        if any(word in user_lower for word in ["nuova config", "ricomincia", "reset", "annulla config"]):
            state["config_status"] = "in_progress"
            # Reset completo dello stato di configurazione:
            # - imposta config_state allo scheletro DEFAULT_PARAMS con tutti i campi null (strategy lista vuota)
            # - rimuove eventuali params legacy a livello root
            state["config_state"] = {
                "step": "market_type",
                "params": copy.deepcopy(DEFAULT_PARAMS),
                "error_count": {},
            }
            state.pop("params", None)
            return {
                "reply": "Ho resettato la configurazione. Iniziamo da capo.\n\n" + _step_question("market_type", {}),
                "state": state,
            }
        
        # Domande generiche: rispondi informativamente
        if _is_generic_question(user_text):
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "La configurazione è completa. Rispondo alla tua domanda.",
                "state": state
            }
        
        # Se non è una richiesta di modifica riconosciuta, ma la configurazione è completa,
        # mostra il riepilogo e chiedi se vuole modificare o avviare
        # NON dire mai "non posso modificare" o "devi crearne una nuova"
        amb_commit = _commit_pending_risk_or_leverage_on_confirm(user_text, cs, params)
        if amb_commit:
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": amb_commit, "state": state}
        params = _sync_strategy_from_periods(params)
        state, cs, params = _sync_state(state, cs, params)
        return {
            "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi modificare qualcosa o avviare il bot adesso?",
            "state": state
        }
    
    # Primo campo mancante nella sequenza FREE (stessa logica dopo merge v2 e prima del saluto)
    mw_pre = free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs)
    if mw_pre is not None:
        cs["step"] = mw_pre
        current_step = mw_pre
        state, cs, params = _sync_state(state, cs, params)

    # FIX: Gestione saluti quando lo step è "market_type"
    # BUG2 FIX: Se config è completa, "ciao" non deve riavviare wizard
    if current_step == "market_type" and _is_greeting(user_text):
        if _is_configuration_complete(state):
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "Ciao! La configurazione è già pronta. Vuoi modificare qualcosa o avviare il bot?",
                "state": state
            }
        # Controlla se il testo contiene già "spot" o "futures" (risposta valida)
        user_lower = user_text.strip().lower()
        has_market_type = "spot" in user_lower or "futures" in user_lower

        # Se contiene già una risposta valida, non gestire come saluto puro
        # L'estrazione del valore verrà gestita normalmente più avanti
        if not has_market_type:
            # Determina quale variante usare (ruota tra 0, 1, 2)
            last_variant = cs.get("last_greeting_variant")
            
            # Se non c'è una variante precedente, usa la prima (0)
            if last_variant is None:
                next_variant = 0
            else:
                # Ruota alla prossima variante (0 -> 1 -> 2 -> 0)
                next_variant = (last_variant + 1) % 3
            
            # Salva la variante usata nello state
            cs["last_greeting_variant"] = next_variant
            
            # Genera la risposta con la variante scelta
            reply = _step_question("market_type", params, greeting_variant=next_variant)
            
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": reply,
                "state": state
            }

    # Controlla se tutti i parametri sono compilati
    if _all_params_filled(params):
        # BUG3: commit pending rischio/leva prima di cleanup (evita pending azzerati senza scrivere params)
        amb_commit = _commit_pending_risk_or_leverage_on_confirm(user_text, cs, params)
        if amb_commit:
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": amb_commit, "state": state}
        state, cs, params = _sync_state(state, cs, params)
        # Marca la configurazione come COMPLETA (stato finale, non modificabile automaticamente)
        if state.get("config_status") != "complete":
            state["config_status"] = "complete"
        # Con tutti i parametri OK: azzera sempre pending/error residui (anche se era già complete)
        _cleanup_config_state_when_complete(cs)

        # Se l'utente dice "avvia bot", imposta config_status="ready"
        user_lower = user_text.strip().lower()
        if "avvia" in user_lower and "bot" in user_lower:
            state["config_status"] = "ready"
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "Bot avviato con la seguente configurazione:\n\n" + _build_summary(params),
                "state": state
            }
        else:
            # Mostra riepilogo e aspetta comando avvia bot
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                "state": state
            }
    
    # ============================================================
    # LOG PUNTO B) - Prima di _extract_step_value
    # ============================================================
    logger.info(
        f"[ANALYSIS_B_BEFORE] Before _extract_step_value: user_text='{user_text}', current_step={current_step}, "
        f"params_strategy={params.get('strategy')}, params_rsi_period={params.get('rsi_period')}, "
        f"params_atr_period={params.get('atr_period')}, params_ema_period={params.get('ema_period')}, "
        f"params_snapshot={params}"
    )
    
    if v2_applied_keys:
        skip_wizard_parallel = True
    else:
        skip_wizard_parallel = False

    if current_step not in ("strategy", "strategy_params") and not skip_wizard_parallel:
        params, cs, wizard_parallel_errors, wizard_parallel_success_msgs = _apply_wizard_parallel_optional_params(
            user_text, current_step, state, cs, params
        )
        state, cs, params = _sync_state(state, cs, params)

    if current_step == "timeframe" and timeframe_pre_extracted_value is None:
        # Caso richiesto: input non interpretabile come timeframe (es. "aggressiva").
        # Non avanzare il wizard, mantieni step timeframe e ripeti solo la domanda timeframe.
        cs["step"] = "timeframe"
        state, cs, params = _sync_state(state, cs, params)
        return {"reply": _step_question("timeframe", params), "state": state}

    if current_step not in ("strategy", "strategy_params"):
        mw_extract = free_plan.first_missing_free_wizard_field(params, _is_step_filled, cs)
        if mw_extract is not None:
            cs["step"] = mw_extract
            current_step = mw_extract
            state, cs, params = _sync_state(state, cs, params)

    # Estrai SOLO il valore per lo step corrente
    extracted_value = _extract_step_value(user_text, current_step, params)
    
    # ============================================================
    # LOG PUNTO B) - Dopo _extract_step_value
    # ============================================================
    extracted_type = type(extracted_value).__name__ if extracted_value is not None else "None"
    extracted_repr = str(extracted_value)[:200] if extracted_value is not None else "None"
    logger.info(
        f"[ANALYSIS_B_AFTER] After _extract_step_value: extracted_value_type={extracted_type}, "
        f"extracted_value={extracted_repr}, current_step={current_step}, "
        f"params_strategy={params.get('strategy')}, params_rsi_period={params.get('rsi_period')}, "
        f"params_atr_period={params.get('atr_period')}, params_ema_period={params.get('ema_period')}, "
        f"params_snapshot={params}"
    )
    
    # PATCH: indicator periods - gestisci salvataggio periodi (step strategy / strategy_params)
    # Usa period_index per chiedere ogni periodo in ordine (EMA -> ATR -> RSI) e sovrascrivere SEMPRE il valore
    if current_step in ("strategy", "strategy_params"):
        required = free_plan.get_required_period_fields_ordered(params)
        period_index = cs.get("period_index", 0)
        missing_step = required[period_index] if (required and period_index < len(required)) else None
        
        logger.info(
            f"[ANALYSIS_D_ENTRY] Entering periodi block: current_step={current_step}, "
            f"missing_step={missing_step}, period_index={period_index}, required={required}, "
            f"params_rsi_period={params.get('rsi_period')}, params_atr_period={params.get('atr_period')}, "
            f"params_ema_period={params.get('ema_period')}"
        )
        
        if missing_step:
            logger.info(f"[ANALYSIS_D] missing_step={missing_step}, params before assignment: strategy={params.get('strategy')}, rsi={params.get('rsi_period')}, atr={params.get('atr_period')}, ema={params.get('ema_period')}")
            period_saved = False
            period_value = None
            
            # Se abbiamo estratto un periodo tramite _extract_step_value
            if isinstance(extracted_value, dict) and "indicator" in extracted_value:
                period_value = extracted_value["period"]
                # Se period è None, usa il default
                if period_value is None:
                    defaults = {"RSI": 14, "ATR": 14, "EMA": 200}
                    indicator = extracted_value["indicator"]
                    period_value = defaults.get(indicator, 14)
                period_saved = True
            
            # Se l'utente ha scritto solo un numero, estrailo e salvalo
            if not period_saved:
                num_match = re.search(r"^(\d+)$", user_text.strip())
                if num_match:
                    try:
                        period_value = int(num_match.group(1))
                        if period_value > 0:
                            period_saved = True
                    except:
                        pass
            
            # Se abbiamo un periodo valido, VALIDALO e applicalo SEMPRE (sovrascrivi)
            if period_saved and period_value is not None:
                indicator_map = {
                    "ema_period": "EMA",
                    "atr_period": "ATR",
                    "rsi_period": "RSI"
                }
                indicator = indicator_map.get(missing_step)
                
                if indicator:
                    is_valid, error_msg = validators.validate_indicator_period(indicator, period_value)
                    if not is_valid:
                        missing_indicator = indicator_map.get(missing_step, missing_step.replace("_period", "").upper())
                        error_count_dict = cs.get("error_count", {})
                        error_count = error_count_dict.get("strategy", 0)
                        
                        if missing_indicator == "EMA":
                            error_variant = phrases.get_invalid_ema_period(str(period_value), error_count)
                            ask_variant = phrases.get_ask_ema_period(error_count)
                        elif missing_indicator == "RSI":
                            error_variant = phrases.get_invalid_rsi_period(str(period_value), error_count)
                            ask_variant = phrases.get_ask_rsi_period(error_count)
                        elif missing_indicator == "ATR":
                            error_variant = phrases.get_invalid_atr_period(str(period_value), error_count)
                            ask_variant = phrases.get_ask_atr_period(error_count)
                        else:
                            error_variant = error_msg
                            ask_variant = f"Che periodo vuoi per {missing_indicator}?"
                        
                        reply = error_variant + "\n\n" + ask_variant
                        error_count += 1
                        error_count_dict["strategy"] = error_count
                        cs["error_count"] = error_count_dict
                        params = _sync_strategy_from_periods(params)
                        state, cs, params = _sync_state(state, cs, params)
                        _log_final_report(state, "PERIODI_ERROR")
                        return {
                            "reply": reply,
                            "state": state
                        }
                
                # Periodo valido: sovrascrivi SEMPRE con _apply_period_input
                params = _apply_period_input(params, missing_step, period_value)
                logger.info("[PERIOD] applied %s=%s", missing_step, period_value)
                logger.info("[PERIOD] params periods: rsi_period=%s atr_period=%s ema_period=%s",
                            params.get("rsi_period"), params.get("atr_period"), params.get("ema_period"))
                cs["period_index"] = period_index + 1
                
                params = _sync_strategy_from_periods(params)
                
                # Altri periodi da chiedere o avanza
                next_period_index = cs.get("period_index", period_index + 1)
                if required and next_period_index < len(required):
                    next_missing = required[next_period_index]
                    next_indicator = next_missing.replace("_period", "").upper()
                    if next_missing == "ema_period":
                        reply = phrases.get_ask_ema_period(0)
                    elif next_missing == "rsi_period":
                        reply = phrases.get_ask_rsi_period(0)
                    else:
                        reply = phrases.get_ask_atr_period(0)
                    cs["step"] = current_step if current_step == "strategy_params" else "strategy_params"
                    state, cs, params = _sync_state(state, cs, params)
                    _log_final_report(state, "PERIODI_NEXT")
                    return {"reply": reply, "state": state}
                
                next_step = _get_next_step(current_step, params, cs)
                if next_step is None:
                    state["config_status"] = "complete"
                    _cleanup_config_state_when_complete(cs)
                    params = _sync_strategy_from_periods(params)
                    state, cs, params = _sync_state(state, cs, params)
                    _log_final_report(state, "PERIODI_COMPLETE")
                    return {
                        "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                        "state": state
                    }
                
                cs["step"] = next_step
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                reply = _step_question(next_step, params)
                _log_final_report(state, "PERIODI_NEXT")
                return {"reply": reply, "state": state}
            else:
                # Input non valido: ripeti la domanda per il periodo corrente
                if missing_step == "ema_period":
                    reply = phrases.get_ask_ema_period(cs.get("error_count", {}).get("strategy", 0))
                elif missing_step == "rsi_period":
                    reply = phrases.get_ask_rsi_period(cs.get("error_count", {}).get("strategy", 0))
                else:
                    reply = phrases.get_ask_atr_period(cs.get("error_count", {}).get("strategy", 0))
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": reply, "state": state}
        else:
            # Nessun periodo da chiedere (tutti già raccolti o strategia senza periodi) -> avanza
            next_step = _get_next_step(current_step, params, cs)
            if next_step is None:
                state["config_status"] = "complete"
                _cleanup_config_state_when_complete(cs)
                params = _sync_strategy_from_periods(params)
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                    "state": state
                }
            cs["step"] = next_step
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": _step_question(next_step, params),
                "state": state
            }
    
    # GESTIONE STEP "strategy_params" (fallback: raccolta periodi con period_index, sovrascrivi sempre)
    if current_step == "strategy_params":
        strategy_id = params.get("free_strategy_id")
        if strategy_id is None:
            cs["step"] = "strategy"
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": _step_question("strategy", params),
                "state": state
            }
        
        required = free_plan.get_required_period_fields_ordered(params)
        period_index = cs.get("period_index", 0)
        field_name = required[period_index] if period_index < len(required) else None
        
        period_value = None
        num_match = re.search(r"^(\d+)$", user_text.strip())
        if num_match:
            try:
                period_value = int(num_match.group(1))
            except:
                pass
        
        if period_value is None or period_value <= 0 or not field_name:
            # Input non valido o nessun periodo da chiedere: ripeti domanda per il periodo corrente
            if field_name:
                if field_name == "ema_period":
                    question_text = phrases.get_ask_ema_period(0)
                elif field_name == "rsi_period":
                    question_text = phrases.get_ask_rsi_period(0)
                else:
                    question_text = phrases.get_ask_atr_period(0)
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": f"Devi inserire un numero valido.\n\n{question_text}",
                    "state": state
                }
            next_step = _get_next_step("strategy", params, cs)
            if next_step:
                cs["step"] = next_step
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": _build_summary(params) + "\n\n" + _step_question(next_step, params), "state": state}
            state["config_status"] = "complete"
            _cleanup_config_state_when_complete(cs)
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                "state": state
            }
        
        indicator_map = {"ema_period": "EMA", "atr_period": "ATR", "rsi_period": "RSI"}
        indicator = indicator_map.get(field_name)
        if indicator:
            is_valid, error_msg = validators.validate_indicator_period(indicator, period_value)
            if not is_valid:
                error_count_dict = cs.get("error_count", {})
                error_count = error_count_dict.get("strategy_params", 0)
                if field_name == "ema_period":
                    error_variant = phrases.get_invalid_ema_period(str(period_value), error_count)
                    ask_variant = phrases.get_ask_ema_period(error_count)
                elif field_name == "rsi_period":
                    error_variant = phrases.get_invalid_rsi_period(str(period_value), error_count)
                    ask_variant = phrases.get_ask_rsi_period(error_count)
                else:
                    error_variant = phrases.get_invalid_atr_period(str(period_value), error_count)
                    ask_variant = phrases.get_ask_atr_period(error_count)
                error_count_dict["strategy_params"] = error_count + 1
                cs["error_count"] = error_count_dict
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": error_variant + "\n\n" + ask_variant, "state": state}
        
        # Sovrascrivi SEMPRE con _apply_period_input
        params = _apply_period_input(params, field_name, period_value)
        logger.info("[PERIOD] applied %s=%s", field_name, period_value)
        logger.info("[PERIOD] params periods: rsi_period=%s atr_period=%s ema_period=%s",
                    params.get("rsi_period"), params.get("atr_period"), params.get("ema_period"))
        cs["period_index"] = period_index + 1
        params = _sync_strategy_from_periods(params)
        state, cs, params = _sync_state(state, cs, params)
        
        next_period_index = cs.get("period_index", period_index + 1)
        if required and next_period_index < len(required):
            next_field = required[next_period_index]
            if next_field == "ema_period":
                next_question_text = phrases.get_ask_ema_period(0)
            elif next_field == "rsi_period":
                next_question_text = phrases.get_ask_rsi_period(0)
            else:
                next_question_text = phrases.get_ask_atr_period(0)
            return {"reply": next_question_text, "state": state}
        
        next_step = _get_next_step("strategy", params, cs)
        if next_step is None:
            state["config_status"] = "complete"
            _cleanup_config_state_when_complete(cs)
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                "state": state
            }
        cs["step"] = next_step
        params = _sync_strategy_from_periods(params)
        state, cs, params = _sync_state(state, cs, params)
        return {
            "reply": _build_summary(params) + "\n\n" + _step_question(next_step, params),
            "state": state
        }
    
    # Valida il valore estratto con validazione rigorosa
    validation_result = None
    if extracted_value is not None:
        validation_result = _validate_step_value(current_step, extracted_value, params)
    
    if extracted_value is not None and validation_result and validation_result[0]:
        # Valore valido: salvalo
        is_valid, error_msg, warning_msg = validation_result
        
        # Se c'è un warning (es. leva alta), mostralo ma procedi
        warning_prefix = ""
        if warning_msg:
            warning_prefix = warning_msg + " "
        if current_step == "operating_mode":
            # Applica preset FREE: operating_mode, strategy_id, strategy_params (nessuna domanda periodi)
            params = _apply_operating_mode_preset(params, extracted_value)  # type: ignore[arg-type]
            state, cs, params = _sync_state(state, cs, params)
            # Avanza allo step successivo (es. leverage per futures), NON a strategy_params
            next_step = _get_next_step("operating_mode", params, cs)
            if next_step:
                cs["step"] = next_step
                question_text = _step_question(next_step, params)
                logger.info(
                    "[WIZARD_STEP] operating_mode done: next_step=%s cs[step]=%s question=%s",
                    next_step, cs["step"], question_text[:60] + "..." if len(question_text) > 60 else question_text,
                )
                state, cs, params = _sync_state(state, cs, params)
                reply = _build_summary(params) + "\n\n" + question_text
                return {"reply": reply, "state": state}
            # Config completa (improbabile dopo solo operating_mode)
            state["config_status"] = "complete"
            _cleanup_config_state_when_complete(cs)
            state, cs, params = _sync_state(state, cs, params)
            return {"reply": "Configurazione completata.\n\n" + _build_summary(params), "state": state}
        elif current_step == "symbol":
            # Salva symbol solo dopo validazione ok usando apply_config_patch per normalizzazione coerente
            patch_result = apply_config_patch(cs, {"symbol": extracted_value})
            if not patch_result.get("ok", True):
                state, cs, params = _sync_state(state, cs, params)
                return {
                    "reply": patch_result.get("message", "Symbol non valido.") + "\n\n" + _step_question("symbol", params),
                    "state": state,
                }
            if patch_result.get("warnings"):
                logger.warning(f"[FLOW_NORMAL] Warnings applying symbol: {patch_result['warnings']}")
            # apply_config_patch modifica cs["params"] direttamente
            params = cs["params"].copy()
            # Log quando symbol viene salvato correttamente (solo nel percorso valido)
            market_type = params.get("market_type", "futures")
            logger.info(f"[SYMBOL_OK] saved symbol=%s market_type=%s", params.get("symbol"), market_type)
            state, cs, params = _sync_state(state, cs, params)
            # Messaggio misto: riallinea la leva con limiti Bybit corretti ora che la coppia è salvata
            if params.get("market_type") == "futures":
                lev_ev = _extract_step_value(user_text, "leverage", params)
                if lev_ev is not None:
                    vr_lev = _validate_step_value("leverage", lev_ev, params)
                    if vr_lev[0]:
                        lev_int = int(lev_ev) if isinstance(lev_ev, int) else int(float(lev_ev))
                        req_c, _ = _check_leverage_warning(lev_int, params.get("symbol") or "questa coppia")
                        if not req_c:
                            pr2 = apply_config_patch(cs, {"leverage": lev_int})
                            if pr2.get("ok", True):
                                params = cs["params"].copy()
                                state, cs, params = _sync_state(state, cs, params)
            # Forza avanzamento step coerente dopo symbol
            next_step = _get_next_step("symbol", params, cs)
            if next_step:
                cs["step"] = next_step
        elif current_step == "market_type":
            params["market_type"] = extracted_value
            # Se market_type=spot, forza leverage=null e azzera pending_leverage_confirmation (BUG 1)
            if extracted_value == "spot":
                params["leverage"] = None
                cs["pending_leverage_confirmation"] = None
            state, cs, params = _sync_state(state, cs, params)
        elif current_step == "strategy":
            logger.info(f"[DBG_STRATEGY_ENTER] user_text={user_text!r} params_before={params}")
            
            # Gestione scelta preset 1-4 (strategie FREE) basata SOLO sui periodi.
            if isinstance(extracted_value, dict) and extracted_value.get("type") == "strategy_choice":
                strategy_id = extracted_value.get("strategy_id")
                if strategy_id and 1 <= strategy_id <= 4:
                    old_strategy_id = params.get("free_strategy_id")
                    
                    # Applica il preset: imposta free_strategy_id (memo UI) e azzera solo i periodi non previsti
                    params = free_plan.apply_free_strategy_to_params(params, strategy_id)
                    
                    # Dopo la scelta preset la configurazione torna in_progress finché non
                    # sono stati raccolti tutti i periodi richiesti
                    prev_status = state.get("config_status")
                    state["config_status"] = "in_progress"
                    logger.info(
                        f"[CONFIG_STATUS_RESET] from={prev_status} to=in_progress "
                        f"reason=strategy_change step_strategy old_strategy_id={old_strategy_id} new_strategy_id={strategy_id}"
                    )
                    
                    # Ricalcola sempre strategy a partire dai periodi (solo display)
                    params = recompute_strategy_from_periods(params)
                    state, cs, params = _sync_state(state, cs, params)
                    
                    # Porta lo step su strategy_params e inizia a chiedere i periodi in ordine (sovrascrivendo sempre)
                    cs["step"] = "strategy_params"
                    cs["period_index"] = 0
                    state, cs, params = _sync_state(state, cs, params)
                    
                    # Chiedi il primo periodo in ordine (EMA -> ATR -> RSI)
                    required = free_plan.get_required_period_fields_ordered(params)
                    period_question = None
                    if required:
                        first_field = required[0]
                        if first_field == "ema_period":
                            question_text = phrases.get_ask_ema_period(0)
                        elif first_field == "rsi_period":
                            question_text = phrases.get_ask_rsi_period(0)
                        else:
                            question_text = phrases.get_ask_atr_period(0)
                        period_question = (first_field, question_text)
                    if period_question:
                        field_name, question_text = period_question
                        logger.info(
                            f"[STRATEGY_CHOICE] Applied strategy_id={strategy_id}, "
                            f"next_missing_field={field_name}, params_snapshot={params}"
                        )
                        return {
                            "reply": question_text,
                            "state": state
                        }
                    else:
                        # Tutti i periodi richiesti sono già presenti: avanza al prossimo step
                        next_step = _get_next_step("strategy", params, cs)
                        if next_step is None:
                            state["config_status"] = "complete"
                            _cleanup_config_state_when_complete(cs)
                            params = recompute_strategy_from_periods(params)
                            state, cs, params = _sync_state(state, cs, params)
                            return {
                                "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                                "state": state
                            }
                        cs["step"] = next_step
                        params = recompute_strategy_from_periods(params)
                        state, cs, params = _sync_state(state, cs, params)
                        reply = _build_summary(params) + "\n\n" + _step_question(next_step, params)
                        return {
                            "reply": reply,
                            "state": state
                        }
            
            # Input non riconosciuto: mostra menu e chiedi di scegliere
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": _step_question("strategy", params),
                "state": state
            }
        elif current_step == "timeframe":
            patch_result = apply_config_patch(cs, {"timeframe": extracted_value})
            if not patch_result.get("ok", True):
                state, cs, params = _sync_state(state, cs, params)
                return {"reply": patch_result.get("message", "Timeframe non valido.") + "\n\n" + _step_question("timeframe", params), "state": state}
            if patch_result.get("warnings"):
                logger.warning(f"[FLOW_NORMAL] Warnings applying timeframe: {patch_result['warnings']}")
            params = cs["params"].copy()
            state, cs, params = _sync_state(state, cs, params)
        elif current_step == "leverage":
            if params.get("market_type") == "futures":
                # Valida prima (senza applicare)
                is_valid, error_msg, _ = _validate_step_value("leverage", extracted_value, params)
                if not is_valid:
                    state, cs, params = _sync_state(state, cs, params)
                    reply = (error_msg or "Leva non valida.") + "\n\n" + _step_question("leverage", params)
                    return {"reply": reply, "state": state}
                lev_int = int(float(extracted_value))
                sym = params.get("symbol") or "questa coppia"
                # BUG3: Verifica se leva alta richiede conferma (prima di applicare)
                requires_confirm, warning_msg = _check_leverage_warning(lev_int, sym)
                if requires_confirm:
                    cs["pending_leverage_confirmation"] = lev_int
                    state, cs, params = _sync_state(state, cs, params)
                    reply = warning_msg
                    if empathetic_response:
                        reply = empathetic_response + " " + reply
                    return {"reply": reply, "state": state}
                # Nessuna conferma richiesta: applica
                patch_result = apply_config_patch(cs, {"leverage": lev_int})
                if not patch_result.get("ok", True):
                    state, cs, params = _sync_state(state, cs, params)
                    reply = patch_result.get("message", "Leva non valida.") + "\n\n" + _step_question("leverage", params)
                    return {"reply": reply, "state": state}
                if patch_result.get("warnings"):
                    logger.warning(f"[FLOW_NORMAL] Warnings applying leverage: {patch_result['warnings']}")
                params = cs["params"].copy()
                state, cs, params = _sync_state(state, cs, params)
            else:
                params["leverage"] = None
                state, cs, params = _sync_state(state, cs, params)
        elif current_step == "risk_pct":
            # Verifica se richiede warning/conferma
            market_type = params.get("market_type", "futures")
            requires_confirm, warning_msg = _check_risk_warning(extracted_value, market_type)
            
            if requires_confirm:
                # Richiede conferma esplicita: salva temporaneamente e chiedi conferma
                cs["pending_risk_confirmation"] = extracted_value
                state, cs, params = _sync_state(state, cs, params)
                reply = warning_msg
                # Aggiungi frase empatica se rilevata
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {
                    "reply": reply,
                    "state": state
                }
            elif warning_msg:
                # Solo warning soft: salva e mostra warning, poi procedi
                params["risk_pct"] = extracted_value
                # Avanza al prossimo step
                next_step = _get_next_step(current_step, params, cs)
                if next_step is None:
                    state["config_status"] = "complete"
                    _cleanup_config_state_when_complete(cs)
                    state, cs, params = _sync_state(state, cs, params)
                    return {
                        "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                        "state": state
                    }
                cs["step"] = next_step
                state, cs, params = _sync_state(state, cs, params)
                reply = warning_msg + " " + _step_question(next_step, params)
                return {"reply": reply, "state": state}
            else:
                # Nessun warning: salva normalmente
                params["risk_pct"] = extracted_value
                state, cs, params = _sync_state(state, cs, params)
        elif current_step == "sl":
            # Normalizza formato
            sl_val = float(str(extracted_value).replace("%", ""))
            
            # Verifica se richiede warning/conferma
            requires_confirm, warning_msg, suggested_sl = _check_sl_warning(sl_val)
            
            if requires_confirm:
                # Richiede conferma esplicita: salva temporaneamente e chiedi conferma
                cs["pending_sl_confirmation"] = sl_val
                cs["suggested_sl"] = suggested_sl
                state, cs, params = _sync_state(state, cs, params)
                reply = warning_msg
                # Aggiungi frase empatica se rilevata
                if empathetic_response:
                    reply = empathetic_response + " " + reply
                return {
                    "reply": reply,
                    "state": state
                }
            else:
                # Nessun warning: salva normalmente usando apply_config_patch
                # apply_config_patch modifica cs["params"] direttamente
                patch_result = apply_config_patch(cs, {"sl": sl_val})
                if patch_result["warnings"]:
                    logger.warning(f"[FLOW_NORMAL] Warnings applying sl: {patch_result['warnings']}")
                # Sincronizza params da cs["params"] (modificato da apply_config_patch) prima di _sync_state
                params = cs["params"].copy()
                state, cs, params = _sync_state(state, cs, params)
        elif current_step == "tp":
            # Usa apply_config_patch per normalizzazione consistente
            # apply_config_patch modifica cs["params"] direttamente
            patch_result = apply_config_patch(cs, {"tp": extracted_value})
            if patch_result["warnings"]:
                logger.warning(f"[FLOW_NORMAL] Warnings applying tp: {patch_result['warnings']}")
            # Sincronizza params da cs["params"] (modificato da apply_config_patch) prima di _sync_state
            params = cs["params"].copy()
            state, cs, params = _sync_state(state, cs, params)
        
        # Avanza allo step successivo (primo campo mancante nella sequenza FREE)
        next_step = _get_next_step(current_step, params, cs)

        if next_step is None:
            # Tutti i parametri sono compilati
            state["config_status"] = "complete"
            _cleanup_config_state_when_complete(cs)
            params = _sync_strategy_from_periods(params)
            state, cs, params = _sync_state(state, cs, params)
            return {
                "reply": "Configurazione completata ✅\n\n" + _build_summary(params) + "\n\nVuoi avviare il bot adesso?",
                "state": state
            }
        
        cs["step"] = next_step
        params = _sync_strategy_from_periods(params)
        # Reset error_count quando si completa uno step con successo
        error_count_dict = cs.get("error_count", {})
        if current_step in error_count_dict:
            del error_count_dict[current_step]
        cs["error_count"] = error_count_dict
        state, cs, params = _sync_state(state, cs, params)
        if current_step == "strategy":
            logger.info(f"[STRATEGY_FIX] after _sync_state (strategy completed): cs['params']['strategy']={cs.get('params', {}).get('strategy')}, state['config_state']['params']['strategy']={state.get('config_state', {}).get('params', {}).get('strategy')}")
        
        # Costruisci risposta con la domanda del prossimo step (usa varianti naturali)
        question_text = _step_question(next_step, params, error_count=0, is_error=False)
        logger.info(
            "[WIZARD_STEP] step completed: current_step=%s next_step=%s cs[step]=%s question=%s",
            current_step, next_step, cs.get("step"), question_text[:70] + "..." if len(question_text) > 70 else question_text,
        )
        reply = question_text
        
        # Aggiungi warning se presente
        if warning_prefix:
            reply = warning_prefix + reply
        
        # Se c'è una frase empatica rilevata, aggiungila come prefisso (solo UNA frase)
        if empathetic_response:
            reply = empathetic_response + " " + reply
    else:
        # Valore non valido o mancante: BLOCCA il flusso e mostra errore chiaro
        # Incrementa error_count per questo step
        error_count_dict = cs.get("error_count", {})
        error_count = error_count_dict.get(current_step, 0)
        error_count += 1
        error_count_dict[current_step] = error_count
        cs["error_count"] = error_count_dict
        
        if extracted_value is not None and validation_result and not validation_result[0]:
            # Errore di validazione: mostra messaggio di errore umano e BLOCCA
            is_valid, error_msg, _ = validation_result
            
            # Costruisci messaggio variato senza "Perfetto/Ottimo"
            # Per symbol, timeframe, leverage: usa phrases per messaggi variati
            if current_step == "symbol":
                # LOGGING DIAGNOSTICO: symbol rifiutato durante flusso normale
                logger.info(
                    f"[SYMBOL_REJECT] file={__file__} function=handle_message "
                    f"step={current_step} config_status={state.get('config_status', 'N/A')} "
                    f"symbol_received={extracted_value} decision=rejected error_msg={error_msg}"
                )
                market_type = params.get("market_type", "spot")
                # Estrai il simbolo dall'errore se possibile
                symbol_from_error = str(extracted_value) if extracted_value else "quel simbolo"
                # Usa varianti per messaggio di errore e domanda
                error_msg_variato = phrases.get_invalid_symbol(symbol_from_error, market_type, error_count)
                question_variata = _step_question(current_step, params, error_count=error_count, is_error=True)
                reply = error_msg_variato + "\n\n" + question_variata
            elif current_step == "timeframe":
                market_type = params.get("market_type", "futures")
                valid_tfs = validators.get_valid_timeframes(None, market_type)
                # Mostra TUTTI i timeframe validi (non solo i primi 6)
                tf_examples = ", ".join(sorted(valid_tfs, key=lambda x: (
                    int(x[:-1]) if x[:-1].isdigit() else 999,
                    x[-1]
                )))
                input_value = extracted_value if extracted_value else "quel timeframe"
                error_msg_variato = phrases.get_invalid_timeframe(str(input_value), tf_examples, error_count)
                question_variata = _step_question(current_step, params, error_count=error_count, is_error=True)
                reply = error_msg_variato + "\n\n" + question_variata
            elif current_step == "leverage":
                symbol = params.get("symbol", "questa coppia")
                min_lev = 1.0
                max_lev = float(_leverage_max_for_params(params))
                error_msg_variato = phrases.get_invalid_leverage(symbol, min_lev, max_lev, error_count)
                question_variata = _step_question(current_step, params, error_count=error_count, is_error=True)
                reply = error_msg_variato + "\n\n" + question_variata
                orch_error_code = "invalid_leverage"
            elif current_step == "strategy":
                # Messaggio errore dedicato per strategy: invita a scegliere 1-4
                question_variata = _step_question(current_step, params, error_count=error_count, is_error=True)
                reply = "Scelta non valida. Scrivi 1, 2, 3 o 4.\n\n" + question_variata
            else:
                # Altri step: usa messaggio errore + domanda variata
                question_variata = _step_question(current_step, params, error_count=error_count, is_error=True)
                reply = error_msg + "\n\n" + question_variata
        else:
            # Valore non estratto o mancante: ripeti la stessa domanda (variata)
            if current_step == "symbol":
                # LOGGING DIAGNOSTICO: symbol non estratto durante flusso normale
                logger.info(
                    f"[SYMBOL_REJECT] file={__file__} function=handle_message "
                    f"step={current_step} config_status={state.get('config_status', 'N/A')} "
                    f"symbol_received=None decision=rejected reason=not_extracted"
                )
            elif current_step == "strategy":
                # Messaggio errore dedicato per strategy: invita a scegliere 1-4
                question_variata = _step_question(current_step, params, error_count=error_count, is_error=True)
                reply = "Scelta non valida. Scrivi 1, 2, 3 o 4.\n\n" + question_variata
            else:
                reply = _step_question(current_step, params, error_count=error_count, is_error=True)
        
        if wizard_parallel_success_msgs:
            reply += "\n\nHo comunque salvato: " + ", ".join(wizard_parallel_success_msgs) + "."
        
        if current_step not in ("strategy", "strategy_params"):
            params = _sync_strategy_from_periods(params)
        state, cs, params = _sync_state(state, cs, params)
        
        # Se c'è una frase empatica rilevata, aggiungila come prefisso (solo UNA frase)
        if empathetic_response:
            reply = empathetic_response + " " + reply
    
    # ============================================================
    # REPORT FINALE - Prima di return finale
    # ============================================================
    _log_final_report(state, "FINAL")
    final_params = state.get("config_state", {}).get("params", {}) or {}
    logger.info(
        "[PERSIST] params.strategy=%s free_strategy_id=%s ema_period=%s rsi_period=%s atr_period=%s",
        final_params.get("strategy"),
        final_params.get("free_strategy_id"),
        final_params.get("ema_period"),
        final_params.get("rsi_period"),
        final_params.get("atr_period"),
    )
    logger.info(f"[STRATEGY_FIX] before final return: state['config_state']['params']['strategy']={state.get('config_state', {}).get('params', {}).get('strategy')}")
    out: Dict[str, Any] = {"reply": reply, "state": state}
    if orch_error_code:
        out["error_code"] = orch_error_code
    return out


# ------------------------------------------------------------
# Compatibility entrypoint for app.py
# ------------------------------------------------------------

def run(payload, state=None, history=None, system_prompt: str = ""):
    """Compatibility wrapper used by FastAPI layer."""
    user_text = None
    if payload is not None:
        # Prova nell'ordine: user_input, message, text, attributo message
        if isinstance(payload, dict):
            user_text = payload.get("user_input") or payload.get("message") or payload.get("text")
            if state is None and "state" in payload:
                state = payload.get("state")
            if history is None and "history" in payload:
                history = payload.get("history")
        else:
            # Se è un oggetto, prova attributo message
            user_text = getattr(payload, "message", None) or getattr(payload, "user_input", None) or getattr(payload, "text", None)
    user_text = (user_text or "").strip()

    # Guard globale ad alta priorita': reset configurazione da qualsiasi step.
    if "reset configurazione" in user_text.lower():
        base_state = copy.deepcopy(state) if isinstance(state, dict) else {}
        config_state = copy.deepcopy(FORCE_FULL_RESET_CONFIG_STATE_SNAPSHOT)
        config_state["__force_full_reset"] = True
        base_state["config_state"] = config_state
        base_state["config_status"] = "in_progress"
        logger.info("[GLOBAL_RESET] reset configurazione intercettato in run(): __force_full_reset=True")
        return {
            "reply": "Configurazione resettata. Partiamo da capo: vuoi operare in Spot o in Futures?",
            "state": base_state,
        }

    result = handle_message(
        user_text=user_text,
        state=state or {},
        history=history or [],
        system_prompt=system_prompt,
    )

    # Log unico e chiaro dello stato config prima della persistenza esterna
    result_state = (result or {}).get("state") or {}
    cs = result_state.get("config_state") or {}
    params = cs.get("params") or {}
    current_step = cs.get("step")
    logger.info(
        "[ORCH] state keys: %s",
        list(result_state.keys()) if isinstance(result_state, dict) else type(result_state).__name__,
    )
    logger.info(
        "[CONFIG_STATE_BEFORE_SAVE] strategy=%s free_strategy_id=%s ema_period=%s rsi_period=%s atr_period=%s current_step=%s",
        params.get("strategy"),
        params.get("free_strategy_id"),
        params.get("ema_period"),
        params.get("rsi_period"),
        params.get("atr_period"),
        current_step,
    )

    return result

# ============================================================
# TEST MANUALE: apply_config_patch
# ============================================================
def test_apply_config_patch():
    """
    Test manuale per apply_config_patch.
    Simula patch {"timeframe":"1m","sl":"4%"} e verifica che config_state.params.timeframe e params.sl siano aggiornati.
    """
    print("\n" + "="*60)
    print("TEST MANUALE: apply_config_patch")
    print("="*60)
    
    # Crea config_state iniziale
    config_state = {
        "step": "timeframe",
        "params": {
            "symbol": "BTCUSDT",
            "market_type": "futures",
            "strategy": ["RSI"],
            "timeframe": "15m",  # Valore iniziale
            "leverage": 10,
            "sl": "3.0%",  # Valore iniziale
            "tp": "6.0%",
            "risk_pct": 2.0,
            "rsi_period": 14,
            "ema_period": None,
            "atr_period": None
        }
    }
    
    print("\n[INITIAL STATE]")
    print(f"  timeframe: {config_state['params'].get('timeframe')}")
    print(f"  sl: {config_state['params'].get('sl')}")
    
    # Test patch con timeframe e sl
    patch_dict = {"timeframe": "1m", "sl": "4%"}
    print(f"\n[APPLYING PATCH] {patch_dict}")
    
    result = apply_config_patch(config_state, patch_dict)
    
    print("\n[RESULT]")
    print(f"  Changed: {result['changed']}")
    if result['warnings']:
        print(f"  Warnings: {result['warnings']}")
    
    print("\n[FINAL STATE]")
    print(f"  timeframe: {config_state['params'].get('timeframe')}")
    print(f"  sl: {config_state['params'].get('sl')}")
    
    # Verifica
    assert config_state['params']['timeframe'] == "1m", f"Expected '1m', got {config_state['params']['timeframe']}"
    assert config_state['params']['sl'] == "4.0%", f"Expected '4.0%', got {config_state['params']['sl']}"
    
    print("\n✅ TEST PASSED: timeframe e sl aggiornati correttamente!")
    

# ============================================================
# TEST MANUALE GUIDATO: aggiungere EMA a RSI+ATR
# ============================================================
# Scenario da riprodurre end‑to‑end (con backend collegato a Supabase):
# 1) Avvia una nuova configurazione e completa i passi fino a scegliere una strategia RSI+ATR,
#    inserendo i relativi periodi (es. rsi_period=14, atr_period=14).
# 2) Completa almeno fino allo step timeframe (es. scegli "1h"), in modo che
#    config_state.step diventi "timeframe".
# 3) A questo punto, invia in chat la frase: "voglio aggiungere EMA"
#    (sono supportate anche varianti tipo "aggiungo ema", "add ema").
#    - Verifica nei log che compaia [CONFIG_STATE_BEFORE_SAVE] con free_strategy_id=4.
# 4) Quando il bot chiede il periodo EMA, rispondi con "10".
#    - Verifica nei log che, dopo questa risposta, [CONFIG_STATE_BEFORE_SAVE]
#      mostri ema_period=10, rsi_period invariato, atr_period invariato,
#      free_strategy_id=4 e strategy contenente ["EMA", "RSI", "ATR"] (ordine normalizzato).
# 5) Controlla in Supabase la riga di configurazione corrispondente e verifica che:
#    - config_state.params.ema_period == 10
#    - config_state.params.free_strategy_id == 4
#    - eventuali periodi già presenti (rsi_period, atr_period) NON siano stati azzerati.
    # Test con alias
    print("\n" + "-"*60)
    print("TEST ALIAS: stoploss -> sl")
    print("-"*60)
    
    config_state2 = {
        "step": "timeframe",
        "params": {
            "sl": "2.0%",
            "tp": "5.0%"
        }
    }
    
    patch_dict2 = {"stoploss": "5%", "takeprofit": "10%"}
    print(f"\n[APPLYING PATCH] {patch_dict2}")
    
    result2 = apply_config_patch(config_state2, patch_dict2)
    
    print(f"\n[RESULT] Changed: {result2['changed']}")
    print(f"  sl: {config_state2['params'].get('sl')}")
    print(f"  tp: {config_state2['params'].get('tp')}")
    
    assert config_state2['params']['sl'] == "5.0%", f"Expected '5.0%', got {config_state2['params']['sl']}"
    assert config_state2['params']['tp'] == "10.0%", f"Expected '10.0%', got {config_state2['params']['tp']}"
    
    print("\n✅ TEST PASSED: alias funzionano correttamente!")
    
    print("\n" + "="*60)
    print("TUTTI I TEST PASSATI!")
    print("="*60 + "\n")

# ============================================================
# TEST MANUALE (non eseguito in prod): deep_merge_config
# ============================================================
# Eseguire manualmente per verificare merge:
#   existing = {"params": {"rsi_period": 14, "atr_period": 10, "ema_period": None}}
#   incoming = {"params": {"rsi_period": 7}}
#   result = deep_merge_config(existing, incoming)
#   => result["params"]["rsi_period"] == 7, result["params"]["atr_period"] == 10
#
#   incoming_none = {"params": {"rsi_period": None}}
#   result2 = deep_merge_config(existing, incoming_none)
#   => result2["params"]["rsi_period"] resta 14 (None = non sovrascrivere)
#
# def _test_deep_merge_config_manual():
#     existing = {"step": "timeframe", "params": {"rsi_period": 14, "atr_period": 10, "ema_period": None}}
#     incoming = {"params": {"rsi_period": 7}}
#     result = deep_merge_config(existing, incoming)
#     assert result["params"]["rsi_period"] == 7, f"expected 7, got {result['params']['rsi_period']}"
#     assert result["params"]["atr_period"] == 10, f"expected 10, got {result['params']['atr_period']}"
#     incoming_none = {"params": {"rsi_period": None}}
#     result2 = deep_merge_config(existing, incoming_none)
#     assert result2["params"]["rsi_period"] == 14, f"None must not overwrite: expected 14, got {result2['params']['rsi_period']}"
#     print("deep_merge_config manual checks OK")


if __name__ == "__main__":
    # Esegui test manuale
    test_apply_config_patch()

"""
Supabase queue module - gestisce comandi e eventi runner via Supabase REST API.
Usa requests per chiamate HTTP dirette a Supabase PostgREST.
Validazione env lazy: non raise a import-time, solo quando le funzioni vengono chiamate.
"""
import os
import logging
import requests
from typing import Dict, Any, Optional, List, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)


def _get_rest_config() -> Tuple[str, Dict[str, str]]:
    """
    Ritorna (rest_base_url, rest_headers). Valida env solo al primo uso.
    Raises RuntimeError solo quando la funzione viene chiamata, non a import-time.
    """
    url = os.getenv("SUPABASE_URL", "").strip()
    raw_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
    key = raw_key.strip()

    if not url:
        logger.error("[SUPABASE_QUEUE] SUPABASE_URL missing in .env")
        raise RuntimeError("SUPABASE_URL missing in .env")
    if not key:
        logger.error("[SUPABASE_QUEUE] SUPABASE_SERVICE_KEY or SUPABASE_SERVICE_ROLE_KEY missing in .env")
        raise RuntimeError("SUPABASE_SERVICE_KEY or SUPABASE_SERVICE_ROLE_KEY missing in .env")

    rest_base_url = f"{url.rstrip('/')}/rest/v1"
    rest_headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    return rest_base_url, rest_headers


def enqueue_runner_command(
    device_id: str,
    payload: dict,
    user_id: Optional[str] = None,
) -> str:
    """
    Inserisce un comando nella tabella runner_commands.
    
    Args:
        device_id: ID del device runner
        payload: Dizionario con i dati del comando (es. {"action": "TRADE_OPEN", "symbol": "ETHUSDT", ...})
        user_id: Opzionale; se assente si usa payload["user_id"] quando presente (non si inventa valori).
    
    Returns:
        command_id: UUID del comando inserito
    
    Raises:
        RuntimeError: Se la chiamata a Supabase fallisce
    """
    if not device_id:
        raise ValueError("device_id is required")
    
    # Genera UUID per il comando
    command_id = str(uuid4())
    
    # Prepara payload per inserimento
    insert_data = {
        "id": command_id,
        "device_id": device_id,
        "status": "pending",
        "payload": payload
    }

    resolved_uid: Optional[str] = None
    if user_id is not None and str(user_id).strip():
        resolved_uid = str(user_id).strip()
    elif isinstance(payload, dict):
        p_uid = payload.get("user_id")
        if p_uid is not None and str(p_uid).strip():
            resolved_uid = str(p_uid).strip()
    if resolved_uid:
        insert_data["user_id"] = resolved_uid
    
    # Estrai informazioni per logging
    command_text = payload.get("text", "N/A") if isinstance(payload, dict) else "N/A"
    status_initial = "pending"
    schema_table = "public.runner_commands"
    
    # LOG PRIMA DELL'INSERT
    logger.info(
        f"[ENQUEUE_BEFORE] About to insert runner command - "
        f"schema_table={schema_table}, "
        f"user_id={resolved_uid if resolved_uid else 'N/A'}, "
        f"device_id={device_id}, "
        f"command_text={command_text}, "
        f"status_initial={status_initial}, "
        f"command_id={command_id}, "
        f"payload_complete={payload}"
    )
    
    # URL esplicito per public.runner_commands (PostgREST usa public di default, ma essere espliciti)
    rest_base_url, rest_headers = _get_rest_config()
    url = f"{rest_base_url}/runner_commands"

    try:
        response = requests.post(
            url,
            headers=rest_headers,
            json=insert_data,
            timeout=10.0
        )
        
        # Salva response.text PRIMA di qualsiasi altra operazione (può essere letto solo una volta)
        response_text = response.text
        
        # LOG DOPO L'INSERT - response RAW
        response_raw = response_text
        error_present = None
        rows_inserted = 0
        result = None
        
        try:
            if response.ok:
                result = response.json()
                # result è una lista con il record inserito (grazie a Prefer: return=representation)
                if result and isinstance(result, list):
                    rows_inserted = len(result)
                elif result:
                    # Se non è una lista, potrebbe essere un singolo dict
                    rows_inserted = 1 if result else 0
            else:
                error_present = response_text[:500] if response_text else "No error details"
        except Exception as parse_error:
            response_raw = f"<Error parsing response: {str(parse_error)}>"
            error_present = str(parse_error)
        
        logger.info(
            f"[ENQUEUE_AFTER] Insert completed - "
            f"schema_table={schema_table}, "
            f"device_id={device_id}, "
            f"command_id={command_id}, "
            f"response_status={response.status_code}, "
            f"response_raw={response_raw}, "
            f"error={error_present if error_present else 'None'}, "
            f"rows_inserted={rows_inserted}"
        )
        
        if not response.ok:
            error_text = response_text[:500] if response_text else "No error details"
            error_msg = f"Supabase POST failed: {response.status_code} - {error_text}"
            logger.error(
                f"[SUPABASE_QUEUE] {error_msg} - "
                f"device_id={device_id}, command_id={command_id}, "
                f"payload_keys={list(payload.keys())}"
            )
            raise RuntimeError(error_msg)
        
        # result già parsato sopra, se None significa che c'è stato un errore nel parsing
        if result is None:
            error_msg = "Failed to parse response from Supabase"
            logger.error(
                f"[SUPABASE_QUEUE] {error_msg} - "
                f"device_id={device_id}, command_id={command_id}, "
                f"response_text={response_text[:200]}"
            )
            raise RuntimeError(error_msg)
        # result è una lista con il record inserito
        if result and len(result) > 0:
            returned_id = result[0].get("id")
            logger.info(
                f"[SUPABASE_QUEUE] Command enqueued successfully: "
                f"command_id={returned_id}, device_id={device_id}, "
                f"payload_text={payload.get('text', 'N/A')[:50]}"
            )
            return returned_id
        else:
            # Fallback: usa l'ID che abbiamo generato
            logger.warning(
                f"[SUPABASE_QUEUE] POST returned empty response, using generated id={command_id} - "
                f"device_id={device_id}"
            )
            return command_id
            
    except requests.RequestException as e:
        error_msg = f"Request exception during enqueue: {str(e)}"
        logger.error(
            f"[SUPABASE_QUEUE] {error_msg} - "
            f"device_id={device_id}, command_id={command_id}",
            exc_info=True
        )
        raise RuntimeError(error_msg)


def list_runner_events(device_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Legge gli ultimi eventi dalla tabella runner_events per un device_id.
    
    Args:
        device_id: ID del device runner
        limit: Numero massimo di eventi da restituire (default: 20)
    
    Returns:
        Lista di dizionari con gli eventi (ordinati per created_at desc)
    
    Raises:
        RuntimeError: Se la chiamata a Supabase fallisce
    """
    if not device_id:
        raise ValueError("device_id is required")

    rest_base_url, rest_headers = _get_rest_config()
    url = f"{rest_base_url}/runner_events"

    # Query parameters per PostgREST
    params = {
        "device_id": f"eq.{device_id}",
        "order": "created_at.desc",
        "limit": str(limit)
    }
    
    try:
        response = requests.get(
            url,
            headers=rest_headers,
            params=params,
            timeout=10.0
        )
        
        if not response.ok:
            error_msg = f"Supabase GET failed: {response.status_code} - {response.text[:500]}"
            logger.error(f"[SUPABASE_QUEUE] {error_msg}")
            raise RuntimeError(error_msg)
        
        events = response.json()
        logger.info(f"[SUPABASE_QUEUE] Retrieved {len(events)} events for device_id={device_id}")
        return events if isinstance(events, list) else []
        
    except requests.RequestException as e:
        error_msg = f"Request exception: {str(e)}"
        logger.error(f"[SUPABASE_QUEUE] {error_msg}")
        raise RuntimeError(error_msg)


def get_latest_runner_status(device_id: str) -> Optional[Dict[str, Any]]:
    """
    Ottiene l'ultimo evento/status per un device_id (facoltativo).
    
    Args:
        device_id: ID del device runner
    
    Returns:
        Dizionario con l'ultimo evento o None se non trovato
    """
    if not device_id:
        return None
    
    try:
        events = list_runner_events(device_id, limit=1)
        if events and len(events) > 0:
            return events[0]
        return None
    except Exception as e:
        logger.warning(f"[SUPABASE_QUEUE] Error getting latest status: {e}")
        return None


"""
Supabase client factory per Idith backend.

Fornisce due tipi di client:
- get_public_supabase(): usa ANON_KEY (per client/runner)
- get_service_supabase(): usa SERVICE_ROLE_KEY (solo backend/server)

IMPORTANTE: get_service_supabase() NON deve essere usato da moduli runner/client.
"""
import os
import logging
import inspect
from typing import Optional
from supabase import create_client, Client

from idith.config.env import load_env, get_env_var

logger = logging.getLogger(__name__)

# Cache per i client (singleton pattern)
_public_client: Optional[Client] = None
_service_client: Optional[Client] = None


def _is_runner_or_client_module() -> bool:
    """
    Verifica se il chiamante è un modulo runner/client.
    
    Controlla il nome del modulo chiamante per evitare che runner/client
    usino il service role key.
    """
    frame = inspect.currentframe()
    if not frame:
        return False
    
    # Salta questo frame (_is_runner_or_client_module) e quello di get_service_supabase
    # frame -> get_service_supabase -> chiamante reale
    caller_frame = frame.f_back
    if caller_frame:
        caller_frame = caller_frame.f_back  # Salta get_service_supabase
    if caller_frame:
        caller_frame = caller_frame.f_back  # Va al chiamante reale
    
    if not caller_frame:
        return False
    
    module_name = caller_frame.f_globals.get("__name__", "")
    file_path = caller_frame.f_code.co_filename
    
    # Moduli runner/client tipici (per nome modulo)
    runner_keywords = ["runner", "client", "tray"]
    if any(keyword in module_name.lower() for keyword in runner_keywords):
        return True
    
    # Controlla anche il path del file
    if "runner" in file_path.lower() or "client" in file_path.lower() or "tray" in file_path.lower():
        return True
    
    return False


def get_public_supabase() -> Client:
    """
    Crea/restituisce un client Supabase con ANON_KEY.
    
    Questo client può essere usato da runner/client.
    Ha permessi limitati secondo le policy RLS di Supabase.
    
    Returns:
        Client Supabase configurato con ANON_KEY
    
    Raises:
        RuntimeError: Se SUPABASE_URL o SUPABASE_ANON_KEY non sono configurati
    """
    global _public_client
    
    if _public_client is not None:
        return _public_client
    
    # Carica env se non già fatto
    load_env()
    
    url = get_env_var("SUPABASE_URL", required=True)
    anon_key = get_env_var("SUPABASE_ANON_KEY", required=True)
    
    if not url or not anon_key:
        raise RuntimeError(
            "SUPABASE_URL e SUPABASE_ANON_KEY devono essere configurati. "
            "Verifica il file .env"
        )
    
    _public_client = create_client(url, anon_key)
    logger.info("[SUPABASE] Client pubblico (ANON) creato")
    
    return _public_client


def get_service_supabase() -> Client:
    """
    Crea/restituisce un client Supabase con SERVICE_ROLE_KEY.
    
    ⚠️ ATTENZIONE: Questo client bypassa RLS e ha permessi amministrativi.
    NON deve essere usato da moduli runner/client.
    
    Guard rail: se chiamato da un modulo runner/client, logga un warning
    e solleva un errore.
    
    Returns:
        Client Supabase configurato con SERVICE_ROLE_KEY
    
    Raises:
        RuntimeError: 
            - Se SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY non sono configurati
            - Se chiamato da un modulo runner/client
    """
    global _service_client
    
    # Guard rail: verifica che non sia chiamato da runner/client
    if _is_runner_or_client_module():
        frame = inspect.currentframe()
        caller_name = "unknown"
        caller_file = "unknown"
        if frame and frame.f_back:
            caller_name = frame.f_back.f_globals.get("__name__", "unknown")
            caller_file = frame.f_back.f_code.co_filename
        
        error_msg = (
            f"⚠️ SICUREZZA: get_service_supabase() chiamato da modulo runner/client\n"
            f"  Modulo: {caller_name}\n"
            f"  File: {caller_file}\n"
            f"Service role key NON deve essere usata da runner/client.\n"
            f"Usa get_public_supabase() invece."
        )
        logger.error(f"[SUPABASE] {error_msg}")
        raise RuntimeError(error_msg)
    
    if _service_client is not None:
        return _service_client
    
    # Carica env se non già fatto
    load_env()
    
    url = get_env_var("SUPABASE_URL", required=True)
    service_key = get_env_var("SUPABASE_SERVICE_ROLE_KEY", required=True)
    
    # Supporta anche il nome legacy SUPABASE_SERVICE_KEY
    if not service_key:
        service_key = get_env_var("SUPABASE_SERVICE_KEY", required=False)
        if service_key:
            logger.warning(
                "[SUPABASE] Trovato SUPABASE_SERVICE_KEY (legacy). "
                "Considera di usare SUPABASE_SERVICE_ROLE_KEY invece."
            )
    
    if not url or not service_key:
        raise RuntimeError(
            "SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY devono essere configurati. "
            "Verifica il file .env"
        )
    
    _service_client = create_client(url, service_key)
    logger.info("[SUPABASE] Client service (SERVICE_ROLE) creato")
    
    return _service_client


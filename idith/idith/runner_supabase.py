"""
Runner Supabase standalone - legge comandi da Supabase e logga eventi.
Non tocca la chat e non modifica app.py.
"""
import os
import sys
import time
import json
import logging
import signal
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

# Prova a importare supabase-py, altrimenti usa il client REST
try:
    from supabase import create_client, Client
    HAS_SUPABASE_PY = True
except ImportError:
    HAS_SUPABASE_PY = False
    try:
        from .supabase_rest import SupabaseRestClient
    except ImportError:
        from idith.supabase_rest import SupabaseRestClient

logger = logging.getLogger(__name__)


def setup_logging(log_file_path: Path):
    """Configura logging su console e file."""
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    # File handler
    file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    
    logger.info(f"Logging configurato: console + file ({log_file_path})")


def load_env() -> tuple[str, Optional[Path]]:
    """
    Carica .env con priorità:
    1. %APPDATA%\IdithRunner\.env (Windows)
    2. .env nella cwd
    
    Returns:
        (env_path_used, Path object del file usato)
    """
    # Prova prima %APPDATA%\IdithRunner\.env
    appdata = os.getenv('APPDATA')
    if appdata:
        appdata_env = Path(appdata) / 'IdithRunner' / '.env'
        if appdata_env.exists():
            load_dotenv(appdata_env, override=True)
            return str(appdata_env), appdata_env
    
    # Fallback a .env nella cwd
    cwd_env = Path.cwd() / '.env'
    if cwd_env.exists():
        load_dotenv(cwd_env, override=True)
        return str(cwd_env), cwd_env
    
    # Se non esiste nessuno, carica comunque (potrebbe essere in env di sistema)
    load_dotenv()
    return "system/env", None


def validate_env() -> tuple[str, str, str]:
    """
    Valida variabili d'ambiente obbligatorie.
    
    Returns:
        (supabase_url, supabase_anon_key, device_id)
    
    Raises:
        RuntimeError: Se manca qualche variabile
    """
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_anon_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    device_id = os.getenv("DEVICE_ID", "").strip()
    
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL mancante in .env")
    if not supabase_anon_key:
        raise RuntimeError("SUPABASE_ANON_KEY mancante in .env")
    if not device_id:
        raise RuntimeError("DEVICE_ID mancante in .env")
    
    return supabase_url, supabase_anon_key, device_id


def create_supabase_client(url: str, anon_key: str):
    """
    Crea client Supabase (usa supabase-py se disponibile, altrimenti REST).
    
    Returns:
        Client (supabase-py Client o SupabaseRestClient)
    """
    if HAS_SUPABASE_PY:
        logger.info("Usando supabase-py client")
        return create_client(url, anon_key)
    else:
        logger.info("Usando SupabaseRestClient (requests)")
        return SupabaseRestClient(url, anon_key)


def get_pending_command(client, device_id: str) -> Optional[dict]:
    """
    Cerca un comando pending per il device.
    
    Returns:
        Record del comando o None
    """
    try:
        if HAS_SUPABASE_PY:
            # Usa supabase-py
            result = client.table("runner_commands").select("*").eq("device_id", device_id).eq("status", "pending").order("created_at", desc=False).limit(1).execute()
            if result.data and len(result.data) > 0:
                return result.data[0]
            return None
        else:
            # Usa REST client
            result = client.select(
                "runner_commands",
                params={
                    "device_id": f"eq.{device_id}",
                    "status": "eq.pending"
                },
                order="created_at.asc",
                limit=1
            )
            if result and len(result) > 0:
                return result[0]
            return None
    except Exception as e:
        logger.error(f"Errore nel recupero comando: {e}", exc_info=True)
        return None


def mark_command_consumed(client, command_id: str) -> bool:
    """
    Marca un comando come consumed.
    
    Returns:
        True se successo, False altrimenti
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        if HAS_SUPABASE_PY:
            result = client.table("runner_commands").update({
                "status": "consumed",
                "consumed_at": now
            }).eq("id", command_id).execute()
            return len(result.data) > 0
        else:
            result = client.update(
                "runner_commands",
                match_dict={"id": f"eq.{command_id}"},
                payload={
                    "status": "consumed",
                    "consumed_at": now
                }
            )
            return len(result) > 0
    except Exception as e:
        logger.error(f"Errore nel marcare comando come consumed: {e}", exc_info=True)
        return False


def write_event(
    client,
    device_id: str,
    event_type: str,
    command_id: Optional[str],
    payload: Optional[dict],
    user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Scrive un evento in runner_events.
    
    Returns:
        ID dell'evento creato o None
    """
    try:
        event_data = {
            "device_id": device_id,
            "type": event_type,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        if command_id:
            event_data["command_id"] = command_id
        if payload:
            event_data["payload"] = payload
        if user_id is not None and str(user_id).strip():
            event_data["user_id"] = str(user_id).strip()
        
        if HAS_SUPABASE_PY:
            result = client.table("runner_events").insert(event_data).execute()
            if result.data and len(result.data) > 0:
                return result.data[0].get("id")
            return None
        else:
            result = client.insert("runner_events", event_data)
            if result and len(result) > 0:
                return result[0].get("id")
            return None
    except Exception as e:
        logger.error(f"Errore nella scrittura evento: {e}", exc_info=True)
        return None


# Flag globale per gestire Ctrl+C
shutdown_requested = False


def signal_handler(signum, frame):
    """Handler per SIGINT (Ctrl+C)."""
    global shutdown_requested
    logger.info("Ricevuto segnale di shutdown (Ctrl+C)")
    shutdown_requested = True


def main():
    """Main entry point del runner."""
    global shutdown_requested
    
    # Setup logging su file PRIMA di qualsiasi log
    appdata = os.getenv('APPDATA')
    if appdata:
        log_dir = Path(appdata) / 'IdithRunner' / 'logs'
        log_file = log_dir / 'runner.log'
    else:
        log_dir = Path.cwd() / 'logs'
        log_file = log_dir / 'runner.log'
    
    setup_logging(log_file)
    
    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Carica .env
    env_path, env_file = load_env()
    logger.info(f"Loaded env from: {env_path}")
    
    # Valida env
    try:
        supabase_url, supabase_anon_key, device_id = validate_env()
    except RuntimeError as e:
        logger.error(f"Errore validazione env: {e}")
        sys.exit(1)
    
    logger.info(f"Supabase URL: {supabase_url}")
    logger.info(f"Device: {device_id}")
    
    # Crea client Supabase
    try:
        client = create_supabase_client(supabase_url, supabase_anon_key)
    except Exception as e:
        logger.error(f"Errore nella creazione client Supabase: {e}", exc_info=True)
        sys.exit(1)
    
    # Loop principale
    last_heartbeat = time.time()
    heartbeat_interval = 30.0  # secondi
    
    logger.info("Runner avviato. In attesa di comandi...")
    
    try:
        while not shutdown_requested:
            # Cerca comando pending
            command = get_pending_command(client, device_id)
            
            if command:
                command_id = command.get("id")
                payload = command.get("payload", {})
                
                logger.info(f"COMMAND_RECEIVED id={command_id} payload={json.dumps(payload)}")
                
                # Marca come consumed
                if mark_command_consumed(client, command_id):
                    logger.info(f"COMMAND_CONSUMED id={command_id}")
                    
                    # Scrivi evento
                    raw_uid = command.get("user_id")
                    cmd_uid_s = str(raw_uid).strip() if raw_uid else None
                    event_id = write_event(
                        client,
                        device_id,
                        "COMMAND_RECEIVED",
                        command_id,
                        payload,
                        user_id=cmd_uid_s,
                    )
                    
                    if event_id:
                        logger.info(f"EVENT_WRITTEN id={event_id}")
                    else:
                        logger.warning(f"EVENT_WRITTEN fallito per command_id={command_id}")
                else:
                    logger.error(f"COMMAND_CONSUMED fallito per command_id={command_id}")
            else:
                # Heartbeat ogni 30 secondi
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    logger.info("heartbeat ok")
                    last_heartbeat = now
            
            # Sleep 2 secondi
            time.sleep(2.0)
    
    except KeyboardInterrupt:
        logger.info("Interruzione da tastiera")
    except Exception as e:
        logger.error(f"Errore nel loop principale: {e}", exc_info=True)
    finally:
        logger.info("Runner terminato")


if __name__ == "__main__":
    main()


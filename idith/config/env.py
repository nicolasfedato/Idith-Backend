"""
Environment configuration loader for Idith backend.

Carica variabili d'ambiente da:
1. .env nella root del repository (sviluppo)
2. %APPDATA%\\IdithRunner\\.env su Windows (runner)
"""
import os
import logging
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def load_env() -> List[str]:
    """
    Carica variabili d'ambiente da file .env in ordine di priorità.
    
    Ordine di caricamento:
    1. .env nella root del repository (sviluppo locale)
    2. %APPDATA%\\IdithRunner\\.env su Windows (runner installato)
    
    Non crasha se i file non esistono, ma logga da dove ha caricato.
    
    Returns:
        Lista di percorsi da cui sono stati caricati i file .env (anche vuota)
    """
    loaded_paths: List[str] = []
    
    # 1. Prova a caricare .env dalla root del repository
    # Assumiamo che il repo root sia 2 livelli sopra idith/config/
    repo_root = Path(__file__).parent.parent.parent
    local_env = repo_root / ".env"
    
    if local_env.exists():
        load_dotenv(local_env, override=False)  # override=False: non sovrascrive variabili già caricate
        loaded_paths.append(str(local_env))
        logger.info(f"[ENV] Caricato .env da: {local_env}")
    else:
        logger.debug(f"[ENV] File .env non trovato in: {local_env}")
    
    # 2. Su Windows, prova anche %APPDATA%\IdithRunner\.env
    if os.name == "nt":  # Windows
        appdata = os.getenv("APPDATA")
        if appdata:
            runner_env = Path(appdata) / "IdithRunner" / ".env"
            if runner_env.exists():
                load_dotenv(runner_env, override=False)
                loaded_paths.append(str(runner_env))
                logger.info(f"[ENV] Caricato .env da: {runner_env}")
            else:
                logger.debug(f"[ENV] File .env runner non trovato in: {runner_env}")
    
    if not loaded_paths:
        logger.warning("[ENV] Nessun file .env trovato. Usando solo variabili d'ambiente di sistema.")
    
    return loaded_paths


def get_env_var(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """
    Ottiene una variabile d'ambiente, con opzione di richiederla.
    
    Args:
        name: Nome della variabile d'ambiente
        default: Valore di default se non trovata
        required: Se True, solleva RuntimeError se la variabile non è presente
    
    Returns:
        Valore della variabile d'ambiente (str) o None
    
    Raises:
        RuntimeError: Se required=True e la variabile non è presente
    """
    value = os.getenv(name, default)
    
    if required and not value:
        raise RuntimeError(f"Variabile d'ambiente richiesta mancante: {name}")
    
    return value.strip() if value else None


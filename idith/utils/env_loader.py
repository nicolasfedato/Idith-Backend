# idith/utils/env_loader.py
"""
Caricamento robusto delle variabili d'ambiente da .env.
Cerca .env in ordine di fallback, indipendentemente dalla cartella di esecuzione.

Dove mettere .env (auto-rilevato in quest'ordine):
  1) Idith_Work/idith-backend/idith/.env
  2) Idith_Work/idith-backend/.env
  3) Idith_Work/.env
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Path assoluto di Idith_Work (repo root), derivato dalla posizione di questo file
# env_loader.py è in: idith-backend/idith/utils/env_loader.py
_THIS_FILE = Path(__file__).resolve()
_IDITH_PKG = _THIS_FILE.parent.parent  # idith-backend/idith
_IDITH_BACKEND = _IDITH_PKG.parent     # idith-backend
_IDITH_WORK = _IDITH_BACKEND.parent    # Idith_Work (repo root)

_ENV_CANDIDATES = [
    _IDITH_WORK / "idith-backend" / "idith" / ".env",
    _IDITH_WORK / "idith-backend" / ".env",
    _IDITH_WORK / ".env",
]


def load_env() -> bool:
    """
    Carica .env da uno dei path candidati (in ordine).
    Usa python-dotenv con override=False.
    Se nessun file viene trovato, logga warning e ritorna False (non crasha).

    Returns:
        True se almeno un file .env è stato caricato, False altrimenti.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("[ENV_LOADER] python-dotenv non installato, skip caricamento .env")
        return False

    loaded = False
    for path in _ENV_CANDIDATES:
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
            logger.info("[ENV_LOADER] Caricato .env da %s", path)
            loaded = True
            break

    if not loaded:
        logger.warning(
            "[ENV_LOADER] Nessun .env trovato. Cercato in: %s",
            [str(p) for p in _ENV_CANDIDATES],
        )
    return loaded

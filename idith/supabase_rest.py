"""
Supabase REST client minimal - usa requests per chiamare PostgREST API.
Non richiede supabase-py, solo requests.
"""
import json
import logging
import requests
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class SupabaseRestClient:
    """
    Client REST minimal per Supabase PostgREST.
    Usa solo requests e anon key.
    """
    
    def __init__(self, url: str, anon_key: str, timeout: float = 10.0):
        """
        Args:
            url: Supabase URL (es. https://xxx.supabase.co)
            anon_key: Supabase anon key
            timeout: Timeout per le richieste HTTP in secondi
        """
        self.base_url = url.rstrip('/')
        self.rest_url = f"{self.base_url}/rest/v1"
        self.anon_key = anon_key
        self.timeout = timeout
        
        self.headers = {
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
    
    def _log_error(self, method: str, url: str, status_code: int, response_text: str):
        """Log dettagliato degli errori HTTP."""
        logger.error(
            f"[SupabaseREST] {method} {url} -> {status_code}\n"
            f"Response body: {response_text[:500]}"
        )
    
    def select(self, table: str, params: Optional[Dict[str, Any]] = None, order: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        SELECT da una tabella.
        
        Args:
            table: Nome tabella
            params: Dict con filtri PostgREST (es. {"device_id": "eq.device123", "status": "eq.pending"})
            order: Ordering PostgREST (es. "created_at.asc" o "created_at.desc")
            limit: Limite di record da restituire
        
        Returns:
            Lista di record (dict)
        
        Raises:
            requests.RequestException: In caso di errore HTTP
        """
        url = f"{self.rest_url}/{table}"
        
        # Costruisci query string per PostgREST
        query_params = {}
        if params:
            for key, value in params.items():
                # Se value contiene già l'operatore (es. "eq.pending"), usa direttamente
                if isinstance(value, str) and '.' in value and value.split('.')[0] in ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'like', 'ilike', 'is']:
                    query_params[key] = value
                else:
                    # Default: uguaglianza
                    query_params[key] = f"eq.{value}"
        
        if order:
            query_params["order"] = order
        if limit is not None:
            query_params["limit"] = str(limit)
        
        try:
            response = requests.get(
                url,
                headers=self.headers,
                params=query_params,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as e:
            self._log_error("GET", url, e.response.status_code if e.response else 0, 
                          e.response.text if e.response else str(e))
            raise
        except requests.RequestException as e:
            logger.error(f"[SupabaseREST] GET {url} -> RequestException: {e}")
            raise
    
    def update(self, table: str, match_dict: Dict[str, Any], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        UPDATE di record che matchano i criteri.
        
        Args:
            table: Nome tabella
            match_dict: Criteri di match (es. {"id": "eq.uuid-here"})
            payload: Campi da aggiornare
        
        Returns:
            Lista di record aggiornati
        """
        url = f"{self.rest_url}/{table}"
        
        # Costruisci query string per il match
        query_params = {}
        for key, value in match_dict.items():
            if isinstance(value, str) and '.' in value:
                query_params[key] = value
            else:
                query_params[key] = f"eq.{value}"
        
        try:
            response = requests.patch(
                url,
                headers=self.headers,
                params=query_params,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as e:
            self._log_error("PATCH", url, e.response.status_code if e.response else 0,
                          e.response.text if e.response else str(e))
            raise
        except requests.RequestException as e:
            logger.error(f"[SupabaseREST] PATCH {url} -> RequestException: {e}")
            raise
    
    def insert(self, table: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        INSERT di un nuovo record.
        
        Args:
            table: Nome tabella
            payload: Dati del record
        
        Returns:
            Lista con il record inserito
        """
        url = f"{self.rest_url}/{table}"
        
        try:
            response = requests.post(
                url,
                headers=self.headers,
                json=payload,
                timeout=self.timeout
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as e:
            self._log_error("POST", url, e.response.status_code if e.response else 0,
                          e.response.text if e.response else str(e))
            raise
        except requests.RequestException as e:
            logger.error(f"[SupabaseREST] POST {url} -> RequestException: {e}")
            raise


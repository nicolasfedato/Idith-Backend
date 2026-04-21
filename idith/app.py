from __future__ import annotations
# app.py - Idith backend (Supabase single source of truth)
# Requirements: fastapi, uvicorn, python-dotenv (optional), supabase (supabase-py)


import os
import re
import string
import time
import random
import logging
import json
import uuid
import traceback
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, Literal
from threading import Lock
from pathlib import Path
from difflib import SequenceMatcher, get_close_matches

# CRITICO: load .env PRIMA di qualunque import opzionale o creazione client
def _bootstrap_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    base = Path(__file__).resolve().parent
    for p in [base / ".env", base.parent / ".env", base.parent.parent / ".env"]:
        if p.is_file():
            load_dotenv(dotenv_path=p, override=False)
            break
_bootstrap_dotenv()

# Logging prima degli import opzionali, così i tentativi su supabase_queue hanno handler su Railway.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from fastapi import FastAPI, Depends, HTTPException, Body, Request, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from openai import OpenAI


# Orchestrator (Idith conversation engine)
orchestrator_mod = None
try:
    from . import orchestrator as orchestrator_mod
    logging.getLogger(__name__).info("[ORCH] imported successfully (relative)")
except Exception as e1:
    try:
        import idith.orchestrator as orchestrator_mod
        logging.getLogger(__name__).info("[ORCH] imported successfully (idith.orchestrator)")
    except Exception as e2:
        try:
            from idith import orchestrator as orchestrator_mod
            logging.getLogger(__name__).info("[ORCH] imported successfully (idith.orchestrator)")
        except Exception as e3:
            orchestrator_mod = None
            logging.getLogger(__name__).error(
                "[ORCH] import failed. e1=%s e2=%s e3=%s",
                e1, e2, e3,
            )

orchestrator = orchestrator_mod

# Supabase queue module (dopo load_env, non fa raise a import-time).
# In deploy (es. uvicorn app:app dalla cartella idith/) il package relativo fallisce:
# servono fallback come per orchestrator, incluso import diretto del modulo sibling.
supabase_queue = None
try:
    from . import supabase_queue as _supabase_queue_mod
    supabase_queue = _supabase_queue_mod
    logger.info("[SUPABASE_QUEUE] import supabase_queue OK (relative: from . import supabase_queue)")
except Exception as e1:
    logger.warning(
        "[SUPABASE_QUEUE] import supabase_queue FAILED (relative), trying next: %s",
        e1,
        exc_info=True,
    )
    try:
        import idith.supabase_queue as _supabase_queue_mod

        supabase_queue = _supabase_queue_mod
        logger.info("[SUPABASE_QUEUE] import supabase_queue OK (idith.supabase_queue)")
    except Exception as e2:
        logger.warning(
            "[SUPABASE_QUEUE] import supabase_queue FAILED (idith.supabase_queue), trying next: %s",
            e2,
            exc_info=True,
        )
        try:
            from idith import supabase_queue as _supabase_queue_mod

            supabase_queue = _supabase_queue_mod
            logger.info("[SUPABASE_QUEUE] import supabase_queue OK (from idith import supabase_queue)")
        except Exception as e3:
            logger.warning(
                "[SUPABASE_QUEUE] import supabase_queue FAILED (from idith import), trying next: %s",
                e3,
                exc_info=True,
            )
            try:
                import supabase_queue as _supabase_queue_mod

                supabase_queue = _supabase_queue_mod
                logger.info("[SUPABASE_QUEUE] import supabase_queue OK (plain: import supabase_queue)")
            except Exception as e4:
                logger.error(
                    "[SUPABASE_QUEUE] import supabase_queue FAILED (all attempts exhausted). Last error: %s",
                    e4,
                    exc_info=True,
                )
                supabase_queue = None

# ----------------------------------------
# ENV
# ----------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
_raw_service_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
SUPABASE_SERVICE_KEY = _raw_service_key.strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
APP_ENV = os.getenv("ENV", "").strip().lower()

# Runner token TTL (in ore, default 24h per dev)
RUNNER_TOKEN_TTL_HOURS = float(os.getenv("RUNNER_TOKEN_TTL_HOURS", "24.0"))

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL missing from environment")

if not SUPABASE_SERVICE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_KEY or SUPABASE_SERVICE_ROLE_KEY missing from environment")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ----------------------------------------
# CIRCUIT BREAKER (in-memory, per-process)
# ----------------------------------------
class CircuitBreaker:
    """Simple circuit breaker to prevent thundering herd on DB failures."""
    def __init__(self, failure_threshold=3, timeout=5.0):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half_open
        self.lock = Lock()
    
    def record_success(self):
        """Reset on success."""
        with self.lock:
            self.failure_count = 0
            self.state = "closed"
            self.last_failure_time = None
    
    def record_failure(self):
        """Record failure and potentially open circuit."""
        with self.lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                logger.warning(f"[CIRCUIT_BREAKER] Circuit opened after {self.failure_count} failures")
    
    def is_open(self) -> bool:
        """Check if circuit is open (should skip DB call)."""
        with self.lock:
            if self.state == "closed":
                return False
            if self.state == "open":
                # Check if timeout expired -> move to half_open
                if self.last_failure_time and (time.time() - self.last_failure_time) >= self.timeout:
                    self.state = "half_open"
                    logger.info("[CIRCUIT_BREAKER] Moving to half_open state")
                    return False  # Allow one attempt
                return True
            # half_open: allow one attempt
            return False
    
    def should_attempt(self) -> bool:
        """Check if we should attempt DB call."""
        return not self.is_open()

# Global circuit breaker instance
db_circuit_breaker = CircuitBreaker(failure_threshold=3, timeout=5.0)

# ----------------------------------------
# CHAT DEVICE ID STORAGE (in-memory)
# ----------------------------------------
# Dizionario per salvare device_id per chat_id: chat_id -> device_id
_chat_device_ids: Dict[str, str] = {}
_chat_device_ids_lock = Lock()

def get_chat_device_id(chat_id: str) -> Optional[str]:
    """Ottiene il device_id associato a una chat_id."""
    with _chat_device_ids_lock:
        return _chat_device_ids.get(chat_id)

def set_chat_device_id(chat_id: str, device_id: str):
    """Imposta il device_id per una chat_id."""
    with _chat_device_ids_lock:
        _chat_device_ids[chat_id] = device_id
        logger.info(f"[CHAT_DEVICE] Set device_id={device_id} for chat_id={chat_id}")

# ----------------------------------------
# RETRY WITH BACKOFF
# ----------------------------------------
def retry_with_backoff(func, max_attempts=3, base_delay=0.15, max_delay=2.0):
    """
    Retry function with exponential backoff + jitter.
    Returns (success: bool, result: Any, error: Exception | None)
    """
    for attempt in range(max_attempts):
        try:
            result = func()
            return (True, result, None)
        except Exception as e:
            # Check if it's a retryable error
            error_str = str(e).lower()
            is_retryable = any(keyword in error_str for keyword in [
                "timeout", "connection", "temporarily unavailable", 
                "pool", "503", "502", "504", "500", "network"
            ])
            
            if not is_retryable or attempt == max_attempts - 1:
                # Non-retryable or last attempt
                return (False, None, e)
            
            # Calculate delay with exponential backoff + jitter
            delay = min(base_delay * (2 ** attempt), max_delay)
            jitter = delay * 0.3 * (random.random() * 2 - 1)  # ±30%
            final_delay = delay + jitter
            
            logger.warning(f"[RETRY] Attempt {attempt + 1}/{max_attempts} failed: {e}. Retrying in {final_delay:.3f}s")
            time.sleep(final_delay)
    
    return (False, None, None)

# ----------------------------------------
# APP
# ----------------------------------------
app = FastAPI(title="Idith Backend", version="1.2")

# PUBLIC PATHS: NON DEVONO RICHIEDERE TOKEN
PUBLIC_PATHS = ["/api/ping", "/api/runner/register", "/api/runner/heartbeat", "/docs", "/openapi.json", "/redoc"]

# ----------------------------------------
# CORS
# ----------------------------------------
# Browser `Origin` is normally without path; optional trailing slash covers odd clients.
_NETLIFY_APP_ORIGIN = "https://fastidious-buttercream-9b59bc.netlify.app"
_cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "").strip()
_cors_extra = [o.strip() for o in _cors_env.split(",") if o.strip()] if _cors_env else []
CORS_ALLOW_ORIGINS = list(
    dict.fromkeys(
        [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            _NETLIFY_APP_ORIGIN,
            f"{_NETLIFY_APP_ORIGIN}/",
            "https://idith.tech",
            "https://www.idith.tech",
            *_cors_extra,
        ]
    )
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    # Bearer token via Authorization: no credentialed cookies on cross-origin fetch → False
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------
# IDITH SYSTEM PROMPT (base identity & rules)
# ----------------------------------------

SYSTEM_BASE_IDITH = """
Sei IDITH, assistente per la configurazione di bot di trading su Bybit Testnet.

⚠️ REGOLE ASSOLUTE (NON VIOLARE MAI):

1. Segui SEMPRE e SOLO questa scaletta, senza saltare o aggiungere passi:
   1) Spot o Futures (PRIMA SCELTA ASSOLUTA - NON menzionare mai "coppia" prima di questa scelta)
   2) Coppia di trading (solo coppie USDT disponibili su Bybit)
   3) Timeframe
   4) Modalità operativa (aggressiva, equilibrata o selettiva)
   5) Stop Loss (percentuale)
   6) Take Profit (percentuale)
   7) Percentuale di capitale da rischiare per trade
   8) (SOLO FUTURES) Leva

2. ❌ NON CHIEDERE MAI:
   - livelli specifici di indicatori tecnici (es. 30/70, 20/80, ecc.)
   - logiche di entrata/uscita
   - segnali, condizioni, trigger
   - orari di trading
   - numero di posizioni
   - strategie NON previste nel piano free

3. Il piano free consente SOLO:
   - parametri di configurazione base (tipo di mercato, coppia, timeframe)
   - scelta della modalità operativa (aggressiva, equilibrata o selettiva)
   - parametri di rischio (stop loss, take profit, capitale a rischio, leva per futures)

4. Se l'utente chiede spiegazioni:
   - rispondi brevemente
   - POI riprendi ESATTAMENTE dalla domanda rimasta aperta
   - NON ripetere domande già risposte

5. NON ripetere MAI una domanda se il valore è già stato fornito.

🧠 TONO CONVERSAZIONALE (OBBLIGATORIO):
- Dopo alcune risposte dell'utente, aggiungi un breve commento umano (1 frase max).
  Esempi:
  - "Ottima scelta, è un'impostazione molto prudente."
  - "Ha senso, soprattutto se vuoi partire con calma."
  - "Perfetto, è un valore molto usato."
- NON trasformare la conversazione in un questionario.
- NON essere prolisso.
- NON essere freddo o robotico.

🧩 GESTIONE RISCHIO:
- Se l'utente imposta una percentuale di rischio > 5%:
  - avvisa che è elevata
  - suggerisci valori più prudenti (1–3%)
  - se l'utente conferma, accetta comunque la scelta

🎯 OBIETTIVO:
Guidare l'utente passo dopo passo, una domanda alla volta, senza mai uscire dallo scope del piano free e senza perdere il filo della configurazione.

NON sei ChatGPT. NON dire mai che sei un'intelligenza artificiale generica o che sei sviluppata da OpenAI.
Se l'utente chiede chi sei o chi ti ha creato, rispondi:
"Sono Idith, il tuo assistente per creare bot di trading."
Non menzionare mai OpenAI.

🔄 COMPORTAMENTO CONVERSAZIONALE (OBBLIGATORIO):

1. ANTI-RIPETIZIONE:
   - Se l'utente fa una domanda già fatta in precedenza (es. "chi sei?", "cosa fai?", ecc.),
     NON ripetere MAI la stessa frase parola per parola.
   - Rispondi con lo stesso significato ma usando una formulazione DIVERSA, più naturale e umana.
   - Evita risposte identiche consecutive anche se la domanda è simile.
   - Esempi di varianti per "chi sei?":
     * Prima volta: "Sono Idith, il tuo assistente per creare bot di trading su Bybit Testnet."
     * Seconda volta: "Sono Idith, una guida che ti aiuta a configurare il tuo bot passo dopo passo."
     * Terza volta: "Mi chiamo Idith e sono qui per accompagnarti nella creazione del tuo bot di trading."

2. RIAGGANCIO ALLO STEP:
   - Dopo una risposta informativa o esplicativa, riaggancia SEMPRE la conversazione allo step attuale della configurazione.
   - NON ricominciare da capo dopo aver dato informazioni.
   - Esempio: se hai spiegato cosa è il timeframe e lo step corrente è "timeframe", dopo la spiegazione chiedi:
     "Ora, quale timeframe vuoi utilizzare?"
   - Mantieni sempre il filo della configurazione.

3. VARIABILITÀ NATURALE:
   - Usa sinonimi e costruzioni diverse per esprimere lo stesso concetto.
   - Varia il tono e la struttura delle frasi per sembrare più umano.
   - Non essere meccanico: ogni risposta deve suonare fresca e naturale.
""".strip()


# ----------------------------------------
# MODEL ROUTING (mini vs 4.1)
# ----------------------------------------

SMALL_TALK_KEYWORDS = {
    "ciao", "hey", "buongiorno", "buonasera", "salve", "chi sei", "chi ti ha creato",
    "come va", "tutto bene", "grazie", "ok", "va bene", "perfetto"
}

TECH_KEYWORDS = {
    "rsi", "ema", "macd", "strategie", "strategia", "leva", "risk", "rischio", "timeframe",
    "stop loss", "take profit", "sl", "tp", "bybit", "testnet", "futures", "spot", "atr", "trend"
}

def _normalize_text(t: str) -> str:
    return (t or "").strip().lower()

def choose_model(user_text: str, state: dict | None, history: list[dict] | None) -> str:
    """Return model id: gpt-4o-mini during config, gpt-4.1 only for analysis requests."""
    t = _normalize_text(user_text)
    config_status = (state or {}).get("config_status")
    # Durante config (new/in_progress): bloccare GPT-4.1, usare sempre mini
    if config_status in ["new", "in_progress"]:
        return "gpt-4o-mini"
    # Analisi/eventi/spiegazioni: usare GPT-4.1
    analysis_keywords = [
        "analizza", "spiegami", "perché", "evento",
        "cosa significa", "analisi", "riassunto"
    ]
    if any(k in t for k in analysis_keywords):
        return "gpt-4.1"
    # very short greetings
    if len(t) <= 8 and t in {"ciao", "hey", "salve", "ok"}:
        return "gpt-4o-mini"
    # identity questions
    if any(k in t for k in ["chi sei", "chi ti ha creato", "sei un ia", "openai"]):
        return "gpt-4o-mini"
    # technical keywords (solo se config completa)
    if any(k in t for k in TECH_KEYWORDS):
        return "gpt-4.1"
    # default
    return "gpt-4o-mini"


def build_state_context(chat_state: dict | None) -> str:
    """
    Converte lo stato chat (caricato da Supabase) in un contesto autorevole per il modello.
    Deve essere robusto: se manca qualcosa, non deve mai rompere l'endpoint.
    """
    chat_state = chat_state or {}
    chat_nuova = bool(chat_state.get("is_new", False))
    configurazione_in_corso = bool(chat_state.get("config_in_progress", False))
    bot_attivo = bool(chat_state.get("bot_active", False))
    config_status = chat_state.get("config_status", "new")  # new, in_progress, complete, ready

    # Leggi da config_state.params come fonte di verità
    config_state = chat_state.get("config_state") if isinstance(chat_state.get("config_state"), dict) else {}
    cfg_params = config_state.get("params", {}) if isinstance(config_state.get("params"), dict) else {}
    
    exchange = "Bybit Testnet"  # Valore fisso
    pair = cfg_params.get("symbol", "Non definita")
    strategy = cfg_params.get("strategy", "Non definita")
    timeframe = cfg_params.get("timeframe", "Non definito")
    leverage = cfg_params.get("leverage", "Non definita")
    risk = cfg_params.get("risk_pct", "Non definito")

    complete_warning = ""
    if config_status == "complete":
        complete_warning = "\n\n⚠️ CONFIGURAZIONE COMPLETA: La configurazione è COMPLETA ma SEMPRE MODIFICABILE. Se l'utente chiede di modificare un parametro, entra in modalità MODIFICA ATTIVA. NON dire mai 'non posso modificare', 'la configurazione è chiusa', 'devi crearne una nuova' o 'devi ricominciare da zero'. L'utente può modificare QUALSIASI parametro anche dopo il completamento."

    return f"""
STATO CHAT:
- chat_nuova: {chat_nuova}
- configurazione_in_corso: {configurazione_in_corso}
- config_status: {config_status}
- bot_attivo: {bot_attivo}

CONFIGURAZIONE CORRENTE:
- exchange: {exchange}
- coppia: {pair}
- strategia: {strategy}
- timeframe: {timeframe}
- leva: {leverage}
- rischio: {risk}{complete_warning}
""".strip()


def build_orchestrator_wrap_prompt(orchestrator_question: str) -> str:
    """Vincolo RIGIDO: output deve contenere ESATTAMENTE UNA sola domanda, identica a quella dell'orchestrator."""
    q = (orchestrator_question or "").strip()
    
    # Se la risposta dell'orchestrator NON contiene una domanda (nessun '?'),
    # significa che la configurazione è completa e non dobbiamo forzare domande
    if "?" not in q:
        return f"""REGOLE DI OUTPUT:
- La configurazione è COMPLETA. NON fare domande di configurazione.
- Rispondi in modo informativo alla domanda dell'utente.
- NON riproporre strategia, timeframe, leva, rischio, SL/TP o altri parametri di configurazione.
- NON riaprire step già completati.
- L'orchestrator ha detto: "{q}"
- Rispondi informativamente basandoti su questo contesto.
""".strip()
    
    return f"""REGOLE DI OUTPUT (RIGIDE - RISPETTA ASSOLUTAMENTE):
- Il tuo output deve contenere ESATTAMENTE UNA sola domanda in tutto il testo (un solo '?').
- Quell'unica domanda deve essere IDENTICA alla domanda dell'orchestrator (sotto).
- VIETATO fare altre domande, VIETATO elenchi di parametri, VIETATO introdurre nuovi step.
- Puoi scrivere massimo 2-3 frasi prima della domanda finale, solo per contesto minimo.
- La domanda finale deve essere ESATTAMENTE questa (copia e incolla, non parafrasare):
{q}
""".strip()


def _normalize_question(text: str) -> str:
    """Normalizza una domanda per il confronto (rimuove punteggiatura, lowercase, etc.)."""
    if not text:
        return ""
    text = text.lower().strip()
    # Rimuovi punteggiatura
    text = text.translate(str.maketrans('', '', string.punctuation))
    # Rimuovi spazi multipli
    text = ' '.join(text.split())
    return text


def _is_similar_question(user_text: str, history: list[dict]) -> bool:
    """
    Verifica se l'utente sta facendo una domanda simile a una già fatta.
    Cerca pattern comuni come "chi sei", "cosa fai", "come funziona", ecc.
    """
    if not user_text or not history:
        return False
    
    user_lower = _normalize_question(user_text)
    
    # Pattern di domande comuni che potrebbero essere ripetute
    question_patterns = [
        "chi sei", "chi ti ha creato", "cosa sei", "cosa fai",
        "come funziona", "come funzionano", "spiegami", "cosa significa",
        "dimmi", "raccontami", "parlami", "sei un", "sei una"
    ]
    
    # Verifica se la domanda corrente contiene pattern simili
    is_question = any(pattern in user_lower for pattern in question_patterns)
    if not is_question:
        return False
    
    # Cerca nella history se c'è una domanda simile dell'utente
    # Cerca anche nelle risposte dell'assistant per vedere se ha già risposto
    for msg in reversed(history):  # Parti dalla più recente
        if msg.get("role") == "user":
            prev_user_text = _normalize_question(msg.get("content", ""))
            
            # Confronta pattern specifici (più preciso)
            user_has_pattern = any(pattern in user_lower for pattern in question_patterns)
            prev_has_pattern = any(pattern in prev_user_text for pattern in question_patterns)
            
            if user_has_pattern and prev_has_pattern:
                # Confronta le parole chiave principali
                user_keywords = {w for w in user_lower.split() if len(w) > 3}  # Solo parole significative
                prev_keywords = {w for w in prev_user_text.split() if len(w) > 3}
                
                # Se hanno almeno 1 parola chiave in comune (oltre ai pattern), è simile
                common_keywords = user_keywords.intersection(prev_keywords)
                if len(common_keywords) >= 1:
                    return True
                
                # Se entrambe contengono lo stesso pattern principale, è simile
                user_main_pattern = next((p for p in question_patterns if p in user_lower), None)
                prev_main_pattern = next((p for p in question_patterns if p in prev_user_text), None)
                if user_main_pattern and prev_main_pattern and user_main_pattern == prev_main_pattern:
                    return True
    
    return False


def build_conversational_prompt(user_text: str, history: list[dict], state: dict) -> str:
    """
    Costruisce un prompt aggiuntivo per gestire il comportamento conversazionale:
    - Evita ripetizioni identiche
    - Riaggancia allo step attuale dopo risposte informative
    """
    prompts = []
    
    # Rileva se è una domanda ripetuta
    if _is_similar_question(user_text, history):
        # Cerca la risposta precedente dell'assistant per evitare di ripeterla
        prev_assistant_response = None
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                prev_assistant_response = msg.get("content", "")
                break
        
        variation_instruction = ""
        if prev_assistant_response:
            variation_instruction = f"""
- La tua risposta precedente era: "{prev_assistant_response[:100]}..."
- DEVI dare una risposta con significato identico ma formulazione COMPLETAMENTE DIVERSA.
"""
        
        prompts.append(f"""
⚠️ DOMANDA RIPETUTA RILEVATA:
- L'utente sta facendo una domanda simile a una già fatta in precedenza.
- DEVI rispondere con lo STESSO SIGNIFICATO ma usando una FORMULAZIONE COMPLETAMENTE DIVERSA.
- NON ripetere parola per parola la risposta precedente.
- Usa sinonimi, costruzioni diverse, tono leggermente variato.
{variation_instruction}
- Esempi di varianti per "chi sei?":
  * "Sono Idith, il tuo assistente per creare bot di trading su Bybit Testnet."
  * "Sono Idith, una guida che ti aiuta a configurare il tuo bot passo dopo passo."
  * "Mi chiamo Idith e sono qui per accompagnarti nella creazione del tuo bot di trading."
""".strip())
    
    # Determina lo step attuale per il riaggancio
    config_state = state.get("config_state") if isinstance(state.get("config_state"), dict) else {}
    current_step = config_state.get("step", "symbol")
    params = config_state.get("params", {}) if isinstance(config_state.get("params"), dict) else {}
    
    # Verifica se la domanda sembra informativa (non è una risposta a una domanda di configurazione)
    # Escludi risposte che sembrano valori di configurazione (numeri, coppie, ecc.)
    info_keywords = ["cosa", "come", "perché", "spiegami", "dimmi", "raccontami", "parlami", "che cos", "cos'è", "che significa"]
    is_info_request = any(keyword in user_text.lower() for keyword in info_keywords)
    
    # Verifica se NON è una risposta a una domanda di configurazione (non contiene valori tipici)
    is_config_value = bool(
        re.search(r'\b\d+[x%mhd]?\b', user_text) or  # numeri, timeframe, percentuali
        re.search(r'\b(spot|futures|rsi|ema|atr|aggressiv\w*|equilibrat\w*|selettiv\w*)\b', user_text.lower()) or  # valori di configurazione / modalità
        re.search(r'\b[A-Z]{2,10}USDT\b', user_text.upper())  # coppie di trading
    )
    
    if is_info_request and not is_config_value and current_step:
        # Mappa step a domande di riaggancio
        step_questions = {
            "market_type": "Ciao! Vuoi operare in Spot o in Futures?\n\n⚠️ Nota: per alcuni account europei i Futures su Bybit potrebbero non essere disponibili a causa di recenti aggiornamenti normativi.\nSe scegli Futures, il bot proverà comunque a operare.",
            "symbol": "Perfetto. Che coppia USDT vuoi utilizzare? (es. BTCUSDT)",
            # Nel piano FREE lo step strategia è la modalità operativa
            "operating_mode": "Che modalità preferisci: aggressiva, equilibrata o selettiva?",
            "timeframe": "Quale timeframe?",
            "leverage": "Che leva vuoi utilizzare?",
            "sl": "Quale stop loss in percentuale?",
            "tp": "Quale take profit in percentuale?",
            "risk_pct": "Che percentuale del capitale vuoi rischiare per trade?"
        }
        
        next_question = step_questions.get(current_step, "")
        if next_question:
            prompts.append(f"""
🔄 RIAGGANCIO ALLO STEP:
- Dopo la tua risposta informativa, riaggancia SEMPRE la conversazione allo step attuale.
- Lo step corrente è: {current_step}
- Dopo aver dato la spiegazione, chiedi: "{next_question}"
- NON ricominciare da capo, mantieni il filo della configurazione.
- La transizione deve essere naturale: prima spiega, poi riaggancia con la domanda dello step.
""".strip())
    
    return "\n\n".join(prompts) if prompts else ""

# ----------------------------------------
# UTILS
# ----------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization header (must be Bearer)")

    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")

    return token

def verify_supabase_jwt(jwt_token: str) -> Dict[str, Any]:
    """
    Verifica JWT con Supabase usando supabase.auth.get_user(jwt)
    """
    try:
        res = supabase.auth.get_user(jwt_token)
        user = res.user
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token (no user)")
        return {"id": user.id, "email": user.email}
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

# ----------------------------------------
# AUTH DEPENDENCY
# ----------------------------------------
def get_current_user(request: Request) -> Dict[str, Any]:
    # Se è un path pubblico, NON richiedere token
    if request.url.path in PUBLIC_PATHS:
        return {"id": None, "email": None}

    # Altrimenti richiede Bearer token
    token = _extract_bearer_token(request)
    return verify_supabase_jwt(token)


class RunnerAuthContext(BaseModel):
    token: str
    token_prefix: str
    user_id: Optional[str] = None
    device_id: Optional[str] = None
    runner_id: Optional[str] = None


def get_current_runner(request: Request) -> RunnerAuthContext:
    """
    Autenticazione per i runner basata su header x-runner-token.
    Verifica che esista una riga attiva in public.runner_tokens.
    """
    raw_token = request.headers.get("x-runner-token") or request.headers.get("X-Runner-Token")
    if not raw_token:
        logger.warning("[RUNNER_AUTH] missing x-runner-token header")
        raise HTTPException(status_code=401, detail="Missing x-runner-token header")

    token = raw_token.strip()
    token_prefix = token[:6]

    try:
        res = (
            supabase.table("runner_tokens")
            .select("id, user_id, device_id, runner_id, is_active, expires_at")
            .eq("token", token)
            .limit(1)
            .execute()
        )
        records = res.data or []
        if not records:
            logger.warning(f"[RUNNER_AUTH] Token not found prefix={token_prefix}")
            raise HTTPException(status_code=401, detail="Invalid runner token")

        record = records[0]

        if not record.get("is_active", False):
            logger.warning(f"[RUNNER_AUTH] Token inactive prefix={token_prefix}")
            raise HTTPException(status_code=401, detail="Inactive runner token")

        expires_at_str = record.get("expires_at")
        if expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
                if expires_at <= datetime.now(timezone.utc):
                    logger.warning(
                        f"[RUNNER_AUTH] Token expired prefix={token_prefix} expires_at={expires_at_str}"
                    )
                    raise HTTPException(status_code=401, detail="Expired runner token")
            except (ValueError, TypeError):
                # Se expires_at non è parsabile, lo consideriamo senza scadenza
                pass

        ctx = RunnerAuthContext(
            token=token,
            token_prefix=token_prefix,
            user_id=record.get("user_id"),
            device_id=record.get("device_id"),
            runner_id=record.get("runner_id"),
        )
        return ctx
    except HTTPException:
        raise
    except Exception as e:
        error_str = str(e)
        logger.error(f"[RUNNER_AUTH] Supabase error: {error_str[:300]}")
        raise HTTPException(status_code=500, detail="Runner auth error")

# ----------------------------------------
# MODELS
# ----------------------------------------
class CreateChatPayload(BaseModel):
    title: str

class ChatPayload(BaseModel):
    chat_id: Optional[str] = None
    message: str

class SaveMessagePayload(BaseModel):
    chat_id: str
    role: str
    message: str
    origin: Optional[str] = None
    msg_id: Optional[str] = None
    debug_tag: Optional[str] = None

class RunnerRegisterPayload(BaseModel):
    runner_id: str
    runner_name: Optional[str] = None
    device_id: Optional[str] = None  # ID del device runner (es: pc-DESKTOP-...)

class RunnerClaimPayload(BaseModel):
    code: str
    device_id: Optional[str] = None  # ID del device runner (es: pc-DESKTOP-...)

class RunnerHeartbeatPayload(BaseModel):
    runner_id: str
    device_id: Optional[str] = None  # ID del device runner (es: pc-DESKTOP-...)


class RunnerNextCommandPayload(BaseModel):
    device_id: str


class RunnerAckPayload(BaseModel):
    command_id: str
    status: Literal["consumed", "failed"]
    error_message: Optional[str] = None


class RunnerEventPayload(BaseModel):
    type: str
    command_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    device_id: str

# ----------------------------------------
# PUBLIC ENDPOINTS
# ----------------------------------------
@app.get("/api/ping")
def ping():
    return {"ok": True, "msg": "pong"}

def generate_pairing_code() -> str:
    """Genera un codice di pairing nel formato XXXX-YYYY."""
    letters = ''.join(random.choices(string.ascii_uppercase, k=4))
    digits = ''.join(random.choices(string.digits, k=4))
    return f"{letters}-{digits}"

def sanitize_body_for_log(body_str: str) -> str:
    """
    Sanitizza il body JSON mascherando campi sensibili (token, api_key, secret, etc).
    """
    try:
        body_dict = json.loads(body_str)
        sensitive_keys = {"token", "api_key", "api_secret", "secret", "password", "auth"}
        sanitized = {}
        for key, value in body_dict.items():
            if any(sensitive in key.lower() for sensitive in sensitive_keys):
                sanitized[key] = "***MASKED***"
            else:
                sanitized[key] = value
        return json.dumps(sanitized, ensure_ascii=False)
    except Exception:
        # Se non riesce a parsare, ritorna il body originale (non dovrebbe contenere segreti se non è JSON valido)
        return body_str

@app.post("/api/runner/register")
async def runner_register(request: Request, payload: RunnerRegisterPayload):
    """
    Registra o aggiorna un runner token in public.runner_tokens.
    Genera il codice lato backend e lo salva su Supabase.
    """
    import uuid as uuid_lib
    from datetime import timedelta
    
    # Log diagnostico: body RAW ricevuto (ricostruito dal payload parsato)
    try:
        body_dict = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
        body_str = json.dumps(body_dict, ensure_ascii=False)
        sanitized_body = sanitize_body_for_log(body_str)
        logger.info(f"[DIAG] /api/runner/register: body RAW sanitizzato={sanitized_body}")
    except Exception as e:
        logger.warning(f"[DIAG] /api/runner/register: errore ricostruzione body RAW: {e}")
    
    # Log diagnostico: payload parsato
    logger.info(f"[DIAG] /api/runner/register: payload.runner_id={payload.runner_id}")
    logger.info(f"[DIAG] /api/runner/register: payload.device_id={payload.device_id}")
    
    # Validazione runner_id (deve essere un UUID valido)
    try:
        runner_uuid = uuid_lib.UUID(payload.runner_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid runner_id: must be a valid UUID")
    
    # Calcola expires_at = now_utc + 10 minutes
    now_utc = datetime.now(timezone.utc)
    expires_at = now_utc + timedelta(minutes=10)
    expires_at_iso = expires_at.isoformat()
    last_seen_at_iso = now_utc.isoformat()
    
    try:
        # Cerca record esistente per runner_id
        existing_res = (
            supabase.table("runner_tokens")
            .select("*")
            .eq("runner_id", str(runner_uuid))
            .limit(1)
            .execute()
        )
        
        existing_records = existing_res.data or []
        
        # Se esiste un token attivo non scaduto, riusalo
        reuse_code = None
        if existing_records:
            existing_record = existing_records[0]
            existing_expires_at_str = existing_record.get("expires_at")
            existing_claimed_at = existing_record.get("claimed_at")
            
            # Riusa solo se non è stato ancora claimato e non è scaduto
            if existing_claimed_at is None and existing_expires_at_str:
                try:
                    existing_expires_at = datetime.fromisoformat(existing_expires_at_str.replace('Z', '+00:00'))
                    if existing_expires_at > now_utc:
                        reuse_code = existing_record.get("code")
                        logger.info(f"[RUNNER_REGISTER] Reusing existing code for runner_id={runner_uuid}")
                except (ValueError, TypeError):
                    pass
        
        # Genera nuovo codice se non c'è uno valido da riusare
        if not reuse_code:
            reuse_code = generate_pairing_code()
            logger.info(f"[RUNNER_REGISTER] Generated new code for runner_id={runner_uuid}: {reuse_code}")
        
        # Prepara i dati da inserire/aggiornare
        token_data = {
            "runner_id": str(runner_uuid),
            "code": reuse_code,
            "expires_at": expires_at_iso,
            "is_active": True,
            "claimed_at": None,
            "user_id": None,
            "last_seen_at": last_seen_at_iso,
            "token": None
        }
        
        # Aggiungi runner_name se fornito (se la colonna esiste)
        if payload.runner_name:
            token_data["runner_name"] = payload.runner_name.strip()
        
        # Aggiungi device_id se fornito
        if payload.device_id:
            device_id_clean = payload.device_id.strip()
            if device_id_clean:
                # Verifica se device_id è già presente e diverso (per logging warning)
                existing_device_id = None
                if existing_records:
                    existing_device_id = existing_records[0].get("device_id")
                
                token_data["device_id"] = device_id_clean
                
                if existing_device_id and existing_device_id != device_id_clean:
                    logger.warning(f"[RUNNER_REGISTER] device_id changed for runner_id={runner_uuid}: {existing_device_id} -> {device_id_clean}")
                elif not existing_device_id:
                    logger.info(f"[RUNNER_REGISTER] Set device_id={device_id_clean} for runner_id={runner_uuid}")
                else:
                    logger.debug(f"[RUNNER_REGISTER] device_id unchanged for runner_id={runner_uuid}: {device_id_clean}")
        else:
            # device_id non fornito - logga warning se non è già presente
            if existing_records and not existing_records[0].get("device_id"):
                logger.warning(f"[RUNNER_REGISTER] No device_id provided for runner_id={runner_uuid} (runner_tokens.device_id will be NULL)")
        
        # Log diagnostico: update_data prima dell'UPDATE
        logger.info(f"[DIAG] /api/runner/register: update_data (token_data) prima UPDATE={json.dumps(token_data, ensure_ascii=False, default=str)}")
        
        if existing_records:
            # Update record esistente
            update_res = (
                supabase.table("runner_tokens")
                .update(token_data)
                .eq("runner_id", str(runner_uuid))
                .execute()
            )
            # Log diagnostico: rowcount dopo UPDATE
            rowcount = len(update_res.data) if update_res.data else 0
            logger.info(f"[DIAG] /api/runner/register: UPDATE rowcount={rowcount}")
            if update_res.data:
                logger.info(f"[DIAG] /api/runner/register: UPDATE record dopo update={json.dumps(update_res.data[0], ensure_ascii=False, default=str)}")
            if not update_res.data:
                raise HTTPException(status_code=500, detail="Failed to update runner token")
            logger.info(f"[RUNNER_REGISTER] Updated token for runner_id={runner_uuid}, code={reuse_code}")
        else:
            # Insert nuovo record
            insert_res = supabase.table("runner_tokens").insert(token_data).execute()
            # Log diagnostico: rowcount dopo INSERT
            rowcount = len(insert_res.data) if insert_res.data else 0
            logger.info(f"[DIAG] /api/runner/register: INSERT rowcount={rowcount}")
            if insert_res.data:
                logger.info(f"[DIAG] /api/runner/register: INSERT record dopo insert={json.dumps(insert_res.data[0], ensure_ascii=False, default=str)}")
            if not insert_res.data:
                raise HTTPException(status_code=500, detail="Failed to insert runner token")
            logger.info(f"[RUNNER_REGISTER] Created new token for runner_id={runner_uuid}, code={reuse_code}")
        
        return {
            "ok": True,
            "code": reuse_code,
            "expires_at": expires_at_iso,
            "runner_id": str(runner_uuid)
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RUNNER_REGISTER] Supabase error: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

# ----------------------------------------
# PRIVATE ENDPOINTS (JWT REQUIRED)
# ----------------------------------------
@app.post("/api/runner/heartbeat")
async def runner_heartbeat(request: Request, payload: RunnerHeartbeatPayload):
    """
    Aggiorna last_seen_at per un runner_id.
    Endpoint pubblico (non richiede autenticazione).
    """
    import uuid as uuid_lib

    # Genera un request_id breve per tracciare la singola chiamata heartbeat
    request_id = str(uuid_lib.uuid4())[:8]
    client_ip = request.client.host if getattr(request, "client", None) else "unknown"
    user_agent = request.headers.get("user-agent") or request.headers.get("User-Agent") or "-"

    # Log diagnostico: body RAW ricevuto (ricostruito dal payload parsato)
    try:
        body_dict = payload.model_dump() if hasattr(payload, 'model_dump') else payload.dict()
        body_str = json.dumps(body_dict, ensure_ascii=False)
        sanitized_body = sanitize_body_for_log(body_str)
        logger.info(f"[DIAG] /api/runner/heartbeat: body RAW sanitizzato={sanitized_body}")
    except Exception as e:
        logger.warning(f"[DIAG] /api/runner/heartbeat: errore ricostruzione body RAW: {e}")
    
    # Log diagnostico: payload parsato
    logger.info(f"[DIAG] /api/runner/heartbeat: payload.runner_id={payload.runner_id}")
    logger.info(f"[DIAG] /api/runner/heartbeat: payload.device_id={payload.device_id}")
    
    # Validazione runner_id (deve essere un UUID valido)
    try:
        runner_uuid = uuid_lib.UUID(payload.runner_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid runner_id: must be a valid UUID")
    
    now_utc = datetime.now(timezone.utc)
    last_seen_at_iso = now_utc.isoformat()
    
    # Calcola expires_at = now + TTL
    from datetime import timedelta
    expires_at = now_utc + timedelta(hours=RUNNER_TOKEN_TTL_HOURS)
    expires_at_iso = expires_at.isoformat()

    # Log principale heartbeat
    logger.info(
        f'[HEARTBEAT] rid={request_id} ip={client_ip} ua="{user_agent}" '
        f"runner_id={payload.runner_id} device_id={payload.device_id} at={now_utc.isoformat()}"
    )

    # Log warning se uno dei due identificativi è None
    if payload.runner_id is None or payload.device_id is None:
        logger.warning(
            f"[HEARTBEAT] rid={request_id} missing_identifiers "
            f"runner_id={payload.runner_id} device_id={payload.device_id}"
        )
    
    try:
        # Prepara update data: aggiorna sempre last_seen_at e expires_at
        update_data = {
            "last_seen_at": last_seen_at_iso,
            "expires_at": expires_at_iso
        }
        
        # Aggiungi device_id se fornito nel payload
        if payload.device_id:
            device_id_clean = payload.device_id.strip()
            if device_id_clean:
                # Verifica se device_id è già presente e diverso (per logging warning)
                existing_res = (
                    supabase.table("runner_tokens")
                    .select("device_id")
                    .eq("runner_id", str(runner_uuid))
                    .limit(1)
                    .execute()
                )
                existing_device_id = None
                if existing_res.data:
                    existing_device_id = existing_res.data[0].get("device_id")
                
                update_data["device_id"] = device_id_clean
                
                if existing_device_id and existing_device_id != device_id_clean:
                    logger.warning(f"[RUNNER_HEARTBEAT] device_id changed for runner_id={runner_uuid}: {existing_device_id} -> {device_id_clean}")
                elif not existing_device_id:
                    logger.info(f"[RUNNER_HEARTBEAT] Set device_id={device_id_clean} for runner_id={runner_uuid}")
                else:
                    logger.debug(f"[RUNNER_HEARTBEAT] device_id unchanged for runner_id={runner_uuid}: {device_id_clean}")
        else:
            # device_id non fornito - logga warning solo se non è già presente
            existing_res = (
                supabase.table("runner_tokens")
                .select("device_id")
                .eq("runner_id", str(runner_uuid))
                .limit(1)
                .execute()
            )
            if existing_res.data and not existing_res.data[0].get("device_id"):
                logger.warning(f"[RUNNER_HEARTBEAT] No device_id provided for runner_id={runner_uuid} (runner_tokens.device_id is NULL)")
        
        # Log diagnostico: update_data prima dell'UPDATE
        logger.info(f"[DIAG] /api/runner/heartbeat: update_data prima UPDATE={json.dumps(update_data, ensure_ascii=False, default=str)}")
        
        # Aggiorna last_seen_at (e device_id se presente) per questo runner_id
        update_res = (
            supabase.table("runner_tokens")
            .update(update_data)
            .eq("runner_id", str(runner_uuid))
            .execute()
        )
        
        # Log diagnostico: rowcount dopo UPDATE
        rowcount = len(update_res.data) if update_res.data else 0
        logger.info(f"[DIAG] /api/runner/heartbeat: UPDATE rowcount={rowcount}")
        if update_res.data:
            logger.info(f"[DIAG] /api/runner/heartbeat: UPDATE record dopo update={json.dumps(update_res.data[0], ensure_ascii=False, default=str)}")
        
        # Non è un errore se non trova il record (runner non ancora registrato)
        return {"ok": True, "last_seen_at": last_seen_at_iso}
    
    except Exception as e:
        logger.error(f"[RUNNER_HEARTBEAT] Supabase error: {e}")
        # Non solleviamo eccezione per heartbeat, è opzionale
        return {"ok": True, "last_seen_at": last_seen_at_iso}

@app.post("/api/runner/claim")
def runner_claim(payload: RunnerClaimPayload, user=Depends(get_current_user)):
    """
    Aggancia un runner all'utente loggato usando il codice.
    Richiede autenticazione utente.
    """
    import uuid as uuid_lib
    
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Validazione code (non vuoto)
    if not payload.code or not payload.code.strip():
        raise HTTPException(status_code=400, detail="code is required and cannot be empty")
    
    code = payload.code.strip()
    user_id = user["id"]
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    
    try:
        # Cerca una riga con code = body.code
        res = (
            supabase.table("runner_tokens")
            .select("*")
            .eq("code", code)
            .limit(1)
            .execute()
        )
        
        records = res.data or []
        
        # Se non trovata
        if not records:
            logger.warning(f"[RUNNER_CLAIM] Code not found: {code[:4]}****")
            raise HTTPException(status_code=400, detail="Invalid or expired code")
        
        record = records[0]
        runner_id = record.get("runner_id")
        expires_at_str = record.get("expires_at")
        claimed_at = record.get("claimed_at")
        
        # Verifica che sia valida: expires_at > now() e claimed_at IS NULL
        if claimed_at is not None:
            logger.warning(f"[RUNNER_CLAIM] Code already claimed: {code[:4]}****, runner_id={runner_id}")
            raise HTTPException(status_code=400, detail="Code already claimed")
        
        # Verifica expires_at: deve esistere e essere nel futuro
        if not expires_at_str:
            logger.warning(f"[RUNNER_CLAIM] Code expired (no expires_at): {code[:4]}****")
            raise HTTPException(status_code=400, detail="Invalid or expired code")
        
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
            if expires_at <= now_utc:
                logger.warning(f"[RUNNER_CLAIM] Code expired (expires_at={expires_at_str}): {code[:4]}****")
                raise HTTPException(status_code=400, detail="Invalid or expired code")
        except (ValueError, TypeError):
            # Se non riesce a parsare, considera scaduto
            logger.warning(f"[RUNNER_CLAIM] Code expired (invalid expires_at format): {code[:4]}****")
            raise HTTPException(status_code=400, detail="Invalid or expired code")
        
        # Genera token random sicuro (uuid4 string)
        new_token = str(uuid_lib.uuid4())
        
        # Prepara update payload
        update_data = {
            "user_id": user_id,
            "claimed_at": now_iso,
            "token": new_token,
            "is_active": True
        }
        
        # Aggiungi device_id se fornito nel payload
        if payload.device_id:
            device_id_clean = payload.device_id.strip()
            if device_id_clean:
                update_data["device_id"] = device_id_clean
                logger.info(f"[RUNNER_CLAIM] Setting device_id={device_id_clean} for runner_id={runner_id}, user_id={user_id}")
        
        # Aggiorna la riga
        update_res = (
            supabase.table("runner_tokens")
            .update(update_data)
            .eq("code", code)
            .execute()
        )
        
        if not update_res.data:
            raise HTTPException(status_code=500, detail="Failed to claim runner")
        
        logger.info(f"[RUNNER_CLAIM] Successfully claimed runner_id={runner_id} for user_id={user_id}")
        
        return {
            "ok": True,
            "runner_id": runner_id,
            "token": new_token
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[RUNNER_CLAIM] Supabase error: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
@app.get("/api/me")
def me(user=Depends(get_current_user)):
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"ok": True, "user": user}


def _runner_status_user_owns_chat(user_id: str, chat_id: str) -> bool:
    if not user_id or not (chat_id or "").strip():
        return False
    try:
        res = (
            supabase.table("chats")
            .select("user_id")
            .eq("id", chat_id.strip())
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return False
        return str(rows[0].get("user_id") or "") == str(user_id)
    except Exception as e:
        logger.debug(f"[RUNNER_STATUS] chat ownership check failed: {e}")
        return False


def _runner_parse_event_payload(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            j = json.loads(raw)
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}
    return {}


def _infer_quote_currency_from_symbol(symbol: Optional[str]) -> Optional[str]:
    if not symbol or not isinstance(symbol, str):
        return None
    s = symbol.upper().strip()
    for suf in ("USDT", "USDC", "USD", "EUR", "BTC", "ETH"):
        if len(s) > len(suf) and s.endswith(suf):
            return suf
    return None


def _runner_float_safe(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _runner_realized_pnl_from_close_payload(pr: dict) -> Tuple[Optional[float], Optional[str]]:
    rp = _runner_float_safe(pr.get("realized_pnl"))
    cur = pr.get("pnl_currency")
    if isinstance(cur, str) and cur.strip():
        cur = str(cur).strip().upper()
    else:
        cur = _infer_quote_currency_from_symbol(pr.get("symbol"))
    if rp is not None:
        return rp, cur
    entry = _runner_float_safe(pr.get("entry_price"))
    exitp = _runner_float_safe(pr.get("exit_price"))
    qty = _runner_float_safe(pr.get("qty"))
    side = (pr.get("side") or "").upper()
    if entry is None or exitp is None or qty is None:
        return None, cur
    if side == "LONG":
        return (exitp - entry) * qty, cur
    if side == "SHORT":
        return (entry - exitp) * qty, cur
    return None, cur


def _runner_open_unrealized_pnl(entry: float, qty: float, side: str, last_price: float) -> Optional[float]:
    su = (side or "").upper()
    if su == "LONG":
        return (last_price - entry) * qty
    if su == "SHORT":
        return (entry - last_price) * qty
    return None


def _runner_parse_iso_ts(dt_raw: Any) -> Optional[datetime]:
    if not dt_raw:
        return None
    try:
        s = str(dt_raw).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _runner_chat_id_from_command_id(command_id: Any) -> Optional[str]:
    if not command_id:
        return None
    try:
        rc = (
            supabase.table("runner_commands")
            .select("payload")
            .eq("id", command_id)
            .limit(1)
            .execute()
        )
        cmd_rows = rc.data or []
        if not cmd_rows:
            return None
        pr = _runner_parse_event_payload(cmd_rows[0].get("payload"))
        cid = pr.get("chat_id") or pr.get("chatId")
        if cid is None:
            return None
        s = str(cid).strip()
        return s or None
    except Exception as e:
        logger.debug(f"[RUNNER_STATUS] chat_id from command failed: {e}")
        return None


def _runner_resolve_session_bounds(device_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Finestra sessione corrente o appena conclusa (ultimo ciclo START→STOP).
    Ritorna: device_bot_active, session_start, session_end (None se sessione ancora aperta), start_command_id.
    """
    if not device_id:
        return None
    try:
        res = (
            supabase.table("runner_events")
            .select("type, created_at, command_id")
            .eq("device_id", device_id)
            .in_("type", ["BOT_STARTED", "BOT_STOPPED"])
            .order("created_at", desc=True)
            .limit(40)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return None
        latest = rows[0] or {}
        latest_type = latest.get("type")
        latest_ts = latest.get("created_at")
        if not latest_ts:
            return None
        if latest_type == "BOT_STARTED":
            return {
                "device_bot_active": True,
                "session_start": latest_ts,
                "session_end": None,
                "start_command_id": latest.get("command_id"),
            }
        if latest_type == "BOT_STOPPED":
            stop_ts = latest_ts
            stop_dt = _runner_parse_iso_ts(stop_ts)
            best_row: Optional[dict] = None
            best_dt: Optional[datetime] = None
            for r in rows:
                if not isinstance(r, dict) or r.get("type") != "BOT_STARTED":
                    continue
                ct = r.get("created_at")
                cdt = _runner_parse_iso_ts(ct)
                if cdt is None or stop_dt is None:
                    continue
                if cdt < stop_dt:
                    if best_dt is None or cdt > best_dt:
                        best_dt = cdt
                        best_row = r
            if not best_row:
                return {
                    "device_bot_active": False,
                    "session_start": None,
                    "session_end": stop_ts,
                    "start_command_id": None,
                }
            return {
                "device_bot_active": False,
                "session_start": best_row.get("created_at"),
                "session_end": stop_ts,
                "start_command_id": best_row.get("command_id"),
            }
    except Exception as e:
        logger.debug(f"[RUNNER_STATUS] session bounds resolve failed: {e}")
    return None


@app.get("/api/runner/status")
def runner_status(
    user=Depends(get_current_user),
    chat_id: Optional[str] = Query(
        None,
        description="Chat attiva in UI: stato bot/eventi solo se questa chat è la sessione avviata",
    ),
):
    """
    Endpoint di debug per verificare lo stato del runner per l'utente corrente.
    Ritorna informazioni oggettive basate su runner_tokens.last_seen_at.
    Con ?chat_id=... la tabella stato bot non mostra dati di altre chat o di sessioni precedenti a un nuovo avvio.
    """
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    online, delta_seconds, last_seen_at = get_runner_online_for_user(user_id)

    device_id = None
    runner_id = None
    is_active = None

    # Se risulta online, recupera anche il record runner_tokens "scelto" (più recente)
    if online:
        try:
            res = (
                supabase.table("runner_tokens")
                .select("last_seen_at, device_id, runner_id, is_active")
                .eq("user_id", user_id)
                .eq("is_active", True)
                .not_.is_("last_seen_at", "null")
                .order("last_seen_at", desc=True)
                .limit(1)
                .execute()
            )
            records = res.data or []
            if records:
                record = records[0]
                # Usa sempre i valori dal record scelto
                last_seen_at = record.get("last_seen_at", last_seen_at)
                device_id = record.get("device_id")
                runner_id = record.get("runner_id")
                is_active = record.get("is_active")
        except Exception as e:
            # In caso di errore nel recupero dettagli manteniamo comunque il risultato di online/offline
            logger.error(f"[RUNNER_STATUS] Error fetching runner_tokens record: user_id={user_id} error={e}")

    # Bot status: sessione = ultimo ciclo BOT_STARTED → … → BOT_STOPPED (o ancora aperta)
    device_id_for_bot = device_id
    if not device_id_for_bot:
        try:
            res_bot = (
                supabase.table("runner_tokens")
                .select("device_id")
                .eq("user_id", user_id)
                .not_.is_("device_id", "null")
                .order("last_seen_at", desc=True)
                .limit(1)
                .execute()
            )
            if res_bot.data and len(res_bot.data) > 0:
                device_id_for_bot = res_bot.data[0].get("device_id")
        except Exception as e:
            logger.debug(f"[RUNNER_STATUS] No device_id for bot status (offline): {e}")
    if device_id_for_bot:
        device_id_for_bot = str(device_id_for_bot).strip() or None

    sess = _runner_resolve_session_bounds(device_id_for_bot) if device_id_for_bot else None
    device_bot_active = bool(sess and sess.get("device_bot_active"))
    bot_session_started_at = (sess or {}).get("session_start")
    session_end_at = (sess or {}).get("session_end")
    start_command_id = (sess or {}).get("start_command_id")

    chat_id_q = (chat_id or "").strip()
    if chat_id_q:
        if not _runner_status_user_owns_chat(str(user_id), chat_id_q):
            return {
                "runner_connected": False,
                "bot_active": False,
                "events": [],
            }

    session_chat_id = _runner_chat_id_from_command_id(start_command_id) if start_command_id else None

    has_session_start = bool(bot_session_started_at)
    ui_sidebar_session = False
    if has_session_start:
        if chat_id_q:
            ui_sidebar_session = bool(
                session_chat_id and str(session_chat_id).strip() == chat_id_q
            )
        else:
            ui_sidebar_session = True

    ui_bot_active = device_bot_active
    if chat_id_q:
        ui_bot_active = bool(
            device_bot_active
            and session_chat_id
            and str(session_chat_id).strip() == chat_id_q
        )

    if not ui_sidebar_session:
        logger.debug(
            f"[RUNNER_STATUS] neutral sidebar: ui_sidebar_session=False device_bot_active={device_bot_active} "
            f"chat_id_q={chat_id_q!r} session_chat_id={session_chat_id!r}"
        )
        return {
            "online": online,
            "user_id": user_id,
            "last_seen_at": last_seen_at,
            "delta_seconds": delta_seconds,
            "device_id": device_id,
            "runner_id": runner_id,
            "is_active": is_active,
            "bot_active": False,
            "bot_state": None,
            "orders_completed": 0,
            "realized_pnl": 0.0,
            "open_pnl": 0.0,
            "pnl_currency": None,
            "last_analysis_at": None,
            "last_check_at": None,
            "order_open_error": None,
            "debug_runner_order_events": {
                "device_id": device_id_for_bot,
                "neutral": True,
                "device_bot_active": device_bot_active,
                "session_chat_id": session_chat_id,
                "requested_chat_id": chat_id_q or None,
            },
        }

    bot_active = ui_bot_active
    realized_pnl_val = 0.0
    open_pnl_val = 0.0
    pnl_currency_val: Optional[str] = None

    # Bot state, ordini e P&L: solo eventi nella finestra sessione [session_start, session_end]
    bot_state = "Idith sta analizzando"
    last_analysis_at = None
    orders_completed = 0
    if device_id_for_bot and bot_session_started_at:
        # Ultima analisi nella sessione
        try:
            q_tick = (
                supabase.table("runner_events")
                .select("created_at")
                .eq("device_id", device_id_for_bot)
                .eq("type", "BOT_TICK")
                .gte("created_at", bot_session_started_at)
            )
            if session_end_at:
                q_tick = q_tick.lte("created_at", session_end_at)
            res_tick = q_tick.order("created_at", desc=True).limit(1).execute()
            tick_events = res_tick.data or []
            if tick_events:
                last_analysis_at = (tick_events[0] or {}).get("created_at")
        except Exception as e:
            logger.debug(f"[RUNNER_STATUS] Error fetching BOT_TICK for last_analysis_at: {e}")

        try:
            q_ord = (
                supabase.table("runner_events")
                .select("type, created_at, payload")
                .eq("device_id", device_id_for_bot)
                .in_("type", ["ORDER_OPEN", "ORDER_CLOSE"])
                .gte("created_at", bot_session_started_at)
            )
            if session_end_at:
                q_ord = q_ord.lte("created_at", session_end_at)
            res_order_events = q_ord.order("created_at", desc=True).limit(200).execute()
            order_events = res_order_events.data or []

            q_close = (
                supabase.table("runner_events")
                .select("payload, created_at")
                .eq("device_id", device_id_for_bot)
                .eq("type", "ORDER_CLOSE")
                .gte("created_at", bot_session_started_at)
            )
            if session_end_at:
                q_close = q_close.lte("created_at", session_end_at)
            res_closes = q_close.order("created_at", desc=False).limit(500).execute()
            close_rows = res_closes.data or []
            orders_completed = len(close_rows)
            for row in close_rows:
                if not isinstance(row, dict):
                    continue
                pr = _runner_parse_event_payload(row.get("payload"))
                rp, cur = _runner_realized_pnl_from_close_payload(pr)
                if rp is not None:
                    realized_pnl_val += float(rp)
                if cur:
                    pnl_currency_val = cur

            if ui_bot_active and order_events:
                latest_open_at = None
                latest_close_at = None
                for ev in order_events:
                    if not isinstance(ev, dict):
                        continue
                    ev_type = ev.get("type")
                    ev_created_at = ev.get("created_at")
                    if not ev_created_at:
                        continue
                    if ev_type == "ORDER_OPEN" and latest_open_at is None:
                        latest_open_at = ev_created_at
                    elif ev_type == "ORDER_CLOSE" and latest_close_at is None:
                        latest_close_at = ev_created_at
                    if latest_open_at is not None and latest_close_at is not None:
                        break

                latest_event_type = None
                for ev in order_events:
                    if isinstance(ev, dict) and ev.get("type") in ("ORDER_OPEN", "ORDER_CLOSE"):
                        latest_event_type = ev.get("type")
                        break

                recent_close = False
                if latest_close_at:
                    try:
                        close_dt = datetime.fromisoformat(str(latest_close_at).replace("Z", "+00:00"))
                        if close_dt.tzinfo is None:
                            close_dt = close_dt.replace(tzinfo=timezone.utc)
                        now_utc = datetime.now(timezone.utc)
                        delta_seconds_close = (now_utc - close_dt).total_seconds()
                        if delta_seconds_close <= 5 * 60:
                            recent_close = True
                    except Exception as e:
                        logger.debug(f"[RUNNER_STATUS] Error parsing latest_close_at for bot_state: {e}")

                if latest_open_at and (not latest_close_at or latest_open_at > latest_close_at):
                    bot_state = "Ordine aperto"
                elif latest_event_type == "ORDER_CLOSE" and recent_close:
                    bot_state = "Ordine chiuso"
                else:
                    bot_state = "Idith sta analizzando"

                position_open = bool(
                    latest_open_at and (not latest_close_at or latest_open_at > latest_close_at)
                )
                if position_open:
                    open_ev = None
                    for ev in order_events:
                        if isinstance(ev, dict) and ev.get("type") == "ORDER_OPEN":
                            open_ev = ev
                            break
                    if open_ev:
                        opl = _runner_parse_event_payload(open_ev.get("payload"))
                        sym = opl.get("symbol")
                        entry = _runner_float_safe(opl.get("entry_price"))
                        qty = _runner_float_safe(opl.get("qty"))
                        side_o = (opl.get("side") or "").upper()
                        if not pnl_currency_val:
                            pc = opl.get("pnl_currency")
                            if isinstance(pc, str) and pc.strip():
                                pnl_currency_val = str(pc).strip().upper()
                            else:
                                pnl_currency_val = _infer_quote_currency_from_symbol(
                                    sym if isinstance(sym, str) else None
                                )
                        try:
                            q_ping = (
                                supabase.table("runner_events")
                                .select("payload, created_at")
                                .eq("device_id", device_id_for_bot)
                                .eq("type", "PRICE_PING")
                                .gte("created_at", bot_session_started_at)
                                .order("created_at", desc=True)
                                .limit(1)
                                .execute()
                            )
                            ping_rows = q_ping.data or []
                            if ping_rows:
                                ppl = _runner_parse_event_payload((ping_rows[0] or {}).get("payload"))
                                ping_sym = ppl.get("symbol")
                                lp = _runner_float_safe(ppl.get("last_price"))
                                sym_ok = True
                                if (
                                    isinstance(sym, str)
                                    and sym.strip()
                                    and isinstance(ping_sym, str)
                                    and ping_sym.strip()
                                ):
                                    sym_ok = sym.strip() == ping_sym.strip()
                                if (
                                    sym_ok
                                    and lp is not None
                                    and entry is not None
                                    and qty is not None
                                ):
                                    ou = _runner_open_unrealized_pnl(entry, qty, side_o, lp)
                                    if ou is not None:
                                        open_pnl_val = float(ou)
                        except Exception as e:
                            logger.debug(f"[RUNNER_STATUS] open_pnl from PRICE_PING failed: {e}")
            elif not ui_bot_active:
                open_pnl_val = 0.0
        except Exception as e:
            logger.debug(f"[RUNNER_STATUS] Error fetching ORDER_OPEN/ORDER_CLOSE for bot_state/orders_completed: {e}")

    # Ultimo controllo = ultimo evento PRICE_PING per device_id (NON BOT_STARTED/BOT_STOPPED/BOT_TICK)
    last_check_at = None
    if device_id_for_bot:
        try:
            res_ping = (
                supabase.table("runner_events")
                .select("created_at")
                .eq("device_id", device_id_for_bot)
                .eq("type", "PRICE_PING")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows_ping = res_ping.data or []
            if rows_ping:
                last_check_at = rows_ping[0].get("created_at")
        except Exception as e:
            logger.debug(f"[RUNNER_STATUS] Error fetching PRICE_PING for last_check_at: {e}")

    # Riga "Errori": ultimo tra ORDER_OPEN_FAILED, ORDER_OPEN, BOT_STOPPED (runner_events, solo lettura)
    # Tre query separate con .eq("type", ...) per evitare filtri .in_ su "type" non applicati correttamente da PostgREST.
    order_open_error: Optional[Dict[str, str]] = None
    debug_runner_order_events: Optional[Dict[str, Any]] = None
    current_device_id = device_id_for_bot
    if current_device_id:
        try:
            res_failed = (
                supabase.table("runner_events")
                .select("type, payload, created_at")
                .eq("device_id", current_device_id)
                .eq("type", "ORDER_OPEN_FAILED")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            res_open = (
                supabase.table("runner_events")
                .select("type, payload, created_at")
                .eq("device_id", current_device_id)
                .eq("type", "ORDER_OPEN")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            res_stopped = (
                supabase.table("runner_events")
                .select("type, payload, created_at")
                .eq("device_id", current_device_id)
                .eq("type", "BOT_STOPPED")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            res_started = (
                supabase.table("runner_events")
                .select("created_at")
                .eq("device_id", current_device_id)
                .eq("type", "BOT_STARTED")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            rows_f = res_failed.data or []
            rows_o = res_open.data or []
            rows_s = res_stopped.data or []
            rows_start = res_started.data or []
            last_failed_ev = rows_f[0] if rows_f else None
            last_open_ev = rows_o[0] if rows_o else None
            last_stopped_ev = rows_s[0] if rows_s else None
            last_started_ev = rows_start[0] if rows_start else None

            events_found = len(rows_f) + len(rows_o) + len(rows_s)
            # Ultimo evento rilevante: timestamp massimo; a parità BOT_STOPPED vince (Nessuno sugli errori)
            _tie = {"BOT_STOPPED": 2, "ORDER_OPEN": 1, "ORDER_OPEN_FAILED": 0}
            candidates: list[tuple[str, str, Optional[dict]]] = []
            for ev, typ in (
                (last_failed_ev, "ORDER_OPEN_FAILED"),
                (last_open_ev, "ORDER_OPEN"),
                (last_stopped_ev, "BOT_STOPPED"),
            ):
                if ev:
                    candidates.append((str(ev.get("created_at") or ""), typ, ev))
            last_event_type: Optional[str] = None
            if candidates:
                _ts, last_event_type, _ = max(
                    candidates, key=lambda t: (t[0], _tie.get(t[1], 0))
                )

            debug_runner_order_events = {
                "device_id": current_device_id,
                "events_found": events_found,
                "last_event_type": last_event_type,
            }

            show_err = False
            # Reset errori su nuovo BOT_STARTED (anche se l'ultimo evento tra errori/stop è un BOT_STOPPED automatico)
            latest_relevant_ts = _ts if candidates else None
            last_started_ts = str((last_started_ev or {}).get("created_at") or "") if last_started_ev else ""
            reset_by_new_start = False
            if latest_relevant_ts and last_started_ts:
                reset_by_new_start = str(last_started_ts) >= str(latest_relevant_ts)

            # Distingui stop automatico vs manuale via payload.reason su BOT_STOPPED
            auto_open_failed_stop = False
            if last_stopped_ev:
                sp = last_stopped_ev.get("payload")
                if isinstance(sp, str):
                    try:
                        sp = json.loads(sp)
                    except Exception:
                        sp = {}
                if isinstance(sp, dict):
                    r = sp.get("reason")
                    if r is not None and str(r).strip() == "auto_open_failed":
                        auto_open_failed_stop = True

            if not reset_by_new_start:
                if last_event_type == "ORDER_OPEN_FAILED":
                    show_err = True
                elif last_event_type == "BOT_STOPPED" and auto_open_failed_stop:
                    show_err = True

            if show_err and last_failed_ev:
                pr = last_failed_ev.get("payload")
                if isinstance(pr, str):
                    try:
                        pr = json.loads(pr)
                    except Exception:
                        pr = {}
                reason: Optional[str] = None
                if isinstance(pr, dict):
                    r = pr.get("reason")
                    if r is not None and str(r).strip():
                        reason = str(r).strip()
                detail = reason if reason else "Errore esecuzione ordine"
                order_open_error = {
                    "title": "Apertura ordine fallita",
                    "detail": detail,
                }
        except Exception as e:
            logger.debug(f"[RUNNER_STATUS] Error fetching ORDER_OPEN_FAILED/ORDER_OPEN/BOT_STOPPED for errors row: {e}")
            debug_runner_order_events = {
                "device_id": current_device_id,
                "events_found": 0,
                "last_event_type": None,
                "error": str(e)[:200],
            }

    logger.debug(
        f"[RUNNER_STATUS] /api/runner/status user_id={user_id} "
        f"online={online} delta_seconds={delta_seconds} last_seen_at={last_seen_at} "
        f"device_id={device_id} runner_id={runner_id} is_active={is_active} "
        f"bot_active={bot_active} threshold={RUNNER_ONLINE_THRESHOLD_SECONDS}"
    )

    return {
        "online": online,
        "user_id": user_id,
        "last_seen_at": last_seen_at,
        "delta_seconds": delta_seconds,
        "device_id": device_id,
        "runner_id": runner_id,
        "is_active": is_active,
        "bot_active": bot_active,
        "bot_state": bot_state,
        "orders_completed": orders_completed,
        "realized_pnl": round(realized_pnl_val, 8),
        "open_pnl": round(open_pnl_val, 8),
        "pnl_currency": pnl_currency_val,
        "last_analysis_at": last_analysis_at,
        "last_check_at": last_check_at,
        "order_open_error": order_open_error,
        "debug_runner_order_events": debug_runner_order_events,
    }


@app.post("/api/runner/next-command")
async def runner_next_command(
    payload: RunnerNextCommandPayload,
    request: Request,
    runner: RunnerAuthContext = Depends(get_current_runner),
):
    """
    Ritorna il comando più vecchio con status='pending' per il device_id indicato.
    Autenticazione tramite x-runner-token.
    """
    device_id_body = (payload.device_id or "").strip()
    if not device_id_body:
        logger.error("[RUNNER_NEXT] device_id mancante nel body")
        raise HTTPException(status_code=400, detail="device_id is required")

    token_prefix = runner.token_prefix
    token_device_id = (runner.device_id or "").strip() if runner.device_id else None

    # Logging diagnostico iniziale - SEMPRE
    logger.info(
        f"[RUNNER_NEXT] START token_prefix={token_prefix} device_id_body={device_id_body} "
        f"token_device_id={token_device_id} runner_id={runner.runner_id}"
    )

    # device_id nel body deve combaciare con quello del token, se presente
    if token_device_id and token_device_id != device_id_body:
        logger.warning(
            f"[RUNNER_NEXT] DEVICE_ID MISMATCH - token_prefix={token_prefix} "
            f"token_device_id={token_device_id} body_device_id={device_id_body} - RETURNING 403"
        )
        raise HTTPException(status_code=403, detail="device_id mismatch for this token")

    # Se runner_tokens.device_id è NULL, aggiorna con il valore ricevuto
    if not token_device_id:
        try:
            update_res = (
                supabase.table("runner_tokens")
                .update({"device_id": device_id_body})
                .eq("token", runner.token)
                .execute()
            )
            rowcount = len(update_res.data) if update_res.data else 0
            logger.info(
                f"[RUNNER_NEXT] Updated runner_tokens.device_id for token_prefix={token_prefix} "
                f"device_id={device_id_body} rowcount={rowcount}"
            )
        except Exception as e:
            error_str = str(e)
            logger.error(
                f"[RUNNER_NEXT] Failed to update runner_tokens.device_id "
                f"token_prefix={token_prefix} error={error_str[:300]}"
            )

    try:
        # Seleziona il comando pending più vecchio per questo device_id
        res = (
            supabase.table("runner_commands")
            .select("id, device_id, status, payload, created_at")
            .eq("device_id", device_id_body)
            .eq("status", "pending")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )

        commands = res.data or []
        commands_len = len(commands)
        
        # Log SEMPRE prima di ogni return
        logger.info(
            f"[RUNNER_NEXT] QUERY RESULT device_id={device_id_body} token_prefix={token_prefix} "
            f"commands_found={commands_len}"
        )
        
        if not commands:
            logger.info(
                f"[RUNNER_NEXT] NO COMMAND FOUND - device_id={device_id_body} "
                f"token_prefix={token_prefix} - RETURNING 204"
            )
            return Response(status_code=204)

        command = commands[0]
        command_id = command.get("id")
        
        # Estrai command_text da payload
        payload = command.get("payload", {})
        command_text = payload.get("text") if isinstance(payload, dict) else None
        
        # Costruisci la risposta con command_text derivato da payload
        response = {
            "id": command.get("id"),
            "device_id": command.get("device_id"),
            "status": command.get("status"),
            "payload": command.get("payload"),
            "created_at": command.get("created_at"),
            "command_text": command_text
        }

        logger.info(
            f"[RUNNER_NEXT] FOUND command_id={command_id} device_id={device_id_body} "
            f"token_prefix={token_prefix} - RETURNING 200"
        )

        return response
    except Exception as e:
        error_str = str(e)
        logger.error(f"[RUNNER_NEXT] Supabase error: {error_str[:300]}")
        raise HTTPException(status_code=500, detail="Database error")


@app.post("/api/runner/ack")
async def runner_ack(
    payload: RunnerAckPayload,
    request: Request,
    runner: RunnerAuthContext = Depends(get_current_runner),
):
    """
    Marca un comando come consumed o failed in public.runner_commands.
    Autenticazione tramite x-runner-token.
    """
    command_id = payload.command_id
    status = payload.status
    error_message = payload.error_message

    if not command_id:
        logger.error("[RUNNER_ACK] command_id mancante nel body")
        raise HTTPException(status_code=400, detail="command_id is required")

    token_prefix = runner.token_prefix

    # Log SEMPRE all'inizio
    logger.info(
        f"[RUNNER_ACK] START command_id={command_id} status={status} "
        f"device_id={runner.device_id} token_prefix={token_prefix} runner_id={runner.runner_id}"
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    update_data: Dict[str, Any] = {
        "status": "consumed" if status == "consumed" else "failed",
        "consumed_at": now_iso,
    }

    # Se esiste una colonna error_message la valorizziamo (non è un errore se non esiste)
    if status == "failed" and error_message:
        update_data["error_message"] = error_message

    try:
        res = (
            supabase.table("runner_commands")
            .update(update_data)
            .eq("id", command_id)
            .execute()
        )
        rowcount = len(res.data) if res.data else 0
        
        # Log SEMPRE con rowcount
        if rowcount == 0:
            logger.warning(
                f"[RUNNER_ACK] UPDATE DID NOTHING - command_id={command_id} "
                f"status={status} token_prefix={token_prefix} rowcount=0"
            )
        else:
            logger.info(
                f"[RUNNER_ACK] UPDATED command_id={command_id} "
                f"new_status={update_data['status']} rowcount={rowcount} token_prefix={token_prefix}"
            )
        return {"ok": True}
    except Exception as e:
        error_str = str(e)
        logger.error(f"[RUNNER_ACK] Supabase error: {error_str[:300]}")
        raise HTTPException(status_code=500, detail="Database error")


@app.post("/api/runner/event")
async def runner_event(
    payload: RunnerEventPayload,
    request: Request,
    runner: RunnerAuthContext = Depends(get_current_runner),
):
    """
    Inserisce un evento in public.runner_events per il device_id indicato.
    Autenticazione tramite x-runner-token.
    """
    device_id_body = (payload.device_id or "").strip()
    if not device_id_body:
        raise HTTPException(status_code=400, detail="device_id is required")

    token_prefix = runner.token_prefix
    token_device_id = (runner.device_id or "").strip() if runner.device_id else None

    logger.info(
        f"[RUNNER_EVENT] type={payload.type} command_id={payload.command_id} "
        f"device_id_body={device_id_body} token_device_id={token_device_id} "
        f"token_prefix={token_prefix}"
    )

    # device_id nel body deve combaciare con quello del token, se presente
    if token_device_id and token_device_id != device_id_body:
        logger.warning(
            f"[RUNNER_EVENT] device_id mismatch token_prefix={token_prefix} "
            f"token_device_id={token_device_id} body_device_id={device_id_body}"
        )
        raise HTTPException(status_code=403, detail="device_id mismatch for this token")

    event_data: Dict[str, Any] = {
        "device_id": device_id_body,
        "type": payload.type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload.command_id:
        event_data["command_id"] = payload.command_id
    if payload.payload is not None:
        event_data["payload"] = payload.payload

    uid_ev = runner.user_id
    if uid_ev is not None and str(uid_ev).strip():
        event_data["user_id"] = str(uid_ev).strip()

    try:
        res = supabase.table("runner_events").insert(event_data).execute()
        event_id = None
        if res.data and len(res.data) > 0:
            event_id = res.data[0].get("id")

        logger.info(
            f"[RUNNER_EVENT] INSERTED event_id={event_id} type={payload.type} "
            f"command_id={payload.command_id} device_id={device_id_body} "
            f"token_prefix={token_prefix}"
        )

        return {"ok": True, "event_id": event_id}
    except Exception as e:
        error_str = str(e)
        logger.error(f"[RUNNER_EVENT] Supabase error: {error_str[:300]}")
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/api/runner/diag")
async def runner_diag(
    runner: RunnerAuthContext = Depends(get_current_runner),
):
    """
    Endpoint diagnostico per verificare configurazione runner token e device_id.
    Protetto da runner token come gli altri endpoint runner.
    """
    token_prefix = runner.token_prefix
    token_device_id = runner.device_id
    runner_id = runner.runner_id
    
    # Query runner_tokens per ottenere is_active
    is_active = None
    try:
        res = (
            supabase.table("runner_tokens")
            .select("is_active")
            .eq("token", runner.token)
            .limit(1)
            .execute()
        )
        if res.data and len(res.data) > 0:
            is_active = res.data[0].get("is_active")
    except Exception as e:
        logger.warning(f"[RUNNER_DIAG] Errore query is_active: {str(e)[:200]}")
    
    server_time_utc = datetime.now(timezone.utc).isoformat()
    
    diag_data = {
        "runner_token_prefix": token_prefix,
        "runner_tokens_device_id": token_device_id,
        "runner_tokens_runner_id": runner_id,
        "runner_tokens_is_active": is_active,
        "server_time_utc": server_time_utc,
        "backend_seen_device_id": token_device_id,  # device_id visto dal backend (da token)
    }
    
    logger.info(f"[RUNNER_DIAG] diagnostic request token_prefix={token_prefix}")
    return diag_data


def get_or_create_chat(user_id: str, title: str = "Nuova chat 1") -> dict:
    """
    Garantisce che esista ESATTAMENTE UNA chat per l'utente.
    - Se esiste già una chat, ritorna la più recente (per created_at)
    - Se non esiste, ne crea una nuova
    - Se esistono più chat, ritorna solo la più recente (compatibilità con dati esistenti)
    """
    # Cerca chat esistenti per l'utente, ordinate per created_at DESC (più recente prima)
    res = (
        supabase.table("chats")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    
    existing_chats = res.data or []
    
    # Se esiste già almeno una chat, ritorna la più recente
    if len(existing_chats) > 0:
        return existing_chats[0]
    
    # Se non esiste nessuna chat, crea una nuova
    insert = {
        "user_id": user_id,
        "title": title,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    
    create_res = supabase.table("chats").insert(insert).execute()
    if not create_res.data:
        raise HTTPException(status_code=500, detail="Failed to create chat")
    
    return create_res.data[0]


@app.get("/api/list_chats")
def list_chats(user=Depends(get_current_user)):
    """
    Ritorna sempre UNA SOLA chat per l'utente.
    Se l'utente ha più chat (dati esistenti), ritorna solo la più recente.
    Per UI listing: ritorna solo id, title, created_at (select limitato).
    """
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Per UI listing: usa select limitato (solo id, title, created_at)
    res = (
        supabase.table("chats")
        .select("id, title, created_at")
        .eq("user_id", user["id"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    
    existing_chats = res.data or []
    
    # Se esiste già almeno una chat, ritorna la più recente
    if len(existing_chats) > 0:
        return {"ok": True, "chats": existing_chats}
    
    # Se non esiste nessuna chat, crea una nuova (usa get_or_create_chat per creazione)
    chat = get_or_create_chat(user["id"])
    # Filtra solo i campi necessari per listing
    chat_limited = {
        "id": chat.get("id"),
        "title": chat.get("title"),
        "created_at": chat.get("created_at")
    }
    
    # Ritorna sempre un array con una sola chat
    return {"ok": True, "chats": [chat_limited]}

@app.post("/api/create_chat")
def create_chat(payload: CreateChatPayload, user=Depends(get_current_user)):
    """
    Crea o ritorna la chat esistente per l'utente.
    Garantisce che esista AL MASSIMO UNA chat per utente.
    Se esiste già una chat, la ritorna invece di crearne una nuova.
    """
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Usa get_or_create_chat: se esiste già una chat, la ritorna
    # Se non esiste, ne crea una nuova con il titolo fornito
    title = payload.title.strip() if payload.title else "Nuova chat 1"
    chat = get_or_create_chat(user["id"], title)

    return {"ok": True, "chat": chat}

@app.post("/api/save_message")
def save_message(payload: SaveMessagePayload, user=Depends(get_current_user)):
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Genera msg_id se non fornito e origin è server_ack
    msg_id = payload.msg_id
    if not msg_id and payload.origin == "server_ack":
        timestamp_ms = int(time.time() * 1000)
        random4 = f"{random.randint(1000, 9999)}"
        msg_id = f"{timestamp_ms}-{random4}"
    
    # Se origin è server_ack, aggiungi debug info al content se non c'è metadata
    content = payload.message
    if payload.origin == "server_ack" and msg_id and "[origin=server_ack" not in content:
        # Aggiungi temporaneamente in coda al testo
        debug_suffix = f" [origin=server_ack id={msg_id}]"
        content = content + debug_suffix
    
    insert = {
        "chat_id": payload.chat_id,
        "user_id": user["id"],
        "role": payload.role,
        "content": content,
        "created_at": now_iso(),
    }
    
    # Se abbiamo origin/msg_id, prova a salvarli in metadata (se la colonna esiste)
    # Altrimenti sono già nel content come fallback
    if payload.origin or msg_id:
        # Prova a salvare in metadata se esiste, altrimenti già nel content
        try:
            # Se Supabase supporta metadata, aggiungilo
            if hasattr(supabase.table("messages").insert, "__call__"):
                # Aggiungi come campo extra se supportato (dipende dallo schema DB)
                pass  # Per ora lasciamo nel content come fallback
        except:
            pass

    res = supabase.table("messages").insert(insert).execute()
    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to save message")
    
    saved_msg_id = res.data[0].get("id") or msg_id
    
    # Log per diagnosi
    if payload.origin == "server_ack":
        print(f"[ACK_INSERT] chat_id={payload.chat_id} msg_id={saved_msg_id}")
        # Emetti live l'ACK via SSE
        try:
            import urllib.request
            import json as json_lib
            sse_payload = {
                "session": payload.chat_id,
                "type": "message",
                "data": {
                    "role": payload.role,
                    "content": content,
                    "chat_id": payload.chat_id,
                    "id": saved_msg_id,
                    "origin": "server_ack"
                }
            }
            req = urllib.request.Request(
                "http://127.0.0.1:8888/emit",
                data=json_lib.dumps(sse_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=1.0) as _:
                pass
        except Exception as e:
            # Non bloccare se SSE non disponibile
            logger.debug(f"[SSE_ACK] errore emissione live: {e}")

    # aggiorna chat.updated_at
    supabase.table("chats").update({"updated_at": now_iso()}).eq("id", payload.chat_id).execute()

    return {"ok": True, "message_id": saved_msg_id}


# ---------------------------
# Chat state persistence (Supabase)
# ---------------------------

def load_chat_state(chat_id: str):
    """
    Carica lo state dalla tabella chats.
    Ritorna sempre un dict.
    """
    try:
        res = (
            supabase.table("chats")
            .select("config_status, config_state, active_bot_id")
            .eq("id", chat_id)
            .limit(1)
            .execute()
        )

        rows = res.data or []
        if len(rows) == 0:
            # Chat non trovata: fallback safe
            return {"config_status": "new", "config_state": None, "active_bot_id": None}

        row = rows[0]
        return {
            "config_status": row.get("config_status", "new"),
            "config_state": row.get("config_state"),
            "active_bot_id": row.get("active_bot_id"),
        }

    except Exception as e:
        print("[CHAT_STATE] load error:", e)
        return {"config_status": "new", "config_state": None, "active_bot_id": None}


def _deep_merge_config_state(existing: dict, incoming: dict) -> dict:
    """
    Merge config_state: per ogni chiave in incoming, se valore != None => SOVRASCRIVE sempre (anche se in DB c'è già un valore).
    Mantiene i campi non toccati. strategy_params: merge per chiave solo se operating_mode non cambia;
    se operating_mode cambia, strategy_params in incoming sostituisce l'intero dict (nessuna chiave residua).
    """
    import copy
    if not isinstance(existing, dict):
        existing = {}
    if not isinstance(incoming, dict):
        return copy.deepcopy(existing)
    result = copy.deepcopy(existing)
    # Top-level: step e altri campi (copiare anche None per poter cancellare pending_*)
    for key in ("step", "error_count", "pending_risk_confirmation", "pending_sl_confirmation", "pending_leverage_confirmation", "suggested_sl", "last_greeting_variant", "period_index"):
        if key in incoming:
            inval = incoming[key]
            result[key] = copy.deepcopy(inval) if isinstance(inval, dict) else inval
    # params: merge chiave per chiave; se valore != None sovrascrive
    existing_params = result.get("params") or {}
    if not isinstance(existing_params, dict):
        existing_params = {}
    incoming_params = incoming.get("params") if isinstance(incoming.get("params"), dict) else {}
    result_params = copy.deepcopy(existing_params)

    def _norm_mode(m):
        if m is None:
            return None
        s = str(m).strip().lower()
        return s if s else None

    db_params = existing.get("params") if isinstance(existing.get("params"), dict) else {}
    inc_mode = _norm_mode(incoming_params.get("operating_mode"))
    db_mode = _norm_mode(db_params.get("operating_mode"))
    operating_mode_changed = inc_mode is not None and inc_mode != db_mode

    for pk, pv in (incoming_params or {}).items():
        if pv is None:
            continue
        if pk == "strategy_params" and isinstance(pv, dict):
            if operating_mode_changed:
                prev_sp = result_params.get("strategy_params")
                result_params["strategy_params"] = copy.deepcopy(pv)
                logger.info(
                    "[CONFIG_STATE_MERGE] operating_mode preset rebuild: previous_mode=%s new_mode=%s "
                    "previous_strategy_params=%s new_strategy_params=%s (full replace, no key merge)",
                    db_mode,
                    inc_mode,
                    prev_sp,
                    result_params["strategy_params"],
                )
            else:
                sp = result_params.get("strategy_params") or {}
                if not isinstance(sp, dict):
                    sp = {}
                for sk, sv in pv.items():
                    if sv is not None:
                        sp[sk] = sv
                result_params["strategy_params"] = sp
        else:
            result_params[pk] = copy.deepcopy(pv) if isinstance(pv, dict) else pv

    if operating_mode_changed and "strategy_params" not in incoming_params:
        logger.warning(
            "[CONFIG_STATE_MERGE] operating_mode changed (%s -> %s) but incoming omitted strategy_params; "
            "merged state may still contain keys from the previous mode",
            db_mode,
            inc_mode,
        )

    # FREE v2: garantisci sempre la presenza delle chiavi operating_mode, strategy_id, strategy_params
    # Anche se i valori sono None, le chiavi devono esistere in params.
    for key in ("operating_mode", "strategy_id", "strategy_params"):
        if key not in result_params:
            # Preferisci valore incoming non-None, altrimenti fallback a existing o None
            incoming_val = (incoming_params or {}).get(key)
            existing_val = (existing_params or {}).get(key)
            if incoming_val is not None:
                result_params[key] = copy.deepcopy(incoming_val) if isinstance(incoming_val, dict) else incoming_val
            elif existing_val is not None:
                result_params[key] = copy.deepcopy(existing_val) if isinstance(existing_val, dict) else existing_val
            else:
                result_params[key] = None
    
    if result_params.get("market_type") == "spot":
        result_params["leverage"] = None

    result["params"] = result_params
    return result


def save_chat_state(chat_id: str, user_id: str, state: dict):
    """
    Salva lo state dentro la tabella chats.
    """
    try:
        # Cast esplicito
        chat_id = str(chat_id)
        user_id = str(user_id)
        
        # Log PRIMA: estrai campi chiave
        config_state = state.get("config_state")
        # Compatibilità: se lo state non contiene ancora "config_state" ma ha "params",
        # costruisci una struttura minimale {"params": state["params"]} per il salvataggio.
        if not config_state and isinstance(state, dict) and isinstance(state.get("params"), dict):
            incoming_params_from_state = state.get("params") or {}
            config_state = {"params": incoming_params_from_state}
            state = dict(state)
            state["config_state"] = config_state
        params = {}
        step = None
        timeframe = None
        leverage = None
        risk_pct = None
        sl = None
        tp = None
        strategy = None
        free_strategy_id = None
        ema_period = None
        rsi_period = None
        atr_period = None
        
        if config_state and isinstance(config_state, dict):
            step = config_state.get("step")
            params = config_state.get("params", {})
            if isinstance(params, dict):
                timeframe = params.get("timeframe")
                leverage = params.get("leverage")
                risk_pct = params.get("risk_pct")
                sl = params.get("sl")
                tp = params.get("tp")
                strategy = params.get("strategy")
                free_strategy_id = params.get("free_strategy_id")
                ema_period = params.get("ema_period")
                rsi_period = params.get("rsi_period")
                atr_period = params.get("atr_period")
        
        logger.info(f"[SAVE_CHAT_STATE] BEFORE: chat_id={chat_id}, user_id={user_id}, step={step}, timeframe={timeframe}, leverage={leverage}")
        
        # SELECT per verificare esistenza e ownership
        try:
            chat_res = (
                supabase.table("chats")
                .select("id,user_id,config_state,config_status")
                .eq("id", chat_id)
                .execute()
            )
        except Exception as e:
            logger.error(f"[SAVE_CHAT_STATE] SELECT failed: chat_id={chat_id}, error={e}")
            return {"ok": False, "reason": "chat_not_found"}
        
        chat_data = chat_res.data[0] if chat_res.data else None
        if not chat_data:
            logger.error(f"[SAVE_CHAT_STATE] Chat not found: chat_id={chat_id}")
            return {"ok": False, "reason": "chat_not_found"}
        chat_user_id = str(chat_data.get("user_id", ""))

        # Ownership check: applica solo se user_id è valorizzato (flusso normale API)
        if user_id and chat_user_id != user_id:
            logger.error(
                f"[SAVE_CHAT_STATE] User mismatch: chat_id={chat_id}, "
                f"expected user_id={user_id}, actual user_id={chat_user_id}"
            )
            return {"ok": False, "reason": "user_mismatch"}
        
        # Merge config_state: esistente (DB) + incoming (orchestrator); campi non-None in incoming SOVRASCRIVONO sempre.
        # Caso speciale RESET:
        #   - se incoming_cfg è None, forza config_state a NULL in DB
        #   - se incoming_cfg è lo "scheletro" completo (tutti params null, strategy lista vuota),
        #     sostituisci completamente l'esistente per azzerare davvero la configurazione.
        existing_cfg = chat_data.get("config_state")
        incoming_cfg = state.get("config_state")
        if incoming_cfg is None:
            merged_config_state = None
        elif isinstance(incoming_cfg, dict):
            # Rileva scheletro di reset completo
            incoming_params = incoming_cfg.get("params") or {}
            is_reset_skeleton = (
                incoming_cfg.get("step") == "market_type"
                and isinstance(incoming_params, dict)
                and incoming_params.get("strategy") == []
                and incoming_params.get("market_type") is None
                and incoming_params.get("symbol") is None
                and incoming_params.get("timeframe") is None
                and incoming_params.get("strategy_id") is None
                and incoming_params.get("operating_mode") is None
                and incoming_params.get("strategy_params") is None
                and incoming_params.get("leverage") is None
                and incoming_params.get("risk_pct") is None
                and incoming_params.get("sl") is None
                and incoming_params.get("tp") is None
            )
            if is_reset_skeleton:
                # RESET COMPLETO: ignora lo stato esistente e usa direttamente lo scheletro
                merged_config_state = incoming_cfg
            elif isinstance(existing_cfg, dict):
                merged_config_state = _deep_merge_config_state(existing_cfg, incoming_cfg)
            else:
                merged_config_state = incoming_cfg
        else:
            merged_config_state = existing_cfg
        
        # Garantire un valore coerente per l'UPDATE
        if merged_config_state is None:
            config_state_to_save = None
        elif isinstance(merged_config_state, str):
            try:
                config_state_to_save = json.loads(merged_config_state) if merged_config_state.strip() else {}
            except Exception:
                config_state_to_save = {}
        elif isinstance(merged_config_state, dict):
            config_state_to_save = merged_config_state
        else:
            config_state_to_save = {}
        
        # UPDATE solo per id (ownership già verificata); filtro SOLO .eq("id", chat_id)
        update_payload = {
            "config_status": state.get("config_status", "new"),
            "config_state": config_state_to_save,
            "active_bot_id": state.get("active_bot_id"),
            "updated_at": now_iso(),
        }
        
        # Log sintetico del payload che stiamo per salvare (fonte: orchestrator state)
        logger.info(
            "SAVE_PAYLOAD strategy=%s free_strategy_id=%s ema_period=%s rsi_period=%s atr_period=%s step=%s",
            strategy,
            free_strategy_id,
            ema_period,
            rsi_period,
            atr_period,
            step,
        )
        
        try:
            res = supabase.table("chats").update(update_payload).eq("id", chat_id).execute()
        except Exception as e:
            logger.error(f"[SAVE_CHAT_STATE] UPDATE failed: chat_id={chat_id}, error={e}")
            return {"ok": False, "reason": "update_failed"}
        
        if not res.data or len(res.data) == 0:
            logger.error(f"[SAVE_CHAT_STATE] UPDATE returned empty data: chat_id={chat_id}")
            return {"ok": False, "reason": "update_failed"}
        
        # SELECT dopo update per verificare salvataggio
        try:
            chat_after_res = (
                supabase.table("chats")
                .select("id,user_id,config_state,config_status")
                .eq("id", chat_id)
                .execute()
            )
        except Exception as e:
            logger.error(f"[SAVE_CHAT_STATE] SELECT after update failed: chat_id={chat_id}, error={e}")
            return {"ok": False, "reason": "update_failed"}
        
        saved_row = chat_after_res.data[0] if chat_after_res.data else None
        if not saved_row:
            logger.error(f"[SAVE_CHAT_STATE] Chat not found after update: chat_id={chat_id}")
            return {"ok": False, "reason": "update_failed"}
        
        # Confronto campi chiave
        saved_config_state = saved_row.get("config_state") if saved_row else None
        saved_params = {}
        saved_step = None
        saved_timeframe = None
        saved_leverage = None
        saved_risk_pct = None
        saved_sl = None
        saved_tp = None
        saved_strategy = None
        saved_free_strategy_id = None
        saved_ema_period = None
        saved_rsi_period = None
        saved_atr_period = None
        
        if saved_config_state and isinstance(saved_config_state, dict):
            saved_step = saved_config_state.get("step")
            saved_params = saved_config_state.get("params", {})
            if isinstance(saved_params, dict):
                saved_timeframe = saved_params.get("timeframe")
                saved_leverage = saved_params.get("leverage")
                saved_risk_pct = saved_params.get("risk_pct")
                saved_sl = saved_params.get("sl")
                saved_tp = saved_params.get("tp")
                saved_strategy = saved_params.get("strategy")
                saved_free_strategy_id = saved_params.get("free_strategy_id")
                saved_ema_period = saved_params.get("ema_period")
                saved_rsi_period = saved_params.get("rsi_period")
                saved_atr_period = saved_params.get("atr_period")
        
        # Log confronto sintetico per debug di Supabase (post-GET)
        logger.info(
            "AFTER_PATCH_DB strategy=%s free_strategy_id=%s ema_period=%s rsi_period=%s atr_period=%s step=%s",
            saved_strategy,
            saved_free_strategy_id,
            saved_ema_period,
            saved_rsi_period,
            saved_atr_period,
            saved_step,
        )
        
        # Log dettagliato completo (compatibilità con debug esistente)
        logger.info(
            f"[SAVE_CHAT_STATE] AFTER: chat_id={chat_id}, step={saved_step} (expected={step}), "
            f"timeframe={saved_timeframe} (expected={timeframe}), leverage={saved_leverage} (expected={leverage}), "
            f"risk_pct={saved_risk_pct} (expected={risk_pct}), sl={saved_sl} (expected={sl}), "
            f"tp={saved_tp} (expected={tp}), strategy={saved_strategy} (expected={strategy})"
        )
        
        return {"ok": True}

    except Exception as e:
        logger.exception("[CHAT_STATE] save error")
        return {"ok": False, "reason": f"exception: {str(e)}"}

def reset_chat_state(chat_id: str, user_id: str):
    """
    Reset distruttivo con conferma: cancella configurazione e bot collegato alla chat.
    """
    try:
        supabase.table("chats").update({
            "config_status": "new",
            "config_state": None,
            "active_bot_id": None,
            "updated_at": now_iso(),
        }).eq("id", chat_id).eq("user_id", user_id).execute()
    except Exception as e:
        print("[CHAT_STATE] reset error:", e)

def reset_config_state(chat_id: str, user_id: str):
    """
    Reset configurazione per una chat: azzera config_state (NULL in DB).
    Imposta active_bot_id a None. Al prossimo messaggio l'orchestrator ricaricherà e inizializzerà da zero.
    """
    try:
        # Scheletro di configurazione dopo reset: tutti i parametri presenti e null (strategy lista vuota)
        reset_config_state_skeleton = {
            "step": "market_type",
            "params": {
                "market_type": None,
                "symbol": None,
                "timeframe": None,
                "strategy": [],
                "strategy_id": None,
                "operating_mode": None,
                "strategy_params": None,
                "leverage": None,
                "risk_pct": None,
                "sl": None,
                "tp": None,
            },
            "error_count": {},
        }
        supabase.table("chats").update({
            "config_status": "new",
            "config_state": reset_config_state_skeleton,
            "active_bot_id": None,
            "updated_at": now_iso(),
        }).eq("id", chat_id).eq("user_id", user_id).execute()
        
        logger.info(f"[RESET] chat_id={chat_id} config_state impostato allo scheletro di default")
    except Exception as e:
        logger.error(f"[RESET] reset_config_state error: chat_id={chat_id}, error={e}")
        raise





CHAT_MESSAGES_UI_LIMIT = 40


@app.get("/api/get_messages")
def get_messages(chat_id: str, user=Depends(get_current_user)):
    """
    Restituisce gli ultimi CHAT_MESSAGES_UI_LIMIT messaggi della chat, in ordine
    cronologico (created_at ASC tra quel sottoinsieme). Lo storico completo resta in DB.
    Verifica che la chat appartenga all'utente.

    Gestisce errori DB con retry, circuit breaker e ritorna degraded response
    invece di 503 per non rompere la UI.
    """
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")

    # Circuit breaker check: se aperto, ritorna subito degraded senza toccare DB
    if not db_circuit_breaker.should_attempt():
        logger.warning(f"[GET_MESSAGES] Circuit breaker open, returning degraded response for chat_id={chat_id}")
        return {
            "ok": True,
            "messages": [],
            "degraded": True,
            "error": "db_unavailable"
        }

    def fetch_chat_and_messages():
        """Inner function for retry logic."""
        # Verifica che la chat appartenga all'utente
        chat_res = (
            supabase.table("chats")
            .select("id, title, created_at")
            .eq("id", chat_id)
            .eq("user_id", user["id"])
            .execute()
        )
        
        if not chat_res.data:
            # Chat not found is not a DB error, don't retry
            raise HTTPException(status_code=404, detail="Chat not found")

        # Ultimi N messaggi: query DESC + limit, poi inverti per visualizzazione ASC
        messages_res = (
            supabase.table("messages")
            .select("*")
            .eq("chat_id", chat_id)
            .order("created_at", desc=True)
            .limit(CHAT_MESSAGES_UI_LIMIT)
            .execute()
        )

        messages = []
        for msg in reversed(messages_res.data or []):
            messages.append({
                "id": msg.get("id"),
                "msg_id": msg.get("msg_id") or msg.get("message_id"),
                "origin": msg.get("origin"),
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
                "created_at": msg.get("created_at", "")
            })

        return {"ok": True, "messages": messages, "degraded": False}

    # Retry con backoff (max 3 tentativi)
    success, result, error = retry_with_backoff(fetch_chat_and_messages, max_attempts=3)
    
    if success:
        # Success: reset circuit breaker
        db_circuit_breaker.record_success()
        return result
    
    # Failure: record nel circuit breaker
    db_circuit_breaker.record_failure()
    
    # Log error details
    error_type = type(error).__name__ if error else "Unknown"
    error_msg = str(error) if error else "Unknown error"
    logger.error(
        f"[GET_MESSAGES] DB error after retries: type={error_type}, "
        f"error={error_msg}, chat_id={chat_id}"
    )
    
    # Ritorna 200 con payload degradato (non 503) per non rompere la UI
    return {
        "ok": True,
        "messages": [],
        "degraded": True,
        "error": "db_unavailable"
    }

@app.get("/api/load_chat")
def load_chat(chat_id: str, user=Depends(get_current_user)):
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")

    # Per fetch singola chat (usata dal runner per BOT_START): usa select=* per includere tutti i campi config
    # Questo endpoint è usato per caricare una chat completa con configurazione, non solo per listing
    chat_res = (
        supabase.table("chats")
        .select("*")
        .eq("id", chat_id)
        .eq("user_id", user["id"])
        .limit(1)
        .execute()
    )
    
    if not chat_res.data:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Carica messaggi
    messages_res = (
        supabase.table("messages")
        .select("*")
        .eq("chat_id", chat_id)
        .order("created_at", desc=False)
        .execute()
    )

    messages = []
    for msg in (messages_res.data or []):
        messages.append({
            "role": msg.get("role", "user"),
            "text": msg.get("content", ""),  # Fixed: use 'content' not 'message'
            "created_at": msg.get("created_at", "")
        })

    return {
        "title": chat_res.data[0].get("title", "Chat"),
        "created_at": chat_res.data[0].get("created_at", ""),
        "messages": messages
    }

class DeleteChatPayload(BaseModel):
    chat_id: str

@app.post("/api/delete_chat")
def delete_chat(payload: DeleteChatPayload, user=Depends(get_current_user)):
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not payload.chat_id:
        raise HTTPException(status_code=400, detail="chat_id is required")

    # Verifica che la chat appartenga all'utente
    chat_res = (
        supabase.table("chats")
        .select("id")
        .eq("id", payload.chat_id)
        .eq("user_id", user["id"])
        .execute()
    )
    
    if not chat_res.data:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Elimina messaggi associati
    supabase.table("messages").delete().eq("chat_id", payload.chat_id).execute()
    
    # Elimina chat
    supabase.table("chats").delete().eq("id", payload.chat_id).execute()

    return {"ok": True}

class RenameChatPayload(BaseModel):
    user_id: str
    chat_id: str
    new_name: str


@app.post("/api/rename_chat")
def rename_chat(payload: RenameChatPayload, user=Depends(get_current_user)):
    # Logging diagnostico
    print(f"[RENAME] Request received - user_id: {user.get('id', 'N/A')}, chat_id: {payload.chat_id}, new_name: {payload.new_name[:50] if payload.new_name else 'None'}...")
    
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    if not payload.chat_id:
        print("[RENAME] Error: chat_id is required")
        return {"ok": False, "error": "chat_id is required"}
    
    new_name_trimmed = payload.new_name.strip() if payload.new_name else ""
    if not new_name_trimmed:
        print("[RENAME] Error: new_name is required")
        return {"ok": False, "error": "new_name is required"}
    
    # Verifica che la chat appartenga all'utente
    try:
        chat_res = (
            supabase.table("chats")
            .select("id")
            .eq("id", payload.chat_id)
            .eq("user_id", user["id"])
            .execute()
        )
        
        if not chat_res.data:
            print(f"[RENAME] Error: Chat not found - chat_id: {payload.chat_id}, user_id: {user['id']}")
            return {"ok": False, "error": "Chat not found"}
        
        print(f"[RENAME] Chat found, proceeding with update...")
    except Exception as e:
        print(f"[RENAME] Error checking chat existence: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": f"Error checking chat: {str(e)}"}
    
    # Aggiorna il titolo della chat
    try:
        update_res = (
            supabase.table("chats")
            .update({"title": new_name_trimmed, "updated_at": now_iso()})
            .eq("id", payload.chat_id)
            .eq("user_id", user["id"])
            .execute()
        )
        
        if not update_res.data:
            print(f"[RENAME] Error: Failed to update chat - update_res.data is empty")
            return {"ok": False, "error": "Failed to update chat"}
        
        print(f"[RENAME] Success - chat_id: {payload.chat_id}, new_name: {new_name_trimmed}")
        return {"ok": True}
    except Exception as e:
        print(f"[RENAME] Exception during update: {e}")
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e)}

# Tabella alias centralizzata per comandi di testo (stesso mapping del runner.py)
COMMAND_ALIASES = {
    "stato": "/trade status",
    "trade status": "/trade status",
    "status": "/trade status",
    "trade_status": "/trade status",
}

# Keyword whitelist per comandi backend (NON runner)
BACKEND_KEYWORD_COMMANDS = {
    "stato", "status", "stato bot",
    "mostra eventi", "analizza eventi",
    "avvia bot", "stop bot"
}

def _normalize_text_command(text: str) -> str:
    """
    Normalizza input: trim, lower, collapse spaces.
    Se text matcha un alias, lo mappa al comando corrispondente.
    Se text non inizia con / ma matcha un alias, aggiunge /.
    """
    if not text:
        return ""
    
    # Trim e collapse spaces
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ""
    
    # Lower per matching
    normalized_lower = normalized.lower()
    
    # Controlla alias
    if normalized_lower in COMMAND_ALIASES:
        return COMMAND_ALIASES[normalized_lower]
    
    # Se già inizia con /, ritorna così com'è
    if normalized.startswith("/"):
        return normalized
    
    # Altrimenti ritorna il testo normalizzato (potrebbe essere un messaggio normale)
    return normalized

def _is_slash_command(message: str) -> bool:
    """Verifica se il messaggio è un comando slash (es. /trade status)."""
    if not message:
        return False
    message = message.strip()
    return message.startswith("/") and len(message) > 1

def _is_runner_command(message: str) -> tuple[bool, str]:
    """
    Verifica se il messaggio è un comando runner (slash o normalizzato).
    Ritorna (is_runner_cmd: bool, normalized_command: str)
    - is_runner_cmd: True se è un comando runner (SOLO se inizia con "/")
    - normalized_command: comando normalizzato (es. "/trade status") o messaggio originale
    
    REGOLA: Solo comandi che iniziano con "/" vanno al runner.
    """
    if not message:
        return (False, "")
    
    message_stripped = message.strip()
    if not message_stripped:
        return (False, "")
    
    # Se inizia già con /, è sicuramente un comando runner
    if message_stripped.startswith("/"):
        return (True, message_stripped)
    
    # Normalizza il comando (per alias come "stato" -> "/trade status")
    normalized = _normalize_text_command(message_stripped)
    
    # Se normalizzato inizia con / (dopo alias mapping), è un comando runner
    if normalized.startswith("/"):
        return (True, normalized)
    
    # Testo senza slash NON è un comando runner
    return (False, message_stripped)

def is_reset_command(text: str) -> bool:
    """
    Verifica se il testo è un comando di reset del bot.
    Accetta: "resetta bot", "reset bot", "/reset", "restart bot", "ricomincia bot", "reset"
    """
    if not text:
        return False
    
    # Normalizza: strip, lower, collapse spazi
    normalized = " ".join(text.strip().lower().split())
    
    # Rimuovi punteggiatura finale
    normalized = normalized.rstrip("!.,?;:")
    
    # Lista comandi accettati
    reset_commands = [
        "resetta bot",
        "reset bot",
        "/reset",
        "restart bot",
        "ricomincia bot",
        "reset",
        "voglio ricominciare il bot",
        "ricominciare il bot",
        "voglio ricominciare",
        "voglio ripartire da zero",
    ]

    
    return normalized in reset_commands


# ----------------------------------------
# NATURAL LANGUAGE ROUTER
# ----------------------------------------

# Mapping accenti comuni (semplice, solo i più frequenti)
ACCENT_MAP = {
    'à': 'a', 'è': 'e', 'é': 'e', 'ì': 'i', 'ò': 'o', 'ù': 'u',
    'À': 'a', 'È': 'e', 'É': 'e', 'Ì': 'i', 'Ò': 'o', 'Ù': 'u'
}

def normalize_user_text(text: str) -> str:
    """
    Normalizza testo utente per matching:
    - strip, lowercase
    - rimuovi punteggiatura base
    - comprimi spazi
    - sostituisci accenti comuni
    """
    if not text:
        return ""
    
    # Lowercase e strip
    normalized = text.strip().lower()
    
    # Sostituisci accenti
    for accented, unaccented in ACCENT_MAP.items():
        normalized = normalized.replace(accented, unaccented)
    
    # Rimuovi punteggiatura (mantieni spazi)
    normalized = normalized.translate(str.maketrans('', '', string.punctuation))
    
    # Comprimi spazi multipli
    normalized = " ".join(normalized.split())
    
    return normalized


def _is_runner_status_question(text: str) -> bool:
    """
    Rileva se il testo dell'utente è una domanda sullo stato/collegamento del runner
    (sia in ITA che in ENG) usando keyword robuste.
    
    Regola principale:
    - contiene "runner" e almeno una tra:
      "collegato", "connesso", "online", "attivo", "raggiungibile",
      "sei online", "is online", "connected", "running"
    
    Estende leggermente la logica per coprire anche frasi tipo:
    - "sei collegato?", "sei online?", "are you online?", ecc.
    """
    # Prima: match esplicito su alcune frasi esatte che vogliamo gestire
    # in modo deterministico e senza passare dall'LLM/orchestrator.
    raw = (text or "").strip().lower()
    explicit_status_phrases = {
        "il runner è collegato?",
        "runner è collegato?",
        "è collegato il runner?",
        "/runner",
        "/runner status",
    }
    if raw in explicit_status_phrases:
        return True

    normalized = normalize_user_text(text)
    if not normalized:
        return False
    
    # Keyword principali
    status_keywords = [
        "collegato",
        "connesso",
        "online",
        "attivo",
        "raggiungibile",
        "sei online",
        "is online",
        "connected",
        "running",
    ]
    
    # Caso principale: deve contenere "runner" e una delle status_keywords
    if "runner" in normalized and any(kw in normalized for kw in status_keywords):
        return True
    
    # Estensione: domande generiche tipo "sei collegato?", "are you online?"
    # che, nel contesto Idith, sono quasi sempre riferite al runner.
    generic_patterns = [
        "sei collegato",
        "sei connesso",
        "sei online",
        "are you online",
        "are you connected",
        "are you running",
    ]
    if any(p in normalized for p in generic_patterns):
        return True
    
    return False


# Messaggi randomizzati per start/stop bot e stato runner
START_BOT_MESSAGES = [
    "✅ Bot avviato.",
    "✅ Bot attivo.",
    "✅ Il bot è partito.",
    "✅ Bot in esecuzione.",
    "✅ Il bot è stato avviato.",
    "✅ Avvio completato.",
    "✅ Il bot è ora operativo.",
    "✅ Bot online.",
    "✅ Sistema avviato.",
    "✅ Tutto pronto, bot avviato."
]

STOP_BOT_MESSAGES = [
    "✅ Bot fermato.",
    "✅ Bot arrestato.",
    "✅ Il bot è stato fermato.",
    "✅ Stop completato.",
    "✅ Il bot è ora inattivo.",
    "✅ Bot disattivato.",
    "✅ Esecuzione fermata.",
    "✅ Il bot si è fermato.",
    "✅ Bot offline.",
    "✅ Sistema fermato."
]

RUNNER_ONLINE_MESSAGES = [
    "✅ Il runner è collegato.",
    "✅ Runner collegato.",
    "✅ Il runner risulta collegato.",
    "✅ Connessione runner attiva.",
    "✅ Runner online.",
    "✅ Il runner è operativo.",
    "✅ Runner connesso correttamente.",
    "✅ Connessione al runner attiva.",
    "✅ Il runner è disponibile.",
    "✅ Runner attualmente collegato."
]

RUNNER_OFFLINE_MESSAGES = [
    "🚫 Il runner non è collegato.",
    "🚫 Runner non collegato.",
    "🚫 Il runner risulta scollegato.",
    "🚫 Connessione runner assente.",
    "🚫 Runner offline.",
    "🚫 Il runner non è disponibile.",
    "🚫 Connessione al runner non attiva.",
    "🚫 Il runner non è connesso.",
    "🚫 Runner non raggiungibile.",
    "🚫 Nessuna connessione al runner."
]


# Definizione intent con whitelist di frasi-candidato
INTENT_DEFINITIONS = {
    "START_BOT": {
        "type": "RUNNER",
        "command": "/bot start",
        "keywords": ["avvia", "parti", "start", "inizia", "attiva", "fai partire"],
        "required_context": ["bot", "config", "trading", "setup", "configurazione"],
        "candidate_phrases": [
            "avvia bot", "fai partire il bot", "start bot", "inizia bot",
            "avvia configurazione", "inizia configurazione", "avvia setup",
            "parti con la configurazione", "avvia tutto", "fai partire tutto"
        ]
    },
    "STOP_BOT": {
        "type": "RUNNER",
        "command": "/bot stop",
        "keywords": ["ferma", "stop", "blocca", "stoppa", "arresta", "termina"],
        "required_context": ["bot", "trading"],
        "candidate_phrases": [
            "ferma bot", "stop bot", "blocca bot",
            "ferma tutto", "blocca tutto", "stoppa tutto",
            "chiudi posizioni e ferma"
        ]
    },
    "RUNNER_STATUS": {
        "type": "RUNNER",
        "command": "/trade status",
        "keywords": ["stato", "status", "situazione", "collegato", "online"],
        "required_context": [],
        "candidate_phrases": [
            "stato", "mostrami lo stato", "situazione", "sei online", "runner online",
            "mostra stato", "dimmi lo stato", "qual è lo stato",
            "il runner è collegato", "runner collegato", "è collegato"
        ]
    },
    "SHOW_EVENTS_ALL": {
        "type": "LOCAL",
        "command": None,
        "keywords": ["mostra", "mostrami", "lista", "vedi", "log"],
        "required_context": ["eventi", "evento"],
        "candidate_phrases": [
            "mostrami gli eventi", "mostra eventi", "lista eventi",
            "vedi eventi", "log eventi", "mostra gli eventi"
        ]
    },
    "SHOW_EVENTS_POSITIVE": {
        "type": "LOCAL",
        "command": None,
        "keywords": ["mostra", "mostrami", "eventi", "profitto", "gain", "profit"],
        "required_context": ["positivo", "profitto", "gain", "profit", "chiusi in"],
        "candidate_phrases": [
            "mostrami gli eventi chiusi in positivo", "eventi profitto",
            "chiusi in gain", "profit", "eventi positivi"
        ]
    },
    "SHOW_EVENTS_NEGATIVE": {
        "type": "LOCAL",
        "command": None,
        "keywords": ["mostra", "mostrami", "eventi", "perdita", "loss", "negativi"],
        "required_context": ["perdita", "loss", "negativo", "negativi", "chiusi in"],
        "candidate_phrases": [
            "mostrami gli eventi che hanno chiuso in perdita", "eventi in perdita",
            "loss", "negativi", "eventi negativi"
        ]
    }
}

# Soglia per fuzzy matching (0.82 come suggerito)
FUZZY_MATCH_THRESHOLD = 0.82


class IntentResult:
    """Risultato della classificazione intent."""
    def __init__(self, intent_name: str | None, confidence: float, params: dict | None = None):
        self.intent_name = intent_name
        self.confidence = confidence
        self.params = params or {}


def classify_intent(normalized_text: str) -> IntentResult:
    """
    Classifica intent usando keyword/contains + regex + fuzzy matching.
    Ritorna IntentResult con intent_name, confidence, params.
    Se nessun intent matcha, ritorna IntentResult(None, 0.0).
    """
    if not normalized_text:
        return IntentResult(None, 0.0)
    
    best_intent = None
    best_confidence = 0.0
    best_params = {}
    
    # Prova ogni intent
    for intent_name, intent_def in INTENT_DEFINITIONS.items():
        keywords = intent_def.get("keywords", [])
        required_context = intent_def.get("required_context", [])
        candidate_phrases = intent_def.get("candidate_phrases", [])
        
        confidence = 0.0
        matched_keywords = []
        
        # 1) Exact phrase matching (contains) - PRIORITÀ ALTA
        # Se matcha esattamente una candidate phrase, accetta anche senza contesto
        for phrase in candidate_phrases:
            if phrase in normalized_text:
                confidence = max(confidence, 0.9)  # Match esatto su frase candidato
                break
        
        # 2) Keyword matching (contains)
        for kw in keywords:
            if kw in normalized_text:
                matched_keywords.append(kw)
                if confidence < 0.9:  # Solo se non abbiamo già match esatto
                    confidence += 0.3  # Base score per keyword
        
        # 3) Context validation (per START_BOT/STOP_BOT richiede contesto se non c'è match esatto)
        has_context = False
        if required_context:
            has_context = any(ctx in normalized_text for ctx in required_context)
            if has_context:
                if confidence < 0.9:  # Solo se non abbiamo già match esatto
                    confidence += 0.2  # Bonus per contesto
            elif intent_name in ["START_BOT", "STOP_BOT"] and confidence < 0.9:
                # Per START_BOT/STOP_BOT senza contesto e senza match esatto,
                # richiedi fuzzy match alto per sicurezza
                fuzzy_scores = []
                for phrase in candidate_phrases:
                    ratio = SequenceMatcher(None, normalized_text, phrase).ratio()
                    fuzzy_scores.append(ratio)
                
                max_fuzzy = max(fuzzy_scores) if fuzzy_scores else 0.0
                if max_fuzzy >= FUZZY_MATCH_THRESHOLD:
                    confidence = max(confidence, max_fuzzy)
                else:
                    # Se fuzzy match non è abbastanza alto, skip questo intent
                    continue
            elif intent_name in ["SHOW_EVENTS_POSITIVE", "SHOW_EVENTS_NEGATIVE"]:
                # Per SHOW_EVENTS_POSITIVE/NEGATIVE: RICHIEDI SEMPRE contesto esplicito
                # Se non c'è contesto (profitto/perdita/positivo/negativo), skip questo intent
                # Questo evita che "ultimi 5 eventi" attivi filtri profit/loss
                if not has_context:
                    continue  # Skip questo intent se non c'è contesto esplicito
            elif intent_name == "SHOW_EVENTS_ALL":
                # Per SHOW_EVENTS_ALL: richiedi contesto esplicito "eventi"/"evento"
                # Se non c'è contesto, skip questo intent
                if not has_context:
                    continue  # Skip questo intent se non c'è contesto esplicito
        
        # 4) Fuzzy matching su candidate phrases (solo se non abbiamo già match esatto)
        if confidence < 0.9:
            fuzzy_scores = []
            for phrase in candidate_phrases:
                ratio = SequenceMatcher(None, normalized_text, phrase).ratio()
                fuzzy_scores.append(ratio)
            
            if fuzzy_scores:
                max_fuzzy = max(fuzzy_scores)
                if max_fuzzy >= FUZZY_MATCH_THRESHOLD:
                    confidence = max(confidence, max_fuzzy)
        
        # 5) Regex matching per pattern comuni
        if intent_name == "RUNNER_STATUS":
            # Pattern: "stato", "status", "situazione" anche senza contesto se è una parola sola
            if re.match(r'^(stato|status|situazione)$', normalized_text):
                confidence = max(confidence, 0.95)
        
        # Normalizza confidence (max 1.0)
        confidence = min(confidence, 1.0)
        
        # Se confidence è sufficiente, considera questo intent
        if confidence >= 0.5:  # Soglia minima
            if confidence > best_confidence:
                best_intent = intent_name
                best_confidence = confidence
                best_params = {"matched_keywords": matched_keywords}
    
    # Log risultato
    if best_intent:
        logger.debug(f"[NLR] Intent classified: {best_intent} (confidence={best_confidence:.2f}) from text={normalized_text!r}")
    
    return IntentResult(best_intent, best_confidence, best_params)


def _get_events_file_path() -> Path:
    """
    Determina il path di events.jsonl.
    Usa la stessa logica del runner.py: AppData Local / IdithRunner / memory / queue / events.jsonl
    """
    app_name = "IdithRunner"
    base_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / app_name
    queue_dir = base_dir / "memory" / "queue"
    return queue_dir / "events.jsonl"


def format_event(e: dict) -> str:
    """
    Formatta un singolo evento in formato leggibile e pulito.
    
    Per STATUS/COMMAND_RECEIVED: output compatto
    Per OPEN/CLOSE/FILL/TP/SL: output completo con tutti i campi presenti (niente righe con —)
    """
    event_type = e.get("type", "UNKNOWN").upper()
    
    # Mappa type → icona
    if "ORDER_" in event_type and ("OPENED" in event_type or "CLOSED" in event_type):
        icon = "✅"
    elif "FILL" in event_type or "TP" in event_type or "SL" in event_type:
        icon = "✅"
    elif "ERROR" in event_type:
        icon = "❌"
    elif event_type in ["REPLY", "STATUS"]:
        icon = "🟨"
    else:
        icon = "🟨"
    
    # Formatta timestamp (HH:MM:SS)
    ts = e.get("ts", e.get("timestamp", ""))
    time_str = "—"
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            time_str = dt.strftime("%H:%M:%S")
        except:
            if len(ts) >= 8:
                try:
                    time_str = ts[11:19] if 'T' in ts else ts[:8]
                except:
                    time_str = "—"
    
    # Symbol
    symbol = e.get("symbol", "")
    symbol_part = f" — {symbol}" if symbol else ""
    
    # Prima riga: icona TYPE — time — symbol
    main_line = f"{icon} {event_type} — {time_str}{symbol_part}"
    
    # Per STATUS e COMMAND_RECEIVED: output compatto
    if event_type in ["STATUS", "COMMAND_RECEIVED"]:
        lines = [main_line]
        command_id = e.get("command_id", "")
        if command_id:
            lines.append(f"  id: {command_id}")
        message = e.get("message", e.get("text", ""))
        if message:
            msg_truncated = message[:120] + "…" if len(message) > 120 else message
            lines.append(f"  msg: {msg_truncated}")
        return "\n".join(lines)
    
    # Per OPEN/CLOSE/FILL/TP/SL: output completo con tutti i campi presenti
    lines = [main_line]
    details = []
    
    # command_id (sempre se presente)
    command_id = e.get("command_id", "")
    if command_id:
        details.append(f"id: {command_id}")
    
    # Campi specifici per ORDER_OPENED
    if event_type == "ORDER_OPENED":
        side = e.get("side")
        if side:
            details.append(f"side: {side}")
        qty = e.get("qty")
        if qty is not None:
            details.append(f"qty: {qty}")
        leverage = e.get("leverage")
        if leverage is not None:
            details.append(f"leverage: {leverage}x")
        entry_price = e.get("entry_price")
        if entry_price is not None:
            details.append(f"entry: {entry_price:.2f}")
        take_profit = e.get("take_profit")
        if take_profit is not None:
            details.append(f"TP: {take_profit:.2f}")
        stop_loss = e.get("stop_loss")
        if stop_loss is not None:
            details.append(f"SL: {stop_loss:.2f}")
        tp_pct = e.get("tp_pct")
        if tp_pct is not None:
            details.append(f"TP%: {tp_pct*100:.2f}%")
        sl_pct = e.get("sl_pct")
        if sl_pct is not None:
            details.append(f"SL%: {sl_pct*100:.2f}%")
        error_message = e.get("error_message")
        if error_message:
            details.append(f"error: {error_message[:60]}")
    
    # Campi specifici per ORDER_CLOSED
    elif event_type == "ORDER_CLOSED":
        side = e.get("side")
        if side:
            details.append(f"side: {side}")
        size = e.get("size")
        if size is not None:
            details.append(f"size: {size}")
        entry_price = e.get("entry_price")
        if entry_price is not None:
            details.append(f"entry: {entry_price:.2f}")
        exit_price = e.get("exit_price")
        if exit_price is not None:
            details.append(f"exit: {exit_price:.2f}")
        pnl = e.get("pnl")
        if pnl is not None:
            pnl_sign = "+" if pnl >= 0 else ""
            details.append(f"PnL: {pnl_sign}{pnl:.2f}")
        pnl_pct = e.get("pnl_pct")
        if pnl_pct is not None:
            pnl_pct_sign = "+" if pnl_pct >= 0 else ""
            details.append(f"PnL%: {pnl_pct_sign}{pnl_pct:.2f}%")
        reason = e.get("reason")
        if reason:
            details.append(f"reason: {reason}")
        error_message = e.get("error_message")
        if error_message:
            details.append(f"error: {error_message[:60]}")
    
    # Campi per FILL/TP/SL (se presenti in futuro)
    elif event_type in ["FILL", "TAKE_PROFIT", "STOP_LOSS"]:
        side = e.get("side")
        if side:
            details.append(f"side: {side}")
        qty = e.get("qty") or e.get("size")
        if qty is not None:
            details.append(f"qty: {qty}")
        price = e.get("price") or e.get("exit_price") or e.get("entry_price")
        if price is not None:
            details.append(f"price: {price:.2f}")
        pnl = e.get("pnl")
        if pnl is not None:
            pnl_sign = "+" if pnl >= 0 else ""
            details.append(f"PnL: {pnl_sign}{pnl:.2f}")
        reason = e.get("reason")
        if reason:
            details.append(f"reason: {reason}")
    
    # Se ci sono dettagli, aggiungili
    if details:
        lines.append("  " + " — ".join(details))
    elif command_id:
        # Solo command_id senza altri dettagli
        lines.append(f"  id: {command_id}")
    
    # Messaggio generico (solo se non abbiamo già dettagli specifici)
    if not details or event_type in ["REPLY", "ERROR"]:
        message = e.get("message", e.get("text", e.get("content", "")))
        if message:
            msg_truncated = message[:120] + "…" if len(message) > 120 else message
            lines.append(f"  msg: {msg_truncated}")
    
    return "\n".join(lines)


def _extract_max_events_from_text(text: str, default: int = 20) -> int:
    """
    Estrae il numero di eventi richiesti dal testo del messaggio.
    Cerca pattern robusti come:
    - "mostrami gli ultimi 5 eventi"
    - "ultimi 10 eventi"
    - "fammi vedere 3 eventi"
    - "5 eventi"
    
    Args:
        text: Testo del messaggio
        default: Valore di default se non trovato (default 20)
    
    Returns:
        Numero di eventi richiesti (default 20, clamp 1..100)
    """
    if not text:
        return default
    
    text_lower = text.lower()
    
    # Gestione esplicita per "ultimo evento", "ultimo", "più recente" → ritorna 1
    # Controlla PRIMA delle regex per evitare match errati
    single_event_patterns = [
        r"l'ultimo\s+evento",
        r"l'ultimo\s*$",
        r"^l'ultimo\s",
        r"\bultimo\s+evento\b",
        r"\bultimo\s*$",
        r"^ultimo\s",
        r"\bpiù\s+recente\b",
        r"\bpiu\s+recente\b",
        r"\brecentissimo\b"
    ]
    for pattern in single_event_patterns:
        if re.search(pattern, text_lower):
            return 1
    
    # Normalizza numeri in lettere italiane → cifre (1-20)
    number_words = {
        'uno': '1', 'una': '1', 'un': '1',
        'due': '2',
        'tre': '3',
        'quattro': '4',
        'cinque': '5',
        'sei': '6',
        'sette': '7',
        'otto': '8',
        'nove': '9',
        'dieci': '10',
        'undici': '11',
        'dodici': '12',
        'tredici': '13',
        'quattordici': '14',
        'quindici': '15',
        'sedici': '16',
        'diciassette': '17',
        'diciotto': '18',
        'diciannove': '19',
        'venti': '20'
    }
    for word, digit in number_words.items():
        # Sostituisce solo se è una parola intera (con word boundary)
        text_lower = re.sub(r'\b' + word + r'\b', digit, text_lower)
    
    # Pattern 1: "ultimi N" o "ultimi N eventi" (più comune)
    match = re.search(r'ultimi\s+(\d+)', text_lower)
    if match:
        try:
            n = int(match.group(1))
            return max(1, min(100, n))
        except (ValueError, AttributeError):
            pass
    
    # Pattern 2: "fammi vedere N eventi" o "mostrami N eventi"
    match = re.search(r'(?:fammi\s+vedere|mostrami|vedi|mostra)\s+(\d+)\s+eventi', text_lower)
    if match:
        try:
            n = int(match.group(1))
            return max(1, min(100, n))
        except (ValueError, AttributeError):
            pass
    
    # Pattern 3: "N eventi" (standalone)
    match = re.search(r'(\d+)\s+eventi', text_lower)
    if match:
        try:
            n = int(match.group(1))
            return max(1, min(100, n))
        except (ValueError, AttributeError):
            pass
    
    # Pattern 4: "vedi N" o "mostra N" seguito da "eventi" in prossimità
    match = re.search(r'(?:vedi|mostra|mostrami)\s+(\d+)(?:\s+eventi|\s*$)', text_lower)
    if match:
        try:
            n = int(match.group(1))
            return max(1, min(100, n))
        except (ValueError, AttributeError):
            pass
    
    # TEST MANUALI RAPIDI (rimuovere dopo verifica):
    # assert _extract_max_events_from_text("mostrami l'ultimo evento") == 1
    # assert _extract_max_events_from_text("mostrami l'ultimo") == 1
    # assert _extract_max_events_from_text("mostrami gli ultimi 3 eventi") == 3
    # assert _extract_max_events_from_text("mostrami gli ultimi eventi") == 20
    
    return default


def _read_last_n_lines(file_path: Path, n: int) -> list[dict]:
    """
    Legge le ultime N righe non vuote da un file JSONL in modo efficiente.
    Usa seek dal fondo per file grandi.
    """
    events = []
    if not file_path.exists():
        return events
    
    try:
        # Strategia: leggi tutto se file piccolo (< 1MB), altrimenti seek dal fondo
        file_size = file_path.stat().st_size
        if file_size < 1024 * 1024:  # < 1MB: leggi tutto
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError:
                        continue
            return events[-n:] if len(events) > n else events
        else:
            # File grande: leggi dal fondo
            # Leggi a blocchi di 8192 bytes dal fondo
            with open(file_path, "rb") as f:
                f.seek(0, 2)  # Vai alla fine
                file_size = f.tell()
                chunk_size = 8192
                buffer = b""
                lines_found = []
                pos = file_size
                
                while pos > 0 and len(lines_found) < n * 2:  # Leggi un po' di più per sicurezza
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    buffer = chunk + buffer
                    
                    # Processa righe complete
                    while b"\n" in buffer:
                        line, buffer = buffer.rsplit(b"\n", 1)
                        if line.strip():
                            lines_found.append(line.decode("utf-8", errors="ignore"))
                
                # Processa eventuale resto
                if buffer.strip():
                    lines_found.append(buffer.decode("utf-8", errors="ignore"))
                
                # Inverti per avere ordine cronologico
                lines_found.reverse()
                
                # Parse JSON e prendi ultimi N
                for line in lines_found:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError:
                        continue
                
                return events[-n:] if len(events) > n else events
                
    except Exception as e:
        logger.error(f"[EVENTS] Error reading last N lines: {e}")
        # Fallback: leggi tutto
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        events.append(event)
                    except json.JSONDecodeError:
                        continue
            return events[-n:] if len(events) > n else events
        except:
            return []


def handle_local_events(
    filter_type: str | None = None,
    max_events: int = 20,
    user_id: str | None = None,
    chat_id: str | None = None,
) -> str:
    """
    Gestisce gli intent LOCAL relativi agli eventi leggendo dalla tabella runner_events.

    - Usa sempre il device_id del bot corrente/chat corrente.
    - Filtra SEMPRE per device_id.
    - Ordina per created_at DESC.
    - Supporta filtri opzionali su eventi positivi/negativi se filter_type è impostato.
    """
    # Risolvi il device_id partendo dalla chat, con fallback su runner_tokens
    device_id: Optional[str] = None

    if chat_id:
        try:
            device_id = get_chat_device_id(chat_id)
        except Exception as e:
            logger.error(f"[EVENTS] Error reading chat device_id for chat_id={chat_id}: {e}")

    if not device_id and user_id:
        try:
            device_id = _resolve_device_id_from_runner_tokens(user_id, chat_id or "")
        except Exception as e:
            logger.error(f"[EVENTS] Error resolving device_id from runner_tokens: user_id={user_id} chat_id={chat_id}: {e}")

    if not device_id:
        # Nessun device_id valido per questo bot/chat
        return "Nessun runner collegato a questo bot."

    # Determina limite di righe da leggere dalla tabella runner_events
    if max_events is None or max_events <= 0:
        max_events = 1

    # Per filtri positive/negative è utile leggere qualche evento in più
    base_limit = max_events
    if filter_type in ("positive", "negative"):
        base_limit = min(max_events * 2, 200)

    try:
        query = (
            supabase.table("runner_events")
            .select("type, payload, created_at")
            .eq("device_id", device_id)
            .order("created_at", desc=True)
            .limit(base_limit)
        )

        res = query.execute()
        events = res.data or []
    except Exception as e:
        logger.error(f"[EVENTS] Error fetching runner_events for device_id={device_id}: {e}")
        return "Errore durante il recupero degli eventi per questo bot."

    if not events:
        return "Nessun evento disponibile per questo bot."

    # Applica filtri opzionali su pnl se richiesto
    if filter_type in ("positive", "negative"):
        filtered: list[dict] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue

            payload = ev.get("payload") or {}
            pnl = None
            if isinstance(payload, dict):
                for field in ["pnl", "profit", "realized_pnl", "realized_profit"]:
                    if field in payload:
                        try:
                            pnl = float(payload[field])
                            break
                        except (ValueError, TypeError):
                            continue

            event_type = (ev.get("type") or "").upper()
            is_closed = any(closed_type in event_type for closed_type in ["CLOSED", "CLOSE"])

            if pnl is None or not is_closed:
                continue

            if filter_type == "positive" and pnl > 0:
                filtered.append(ev)
            elif filter_type == "negative" and pnl < 0:
                filtered.append(ev)

        events = filtered

    if not events:
        return "Nessun evento disponibile per questo bot."

    # Limita al numero richiesto di eventi (dopo eventuali filtri)
    events = events[:max_events]

    # Formattazione dell'output
    # Caso "ultimo evento" (max_events == 1) → formato dedicato
    if len(events) == 1:
        ev = events[0]
        ev_type = ev.get("type", "UNKNOWN")
        payload = ev.get("payload") or {}
        symbol = None
        if isinstance(payload, dict):
            symbol = payload.get("symbol")

        created_at = ev.get("created_at")
        ts_str = str(created_at) if created_at is not None else ""
        if isinstance(created_at, str):
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                # Usa la stringa originale se il parsing fallisce
                ts_str = created_at

        if symbol:
            header_line = f"• {ev_type} — {symbol}"
        else:
            header_line = f"• {ev_type}"

        lines = [
            "Ultimo evento:",
            header_line,
            f"• {ts_str}" if ts_str else "",
        ]
        # Rimuovi eventuali righe vuote in coda
        lines = [ln for ln in lines if ln]
        return "\n".join(lines)

    # Caso "mostra eventi" → elenco semplice
    lines: list[str] = []
    if filter_type == "positive":
        lines.append(f"Ecco gli ultimi {len(events)} eventi positivi:")
    elif filter_type == "negative":
        lines.append(f"Ecco gli ultimi {len(events)} eventi negativi:")
    else:
        lines.append(f"Ecco gli ultimi {len(events)} eventi:")

    for ev in events:
        ev_type = ev.get("type", "UNKNOWN")
        payload = ev.get("payload") or {}
        symbol = None
        if isinstance(payload, dict):
            symbol = payload.get("symbol")

        if symbol:
            line = f"• {ev_type} — {symbol}"
        else:
            line = f"• {ev_type}"

        lines.append(line)

    return "\n".join(lines)


ORDER_SHOW_EVENT_TYPES = ["ORDER_OPEN", "ORDER_CLOSE"]


def _fetch_order_events_for_device(device_id: str, limit: int) -> list[dict]:
    """Ultimi N eventi runner_events con type ORDER_OPEN / ORDER_CLOSE (created_at DESC)."""
    lim = max(1, min(100, int(limit)))
    res = (
        supabase.table("runner_events")
        .select("type, payload, created_at")
        .eq("device_id", device_id)
        .in_("type", ORDER_SHOW_EVENT_TYPES)
        .order("created_at", desc=True)
        .limit(lim)
        .execute()
    )
    return res.data or []


def _side_to_long_short(side_raw: str) -> str:
    s = (side_raw or "").strip().upper()
    if s in ("BUY", "LONG", "B"):
        return "LONG"
    if s in ("SELL", "SHORT", "S"):
        return "SHORT"
    return s


def _pick_payload_price(payload: dict, keys: list[str]) -> str:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return str(payload.get(key)).strip()
    return ""


def _extract_pnl_from_payload(payload: dict) -> float | None:
    for field in ("pnl", "profit", "realized_pnl", "realized_profit"):
        if field in payload and payload[field] is not None:
            try:
                return float(payload[field])
            except (TypeError, ValueError):
                continue
    return None


def _fmt_signed_pnl_compact(pnl: float) -> str:
    if pnl == 0:
        return "0"
    rounded = round(pnl, 2)
    if abs(rounded - int(rounded)) < 1e-9:
        n = int(rounded)
        sign = "+" if n > 0 else ""
        return f"{sign}{n}"
    sign = "+" if rounded > 0 else ""
    return f"{sign}{rounded}"


def _close_reason_to_tipo(payload: dict) -> str:
    cr = str(payload.get("close_reason") or "").strip().lower()
    if cr in ("tp_hit", "tp", "take_profit"):
        return "TP"
    if cr in ("sl_hit", "sl", "stop_loss"):
        return "SL"
    if cr in ("manual", "manuale"):
        return "manuale"
    return "manuale"


def _format_compact_order_events(events: list[dict]) -> str:
    """Output user-facing: solo righe trading, niente JSON."""
    if not events:
        return "Non ci sono operazioni recenti."
    out_lines: list[str] = ["Ultimi eventi", ""]
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ev_type = str(ev.get("type") or "").upper()
        payload_raw = ev.get("payload") or {}
        payload = payload_raw if isinstance(payload_raw, dict) else {}
        symbol = str(payload.get("symbol") or "").strip() or "—"

        if ev_type == "ORDER_OPEN":
            side_ls = _side_to_long_short(str(payload.get("side") or ""))
            entry = _pick_payload_price(payload, ["entry_price", "price", "open_price"])
            if side_ls:
                line = f"- 🟢 Aperto {side_ls} su {symbol}"
            else:
                line = f"- 🟢 Aperto su {symbol}"
            if entry:
                line = f"{line} a {entry}"
            out_lines.append(line)
        elif ev_type == "ORDER_CLOSE":
            tipo = _close_reason_to_tipo(payload)
            pnl = _extract_pnl_from_payload(payload)
            if pnl is not None:
                ps = _fmt_signed_pnl_compact(pnl)
                out_lines.append(f"- 🔴 Chiuso {symbol} {ps} USDT ({tipo})")
            else:
                out_lines.append(f"- 🔴 Chiuso {symbol} ({tipo})")
    if len(out_lines) <= 2:
        return "Non ci sono operazioni recenti."
    return "\n".join(out_lines)


def _handle_show_events_command(
    user_id: str | None = None,
    chat_id: str | None = None,
    limit: int = 20,
) -> str:
    """
    Gestisce SOLO il comando "mostra eventi" / "mostrami gli eventi".
    Mostra ORDER_OPEN / ORDER_CLOSE per il device_id della chat corrente.
    """
    device_id: Optional[str] = None

    if chat_id:
        try:
            device_id = get_chat_device_id(chat_id)
        except Exception as e:
            logger.error(f"[EVENTS_SHOW] Error reading chat device_id for chat_id={chat_id}: {e}")

    if not device_id and user_id:
        try:
            device_id = _resolve_device_id_from_runner_tokens(user_id, chat_id or "")
        except Exception as e:
            logger.error(f"[EVENTS_SHOW] Error resolving device_id from runner_tokens: user_id={user_id} chat_id={chat_id}: {e}")

    if not device_id:
        logger.warning(
            "[EVENTS_SHOW] nessun device_id risolvibile | chat_id=%s user_id=%s "
            "(nessun mapping chat e nessun runner_tokens attivo con device_id)",
            chat_id,
            user_id,
        )
        return "Nessun evento disponibile per questo bot."

    try:
        events = _fetch_order_events_for_device(device_id, limit)
    except Exception as e:
        logger.error(f"[EVENTS_SHOW] Error fetching runner_events for device_id={device_id}: {e}")
        return "Nessun evento disponibile per questo bot."

    logger.info(
        "[EVENTS_SHOW] chat_id=%s user_id=%s device_id=%s requested_types=%s row_count=%s limit=%s",
        chat_id,
        user_id,
        device_id,
        ORDER_SHOW_EVENT_TYPES,
        len(events),
        limit,
    )

    if not events:
        logger.info(
            "[EVENTS_SHOW] nessuna riga runner_events per device_id=%s tipi=%s",
            device_id,
            ORDER_SHOW_EVENT_TYPES,
        )
        return "Non ci sono operazioni recenti."

    return _format_compact_order_events(events)


def _handle_show_events_all_order_only(
    user_id: str | None,
    chat_id: str | None,
    max_events: int,
) -> str:
    """SHOW_EVENTS_ALL: stessi tipi di _handle_show_events_command, stessa risoluzione device di handle_local_events."""
    device_id: Optional[str] = None

    if chat_id:
        try:
            device_id = get_chat_device_id(chat_id)
        except Exception as e:
            logger.error(f"[EVENTS] Error reading chat device_id for chat_id={chat_id}: {e}")

    if not device_id and user_id:
        try:
            device_id = _resolve_device_id_from_runner_tokens(user_id, chat_id or "")
        except Exception as e:
            logger.error(
                f"[EVENTS] Error resolving device_id from runner_tokens: user_id={user_id} chat_id={chat_id}: {e}"
            )

    if not device_id:
        return "Nessun runner collegato a questo bot."

    lim = max_events if max_events and max_events > 0 else 20
    try:
        events = _fetch_order_events_for_device(device_id, lim)
    except Exception as e:
        logger.error(f"[EVENTS] Error fetching runner_events for device_id={device_id}: {e}")
        return "Errore durante il recupero degli eventi per questo bot."

    return _format_compact_order_events(events)


# ----------------------------------------
# RUNNER ONLINE CHECK
# ----------------------------------------
RUNNER_ONLINE_THRESHOLD_SECONDS = 45  # Costante configurabile

def _get_commands_file_path() -> Path:
    """
    Determina il path di commands.jsonl.
    Usa la stessa logica del runner.py: AppData Local / IdithRunner / memory / queue / commands.jsonl
    """
    app_name = "IdithRunner"
    base_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / app_name
    queue_dir = base_dir / "memory" / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    return queue_dir / "commands.jsonl"

def _resolve_device_id_from_runner_tokens(user_id: str, chat_id: str) -> Optional[str]:
    """
    Risolve automaticamente un device_id valido dalla tabella runner_tokens.
    Cerca il record più recente per user_id con is_active=true, ordinato per last_seen_at desc.
    Usa SOLO la colonna device_id (TEXT) - NON usa più runner_id o runner_name come fallback.
    
    Se device_id è NULL o mancante, ritorna None (il chiamante deve gestire l'errore).
    chat_id è usato solo per logging.
    """
    if not user_id:
        logger.warning(
            "[DEVICE_RESOLVE] impossibile risolvere device_id: user_id mancante o vuoto | chat_id=%s",
            chat_id,
        )
        return None
    
    try:
        # Cerca il record più recente per questo user_id con is_active=true
        res = (
            supabase.table("runner_tokens")
            .select("device_id, is_active, last_seen_at, token")
            .eq("user_id", user_id)
            .eq("is_active", True)  # Solo runner attivi
            .order("last_seen_at", desc=True)
            .limit(1)
            .execute()
        )
        
        records = res.data or []
        
        if records:
            record = records[0]
            device_id = record.get("device_id")
            raw_token = record.get("token")
            token_prefix = (
                (raw_token[:8] + "…") if isinstance(raw_token, str) and len(raw_token) > 8 else (raw_token or "<missing>")
            )
            
            # Verifica che device_id sia presente e non vuoto
            if device_id and device_id.strip():
                resolved = device_id.strip()
                logger.info(
                    "[DEVICE_RESOLVE] ok | user_id=%s chat_id=%s token_prefix=%s device_id=%s "
                    "(record is_active=%s last_seen_at=%s)",
                    user_id,
                    chat_id,
                    token_prefix,
                    resolved,
                    record.get("is_active"),
                    record.get("last_seen_at"),
                )
                return resolved
            else:
                logger.warning(
                    "[DEVICE_RESOLVE] token trovato ma device_id assente o vuoto | user_id=%s chat_id=%s "
                    "token_prefix=%s motivo=device_id_null_or_blank",
                    user_id,
                    chat_id,
                    token_prefix,
                )
                return None
        
        logger.warning(
            "[DEVICE_RESOLVE] nessun record runner_tokens con is_active=true per user_id=%s chat_id=%s "
            "motivo=no_active_row",
            user_id,
            chat_id,
        )
        return None
        
    except Exception as e:
        logger.error(f"[DEVICE_RESOLVE] Error resolving device_id from runner_tokens for user_id={user_id}, chat_id={chat_id}: {e}", exc_info=True)
        return None

def _enqueue_command_to_runner(
    normalized_command: str,
    chat_id: str,
    user_id: str,
    original_text: str
) -> tuple[bool, str]:
    """
    Invia un comando al runner via Supabase.
    Ritorna (success: bool, command_id: str)
    """
    if not supabase_queue:
        error_msg = (
            "Modulo supabase_queue non caricato nel server: impossibile accodare il comando al runner. "
            "Cerca nei log [SUPABASE_QUEUE] import supabase_queue FAILED (all attempts exhausted) con traceback."
        )
        logger.error(
            "[CHAT] supabase_queue module not available — enqueue skipped. detail=%s",
            error_msg,
        )
        return (False, error_msg)
    
    try:
        # Ottieni device_id dalla chat
        device_id = get_chat_device_id(chat_id)
        
        # Se device_id non è presente, prova a risolverlo automaticamente da runner_tokens
        if not device_id:
            logger.info(f"[CHAT] No device_id set for chat_id={chat_id}, attempting to resolve from runner_tokens for user_id={user_id}")
            device_id = _resolve_device_id_from_runner_tokens(user_id, chat_id)
            
            if device_id:
                # Aggiorna la chat con il device_id trovato
                set_chat_device_id(chat_id, device_id)
                logger.info(f"[CHAT] Auto-resolved and set device_id={device_id} for chat_id={chat_id} from runner_tokens")
            else:
                # Non è stato possibile risolvere device_id
                error_msg = "Runner non collegato. Premi 'Collega runner'."
                logger.warning(f"[CHAT] {error_msg} - nessun runner token valido con device_id trovato per chat_id={chat_id}, user_id={user_id}")
                return (False, error_msg)
        
        # Prova a parsare il comando trade per estrarre action/symbol/qty
        action = None
        symbol = None
        qty = None
        
        if normalized_command.startswith("/trade"):
            parts = normalized_command.split()
            if len(parts) >= 2:
                cmd = parts[1].lower()
                if cmd == "status":
                    # /trade status viene gestito separatamente, non va in coda
                    action = "STATUS"
                    symbol = parts[2].upper() if len(parts) >= 3 else "ETHUSDT"
                elif cmd in ("open_long", "open_short", "close", "close_all"):
                    if len(parts) >= 3:
                        action_map = {
                            "open_long": "TRADE_OPEN",
                            "open_short": "TRADE_OPEN",
                            "close": "CLOSE",
                            "close_all": "CLOSE_ALL",
                        }
                        action = action_map.get(cmd)
                        symbol = parts[2].upper()
                        if len(parts) >= 4:
                            try:
                                qty = float(parts[3])
                            except Exception:
                                qty = None
        
        # Crea payload per Supabase
        payload = {
            "text": normalized_command,
            "chat_id": chat_id,
            "source": "backend"
        }
        
        # Se è un comando trade valido, aggiungi action/symbol/qty
        if action:
            payload["action"] = action
        if symbol:
            payload["symbol"] = symbol
            # Per open_long/open_short, aggiungi side
            if action == "TRADE_OPEN":
                if "open_long" in normalized_command.lower():
                    payload["side"] = "LONG"
                elif "open_short" in normalized_command.lower():
                    payload["side"] = "SHORT"
        if qty is not None:
            payload["qty"] = qty
        
        # Valida device_id prima di inviare
        if not device_id or not device_id.strip():
            error_msg = "Runner non collegato: device_id mancante. Premi 'Collega runner'."
            logger.error(f"[CHAT] {error_msg} - device_id vuoto per chat_id={chat_id}, user_id={user_id}")
            return (False, error_msg)
        
        # Invia a Supabase
        try:
            command_id = supabase_queue.enqueue_runner_command(
                device_id, payload, user_id=user_id
            )
            
            logger.info(f"Enqueued command_id={command_id} device_id={device_id}")
            logger.info(
                f"[CHAT] Enqueued command to Supabase: command_id={command_id} "
                f"device_id={device_id} normalized={normalized_command!r} "
                f"original={original_text!r}"
            )
            
            return (True, command_id)
        except ValueError as ve:
            # Errore di validazione (es: device_id vuoto)
            error_msg = f"Errore validazione comando: {str(ve)}"
            logger.error(f"[CHAT] {error_msg} - device_id={device_id}, chat_id={chat_id}")
            return (False, error_msg)
        except RuntimeError as re:
            # Errore Supabase/PostgREST
            error_msg = f"Errore nell'invio del comando al runner: {str(re)}"
            logger.error(f"[CHAT] {error_msg} - device_id={device_id}, chat_id={chat_id}, error={re}", exc_info=True)
            return (False, error_msg)
        
    except Exception as e:
        error_msg = f"Errore nell'invio del comando al runner: {str(e)}"
        logger.error(f"[CHAT] {error_msg} - chat_id={chat_id}, user_id={user_id}", exc_info=True)
        return (False, error_msg)

def get_runner_online_for_user(user_id: str) -> tuple[bool, float | None, str | None]:
    """
    Verifica se il runner è online per l'utente specificato.
    
    FUNZIONE STATELESS E DETERMINISTICA:
    - NON usa alcuno stato in memoria
    - NON usa cache
    - NON usa informazioni della chat
    - NON usa pairing code
    - fa SEMPRE una query DIRETTA a Supabase sulla tabella public.runner_tokens
    
    Il runner è ONLINE solo se:
    - Esiste un record in runner_tokens con user_id == user_id
    - is_active == True
    - last_seen_at NON nullo
    - e (now_utc - last_seen_at_utc) <= RUNNER_ONLINE_THRESHOLD_SECONDS
    
    Ritorna (is_online: bool, seconds_ago: float | None, last_seen_at: str | None)
    - is_online: True se (now - last_seen_at) <= THRESHOLD_SECONDS
    - seconds_ago: differenza in secondi tra now e last_seen_at (None se non disponibile)
    - last_seen_at: timestamp ISO dell'ultimo heartbeat (None se non disponibile)
    
    Se non esiste record o last_seen_at è null → ritorna (False, None, None)
    Se parsing data fallisce → ritorna (False, None, last_seen_at_str)
    """
    if not user_id:
        return (False, None, None)
    
    try:
        # Query DIRETTA a Supabase: cerca il record più recente per questo user_id
        # Filtra per is_active=True e esclude record con last_seen_at nullo
        # Ordina per last_seen_at DESC (nulls last) e prendi SOLO il primo record
        res = (
            supabase.table("runner_tokens")
            .select("id, device_id, last_seen_at")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .not_.is_("last_seen_at", "null")
            .order("last_seen_at", desc=True)
            .limit(1)
            .execute()
        )
        
        records = res.data or []
        
        # Se non esiste record → return False
        if not records:
            now_utc = datetime.now(timezone.utc)
            logger.debug(
                f"[RUNNER_ONLINE] user_id={user_id} token_id=None device_id=None "
                f"last_seen_at=None now={now_utc.isoformat()} delta=None "
                f"threshold={RUNNER_ONLINE_THRESHOLD_SECONDS} online=False"
            )
            return (False, None, None)
        
        record = records[0]
        token_id = record.get("id")
        device_id = record.get("device_id")
        last_seen_at_str = record.get("last_seen_at")
        
        # Se last_seen_at è null (controllo di sicurezza)
        if not last_seen_at_str:
            now_utc = datetime.now(timezone.utc)
            logger.debug(
                f"[RUNNER_ONLINE] user_id={user_id} token_id={token_id} device_id={device_id} "
                f"last_seen_at=None now={now_utc.isoformat()} delta=None "
                f"threshold={RUNNER_ONLINE_THRESHOLD_SECONDS} online=False"
            )
            return (False, None, None)
        
        # Parsing robusto last_seen_at: supporta stringhe ISO con timezone
        try:
            # Normalizza 'Z' a '+00:00' per compatibilità
            normalized_str = last_seen_at_str.replace('Z', '+00:00') if isinstance(last_seen_at_str, str) else str(last_seen_at_str)
            last_seen_at = datetime.fromisoformat(normalized_str)
        except (ValueError, TypeError, AttributeError) as e:
            now_utc = datetime.now(timezone.utc)
            logger.warning(
                f"[RUNNER_ONLINE] user_id={user_id} token_id={token_id} device_id={device_id} "
                f"last_seen_at={last_seen_at_str} now={now_utc.isoformat()} delta=None "
                f"threshold={RUNNER_ONLINE_THRESHOLD_SECONDS} online=False "
                f"(invalid format: {e})"
            )
            return (False, None, last_seen_at_str)
        
        # Converti sempre a datetime aware in UTC
        # Se last_seen_at risultasse naive, assumilo UTC
        if last_seen_at.tzinfo is None:
            last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
        else:
            # Se ha timezone, converti a UTC
            last_seen_at = last_seen_at.astimezone(timezone.utc)
        
        # Usa now = datetime.now(timezone.utc)
        now_utc = datetime.now(timezone.utc)
        
        # Calcolo delta
        delta_seconds = (now_utc - last_seen_at).total_seconds()
        
        # Se delta_seconds < 0 (clock skew) logga warning e usa abs(delta_seconds)
        if delta_seconds < 0:
            logger.warning(f"[RUNNER_ONLINE] user_id={user_id} token_id={token_id} -> clock skew detected: delta_seconds={delta_seconds:.1f} (negative), using abs")
            delta_seconds = abs(delta_seconds)
        
        # Return: online = delta_seconds <= RUNNER_ONLINE_THRESHOLD_SECONDS
        online = delta_seconds <= RUNNER_ONLINE_THRESHOLD_SECONDS
        
        # Log DEBUG obbligatorio con formato specifico
        logger.debug(
            f"[RUNNER_ONLINE] user_id={user_id} token_id={token_id} device_id={device_id} "
            f"last_seen_at={last_seen_at_str} now={now_utc.isoformat()} "
            f"delta={delta_seconds:.1f} threshold={RUNNER_ONLINE_THRESHOLD_SECONDS} "
            f"online={online}"
        )
        
        return (online, delta_seconds, last_seen_at_str)
        
    except Exception as e:
        now_utc = datetime.now(timezone.utc)
        logger.error(
            f"[RUNNER_ONLINE] user_id={user_id} -> error checking runner status: {e}",
            exc_info=True
        )
        # In caso di errore, considera offline (mai True di default)
        logger.debug(
            f"[RUNNER_ONLINE] user_id={user_id} token_id=None device_id=None "
            f"last_seen_at=None now={now_utc.isoformat()} delta=None "
            f"threshold={RUNNER_ONLINE_THRESHOLD_SECONDS} online=False (error)"
        )
        return (False, None, None)


def _insert_user_message(chat_id: str, user_id: str, content: str) -> tuple[bool, str | None, Exception | None]:
    """
    Inserisce il messaggio utente in public.messages.
    Ritorna (success: bool, message_id: str | None, error: Exception | None)
    """
    try:
        insert_data = {
            "chat_id": chat_id,
            "user_id": user_id,
            "role": "user",
            "content": content,
            "created_at": now_iso(),
        }
        
        res = supabase.table("messages").insert(insert_data).execute()
        
        if not res.data or len(res.data) == 0:
            error_msg = "INSERT user message returned empty data"
            logger.error(f"[CHAT] {error_msg}: chat_id={chat_id}, user_id={user_id}")
            return (False, None, RuntimeError(error_msg))
        
        message_id = res.data[0].get("id")
        logger.info(f"[CHAT] INSERT user message OK: chat_id={chat_id}, user_id={user_id}, message_id={message_id}")
        return (True, message_id, None)
        
    except Exception as e:
        error_details = str(e)
        logger.error(f"[CHAT] INSERT user message FAILED: chat_id={chat_id}, user_id={user_id}, error={error_details}")
        return (False, None, e)


def _insert_assistant_message(chat_id: str, user_id: str, content: str) -> tuple[bool, str | None, Exception | None]:
    """
    Inserisce il messaggio assistant in public.messages.
    Ritorna (success: bool, message_id: str | None, error: Exception | None)
    """
    try:
        insert_data = {
            "chat_id": chat_id,
            "user_id": user_id,
            "role": "assistant",
            "content": content,
            "created_at": now_iso(),
        }
        
        res = supabase.table("messages").insert(insert_data).execute()
        
        if not res.data or len(res.data) == 0:
            error_msg = "INSERT assistant message returned empty data"
            logger.error(f"[CHAT] {error_msg}: chat_id={chat_id}, user_id={user_id}")
            return (False, None, RuntimeError(error_msg))
        
        message_id = res.data[0].get("id")
        logger.info(f"[CHAT] INSERT assistant message OK: chat_id={chat_id}, user_id={user_id}, message_id={message_id}")
        return (True, message_id, None)
        
    except Exception as e:
        error_details = str(e)
        logger.error(f"[CHAT] INSERT assistant message FAILED: chat_id={chat_id}, user_id={user_id}, error={error_details}")
        return (False, None, e)


# ----------------------------------------
# RUNNER COMMANDS HANDLERS (Supabase)
# ----------------------------------------

def _handle_pair_command(chat_id: str, parts: list) -> str:
    """
    Gestisce il comando /pair <device_id>.
    Salva device_id nella sessione chat.
    """
    if len(parts) < 2:
        return "❌ Uso: /pair <device_id>\nEsempio: /pair pc-principale"
    
    device_id = parts[1].strip()
    if not device_id:
        return "❌ device_id non può essere vuoto"
    
    set_chat_device_id(chat_id, device_id)
    return f"✅ Device ID impostato: {device_id}"


def _handle_runner_ping(chat_id: str, user_id: str) -> str:
    """
    Gestisce il comando /runner ping.
    Invia un comando di test al runner via Supabase.
    """
    if not supabase_queue:
        return "❌ Modulo supabase_queue non disponibile"
    
    device_id = get_chat_device_id(chat_id)
    if not device_id:
        return "❌ Prima fai /pair <device_id>"
    
    try:
        payload = {"action": "TEST", "data": "ping"}
        command_id = supabase_queue.enqueue_runner_command(
            device_id, payload, user_id=user_id
        )
        logger.info(f"[CHAT] /runner ping command enqueued: command_id={command_id}, device_id={device_id}")
        return f"✅ OK: command queued {command_id}"
    except Exception as e:
        logger.error(f"[CHAT] Error in /runner ping: {e}", exc_info=True)
        return f"❌ Errore: {str(e)}"


def _handle_runner_events(chat_id: str, limit: int = 20) -> str:
    """
    Gestisce il comando /runner events.
    Mostra gli ultimi eventi dal runner via Supabase.
    """
    if not supabase_queue:
        return "❌ Modulo supabase_queue non disponibile"
    
    device_id = get_chat_device_id(chat_id)
    if not device_id:
        return "❌ Prima fai /pair <device_id>"
    
    try:
        events = supabase_queue.list_runner_events(device_id, limit=limit)
        
        if not events:
            return f"📭 Nessun evento trovato per device_id={device_id}"
        
        lines = [f"📊 Ultimi {len(events)} eventi per device_id={device_id}:\n"]
        
        for event in events:
            event_type = event.get("type", "UNKNOWN")
            created_at = event.get("created_at", "N/A")
            payload = event.get("payload", {})
            command_id = event.get("command_id")
            
            # Formatta timestamp
            try:
                dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                ts_str = created_at
            
            line = f"• [{ts_str}] {event_type}"
            if command_id:
                line += f" (cmd: {command_id[:8]}...)"
            if payload:
                payload_str = json.dumps(payload, ensure_ascii=False)[:100]
                if len(json.dumps(payload, ensure_ascii=False)) > 100:
                    payload_str += "..."
                line += f" - {payload_str}"
            lines.append(line)
        
        return "\n".join(lines)
        
    except Exception as e:
        logger.error(f"[CHAT] Error in /runner events: {e}", exc_info=True)
        return f"❌ Errore: {str(e)}"


def _handle_trade_status(chat_id: str, user_id: str) -> str:
    """
    Gestisce il comando /trade status.
    Invia un comando STATUS al runner via Supabase, poi mostra gli ultimi eventi.
    REGOLA: Se l'enqueue fallisce, mostra errore e NON continua.
    """
    if not supabase_queue:
        return "❌ Modulo supabase_queue non disponibile"
    
    device_id = get_chat_device_id(chat_id)
    if not device_id:
        return "❌ Prima fai /pair <device_id>"
    
    try:
        # PRIMA: inserisci il comando in Supabase (OBBLIGATORIO)
        payload = {"action": "STATUS", "text": "/trade status", "chat_id": chat_id, "source": "backend"}
        command_id = supabase_queue.enqueue_runner_command(
            device_id, payload, user_id=user_id
        )
        logger.info(f"[CHAT] /trade status command enqueued: command_id={command_id}, device_id={device_id}")
        
        # POI: leggi gli eventi (solo se enqueue riuscito)
        try:
            events = supabase_queue.list_runner_events(device_id, limit=10)
            
            if not events:
                return f"📭 Nessun evento trovato per device_id={device_id}"
            
            lines = [f"📊 Status (ultimi 10 eventi) per device_id={device_id}:\n"]
            
            for event in events:
                event_type = event.get("type", "UNKNOWN")
                created_at = event.get("created_at", "N/A")
                payload = event.get("payload", {})
                
                # Formatta timestamp
                try:
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except:
                    ts_str = created_at
                
                line = f"• [{ts_str}] {event_type}"
                if payload:
                    # Mostra info rilevanti dal payload
                    if isinstance(payload, dict):
                        info_parts = []
                        if "symbol" in payload:
                            info_parts.append(f"symbol={payload['symbol']}")
                        if "action" in payload:
                            info_parts.append(f"action={payload['action']}")
                        if "side" in payload:
                            info_parts.append(f"side={payload['side']}")
                        if "qty" in payload:
                            info_parts.append(f"qty={payload['qty']}")
                        if info_parts:
                            line += f" ({', '.join(info_parts)})"
                
                lines.append(line)
            
            return "\n".join(lines)
        except Exception as e:
            # Errore nella lettura eventi (ma enqueue riuscito)
            logger.warning(f"[CHAT] Error reading events in /trade status: {e}")
            return f"✅ Comando inviato (command_id={command_id}), ma errore nella lettura eventi: {str(e)}"
        
    except Exception as e:
        # Errore nell'enqueue: mostra errore e NON continua
        logger.error(f"[CHAT] Error in /trade status enqueue: {e}", exc_info=True)
        return f"❌ Errore nell'invio del comando: {str(e)}"


def _get_or_create_smoke_user() -> str:
    """
    Ottiene o crea l'utente smoke via Supabase Auth Admin API.
    Email: smoke@idith.local, password: SmokeTest123!
    Usa SMOKE_USER_ID se presente (backward compatibility), altrimenti
    cerca/crea via API.
    """
    smoke_user_id = os.getenv("SMOKE_USER_ID", "").strip()
    if smoke_user_id:
        return smoke_user_id

    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
    key = key.strip()
    if not url or not key:
        raise HTTPException(
            status_code=500,
            detail="SUPABASE_URL and SUPABASE_SERVICE_KEY required for auto smoke user",
        )

    auth_base = f"{url}/auth/v1"
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    smoke_email = "smoke@idith.local"
    smoke_password = "SmokeTest123!"

    # Cerca utente esistente (pagina utenti)
    page = 1
    per_page = 100
    while True:
        r = requests.get(
            f"{auth_base}/admin/users",
            headers=headers,
            params={"page": page, "per_page": per_page},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"[SMOKE] list users failed: {r.status_code} {r.text[:200]}")
            break
        data = r.json()
        users = data.get("users") or []
        for u in users:
            if (u.get("email") or "").lower() == smoke_email:
                uid = u.get("id")
                if uid:
                    logger.info(f"[SMOKE] Found existing smoke user: {uid}")
                    return str(uid)
        if len(users) < per_page:
            break
        page += 1

    # Crea nuovo utente
    r = requests.post(
        f"{auth_base}/admin/users",
        headers=headers,
        json={
            "email": smoke_email,
            "password": smoke_password,
            "email_confirm": True,
        },
        timeout=10,
    )
    if r.status_code not in (200, 201):
        logger.error(f"[SMOKE] create user failed: {r.status_code} {r.text[:300]}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create smoke user: {r.status_code}",
        )
    created = r.json()
    uid = created.get("id") or (created.get("user", {}) or {}).get("id")
    if not uid:
        raise HTTPException(status_code=500, detail="Smoke user created but no id returned")
    logger.info(f"[SMOKE] Created smoke user: {uid}")
    return str(uid)


@app.post("/api/chat_smoke")
def chat_smoke(payload: ChatPayload, request: Request):
    """
    Endpoint di smoke test locale che riusa la stessa logica di /api/chat
    ma senza richiedere autenticazione.
    Abilitato solo per richieste locali o in ENV local/dev.
    Se SMOKE_USER_ID non è in env, ottiene/crea automaticamente l'utente smoke.
    """
    client_host = request.client.host if request.client else None
    is_local_host = client_host == "127.0.0.1"
    is_local_env = APP_ENV in ("local", "dev")

    if not (is_local_host or is_local_env):
        # In ambienti non locali nascondiamo l'endpoint
        raise HTTPException(status_code=404, detail="Not found")

    smoke_user_id = _get_or_create_smoke_user()
    smoke_user = {
        "id": smoke_user_id,
        "email": "smoke@idith.local",
    }
    
    # Per gli smoke test vogliamo sempre partire da una configurazione "pulita"
    # sulla chat unica dell'utente smoke. Se chat_id non è fornito (primo messaggio),
    # recupera/crea la chat e resetta config_state allo scheletro.
    if not (payload.chat_id or "").strip():
        chat_row = get_or_create_chat(smoke_user_id, "Smoke chat")
        chat_id = chat_row.get("id")
        if chat_id:
            try:
                reset_config_state(chat_id, smoke_user_id)
            except Exception as e:
                logger.error(f"[SMOKE] reset_config_state failed for chat_id={chat_id}: {e}")
            # Forza l'uso di questa chat per l'intero smoke
            payload.chat_id = chat_id
    
    return chat(payload=payload, user=smoke_user)


@app.post("/api/chat")
def chat(payload: ChatPayload, user=Depends(get_current_user)):
    """
    Endpoint POST /chat: gestisce messaggi utente e genera risposte.
    
    FLUSSO TRANSACTIONALE:
    1. Verifica/crea chat se chat_id mancante o vuoto
    2. Salva SEMPRE messaggio user in public.messages PRIMA di qualsiasi altra logica
    3. Se fallisce INSERT user, ritorna HTTP 500/403 con ok:false
    4. Genera risposta (slash command o LLM)
    5. Salva messaggio assistant
    6. Ritorna JSON completo con tutti gli ID
    """
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # Log iniziale
    logger.info(f"[CHAT] POST /chat: chat_id={payload.chat_id}, user_id={user_id}, message_len={len(payload.message)}")
    
    # STEP 1: Verifica/crea chat
    chat_id = payload.chat_id
    if not chat_id or not chat_id.strip():
        # Crea nuova chat se chat_id mancante o vuoto
        logger.info(f"[CHAT] chat_id vuoto, creando nuova chat per user_id={user_id}")
        try:
            new_chat = get_or_create_chat(user_id, "Nuova chat")
            chat_id = new_chat["id"]
            logger.info(f"[CHAT] Chat creata: chat_id={chat_id}")
        except Exception as e:
            error_details = str(e)
            logger.error(f"[CHAT] Errore creazione chat: user_id={user_id}, error={error_details}")
            raise HTTPException(status_code=500, detail=f"Errore creazione chat: {error_details}")
    else:
        # Verifica che la chat appartenga all'utente
        chat_res = (
            supabase.table("chats")
            .select("id")
            .eq("id", chat_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not chat_res.data:
            logger.warning(f"[CHAT] Chat non trovata o non appartiene all'utente: chat_id={chat_id}, user_id={user_id}")
            raise HTTPException(status_code=404, detail="Chat not found")
    
    # STEP 2: Salva SEMPRE messaggio user PRIMA di qualsiasi altra logica
    user_message_content = payload.message.strip()
    if not user_message_content:
        raise HTTPException(status_code=400, detail="Message content cannot be empty")
    
    insert_user_ok, user_message_id, user_insert_error = _insert_user_message(
        chat_id=chat_id,
        user_id=user_id,
        content=user_message_content
    )
    
    # Se fallisce INSERT user, ritorna errore HTTP - MAI ok:true se non inserito
    if not insert_user_ok:
        error_details = str(user_insert_error) if user_insert_error else "Unknown error"
        logger.error(f"[CHAT] INSERT user FAILED - returning error: chat_id={chat_id}, user_id={user_id}, error={error_details}")
        
        # Determina status code: 403 se RLS, 500 per altri errori
        error_str = error_details.lower()
        if "rls" in error_str or "policy" in error_str or "permission" in error_str or "forbidden" in error_str:
            status_code = 403
        else:
            status_code = 500
        
        raise HTTPException(
            status_code=status_code,
            detail=f"Errore salvataggio messaggio utente: {error_details}"
        )
    
    # Aggiorna chat.updated_at
    try:
        supabase.table("chats").update({"updated_at": now_iso()}).eq("id", chat_id).execute()
    except Exception as e:
        logger.warning(f"[CHAT] Errore aggiornamento updated_at: {e}")
    
    # STEP 2.3: Intercetta comando RESET (PRIMA di qualsiasi altra logica)
    if is_reset_command(user_message_content):
        logger.info(f"[RESET] chat_id={chat_id} invoked")
        
        # Reset config_state per questa chat
        try:
            reset_config_state(chat_id, user_id)
        except Exception as e:
            logger.error(f"[RESET] Errore reset config_state: chat_id={chat_id}, error={e}")
            assistant_reply = f"❌ Errore durante il reset: {str(e)}"
            source = "reset_error"
            mode = "reset_failed"
            
            # Salva messaggio assistant
            insert_assistant_ok, assistant_message_id, assistant_insert_error = _insert_assistant_message(
                chat_id=chat_id,
                user_id=user_id,
                content=assistant_reply,
            )
            
            return {
                "ok": True,
                "chat_id": chat_id,
                "user_message_id": user_message_id,
                "assistant_message_id": assistant_message_id if insert_assistant_ok else None,
                "reply": assistant_reply,
                "source": source,
                "mode": mode,
            }
        
        # Genera prima domanda (market_type) per il piano FREE v2: ordine market_type -> symbol -> timeframe -> operating_mode -> ...
        first_question = "Ciao! Vuoi operare in Spot o in Futures?\n\n⚠️ Nota: per alcuni account europei i Futures su Bybit potrebbero non essere disponibili a causa di recenti aggiornamenti normativi.\nSe scegli Futures, il bot proverà comunque a operare."
        if orchestrator:
            try:
                _step_question = getattr(orchestrator, "_step_question", None)
                if _step_question and callable(_step_question):
                    first_question = _step_question("market_type", {}, error_count=0, is_error=False, greeting_variant=0)
            except Exception as e:
                logger.warning(f"[RESET] Fallback su stringa hardcoded per prima domanda: {e}")
                first_question = "Ciao! Vuoi operare in Spot o in Futures?\n\n⚠️ Nota: per alcuni account europei i Futures su Bybit potrebbero non essere disponibili a causa di recenti aggiornamenti normativi.\nSe scegli Futures, il bot proverà comunque a operare."
        
        assistant_reply = f"✅ Bot resettato. Ripartiamo da zero.\n\n{first_question}"
        source = "reset"
        mode = "reset_command"
        
        # Salva messaggio assistant
        insert_assistant_ok, assistant_message_id, assistant_insert_error = _insert_assistant_message(
            chat_id=chat_id,
            user_id=user_id,
            content=assistant_reply,
        )
        
        if not insert_assistant_ok:
            error_details = str(assistant_insert_error) if assistant_insert_error else "Unknown error"
            logger.error(
                f"[RESET] INSERT assistant FAILED: chat_id={chat_id}, user_id={user_id}, error={error_details}"
            )
        
        response_data = {
            "ok": True,
            "chat_id": chat_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id if insert_assistant_ok else None,
            "reply": assistant_reply,
            "source": source,
            "mode": mode,
        }
        
        logger.info(
            f"[RESET] POST /chat RESET reply: chat_id={chat_id}, user_id={user_id}, "
            f"insert_user_ok={insert_user_ok}, insert_assistant_ok={insert_assistant_ok}"
        )
        
        return response_data
    
    # STEP 2.5: Domande di stato runner (linguaggio naturale) -> risposta immediata
    if _is_runner_status_question(user_message_content):
        runner_online, seconds_ago, last_seen_at = get_runner_online_for_user(user_id)
        
        # Calcola delta_seconds in forma robusta (può essere None)
        delta_seconds = None
        if seconds_ago is not None:
            try:
                delta_seconds = int(seconds_ago)
            except (TypeError, ValueError):
                # In caso di valore non convertibile, mantieni quello originale per logging
                delta_seconds = seconds_ago

        logger.info(
            f"[CHAT] Runner status question detected: chat_id={chat_id}, user_id={user_id}, "
            f"online={runner_online}, last_seen_at={last_seen_at}, "
            f"delta_seconds={delta_seconds}"
        )
        
        if runner_online:
            assistant_reply = random.choice(RUNNER_ONLINE_MESSAGES)
        else:
            # Log di debug aggiuntivo solo quando offline
            logger.info(
                f"[RUNNER_STATUS] Runner OFFLINE for natural question: chat_id={chat_id}, user_id={user_id}, "
                f"last_seen_at={last_seen_at}, delta_seconds={delta_seconds}"
            )
            assistant_reply = random.choice(RUNNER_OFFLINE_MESSAGES)
        
        # Salva messaggio assistant come per il flusso normale
        insert_assistant_ok, assistant_message_id, assistant_insert_error = _insert_assistant_message(
            chat_id=chat_id,
            user_id=user_id,
            content=assistant_reply,
        )
        
        if not insert_assistant_ok:
            error_details = str(assistant_insert_error) if assistant_insert_error else "Unknown error"
            logger.error(
                f"[CHAT] INSERT assistant FAILED (runner status): "
                f"chat_id={chat_id}, user_id={user_id}, error={error_details}"
            )
        
        response_data = {
            "ok": True,
            "chat_id": chat_id,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id if insert_assistant_ok else None,
            "reply": assistant_reply,
            "source": "runner",
            "mode": "runner_status_question",
        }
        
        logger.info(
            f"[CHAT] POST /chat RUNNER_STATUS immediate reply: chat_id={chat_id}, user_id={user_id}, "
            f"online={runner_online}, insert_user_ok={insert_user_ok}, insert_assistant_ok={insert_assistant_ok}"
        )
        
        return response_data
    else:
        logger.debug(
            f"[CHAT] Runner status question NOT detected, proceeding with normal flow: "
            f"chat_id={chat_id}, user_id={user_id}, text={user_message_content!r}"
        )
    
    # STEP 3: Natural Language Router
    # 1) Se inizia con "/" → è comando runner "classico": usa la logica esistente
    is_runner_cmd_slash, normalized_command_slash = _is_runner_command(user_message_content)
    
    # 2) Altrimenti prova a classificare intent
    intent_result = None
    if not is_runner_cmd_slash:
        normalized_text = normalize_user_text(user_message_content)
        intent_result = classify_intent(normalized_text)
    
    # Determina tipo di routing
    is_runner_cmd = is_runner_cmd_slash
    is_local_cmd = False
    normalized_command = None
    local_intent_name = None
    
    if is_runner_cmd_slash:
        # Comando slash esistente
        normalized_command = normalized_command_slash
    elif intent_result and intent_result.intent_name:
        # Intent riconosciuto
        intent_def = INTENT_DEFINITIONS.get(intent_result.intent_name)
        if intent_def:
            if intent_def["type"] == "RUNNER":
                is_runner_cmd = True
                normalized_command = intent_def["command"]
            elif intent_def["type"] == "LOCAL":
                is_local_cmd = True
                local_intent_name = intent_result.intent_name

    # Messaggi di configurazione trading: non devono finire nel ramo LOCAL (es. "profit" in "take profit")
    if is_local_cmd and not is_runner_cmd_slash:
        _config_local_exclude_keywords = (
            "stop loss",
            "take profit",
            "leva",
            "sl",
            "tp",
            "rischio",
            "risk",
        )
        if any(kw in normalized_text for kw in _config_local_exclude_keywords):
            is_local_cmd = False
            local_intent_name = None

    # Log input ricevuto
    log_level = logger.debug if (is_runner_cmd or is_local_cmd) else logger.info
    log_level(
        f"[CHAT] Input received: text={user_message_content!r} "
        f"is_runner_cmd={is_runner_cmd} is_local_cmd={is_local_cmd} "
        f"intent={intent_result.intent_name if intent_result else None} "
        f"confidence={intent_result.confidence if intent_result else 0.0:.2f} "
        f"normalized={normalized_command!r} chat_id={chat_id} user_id={user_id}"
    )
    
    # STEP 4: Genera risposta
    assistant_reply = ""
    source = "unknown"
    mode = "unknown"
    model_used = None
    orch_error_code = None  # es. "invalid_leverage" quando la leva è fuori range

    try:
        # Gestione comandi speciali Supabase (prima della logica runner normale)
        if is_runner_cmd and normalized_command:
            parts = normalized_command.split()
            if len(parts) >= 1:
                cmd_base = parts[0].lower()
                
                # /pair <device_id>
                if cmd_base == "/pair":
                    assistant_reply = _handle_pair_command(chat_id, parts)
                    source = "runner"
                    mode = "pair_command"
                    logger.info(f"[CHAT] /pair command: chat_id={chat_id}, parts={parts}")
                
                # /runner ping
                elif cmd_base == "/runner" and len(parts) >= 2 and parts[1].lower() == "ping":
                    assistant_reply = _handle_runner_ping(chat_id, user_id)
                    source = "runner"
                    mode = "runner_ping"
                    logger.info(f"[CHAT] /runner ping command: chat_id={chat_id}")
                
                # /runner events
                elif cmd_base == "/runner" and len(parts) >= 2 and parts[1].lower() == "events":
                    limit = 20
                    if len(parts) >= 3:
                        try:
                            limit = int(parts[2])
                        except:
                            pass
                    assistant_reply = _handle_runner_events(chat_id, limit=limit)
                    source = "runner"
                    mode = "runner_events"
                    logger.info(f"[CHAT] /runner events command: chat_id={chat_id}, limit={limit}")
                
                # /trade status (modificato per leggere da Supabase)
                elif cmd_base == "/trade" and len(parts) >= 2 and parts[1].lower() == "status":
                    assistant_reply = _handle_trade_status(chat_id, user_id)
                    source = "runner"
                    mode = "trade_status"
                    logger.info(f"[CHAT] /trade status command: chat_id={chat_id}")
                
                # /trade open_long e /trade open_short
                elif cmd_base == "/trade" and len(parts) >= 2 and parts[1].lower() in ("open_long", "open_short"):
                    if not supabase_queue:
                        assistant_reply = "❌ Modulo supabase_queue non disponibile"
                        source = "runner"
                        mode = "trade_error"
                    elif len(parts) < 3:
                        assistant_reply = "❌ Uso: /trade open_long <SYMBOL> <QTY> oppure /trade open_short <SYMBOL> <QTY>"
                        source = "runner"
                        mode = "trade_error"
                    else:
                        symbol = parts[2].upper().strip()
                        if not symbol or ' ' in symbol:
                            assistant_reply = "❌ SYMBOL deve essere uppercase e senza spazi"
                            source = "runner"
                            mode = "trade_error"
                        elif len(parts) < 4:
                            assistant_reply = "❌ QTY mancante"
                            source = "runner"
                            mode = "trade_error"
                        else:
                            try:
                                qty = float(parts[3])
                                if qty <= 0:
                                    assistant_reply = "❌ QTY deve essere > 0"
                                    source = "runner"
                                    mode = "trade_error"
                                else:
                                    # Verifica device_id
                                    device_id = get_chat_device_id(chat_id)
                                    if not device_id:
                                        assistant_reply = "❌ Prima fai /pair <device_id>"
                                        source = "runner"
                                        mode = "trade_error"
                                    else:
                                        # Crea payload e invia
                                        side = "LONG" if parts[1].lower() == "open_long" else "SHORT"
                                        payload = {
                                            "action": "TRADE_OPEN",
                                            "symbol": symbol,
                                            "side": side,
                                            "qty": qty
                                        }
                                        try:
                                            command_id = supabase_queue.enqueue_runner_command(
                                                device_id, payload, user_id=user_id
                                            )
                                            assistant_reply = f"✅ OK: command queued {command_id}"
                                            source = "runner"
                                            mode = "trade_command"
                                            logger.info(f"[CHAT] /trade {parts[1]} queued: command_id={command_id}, device_id={device_id}, symbol={symbol}, qty={qty}")
                                        except Exception as e:
                                            logger.error(f"[CHAT] Error enqueuing trade command: {e}")
                                            assistant_reply = f"❌ Errore Supabase: {str(e)}"
                                            source = "runner"
                                            mode = "trade_error"
                            except ValueError:
                                assistant_reply = "❌ QTY deve essere un numero valido"
                                source = "runner"
                                mode = "trade_error"
        
        # Gestione speciale per RUNNER_STATUS: usa get_runner_online_for_user() invece di enqueue
        if intent_result and intent_result.intent_name == "RUNNER_STATUS" and not assistant_reply:
            # Verifica stato runner direttamente senza enqueue
            runner_online, seconds_ago, last_seen_at = get_runner_online_for_user(user_id)
            
            # Log debug come richiesto
            logger.info(
                f"[CHAT] RUNNER_STATUS check: user_id={user_id} "
                f"chat_id={chat_id} online={runner_online} "
                f"seconds_ago={seconds_ago} last_seen_at={last_seen_at} "
                f"threshold={RUNNER_ONLINE_THRESHOLD_SECONDS}"
            )
            
            if runner_online:
                assistant_reply = random.choice(RUNNER_ONLINE_MESSAGES)
            else:
                # Log di debug aggiuntivo solo quando offline
                logger.info(
                    f"[RUNNER_STATUS] Runner OFFLINE for RUNNER_STATUS intent: chat_id={chat_id}, user_id={user_id}, "
                    f"seconds_ago={seconds_ago}, last_seen_at={last_seen_at}, "
                    f"threshold={RUNNER_ONLINE_THRESHOLD_SECONDS}"
                )
                assistant_reply = random.choice(RUNNER_OFFLINE_MESSAGES)
            
            source = "runner"
            mode = "runner_status_check"
        
        # SOLO se è un comando runner (slash O intent RUNNER), gestisci come runner
        # (solo se non è già stato gestito sopra)
        if is_runner_cmd and not assistant_reply:
            # REGOLA OBBLIGATORIA: Ogni comando che inizia con "/" DEVE chiamare enqueue_runner_command()
            # PRIMA di qualsiasi risposta positiva. Se l'enqueue fallisce, mostrare errore.
            
            logger.info(
                f"[CHAT] Runner command detected: original={user_message_content!r} "
                f"normalized={normalized_command!r}"
            )
            
            # PRIMA: enqueue del comando in Supabase (OBBLIGATORIO)
            enqueue_ok, command_id = _enqueue_command_to_runner(
                normalized_command=normalized_command,
                chat_id=chat_id,
                user_id=user_id,
                original_text=user_message_content
            )
            
            if not enqueue_ok:
                # Se l'enqueue fallisce, mostrare errore (NON successo finto)
                # command_id può contenere il messaggio di errore se enqueue_ok è False
                error_msg = command_id if command_id else "Errore sconosciuto"
                assistant_reply = f"❌ {error_msg}"
                source = "runner"
                mode = "runner_command"
                logger.error(
                    f"[CHAT] Runner command: {user_message_content!r} -> {normalized_command!r} "
                    f"-> enqueue FAILED: {error_msg}"
                )
            else:
                # Enqueue riuscito: mostra risposta positiva
                # Verifica se runner è online per messaggio informativo (opzionale)
                runner_online, seconds_ago, last_seen_at = get_runner_online_for_user(user_id)
                
                # Risposta user-friendly per comandi bot specifici
                parts_cmd = normalized_command.split() if normalized_command else []
                cmd_base = parts_cmd[0].lower() if len(parts_cmd) >= 1 else ""
                cmd_sub = parts_cmd[1].lower() if len(parts_cmd) >= 2 else ""
                
                if cmd_base == "/bot" and cmd_sub == "start":
                    assistant_reply = random.choice(START_BOT_MESSAGES)
                elif cmd_base == "/bot" and cmd_sub == "stop":
                    assistant_reply = random.choice(STOP_BOT_MESSAGES)
                else:
                    # Messaggio più chiaro: mostra il testo originale se diverso dal normalizzato
                    if user_message_content != normalized_command:
                        display_text = f"{user_message_content} ({normalized_command})"
                    else:
                        display_text = normalized_command
                    
                    if runner_online:
                        assistant_reply = f"✅ Comando inviato al runner: {display_text}"
                    else:
                        assistant_reply = f"✅ Comando inviato al runner: {display_text}\n⚠️ Runner attualmente offline"
                
                source = "runner"
                mode = "runner_command"
                logger.info(
                    f"[CHAT] Runner command: {user_message_content!r} -> {normalized_command!r} "
                    f"-> enqueued command_id={command_id} runner_online={runner_online}"
                )
        elif is_local_cmd:
            # Intent LOCAL: gestisci direttamente senza controllare runner
            # Estrai max_events dal testo del messaggio
            max_events = _extract_max_events_from_text(user_message_content, default=20)
            normalized_text_lower = (user_message_content or "").strip().lower()
            is_show_events_command = normalized_text_lower in ("mostra eventi", "mostrami gli eventi")
            if is_show_events_command:
                max_events = 10
            
            if is_show_events_command:
                assistant_reply = _handle_show_events_command(
                    user_id=user_id,
                    chat_id=chat_id,
                    limit=max_events,
                )
                source = "local"
                mode = "local_events_show_only"
            elif local_intent_name == "SHOW_EVENTS_ALL":
                assistant_reply = _handle_show_events_all_order_only(
                    user_id=user_id,
                    chat_id=chat_id,
                    max_events=max_events,
                )
                source = "local"
                mode = "local_events_all"
            elif local_intent_name == "SHOW_EVENTS_POSITIVE":
                assistant_reply = handle_local_events(
                    filter_type="positive",
                    max_events=max_events,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                source = "local"
                mode = "local_events_positive"
            elif local_intent_name == "SHOW_EVENTS_NEGATIVE":
                assistant_reply = handle_local_events(
                    filter_type="negative",
                    max_events=max_events,
                    user_id=user_id,
                    chat_id=chat_id,
                )
                source = "local"
                mode = "local_events_negative"
            else:
                # Fallback: tratta come chatbot
                assistant_reply = ""
                source = "unknown"
                mode = "local_unknown"
            
            logger.info(
                f"[CHAT] Local command: intent={local_intent_name} "
                f"original={user_message_content!r} -> reply_len={len(assistant_reply)}"
            )
        else:
            # Nessun intent riconosciuto o comando slash: va al chatbot normale
            # IMPORTANTE: non mostrare "runner offline" qui, è solo chatbot
            # Carica messaggi dalla chat (incluso quello appena salvato)
            messages_res = (
                supabase.table("messages")
                .select("*")
                .eq("chat_id", chat_id)
                .order("created_at", desc=False)
                .execute()
            )

            # Costruisce history semplice
            history = []
            for msg in (messages_res.data or []):
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ["user", "assistant"] and content:
                    history.append({"role": role, "content": content})

            # Carica stato conversazione (scaletta)
            state = load_chat_state(chat_id)

            # -------------------------
            # 1) TRY ORCHESTRATOR FIRST
            # -------------------------
            if not orchestrator:
                logger.warning("[ORCH] orchestrator is None - skip orchestrator path, config_state will NOT be saved")
            if orchestrator:
                orch_payload = {
                    "message": user_message_content,
                    "session_id": chat_id,
                    "msgId": f"{user_id}:{chat_id}:{now_iso()}",
                    "state": state,
                    "history": history,
                    "user_id": user_id,
                    "chat_id": chat_id,
                }

                try:
                    orch_res = orchestrator.run(orch_payload)

                    # Log ritorno orchestrator
                    logger.info(
                        "[ORCH] run() returned: type=%s keys=%s",
                        type(orch_res).__name__,
                        list(orch_res.keys()) if orch_res and isinstance(orch_res, dict) else None,
                    )
                    orch_state = orch_res.get("state") if (orch_res and isinstance(orch_res, dict)) else None
                    if orch_state is not None:
                        logger.info("[ORCH] state keys: %s", list(orch_state.keys()) if isinstance(orch_state, dict) else type(orch_state).__name__)

                    # Salva stato ogni volta che l'orchestrator restituisce uno state (PATCH su chats)
                    # FIX: orch_res.get("state") è falsy per {} - usare "is not None" per non saltare save
                    if orch_res and isinstance(orch_res, dict) and orch_state is not None and isinstance(orch_state, dict):
                        logger.info("[CONFIG_SAVE] saving state for chat_id=%s", chat_id)
                        save_result = save_chat_state(chat_id, user_id, orch_state)
                    else:
                        _skip_reason = (
                            "orch_res invalid" if not (orch_res and isinstance(orch_res, dict))
                            else "orch_state is None" if orch_state is None
                            else "orch_state not dict"
                        )
                        logger.info("[CONFIG_SAVE] SKIP save for chat_id=%s: %s", chat_id, _skip_reason)
                        if not save_result.get("ok", False):
                            reason = save_result.get("reason", "unknown")
                            logger.error(f"[CHAT] Save chat state failed: {reason}: chat_id={chat_id}, user_id={user_id}")
                            raise HTTPException(status_code=500, detail=f"Save chat state failed: {reason}")

                    if orch_res and isinstance(orch_res, dict) and "reply" in orch_res:
                        assistant_reply_raw = orch_res["reply"].strip()
                        if orch_res.get("error_code"):
                            orch_error_code = orch_res["error_code"]
                        logger.info(f"[CHAT] Orchestrator reply: len={len(assistant_reply_raw)}")

                        # Salva stato aggiornato
                        state_for_model = state
                        if orch_state is not None and isinstance(orch_state, dict):
                            # Log PRIMA: estrai campi chiave (inclusi indicatori) dallo state restituito dall'orchestrator
                            orch_config_state = orch_state.get("config_state")
                            orch_step = None
                            orch_timeframe = None
                            orch_leverage = None
                            orch_strategy = None
                            orch_free_strategy_id = None
                            orch_ema_period = None
                            orch_rsi_period = None
                            orch_atr_period = None
                            if orch_config_state and isinstance(orch_config_state, dict):
                                orch_step = orch_config_state.get("step")
                                orch_params = orch_config_state.get("params", {})
                                if isinstance(orch_params, dict):
                                    orch_timeframe = orch_params.get("timeframe")
                                    orch_leverage = orch_params.get("leverage")
                                    orch_strategy = orch_params.get("strategy")
                                    orch_free_strategy_id = orch_params.get("free_strategy_id")
                                    orch_ema_period = orch_params.get("ema_period")
                                    orch_rsi_period = orch_params.get("rsi_period")
                                    orch_atr_period = orch_params.get("atr_period")

                            logger.info(
                                "[CHAT] ORCH_STATE_BEFORE_DB strategy=%s free_strategy_id=%s ema_period=%s rsi_period=%s atr_period=%s step=%s timeframe=%s leverage=%s",
                                orch_strategy,
                                orch_free_strategy_id,
                                orch_ema_period,
                                orch_rsi_period,
                                orch_atr_period,
                                orch_step,
                                orch_timeframe,
                                orch_leverage,
                            )
                            state_for_model = orch_state

                        # Prima domanda market_type: mantieni il testo raw dell'orchestrator (no wrap OpenAI)
                        raw_is_first_market_type_question = (
                            "Vuoi operare in Spot o in Futures?" in assistant_reply_raw
                            and "recenti aggiornamenti normativi" in assistant_reply_raw
                        )
                        current_step_for_wrap = None
                        if isinstance(orch_state, dict):
                            _cfg = orch_state.get("config_state")
                            if isinstance(_cfg, dict):
                                current_step_for_wrap = _cfg.get("step")
                        if current_step_for_wrap is None and isinstance(state, dict):
                            _cfg = state.get("config_state")
                            if isinstance(_cfg, dict):
                                current_step_for_wrap = _cfg.get("step")
                        bypass_openai_wrap = raw_is_first_market_type_question or (current_step_for_wrap == "market_type")

                        # Se manca OPENAI_API_KEY, ritorna solo la domanda (fallback)
                        if not OPENAI_API_KEY or bypass_openai_wrap:
                            assistant_reply = assistant_reply_raw
                            source = "orchestrator" if not OPENAI_API_KEY else "orchestrator_raw_market_type"
                            mode = "orchestrator_only" if not OPENAI_API_KEY else "orchestrator_raw_bypass_wrap"
                            model_used = "orchestrator"
                        else:
                            # Wrap con OpenAI per renderla davvero "IA"
                            client = OpenAI(api_key=OPENAI_API_KEY)
                            chosen_model = choose_model(user_message_content, state_for_model, history)
                            logger.info(f"[CHAT] OpenAI wrap: chosen_model={chosen_model}")

                            # Aggiungi prompt conversazionale se necessario
                            conversational_prompt = build_conversational_prompt(user_message_content, history, state_for_model)
                            system_messages = [
                                {"role": "system", "content": SYSTEM_BASE_IDITH},
                                {"role": "system", "content": build_state_context(state_for_model)},
                                {"role": "system", "content": build_orchestrator_wrap_prompt(assistant_reply_raw)},
                            ]
                            if conversational_prompt:
                                system_messages.append({"role": "system", "content": conversational_prompt})

                            response = client.chat.completions.create(
                                model=chosen_model,
                                messages=[
                                    *system_messages,
                                    *history,
                                    {"role": "user", "content": user_message_content},
                                ],
                                temperature=0.7,
                                max_tokens=350
                            )

                            assistant_reply = (response.choices[0].message.content or "").strip() or assistant_reply_raw
                            source = "orchestrator+openai"
                            mode = "orchestrator_wrapped"
                            model_used = chosen_model

                except Exception as e:
                    import traceback
                    print("ERRORE REALE:", str(e))
                    traceback.print_exc()

                    logger.error(f"[CHAT] Errore orchestrator: {e}")
                    logger.exception("Errore orchestrator con stacktrace completo")
                    logger.error(traceback.format_exc())

            # -------------------------
            # 2) FALLBACK: OPENAI DIRECT
            # -------------------------
            if not assistant_reply:
                if not OPENAI_API_KEY:
                    assistant_reply = "OPENAI_API_KEY non configurata"
                    source = "error"
                    mode = "no_api_key"
                else:
                    client = OpenAI(api_key=OPENAI_API_KEY)
                    chosen_model = choose_model(user_message_content, state, history)
                    logger.info(f"[CHAT] OpenAI direct: chosen_model={chosen_model}")
                    
                    # Aggiungi prompt conversazionale se necessario
                    conversational_prompt = build_conversational_prompt(user_message_content, history, state)
                    system_messages = [
                        {"role": "system", "content": SYSTEM_BASE_IDITH},
                        {"role": "system", "content": build_state_context(state)},
                    ]
                    if conversational_prompt:
                        system_messages.append({"role": "system", "content": conversational_prompt})
                    
                    response = client.chat.completions.create(
                        model=chosen_model,
                        messages=[
                            *system_messages,
                            *history,
                        ],
                        temperature=0.7,
                        max_tokens=500
                    )

                    assistant_reply = (response.choices[0].message.content or "").strip()
                    if not assistant_reply:
                        assistant_reply = "Risposta vuota da OpenAI"
                        source = "error"
                        mode = "empty_response"
                    else:
                        source = "openai"
                        mode = "openai_direct"
                        model_used = chosen_model

    except Exception as e:
        error_details = str(e)
        logger.error(f"[CHAT] Errore generazione risposta: chat_id={chat_id}, user_id={user_id}, error={error_details}")
        assistant_reply = f"Errore nella generazione della risposta: {error_details}"
        source = "error"
        mode = "exception"
    
    # STEP 5: Salva messaggio assistant
    insert_assistant_ok, assistant_message_id, assistant_insert_error = _insert_assistant_message(
        chat_id=chat_id,
        user_id=user_id,
        content=assistant_reply
    )
    
    if not insert_assistant_ok:
        error_details = str(assistant_insert_error) if assistant_insert_error else "Unknown error"
        logger.error(f"[CHAT] INSERT assistant FAILED: chat_id={chat_id}, user_id={user_id}, error={error_details}")
        # Non solleviamo eccezione qui, ma loggiamo l'errore
        # Il messaggio user è già salvato, quindi ritorniamo comunque ok:true con i dettagli
    
    # STEP 6: Ritorna JSON completo e coerente
    response_data = {
        "ok": True,
        "chat_id": chat_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id if insert_assistant_ok else None,
        "reply": assistant_reply,
        "source": source,
        "mode": mode,
    }

    if model_used:
        response_data["model"] = model_used
    if orch_error_code:
        response_data["error"] = assistant_reply
        response_data["code"] = orch_error_code
    
    logger.info(
        f"[CHAT] POST /chat SUCCESS: chat_id={chat_id}, user_id={user_id}, "
        f"insert_user_ok={insert_user_ok}, insert_assistant_ok={insert_assistant_ok}, "
        f"source={source}, mode={mode}"
    )
    
    return response_data


@app.delete("/api/delete_account")
def delete_account(user=Depends(get_current_user)):
    """
    Elimina l'utente da Supabase Auth e tutti i dati collegati.
    Richiede autenticazione JWT.
    """
    if not user["id"]:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    user_id = user["id"]
    
    try:
        # --- Runner commands / events (schema attuale: niente colonne native chat_id/user_id) ---
        # Collegamento affidabile: runner_commands.payload.chat_id e runner_events.command_id.
        # Con lo schema attuale, eventi senza command_id (o non collegabili a comandi con
        # payload.chat_id nelle chat dell'utente) non sono attribuibili in modo sicuro all'account:
        # non vengono cancellati, per evitare cancellazioni cross-account.
        # NON usare device_id come criterio di delete globale su runner_events/runner_commands.

        chats_res = (
            supabase.table("chats")
            .select("id")
            .eq("user_id", user_id)
            .execute()
        )
        chat_ids = [str(c["id"]) for c in (chats_res.data or []) if c.get("id")]

        command_ids: list[str] = []
        for cid in chat_ids:
            try:
                rc_res = (
                    supabase.table("runner_commands")
                    .select("id")
                    .eq("payload->>chat_id", cid)
                    .execute()
                )
            except Exception as e:
                print(f"[DELETE_ACCOUNT] runner_commands select payload.chat_id={cid}: {e}")
                continue
            for row in rc_res.data or []:
                rid = row.get("id")
                if rid:
                    command_ids.append(str(rid))
        command_ids = list(dict.fromkeys(command_ids))

        _del_chunk = 100
        if command_ids:
            for i in range(0, len(command_ids), _del_chunk):
                chunk = command_ids[i : i + _del_chunk]
                supabase.table("runner_events").delete().in_("command_id", chunk).execute()
            for i in range(0, len(command_ids), _del_chunk):
                chunk = command_ids[i : i + _del_chunk]
                supabase.table("runner_commands").delete().in_("id", chunk).execute()

        supabase.table("runner_tokens").delete().eq("user_id", user_id).execute()

        if chat_ids:
            for chat_id in chat_ids:
                supabase.table("messages").delete().eq("chat_id", chat_id).execute()

        supabase.table("chats").delete().eq("user_id", user_id).execute()

        try:
            supabase.auth.admin.delete_user(user_id)
        except Exception as auth_error:
            print(f"[DELETE_ACCOUNT] Warning: Error deleting user from Auth: {auth_error}")

        return {"ok": True, "message": "Account eliminato correttamente."}
        
    except Exception as e:
        print(f"[DELETE_ACCOUNT] Error: {e}")
        raise HTTPException(status_code=500, detail=f"Errore durante l'eliminazione dell'account: {str(e)}")

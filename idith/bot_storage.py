import os
import json
import uuid
from datetime import datetime

# ============================================================
#  BOT STORAGE SYSTEM – versione compatibile con orchestrator
# ============================================================
# Gestisce:
# - creazione bot
# - aggiornamento config bot
# - logging eventi
# - stato runtime
# - eliminazione bot
#
# + funzione speciale: upsert_bot_for_chat(...)
#   compatibile con il vecchio orchestrator di Idith.
# ============================================================


# ------------------------------------------------------------
# Generate SHORT UUID bot_id
# ------------------------------------------------------------
def generate_bot_id():
    return "chat_" + uuid.uuid4().hex[:8]


# ------------------------------------------------------------
# Directory helpers
# ------------------------------------------------------------
def _bots_dir(user_id):
    return os.path.join("idith", "users", user_id, "bots")

def _events_dir(user_id):
    return os.path.join("idith", "users", user_id, "events")

def _state_dir(user_id):
    return os.path.join("idith", "users", user_id, "state")

def _bot_path(user_id, bot_id):
    return os.path.join(_bots_dir(user_id), f"{bot_id}.json")

def _events_path(user_id, bot_id):
    return os.path.join(_events_dir(user_id), f"{bot_id}.jsonl")

def _state_path(user_id, bot_id):
    return os.path.join(_state_dir(user_id), f"{bot_id}.json")


def ensure_directories(user_id):
    os.makedirs(_bots_dir(user_id), exist_ok=True)
    os.makedirs(_events_dir(user_id), exist_ok=True)
    os.makedirs(_state_dir(user_id), exist_ok=True)


# ------------------------------------------------------------
# CREATE NEW BOT
# ------------------------------------------------------------
def create_new_bot(user_id, config: dict):
    ensure_directories(user_id)

    bot_id = generate_bot_id()
    bot_file = _bot_path(user_id, bot_id)

    data = {
        "bot_id": bot_id,
        "user_id": user_id,
        "config": config,
        "created_at": datetime.utcnow().isoformat()
    }

    with open(bot_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return bot_id


# ------------------------------------------------------------
# LOAD BOT
# ------------------------------------------------------------
def load_bot(user_id, bot_id):
    path = _bot_path(user_id, bot_id)
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------
# SAVE BOT CONFIG
# ------------------------------------------------------------
def save_bot_config(user_id, bot_id, config: dict):
    bot = load_bot(user_id, bot_id)
    if bot is None:
        return False

    bot["config"] = config

    with open(_bot_path(user_id, bot_id), "w", encoding="utf-8") as f:
        json.dump(bot, f, indent=2)

    return True


# ------------------------------------------------------------
# REAL EVENTS LOGGING (runner → backend)
# ------------------------------------------------------------
def log_event(user_id, bot_id, event: dict):
    ensure_directories(user_id)

    event_file = _events_path(user_id, bot_id)

    if "timestamp" not in event:
        event["timestamp"] = datetime.utcnow().timestamp()

    with open(event_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")

    return True


# ------------------------------------------------------------
# LOAD EVENTS FOR BRAIN / IDITH ("mostra eventi")
# ------------------------------------------------------------
def load_events(user_id, bot_id, limit=50):
    event_file = _events_path(user_id, bot_id)
    if not os.path.exists(event_file):
        return []

    events = []
    with open(event_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except:
                continue

    events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)
    return events[:limit]


# ------------------------------------------------------------
# SAVE STATE (open position / pnl / etc)
# ------------------------------------------------------------
def save_bot_state(user_id, bot_id, state: dict):
    ensure_directories(user_id)

    with open(_state_path(user_id, bot_id), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def load_bot_state(user_id, bot_id):
    path = _state_path(user_id, bot_id)
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------
# DELETE BOT (bot + events + state)
# ------------------------------------------------------------
def delete_bot(user_id, bot_id):
    for path in [
        _bot_path(user_id, bot_id),
        _events_path(user_id, bot_id),
        _state_path(user_id, bot_id)
    ]:
        try:
            os.remove(path)
        except:
            pass

    return True


# ============================================================
# 🔥 upsert_bot_for_chat – FUNZIONE CHIAVE
# ============================================================
# Questa funzione replica la firma del tuo orchestrator attuale:
#
# info = bot_storage.upsert_bot_for_chat(user_id, chat_id, chat_name, blueprint)
#
# • Se la chat NON ha bot → crea nuovo bot
# • Se la chat ha bot → aggiorna config
# • Non tocca chat_name
# • Ritorna dict compatibile { "bot_id": ..., "updated": True/False }
# ============================================================

def upsert_bot_for_chat(user_id, chat_id, chat_name, blueprint):
    """
    Compatibile al 100% con il tuo orchestrator.
    """

    ensure_directories(user_id)

    # blueprint contiene già bot_id?
    bot_id = blueprint.get("bot_id")

    # se manca → creiamo bot nuovo
    if not bot_id:
        bot_id = create_new_bot(user_id, blueprint)
        blueprint["bot_id"] = bot_id
        save_bot_config(user_id, bot_id, blueprint)
        return {"bot_id": bot_id, "updated": False}

    # se esiste → aggiorniamo config
    save_bot_config(user_id, bot_id, blueprint)
    return {"bot_id": bot_id, "updated": True}

import os
import json
import shutil
from datetime import datetime, timezone
from .brain import call_brain 

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask import send_from_directory

# =========================================================
# CONFIG
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_DIR = os.path.join(BASE_DIR, "users")

os.makedirs(USERS_DIR, exist_ok=True)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

# =========================================================
# UTILS
# =========================================================
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_name(s: str) -> str:
    s = (s or "").strip()
    # evita path traversal e caratteri strani
    s = s.replace("..", "").replace("\\", "_").replace("/", "_")
    return s

def normalize_user_id(user_id: str) -> str:
    """Normalizza user_id per uso nel filesystem: aggiunge 'user_' se mancante."""
    if not user_id or not user_id.strip():
        return "anonymous"
    uid = safe_name(user_id.strip())
    if not uid or uid.lower() in ("anonymous", "anon", "guest", "web-anonymous"):
        return "anonymous"
    # Assicura che inizi con "user_"
    if not uid.startswith("user_"):
        uid = f"user_{uid}"
    return uid

def user_dir(user_id: str) -> str:
    """Restituisce il path della cartella utente basato SOLO su user_id normalizzato."""
    normalized_id = normalize_user_id(user_id)
    return os.path.join(USERS_DIR, normalized_id)

def chats_dir(user_id: str) -> str:
    return os.path.join(user_dir(user_id), "chats")

def chat_dir(user_id: str, chat_id: str) -> str:
    return os.path.join(chats_dir(user_id), safe_name(chat_id))

def messages_path(user_id: str, chat_id: str) -> str:
    return os.path.join(chat_dir(user_id, chat_id), "messages.json")

def state_path(user_id: str, chat_id: str) -> str:
    return os.path.join(chat_dir(user_id, chat_id), "state.json")

def ensure_chat(user_id: str, chat_id: str):
    os.makedirs(chat_dir(user_id, chat_id), exist_ok=True)

def load_json(path: str, default: dict):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_messages(user_id: str, chat_id: str) -> dict:
    ensure_chat(user_id, chat_id)
    default = {
        "title": chat_id,
        "created_at": utc_iso(),
        "messages": []
    }
    return load_json(messages_path(user_id, chat_id), default)

def save_messages(user_id: str, chat_id: str, convo: dict):
    ensure_chat(user_id, chat_id)
    save_json(messages_path(user_id, chat_id), convo)

def load_state(user_id: str, chat_id: str) -> dict:
    ensure_chat(user_id, chat_id)
    return load_json(state_path(user_id, chat_id), {"step": "start", "data": {}, "updated_at": utc_iso()})

def save_state(user_id: str, chat_id: str, state: dict):
    state["updated_at"] = utc_iso()
    save_json(state_path(user_id, chat_id), state)

def parse_body():
    return request.get_json(silent=True) or {}


# =========================================================
# API
# =========================================================
@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "ts": utc_iso()})

@app.route("/api/load_chat", methods=["GET"])
def api_load_chat():
    user_id = request.args.get("user_id", "anonymous")
    chat_id = request.args.get("chat_id")
    if not chat_id:
        return jsonify({"error": "chat_id mancante"}), 400
    convo = load_messages(user_id, chat_id)
    return jsonify(convo)

@app.route("/api/save_message", methods=["POST"])
def api_save_message():
    data = parse_body()
    user_id = data.get("user_id", "anonymous")
    chat_id = data.get("chat_id")
    msg = data.get("message")

    if not chat_id or not msg or not isinstance(msg, dict):
        return jsonify({"error": "dati mancanti"}), 400

    convo = load_messages(user_id, chat_id)
    convo["messages"].append({
        "role": msg.get("role", "user"),
        "text": msg.get("text", ""),
        "ts": utc_iso()
    })
    save_messages(user_id, chat_id, convo)
    return jsonify({"ok": True})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = parse_body()

    user_id = data.get("user_id", "anonymous")
    chat_id = data.get("chat_id")
    text = data.get("text") or data.get("message") or data.get("content")

    if not chat_id or not text:
        return jsonify({"error": "dati mancanti"}), 400

    # 1) salva messaggio utente
    convo = load_messages(user_id, chat_id)
    convo["messages"].append({
        "role": "user",
        "text": text,
        "ts": utc_iso()
    })

    # 2) genera risposta "IA" (mini brain)
    reply = call_brain(f"{user_id}__{chat_id}", text)

    # 3) salva risposta assistant
    convo["messages"].append({
        "role": "assistant",
        "text": reply,
        "ts": utc_iso()
    })
    save_messages(user_id, chat_id, convo)

    # 4) risposta compatibile col tuo frontend (stringa)
    return jsonify({"reply": reply})

@app.route("/")
def home():
    return send_from_directory(os.path.dirname(__file__), "chat.html")

# =========================================================
# RUN
# =========================================================
if __name__ == "__main__":
    # porta 5500 come nel tuo setup
    app.run(host="127.0.0.1", port=5500, debug=True)

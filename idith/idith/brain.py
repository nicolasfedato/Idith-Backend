import os
import json
import shutil
from collections import deque
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_DIR = os.path.join(BASE_DIR, "users")
os.makedirs(USERS_DIR, exist_ok=True)
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
SESSIONS_DIR = os.path.join(MEMORY_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)


# ------------------------
# Helpers
# ------------------------
def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(s: str) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    # evita path traversal e caratteri rognosi su Windows
    for ch in ['..', '\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        s = s.replace(ch, "_")
    s = s.replace("\n", " ").replace("\r", " ")
    return s.strip() or "anonymous"


def _read_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_user_dir(user_id: str) -> str:
    """
    Restituisce il path della cartella utente basato SOLO su user_id.
    NON usa mai email nel filesystem.
    
    Regole:
    - Normalizza user_id (aggiunge "user_" se mancante)
    - Crea la cartella se non esiste
    - Ritorna sempre users/user_<id_alfanumerico>
    """
    if not user_id or not user_id.strip():
        user_id = "anonymous"
    
    uid = user_id.strip()
    
    # Normalizza per anonymous/guest
    if uid.lower() in ("anonymous", "anon", "guest", "web-anonymous"):
        uid = "anonymous"
    else:
        # Normalizza: rimuove caratteri non sicuri
        uid = safe_name(uid)
        # Assicura che inizi con "user_"
        if not uid.startswith("user_"):
            uid = f"user_{uid}"
    
    direct = os.path.join(USERS_DIR, uid)
    os.makedirs(direct, exist_ok=True)
    return direct


def resolve_user_dir(user_id: str) -> str:
    """
    DEPRECATED: Usa get_user_dir() invece.
    Mantenuto per compatibilità, ma ora usa solo user_id.
    """
    return get_user_dir(user_id)


def chat_folder(user_id: str, chat_id: str) -> str:
    udir = resolve_user_dir(user_id)
    cid = safe_name(chat_id or "default")
    return os.path.join(udir, "chats", cid)


def messages_path(user_id: str, chat_id: str) -> str:
    return os.path.join(chat_folder(user_id, chat_id), "messages.json")


def ensure_chat_exists(user_id: str, chat_id: str):
    cdir = chat_folder(user_id, chat_id)
    os.makedirs(cdir, exist_ok=True)
    mpath = os.path.join(cdir, "messages.json")
    if not os.path.exists(mpath):
        _write_json(mpath, {
            "title": chat_id or "Nuova chat",
            "created_at": utc_iso(),
            "messages": []
        })


def load_convo(user_id: str, chat_id: str) -> dict:
    ensure_chat_exists(user_id, chat_id)
    convo = _read_json(messages_path(user_id, chat_id), {})
    if not isinstance(convo, dict):
        convo = {}
    convo.setdefault("title", chat_id or "Nuova chat")
    convo.setdefault("created_at", utc_iso())
    convo.setdefault("messages", [])
    if not isinstance(convo["messages"], list):
        convo["messages"] = []
    return convo


def save_convo(user_id: str, chat_id: str, convo: dict):
    _write_json(messages_path(user_id, chat_id), convo)


def append_message(user_id: str, chat_id: str, role: str, text: str):
    convo = load_convo(user_id, chat_id)
    convo["messages"].append({
        "role": role,
        "text": text,
        "ts": utc_iso()
    })
    save_convo(user_id, chat_id, convo)


def _read_events(path: str, max_events: int = 30) -> list[str]:
    if not os.path.exists(path):
        return []

    out: deque[str] = deque(maxlen=max_events)

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue

                if not isinstance(evt, dict):
                    continue

                ts = evt.get("ts", "")
                etype = evt.get("type", "EVENT")
                payload = evt.get("payload")

                if payload is None:
                    payload = {k: v for k, v in evt.items() if k not in ("ts", "type", "user_id", "chat_id")}

                out.append(f"- [{ts}] {etype} | {json.dumps(payload, ensure_ascii=False)}")
    except Exception:
        return []

    return list(out)


def _read_events(path: str, max_events: int = 30) -> list[str]:
    """
    Legge un file .jsonl e ritorna al massimo gli ultimi N eventi
    già formattati come:
    - [ts] TYPE | {payload}
    """
    if not os.path.exists(path):
        return []

    buf: deque[dict] = deque(maxlen=max_events)

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        buf.append(obj)
                except Exception:
                    continue
    except Exception:
        return []

    out: list[str] = []
    for evt in list(buf):
        ts = evt.get("ts", "")
        etype = evt.get("type", "EVENT")
        payload = evt.get("payload")

        if payload is None:
            payload = {k: v for k, v in evt.items() if k not in ("ts", "type")}

        try:
            payload_str = json.dumps(payload, ensure_ascii=False)
        except Exception:
            payload_str = str(payload)

        out.append(f"- [{ts}] {etype} | {payload_str}")

    return out



def handle_message(session_id: str, message: str):
    text = str(message or "").strip()
    if text.lower() == "eventi":
        primary_path = os.path.join(BASE_DIR, "memory", "queue", "events.jsonl")
        fallback_path = os.path.join(BASE_DIR, "memory", "events.jsonl")
        events = _read_events(primary_path)
        if not events:
            events = _read_events(fallback_path)
        if not events:
            return {"reply": "Nessun evento disponibile"}
        return {"reply": "\n".join(events)}
    try:
        from .memory_manager import memory  # type: ignore
        from . import policy_engine  # type: ignore

        profile = memory.load_profile()
        out = policy_engine.reply_for(text, profile=profile)
        if isinstance(out, dict):
            reply = out.get("reply") or out.get("text") or out.get("message")
            if isinstance(reply, str) and reply.strip():
                out["reply"] = reply.strip()
                out.setdefault("profile", memory.load_profile())
                return out
        if isinstance(out, str) and out.strip():
            return {"reply": out.strip(), "profile": memory.load_profile()}
    except Exception:
        pass

    return {"reply": "Non ho capito. Puoi riformulare?"}


def call_brain(session_id: str, text: str) -> str:
    resp = handle_message(session_id, text)
    if isinstance(resp, dict):
        reply = resp.get("reply") or resp.get("text") or resp.get("message")
        if isinstance(reply, str) and reply.strip():
            return reply.strip()
    if isinstance(resp, str) and resp.strip():
        return resp.strip()
    return "Non ho capito. Puoi riformulare?"


# ------------------------
# Routes
# ------------------------
@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "ts": utc_iso()})


@app.route("/api/list_chats", methods=["GET"])
def api_list_chats():
    user_id = request.args.get("user_id", "anonymous")
    udir = resolve_user_dir(user_id)
    chats_dir = os.path.join(udir, "chats")
    os.makedirs(chats_dir, exist_ok=True)

    chats = []
    try:
        for name in os.listdir(chats_dir):
            cdir = os.path.join(chats_dir, name)
            if not os.path.isdir(cdir):
                continue
            mpath = os.path.join(cdir, "messages.json")
            convo = _read_json(mpath, {})
            title = convo.get("title") if isinstance(convo, dict) else None
            chats.append({
                "chat_id": name,
                "title": title or name
            })
    except Exception:
        pass

    # ordinamento stabile
    chats.sort(key=lambda x: (x.get("title") or x.get("chat_id") or "").lower())
    return jsonify({"chats": chats})


@app.route("/api/load_chat", methods=["GET"])
def api_load_chat():
    user_id = request.args.get("user_id", "anonymous")
    chat_id = request.args.get("chat_id")
    if not chat_id:
        return jsonify({"error": "chat_id mancante"}), 400

    convo = load_convo(user_id, chat_id)
    return jsonify(convo)


@app.route("/api/save_message", methods=["POST"])
def api_save_message():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id", "anonymous")
    chat_id = data.get("chat_id")
    role = data.get("role")
    text = data.get("text")

    if not chat_id:
        return jsonify({"ok": False, "error": "chat_id mancante"}), 400
    if not role or not text:
        return jsonify({"ok": False, "error": "role/text mancanti"}), 400

    append_message(user_id, chat_id, role, str(text))
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}

    user_id = data.get("user_id", "anonymous")
    chat_id = data.get("chat_id") or data.get("chat") or data.get("title")
    text = data.get("text") or data.get("message") or ""

    if not chat_id:
        return jsonify({"reply": "Errore: chat_id mancante."}), 400

    text = str(text).strip()
    if not text:
        return jsonify({"reply": "Scrivi un messaggio valido."}), 400

    # salva user message
    append_message(user_id, chat_id, "user", text)

    # chiama cervello
    session_id = f"{safe_name(user_id)}__{safe_name(chat_id)}"
    reply = call_brain(session_id=session_id, text=text)

    # salva assistant message
    append_message(user_id, chat_id, "assistant", reply)

    # IMPORTANTISSIMO: il frontend si aspetta "reply" stringa
    return jsonify({
        "reply": reply,
        "source": "brain",
        # extra compatibilità (se da qualche parte usi message.role/text)
        "message": {"role": "assistant", "text": reply}
    })


if __name__ == "__main__":
    # server su 5500 come il tuo setup attuale
    app.run(host="127.0.0.1", port=5500, debug=True)


# --- VALIDATION PATCH (FREE PLAN) ---
ALLOWED_TIMEFRAMES = ['1m','3m','5m','15m','30m','1h','4h','1d']
FREE_STRATEGIES = ['atr','ema','rsi']

def validate_timeframe(tf):
    return tf in ALLOWED_TIMEFRAMES

def validate_strategy(name):
    return name.lower() in FREE_STRATEGIES

def validate_pair(symbol, bybit_client=None):
    try:
        if not bybit_client:
            return True
        symbols = bybit_client.get_symbols()
        return symbol.upper() in symbols
    except Exception:
        return False

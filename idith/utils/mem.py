# idith/utils/mem.py
import os, json, time

ROOT = os.path.dirname(os.path.dirname(__file__)) # .../idith
MEM = os.path.join(ROOT, "memory")
SESS = os.path.join(MEM, "sessions")
os.makedirs(SESS, exist_ok=True)
os.makedirs(MEM, exist_ok=True)

PROFILE_PATH = os.path.join(MEM, "profile.json")

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# --------- PROFILO (persistente) ---------
def load_profile():
    # valori di default per tono/stile
    default = {
        "lang": "it",
        "tone": "empatico-naturale",
        "formality": "tu",
        "name": "Idith",
        "last_strategy": None,
        "last_timeframe": None,
        "updated_at": int(time.time())
    }
    data = _read_json(PROFILE_PATH, default)
    return data

def save_profile(patch: dict):
    prof = load_profile()
    prof.update(patch)
    prof["updated_at"] = int(time.time())
    _write_json(PROFILE_PATH, prof)
    return prof

# --------- SESSIONI (persistenti) ---------
def _sess_path(session_id: str):
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return os.path.join(SESS, f"{safe}.json")

def load_session(session_id: str):
    default = {
        "session_id": session_id,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
        "history": [] # [{role:"user/assistant", text:"..."}]
    }
    return _read_json(_sess_path(session_id), default)

def save_session(state: dict, append: dict | None = None):
    if append:
        state.setdefault("history", []).append(append)
    state["updated_at"] = int(time.time())
    _write_json(_sess_path(state["session_id"]), state)
    return state

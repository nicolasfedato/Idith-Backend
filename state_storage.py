# state_storage.py
import os
import json
from datetime import datetime
from typing import Dict, Any

from . import chat_storage

def _default_state() -> Dict[str, Any]:
    return {
        "step": "intent",
        "intent": None,
        "pair": None,
        "timeframe": None,
        "strategy": None,
        "indicators": [],
        "risk_profile": None,
        "sl_pct": None,
        "tp_pct": None,
        "order_size_usdt": None,
        "updated_at": datetime.utcnow().isoformat()
    }

def state_path(user_id: str, chat_id: str) -> str:
    # ensure chat folder exists
    chat_storage.ensure_chat_files(user_id, chat_id)
    # Usa normalize_user_id per garantire formato corretto (user_xxx)
    normalized_user_id = chat_storage.normalize_user_id(user_id)
    normalized_chat_id = chat_storage.normalize_chat_id(chat_id)
    cdir = os.path.join(os.path.dirname(__file__), "users", normalized_user_id, "chats", normalized_chat_id)
    return os.path.join(cdir, "state.json")

def load_state(user_id: str, chat_id: str) -> Dict[str, Any]:
    path = state_path(user_id, chat_id)
    if not os.path.exists(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_state()
        return {**_default_state(), **data}
    except Exception:
        return _default_state()

def save_state(user_id: str, chat_id: str, state: Dict[str, Any]) -> None:
    path = state_path(user_id, chat_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(state or {})
    payload["updated_at"] = datetime.utcnow().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def reset_state(user_id: str, chat_id: str) -> None:
    path = state_path(user_id, chat_id)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

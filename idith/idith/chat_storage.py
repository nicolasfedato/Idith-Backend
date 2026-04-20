import os
import json
import shutil
from datetime import datetime
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(__file__)
USERS_DIR = os.path.join(BASE_DIR, "users")

def _safe_name(s: str) -> str:
    s = (s or "").strip()
    # keep spaces (your folders are "Nuova chat 41"), but remove slashes etc.
    for ch in ["/", "\\", ":", "*", "?", "\"", "<", ">", "|", "@", "."]:
        s = s.replace(ch, "_")
    return s

def normalize_user_id(raw_user_id: str | None) -> str:
    raw = (raw_user_id or "").strip()
    if not raw:
        raise ValueError("Missing user_id")
    if "@" in raw:
        raise ValueError("Email passed as user_id")
    uid = _safe_name(raw)
    if not uid or uid.lower() in ("anonymous", "anon", "guest", "web-anonymous"):
        return "anonymous"
    # keep prefix if already present
    if not uid.startswith("user_"):
        uid = f"user_{uid}"
    return uid

def normalize_chat_id(raw_chat_id: str | None) -> str:
    cid = _safe_name(raw_chat_id or "")
    return cid or "Nuova chat"

def migrate_legacy_for_user(user_id: str) -> None:
    """Assicura che la struttura cartelle per l'utente esista (no-op se già presente)."""
    _user_folder(user_id)

def copy_user_template(user_id: str, email: str) -> None:
    """
    Copia template utente per nuovo account.
    Crea struttura completa da _template_user.
    """
    normalized_user_id = normalize_user_id(user_id)
    template_dir = os.path.join(USERS_DIR, "_template_user")
    user_dir = os.path.join(USERS_DIR, normalized_user_id)

    if not os.path.exists(template_dir):
        print(f"[NEW_USER_INIT] WARNING: template not found at {template_dir}, creating basic structure")
        _user_folder(normalized_user_id)
        return

    if os.path.exists(user_dir):
        print(f"[NEW_USER_INIT] User directory already exists: {user_dir}, skipping template copy")
        return

    try:
        # Copia ricorsiva del template
        shutil.copytree(template_dir, user_dir)
        print(f"[NEW_USER_INIT] template copied -> users/{normalized_user_id}")

        # ✅ Rimuove eventuale chat seed dal template (la seed viene creata via codice una sola volta al signup)
        seed_dir = os.path.join(user_dir, "chats", "Nuova chat 1")
        if os.path.exists(seed_dir):
            shutil.rmtree(seed_dir, ignore_errors=True)

        # Aggiorna profile.json con dati utente
        profile_path = os.path.join(user_dir, "profile.json")
        if os.path.exists(profile_path):
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)

            # Estrai username dalla email (parte prima della @)
            username = email.split("@")[0] if "@" in email else email

            profile.update({
                "user_id": normalized_user_id,
                "username": username,
                "email": email,
                "plan": "free",
                "created_at": datetime.utcnow().isoformat()
            })

            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump(profile, f, ensure_ascii=False, indent=2)

            print(f"[NEW_USER_INIT] profile.json updated with user_id={normalized_user_id}, email={email}, username={username}")

        print(f"[NEW_USER_INIT] default chat created: Nuova chat 1")
    except Exception as e:
        print(f"[NEW_USER_INIT] ERROR copying template: {e}")
        raise


def ensure_user_initialized(user_id: str, email: str | None = None) -> None:
    """
    Ensure that a Supabase-authenticated user has the local filesystem structure Idith expects:
    - users/<uid>/profile.json
    - users/<uid>/chats/...
    - at least one chat: 'Nuova chat 1'
    This restores the previous behavior that existed at account creation.
    """
    normalized_user_id = normalize_user_id(user_id)
    _user_folder(normalized_user_id)

    # profile
    profile_path = os.path.join(USERS_DIR, normalized_user_id, "profile.json")
    if not os.path.exists(profile_path):
        username = (email.split("@")[0] if email and "@" in email else (email or "user"))
        profile = {
            "user_id": normalized_user_id,
            "username": username,
            "email": email or "",
            "plan": "free",
            "created_at": datetime.utcnow().isoformat()
        }
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, ensure_ascii=False, indent=2)
        print(f"[USER_INIT] profile.json created for {normalized_user_id}")

    # chats folder and default chat
    chats = []
    try:
        chats = list_chats(normalized_user_id)
    except Exception:
        chats = []

    if len(chats) == 0:
        default_chat = "Nuova chat 1"
        ensure_chat_files(normalized_user_id, default_chat)
        print(f"[USER_INIT] default chat ensured: {default_chat}")


def _user_folder(user_id: str) -> str:
    os.makedirs(USERS_DIR, exist_ok=True)
    p = os.path.join(USERS_DIR, normalize_user_id(user_id))
    os.makedirs(p, exist_ok=True)
    # standard subfolders
    for sub in ("chats", "configs", "exports", "logs", "sessions"):
        os.makedirs(os.path.join(p, sub), exist_ok=True)
    # profile file (optional)
    profile = os.path.join(p, "profile.json")
    if not os.path.exists(profile):
        with open(profile, "w", encoding="utf-8") as f:
            json.dump({"created_at": datetime.utcnow().isoformat()}, f, ensure_ascii=False, indent=2)
    return p

def _chat_folder(user_id: str, chat_id: str) -> str:
    uf = _user_folder(user_id)
    chats_dir = os.path.join(uf, "chats")
    cf = os.path.join(chats_dir, normalize_chat_id(chat_id))
    os.makedirs(cf, exist_ok=True)
    return cf

def ensure_chat_files(user_id: str, chat_id: str) -> None:
    cf = _chat_folder(user_id, chat_id)
    mp = os.path.join(cf, "messages.json")
    if not os.path.exists(mp):
        with open(mp, "w", encoding="utf-8") as f:
            json.dump({"title": chat_id, "created_at": datetime.utcnow().isoformat(), "messages": []},
                      f, ensure_ascii=False, indent=2)
    sp = os.path.join(cf, "state.json")
    if not os.path.exists(sp):
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
    ep = os.path.join(cf, "events.json")
    if not os.path.exists(ep):
        with open(ep, "w", encoding="utf-8") as f:
            json.dump({"title": chat_id, "created_at": datetime.utcnow().isoformat(), "events": []},
                      f, ensure_ascii=False, indent=2)

def load_chat(user_id: str, chat_id: str) -> Dict[str, Any]:
    mp = os.path.join(_chat_folder(user_id, chat_id), "messages.json")
    if not os.path.exists(mp):
        return {"title": chat_id, "created_at": datetime.utcnow().isoformat(), "messages": [], "not_found": True}
    try:
        with open(mp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"title": chat_id, "created_at": datetime.utcnow().isoformat(), "messages": []}

def save_message(user_id: str, chat_id: str, role: str, text: str) -> bool:
    ensure_chat_files(user_id, chat_id)
    cf = _chat_folder(user_id, chat_id)
    mp = os.path.join(cf, "messages.json")

    try:
        data = load_chat(user_id, chat_id)
        if "messages" not in data or not isinstance(data["messages"], list):
            data["messages"] = []
        data["messages"].append({
            "role": role,
            "text": text,
            "ts": datetime.utcnow().isoformat()
        })
        # atomic write
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, mp)
        return True
    except Exception as e:
        print("[chat_storage] save_message error:", e)
        return False

def save_chat_messages(user_id: str, chat_id: str, messages: List[Dict[str, Any]]) -> None:
    """Save a list of messages to chat"""
    ensure_chat_files(user_id, chat_id)
    cf = _chat_folder(user_id, chat_id)
    mp = os.path.join(cf, "messages.json")

    try:
        data = load_chat(user_id, chat_id)
        data["messages"] = messages if isinstance(messages, list) else []
        # atomic write
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, mp)
    except Exception as e:
        print(f"[chat_storage] save_chat_messages error: {e}")

def _is_empty_chat(user_id: str, chat_id: str) -> bool:
    mp = os.path.join(_chat_folder(user_id, chat_id), "messages.json")
    if not os.path.exists(mp):
        return True
    try:
        with open(mp, "r", encoding="utf-8") as f:
            data = json.load(f)
        msgs = data.get("messages", [])
        return isinstance(msgs, list) and len(msgs) == 0
    except Exception:
        return True

def list_chats(user_id: str) -> List[str]:
    uf = _user_folder(user_id)
    chats_dir = os.path.join(uf, "chats")
    try:
        items = []
        for name in os.listdir(chats_dir):
            p = os.path.join(chats_dir, name)
            if os.path.isdir(p):
                items.append(name)
        # newest first if possible (by folder mtime)
        items.sort(key=lambda n: os.path.getmtime(os.path.join(chats_dir, n)), reverse=True)
        return items
    except Exception as e:
        print("[chat_storage] list_chats error:", e)
        return []

def delete_chat(user_id: str, chat_id: str) -> bool:
    try:
        # Costruisce il path senza creare la cartella (a differenza di _chat_folder)
        uf = _user_folder(user_id)
        chats_dir = os.path.join(uf, "chats")
        cf = os.path.join(chats_dir, normalize_chat_id(chat_id))

        if os.path.exists(cf) and os.path.isdir(cf):
            shutil.rmtree(cf, ignore_errors=False)
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        print("[chat_storage] delete_chat error:", e)
        return False

def rename_chat(user_id: str, old_chat_id: str, new_chat_id: str) -> Dict[str, Any]:
    try:
        # Costruisce i path senza creare le cartelle
        uf = _user_folder(user_id)
        chats_dir = os.path.join(uf, "chats")
        old_normalized = normalize_chat_id(old_chat_id)
        new_normalized = normalize_chat_id(new_chat_id)
        old_path = os.path.join(chats_dir, old_normalized)
        new_path = os.path.join(chats_dir, new_normalized)

        # Se la cartella normalizzata non esiste, prova fallback: cerca tra tutte le cartelle
        if not os.path.exists(old_path) or not os.path.isdir(old_path):
            # Fallback: cerca una cartella che matcha dopo normalizzazione
            if os.path.exists(chats_dir):
                existing_chats = [name for name in os.listdir(chats_dir)
                                if os.path.isdir(os.path.join(chats_dir, name))]
                for existing_name in existing_chats:
                    if normalize_chat_id(existing_name) == old_normalized:
                        old_path = os.path.join(chats_dir, existing_name)
                        old_normalized = existing_name
                        break
                else:
                    # Se ancora non trovata, ritorna errore con lista chat disponibili (per debug)
                    available = existing_chats[:10]  # max 10 per non intasare
                    return {
                        "ok": False,
                        "error": f"Chat da rinominare non trovata (cercato: '{old_normalized}')",
                        "old_chat_id": old_normalized,
                        "new_chat_id": new_normalized,
                        "available_chats": available
                    }

        if os.path.exists(new_path):
            return {"ok": False, "error": "Esiste già una chat con questo nome", "old_chat_id": old_normalized, "new_chat_id": new_normalized}

        # Rinomina la cartella
        os.rename(old_path, new_path)

        # Aggiorna il campo "title" nei file JSON se presente
        messages_path = os.path.join(new_path, "messages.json")
        if os.path.exists(messages_path):
            try:
                with open(messages_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["title"] = new_chat_id
                with open(messages_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass  # ignora errori nell'aggiornamento del title

        events_path = os.path.join(new_path, "events.json")
        if os.path.exists(events_path):
            try:
                with open(events_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["title"] = new_chat_id
                with open(events_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass  # ignora errori nell'aggiornamento del title

        return {"ok": True, "old_chat_id": old_normalized, "new_chat_id": new_normalized}
    except Exception as e:
        print(f"[chat_storage] rename_chat error: {e}")
        return {"ok": False, "error": str(e), "old_chat_id": old_chat_id, "new_chat_id": new_chat_id}

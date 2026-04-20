import json
import uuid
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any

# Directory base dove verranno create le cartelle utente
USERS_ROOT = Path("users")
USERS_ROOT.mkdir(parents=True, exist_ok=True)

# File indice globale: email -> user_id
INDEX_FILE = Path("users_index.json")

# URL base del frontend (da sostituire quando avrai dominio reale)
FRONTEND_BASE_URL = "http://localhost:8000"  # modifica quando sarà necessario


# -------------------- Utility tempo / JSON --------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


# -------------------- Gestione indice globale --------------------

def _load_index() -> Dict[str, Any]:
    if not INDEX_FILE.exists():
        return {"email_to_id": {}}
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        if "email_to_id" not in data:
            data["email_to_id"] = {}
        return data
    except Exception:
        return {"email_to_id": {}}


def _save_index(data: Dict[str, Any]) -> None:
    data.setdefault("email_to_id", {})
    INDEX_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# -------------------- Gestione profilo utente --------------------

def _user_folder(user_id: str) -> Path:
    return USERS_ROOT / user_id


def _profile_path(user_id: str) -> Path:
    return _user_folder(user_id) / "profile.json"


def _load_profile(user_id: str) -> Optional[Dict[str, Any]]:
    p = _profile_path(user_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_profile(user_id: str, profile: Dict[str, Any]) -> None:
    folder = _user_folder(user_id)
    folder.mkdir(parents=True, exist_ok=True)

    # Crea le sottocartelle standard
    for sub in ("chats", "sessions", "configs", "exports", "logs"):
        (folder / sub).mkdir(exist_ok=True)

    _profile_path(user_id).write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")


def _generate_user_id() -> str:
    return "user_" + uuid.uuid4().hex


def _hash_password(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# -------------------- Email (placeholder) --------------------

def send_verification_email(email: str, token: str):
    """
    Placeholder: invio email disabilitato.
    """
    print("[EMAIL DEBUG] Invio email disabilitato")

def send_account_deleted_email(email: str):
    """
    Placeholder: stampa a console invece di inviare una mail reale.
    """
    print("\n[EMAIL DEBUG] Account eliminato")
    print("Destinatario:", email)
    print("Il tuo account e stato cancellato correttamente.")
    print("---------\n")


# -------------------- Cleanup pending scaduti --------------------

def cleanup_expired_pending_users():
    index = _load_index()
    mapping = index.get("email_to_id", {})
    now = _now_utc()
    changed = False

    for email, user_id in list(mapping.items()):
        profile = _load_profile(user_id)
        if not profile:
            mapping.pop(email, None)
            changed = True
            continue

        if profile.get("status") != "pending":
            continue

        expires = profile.get("expires_at")
        if not expires:
            continue

        try:
            exp_dt = _from_iso(expires)
        except Exception:
            continue

        if exp_dt < now:
            folder = _user_folder(user_id)
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)

            mapping.pop(email, None)
            changed = True

    if changed:
        _save_index(index)


# -------------------- Registrazione --------------------

def register_user(email: str, password: str) -> Dict[str, Any]:
    cleanup_expired_pending_users()

    email_norm = email.strip().lower()
    if not email_norm or not password:
        raise ValueError("Email e password sono obbligatorie.")

    index = _load_index()
    mapping = index.setdefault("email_to_id", {})

    now = _now_utc()

    if email_norm in mapping:
        # Utente gia esistente
        user_id = mapping[email_norm]
        profile = _load_profile(user_id)

        if not profile:
            # Mapping sporco
            mapping.pop(email_norm, None)
        else:
            if profile.get("status") == "active":
                raise ValueError("Email gia registrata. Effettua il login.")

            profile.update({
                "email": email_norm,
                "password_hash": _hash_password(password),
                "status": "active",
                "verified_at": _to_iso(now),
                "deleted_at": None
            })
            profile.pop("verify_token", None)
            profile.pop("expires_at", None)
            _save_profile(user_id, profile)
            _save_index(index)

            return {
                "status": "ok",
                "message": "Registrazione completata.",
                "userId": user_id,
                "email": email_norm
            }

    # Nuova registrazione
    user_id = _generate_user_id()
    mapping[email_norm] = user_id

    profile = {
        "user_id": user_id,
        "email": email_norm,
        "password_hash": _hash_password(password),
        "status": "active",
        "created_at": _to_iso(now),
        "verified_at": _to_iso(now),
        "deleted_at": None,
        "plan": "free"
    }

    _save_profile(user_id, profile)
    _save_index(index)

    return {
        "status": "ok",
        "message": "Registrazione completata.",
        "userId": user_id,
        "email": email_norm
    }


# -------------------- Verifica account --------------------

def verify_account(token: str) -> Dict[str, Any]:
    """
    Verifica account disabilitata.
    """
    return {"status": "error", "message": "Verifica disabilitata."}


# -------------------- Login --------------------

def login_user(email: str, password: str) -> Dict[str, Any]:
    """
    Login di base:
    - consente accesso se email+password sono corretti
    - se deleted → errore
    """
    cleanup_expired_pending_users()

    email_norm = (email or "").strip().lower()
    index = _load_index()
    mapping = index.get("email_to_id", {})

    user_id = mapping.get(email_norm)
    if not user_id:
        raise ValueError("Email o password errati.")

    profile = _load_profile(user_id)
    if not profile:
        raise ValueError("Email o password errati.")

    status = profile.get("status")

    if status == "deleted":
        raise ValueError("Questo account e stato eliminato. Registrati di nuovo con questa email per crearne uno nuovo.")

    if profile.get("password_hash") != _hash_password(password):
        raise ValueError("Email o password errati.")

    return {
        "status": "ok",
        "userId": user_id,
        "email": email_norm,
        "plan": profile.get("plan", "free"),
    }


# -------------------- Eliminazione account --------------------

def delete_account(user_id: Optional[str] = None, email: Optional[str] = None) -> Dict[str, Any]:
    cleanup_expired_pending_users()

    index = _load_index()
    mapping = index.get("email_to_id", {})

    if email:
        email = email.strip().lower()

    if not user_id and email:
        user_id = mapping.get(email)

    if not user_id:
        return {"status": "error", "message": "Utente non trovato."}

    profile = _load_profile(user_id)
    if not profile:
        if email in mapping:
            mapping.pop(email, None)
            _save_index(index)
        return {"status": "ok", "message": "Account già eliminato."}

    user_email = profile.get("email")

    # Marca come eliminato
    profile["status"] = "deleted"
    profile["deleted_at"] = _to_iso(_now_utc())
    _save_profile(user_id, profile)

    # Rimuovi cartella
    folder = _user_folder(user_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)

    # Rimuovi mapping
    if user_email in mapping:
        mapping.pop(user_email)
        _save_index(index)

    # Invia email
    send_account_deleted_email(user_email)

    return {"status": "ok", "message": "Account eliminato correttamente."}

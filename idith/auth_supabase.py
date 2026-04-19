import os
import requests
from dotenv import load_dotenv
from fastapi import Header, HTTPException

# Load env from idith-backend/idith/.env (same place where you keep OPENAI + Bybit vars)
BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE_DIR, ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    # We don't raise here to allow app startup; endpoints will error clearly.
    print("[SUPABASE_AUTH] WARNING: SUPABASE_URL or SUPABASE_ANON_KEY missing in idith/.env")

def get_current_user(authorization: str = Header(None)):
    """Validate Supabase JWT (access_token) and return user JSON."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization Bearer token")

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise HTTPException(status_code=500, detail="Supabase env missing (SUPABASE_URL / SUPABASE_ANON_KEY)")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")

    r = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": SUPABASE_ANON_KEY
        },
        timeout=10
    )

    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Supabase token")

    return r.json()

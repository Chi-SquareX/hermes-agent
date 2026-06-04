import json
import os
import signal
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from gauth import build_google_login_url, verify_google_oauth_callback

load_dotenv(override=True)
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

app = FastAPI()

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/callback")
AUTO_KILL_AFTER_SECONDS = float(os.getenv("AUTO_KILL_AFTER_SECONDS", "300"))
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
TOKEN_FILE = Path("google_token.json")
STATE_FILE = Path(".oauth_state.json")

_oauth_state: str | None = None


def _stop_self(delay_seconds: float = 0.2):
    time.sleep(delay_seconds)
    os.kill(os.getpid(), signal.SIGINT)


def _auto_kill_worker():
    time.sleep(AUTO_KILL_AFTER_SECONDS)
    os.kill(os.getpid(), signal.SIGINT)

@app.on_event("startup")
def _start_auto_kill_timer():
    threading.Thread(target=_auto_kill_worker, daemon=True).start()


@app.get("/login")
def login():
    global _oauth_state

    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Missing Google OAuth env vars")

    login_url, state = build_google_login_url(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scopes=SCOPES,
    )
    _oauth_state = state
    STATE_FILE.write_text(state, encoding="utf-8")
    return {"login_url": login_url}


@app.get("/auth/callback")
def auth_callback(request: Request, background_tasks: BackgroundTasks):
    state = _oauth_state
    if not state and STATE_FILE.exists():
        state = STATE_FILE.read_text(encoding="utf-8").strip()
    if not state:
        raise HTTPException(status_code=400, detail="OAuth state not found. Call /login first.")
    if request.query_params.get("error"):
        raise HTTPException(
            status_code=400,
            detail=f"Google OAuth error: {request.query_params.get('error')}",
        )

    try:
        token_data = verify_google_oauth_callback(
            callback_url=str(request.url),
            expected_state=state,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scopes=SCOPES,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"OAuth callback failed: {exc}")

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)
    if STATE_FILE.exists():
        STATE_FILE.unlink()

    background_tasks.add_task(_stop_self)
    return {"message": "Token saved", "token_file": str(TOKEN_FILE)}

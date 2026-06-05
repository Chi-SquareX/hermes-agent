import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import base64
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

mcp = FastMCP("hermes-oauth")

APP_HOST = "127.0.0.1"
APP_PORT = 8000
APP_URL = f"http://{APP_HOST}:{APP_PORT}"
APP_LOGIN_URL = f"{APP_URL}/login"
APP_FILE = Path(__file__).with_name("app.py")
TOKEN_FILE = Path(__file__).with_name("google_token.json")

_app_process: subprocess.Popen | None = None


def _is_app_up() -> bool:
    try:
        with urllib.request.urlopen(APP_LOGIN_URL, timeout=1.5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def _kill_any_app_on_port() -> None:
    try:
        out = subprocess.check_output(["lsof", "-ti", f"tcp:{APP_PORT}"], text=True).strip()
    except Exception:
        return
    for pid_text in out.splitlines():
        if pid_text.strip():
            try:
                os.kill(int(pid_text.strip()), 15)
            except Exception:
                pass


def _start_fastapi_subprocess(auto_kill_after_seconds: float) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            APP_HOST,
            "--port",
            str(APP_PORT),
        ],
        cwd=str(APP_FILE.parent),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "AUTO_KILL_AFTER_SECONDS": str(auto_kill_after_seconds),
            "OAUTHLIB_INSECURE_TRANSPORT": "1",
        },
    )


def _get_creds() -> Credentials:
    if not TOKEN_FILE.exists():
        raise ValueError("google_token.json not found. Run OAuth first.")
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(data, data.get("scopes", []))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


@mcp.tool()
def start_google_oauth_and_get_login_url(
    startup_timeout_seconds: float = 12.0,
    auto_kill_after_seconds: float = 900.0,
    force_restart: bool = True,
) -> dict:
    global _app_process

    if startup_timeout_seconds <= 0 or auto_kill_after_seconds <= 0:
        return {"ok": False, "error": "Timeout values must be > 0"}

    if _app_process is not None and _app_process.poll() is not None:
        _app_process = None

    if force_restart:
        _kill_any_app_on_port()
        _app_process = None

    if not _is_app_up():
        _app_process = _start_fastapi_subprocess(auto_kill_after_seconds)
        deadline = time.time() + startup_timeout_seconds

        while time.time() < deadline:
            if _app_process.poll() is not None:
                return {
                    "ok": False,
                    "error": "FastAPI app exited before startup.",
                    "returncode": _app_process.returncode,
                }
            if _is_app_up():
                break
            time.sleep(0.25)
        else:
            _app_process.terminate()
            _app_process = None
            return {"ok": False, "error": "FastAPI app did not start in time."}

    try:
        with urllib.request.urlopen(APP_LOGIN_URL, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Failed to fetch /login: {exc}"}

    return {
        "ok": True,
        "login_url": payload.get("login_url"),
        "app_pid": _app_process.pid if _app_process is not None else None,
        "app_base_url": APP_URL,
        "auto_kill_after_seconds": auto_kill_after_seconds,
    }


@mcp.tool()
def has_access_token() -> dict:
    if not TOKEN_FILE.exists():
        return {"ok": True, "has_token": False}
    data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    expiry = data.get("expiry")
    is_expired = False
    if expiry:
        is_expired = datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
    if is_expired and data.get("refresh_token"):
        try:
            creds = Credentials.from_authorized_user_info(data, data.get("scopes", []))
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
            data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
            expiry = data.get("expiry")
            is_expired = False
            if expiry:
                is_expired = datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except Exception:
            pass
    return {"ok": True, "has_token": bool(data.get("token")), "expiry": expiry, "is_expired": is_expired}


@mcp.tool()
def schedule_meet(
    summary: str,
    start_iso: str,
    end_iso: str,
    guest_emails: list[str],
    description: str = "",
    timezone_name: str = "Asia/Kolkata",
) -> dict:
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": timezone_name},
        "end": {"dateTime": end_iso, "timeZone": timezone_name},
        "attendees": [{"email": e} for e in guest_emails],
        "conferenceData": {
            "createRequest": {
                "requestId": f"meet-{int(time.time() * 1000)}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }
    created = build("calendar", "v3", credentials=_get_creds()).events().insert(
        calendarId="primary", body=event, sendUpdates="all", conferenceDataVersion=1
    ).execute()
    meet_link = (
        (created.get("conferenceData") or {}).get("entryPoints", [{}])[0].get("uri")
    )
    return {"ok": True, "event_id": created.get("id"), "event_link": created.get("htmlLink"), "meet_link": meet_link}


@mcp.tool()
def send_email(to_emails: list[str], subject: str, body: str) -> dict:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = ", ".join(to_emails)
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = build("gmail", "v1", credentials=_get_creds()).users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    return {"ok": True, "message_id": sent.get("id")}


@mcp.tool()
def read_emails(query: str = "", max_results: int = 10) -> dict:
    gmail = build("gmail", "v1", credentials=_get_creds()).users().messages()
    listed = gmail.list(userId="me", q=query, maxResults=max(1, min(max_results, 50))).execute()
    items = []
    for m in listed.get("messages", []):
        full = gmail.get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject"]).execute()
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        items.append({"id": m["id"], "from": headers.get("From", ""), "subject": headers.get("Subject", ""), "snippet": full.get("snippet", "")})
    return {"ok": True, "emails": items}


@mcp.tool()
def archive_email(message_id: str) -> dict:
    build("gmail", "v1", credentials=_get_creds()).users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()
    return {"ok": True, "archived": True, "message_id": message_id}


if __name__ == "__main__":
    mcp.run()

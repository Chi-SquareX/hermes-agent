import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import base64
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

from profiles import get_creds, list_profiles, profile_token_status, sanitize_profile_id

mcp = FastMCP("hermes-oauth")

APP_FILE = Path(__file__).with_name("app.py")
load_dotenv(APP_FILE.with_name(".env"), override=True)

APP_HOST = "127.0.0.1"
APP_PORT = int(os.getenv("HERMES_PORT", "8010"))
APP_URL = f"http://{APP_HOST}:{APP_PORT}"
APP_LOGIN_URL = f"{APP_URL}/login"

_app_process: subprocess.Popen | None = None


def _probe_app() -> tuple[bool, str | None]:
    try:
        with urllib.request.urlopen(f"{APP_URL}/api/profiles", timeout=1.5) as response:
            return response.status == 200, None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, f"/api/profiles returned HTTP {exc.code}: {body[:500]}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"App is not reachable: {exc}"


def _is_app_up() -> bool:
    ok, _ = _probe_app()
    return ok


def _kill_any_app_on_port() -> None:
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
            )
        except Exception:
            return
        pids: set[int] = set()
        for line in out.splitlines():
            if f":{APP_PORT}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    try:
                        pids.add(int(parts[-1]))
                    except ValueError:
                        pass
        for pid in pids:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for _ in range(10):
            still = [
                line
                for line in subprocess.check_output(["netstat", "-ano"], text=True).splitlines()
                if f":{APP_PORT}" in line and "LISTENING" in line
            ]
            if not still:
                break
            time.sleep(0.3)
        return

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
            "--reload",
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
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
    )


def _is_expired(expiry: str | None) -> bool:
    if not expiry:
        return False
    expiry_time = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    if expiry_time.tzinfo is None:
        expiry_time = expiry_time.replace(tzinfo=timezone.utc)
    return expiry_time <= datetime.now(timezone.utc)


@mcp.tool()
def list_email_profiles() -> dict:
    profiles = list_profiles(refresh_expired=True)
    return {"ok": True, "count": len(profiles), "profiles": profiles}


@mcp.tool()
def start_google_oauth_and_get_login_url(
    profile_id: str = "",
    startup_timeout_seconds: float = 12.0,
    auto_kill_after_seconds: float = 900.0,
    force_restart: bool = True,
) -> dict:
    global _app_process

    if startup_timeout_seconds <= 0 or auto_kill_after_seconds <= 0:
        return {"ok": False, "error": "Timeout values must be > 0"}

    try:
        resolved_profile_id = sanitize_profile_id(profile_id) if profile_id.strip() else ""
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if _app_process is not None and _app_process.poll() is not None:
        _app_process = None

    if force_restart:
        _kill_any_app_on_port()
        _app_process = None

    if not _is_app_up():
        _app_process = _start_fastapi_subprocess(auto_kill_after_seconds)
        deadline = time.time() + startup_timeout_seconds
        last_error = None

        while time.time() < deadline:
            if _app_process.poll() is not None:
                return {
                    "ok": False,
                    "error": "FastAPI app exited before startup.",
                    "returncode": _app_process.returncode,
                }
            is_up, last_error = _probe_app()
            if is_up:
                break
            time.sleep(0.25)
        else:
            _app_process.terminate()
            _app_process = None
            return {"ok": False, "error": "FastAPI app did not start in time.", "last_error": last_error}

    login_url = f"{APP_LOGIN_URL}?{urllib.parse.urlencode({'profile_id': resolved_profile_id})}"
    try:
        with urllib.request.urlopen(login_url, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Failed to fetch /login: {exc}"}

    return {
        "ok": True,
        "profile_id": resolved_profile_id,
        "login_url": payload.get("login_url"),
        "dashboard_url": APP_URL,
        "app_pid": _app_process.pid if _app_process is not None else None,
        "app_base_url": APP_URL,
        "auto_kill_after_seconds": auto_kill_after_seconds,
    }


@mcp.tool()
def has_access_token(profile_id: str = "", email: str = "") -> dict:
    profiles = list_profiles()
    if not profiles:
        return {"ok": True, "has_token": False, "profiles": []}

    if profile_id or email:
        try:
            status = profile_token_status(
                profile_id=profile_id or None,
                email=email or None,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "has_token": status["has_token"],
            "profile_id": status["profile_id"],
            "email": status["email"],
            "expiry": status["expiry"],
            "is_expired": status["is_expired"],
        }

    active = [p for p in profiles if p["has_token"] and not p["is_expired"]]
    return {
        "ok": True,
        "has_token": bool(active),
        "profile_count": len(profiles),
        "active_profile_count": len(active),
        "profiles": profiles,
    }


@mcp.tool()
def schedule_meet(
    summary: str,
    start_iso: str,
    end_iso: str,
    guest_emails: list[str],
    description: str = "",
    timezone_name: str = "Asia/Kolkata",
    profile_id: str = "",
    email: str = "",
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
    created = build("calendar", "v3", credentials=get_creds(profile_id=profile_id or None, email=email or None)).events().insert(
        calendarId="primary", body=event, sendUpdates="all", conferenceDataVersion=1
    ).execute()
    meet_link = (
        (created.get("conferenceData") or {}).get("entryPoints", [{}])[0].get("uri")
    )
    return {"ok": True, "event_id": created.get("id"), "event_link": created.get("htmlLink"), "meet_link": meet_link}


@mcp.tool()
def send_email(
    to_emails: list[str],
    subject: str,
    body: str,
    profile_id: str = "",
    email: str = "",
) -> dict:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = ", ".join(to_emails)
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = build("gmail", "v1", credentials=get_creds(profile_id=profile_id or None, email=email or None)).users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    return {"ok": True, "message_id": sent.get("id")}


@mcp.tool()
def read_emails(
    query: str = "",
    max_results: int = 10,
    profile_id: str = "",
    email: str = "",
) -> dict:
    gmail = build("gmail", "v1", credentials=get_creds(profile_id=profile_id or None, email=email or None)).users().messages()
    listed = gmail.list(userId="me", q=query, maxResults=max(1, min(max_results, 50))).execute()
    items = []
    for m in listed.get("messages", []):
        full = gmail.get(userId="me", id=m["id"], format="metadata", metadataHeaders=["From", "Subject"]).execute()
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        items.append({"id": m["id"], "from": headers.get("From", ""), "subject": headers.get("Subject", ""), "snippet": full.get("snippet", "")})
    return {"ok": True, "emails": items}


@mcp.tool()
def archive_email(message_id: str, profile_id: str = "", email: str = "") -> dict:
    build("gmail", "v1", credentials=get_creds(profile_id=profile_id or None, email=email or None)).users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
    ).execute()
    return {"ok": True, "archived": True, "message_id": message_id}


if __name__ == "__main__":
    mcp.run()

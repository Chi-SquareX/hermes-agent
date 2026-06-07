import json
import re
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

PROFILES_DIR = Path(__file__).with_name("google_profiles")
LEGACY_TOKEN_FILE = Path(__file__).with_name("google_token.json")
INDEX_FILE = PROFILES_DIR / "index.json"
MAX_PROFILES = 50

_PROFILE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def sanitize_profile_id(profile_id: str) -> str:
    cleaned = profile_id.strip().lower().replace("@", "_at_").replace(" ", "-")
    cleaned = re.sub(r"[^a-z0-9._-]", "", cleaned)
    if not cleaned or not _PROFILE_ID_RE.match(cleaned):
        raise ValueError(
            "profile_id must be 1-64 chars: letters, numbers, dots, dashes, underscores"
        )
    return cleaned


def _ensure_profiles_dir() -> None:
    PROFILES_DIR.mkdir(exist_ok=True)


def _token_path(profile_id: str) -> Path:
    return PROFILES_DIR / f"{profile_id}.json"


def _load_index() -> dict:
    _ensure_profiles_dir()
    if not INDEX_FILE.exists():
        return {"profiles": {}}
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


def _save_index(index: dict) -> None:
    _ensure_profiles_dir()
    INDEX_FILE.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _is_expired(expiry: str | None) -> bool:
    if not expiry:
        return False
    expiry_time = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    if expiry_time.tzinfo is None:
        expiry_time = expiry_time.replace(tzinfo=timezone.utc)
    return expiry_time <= datetime.now(timezone.utc)


def _fetch_google_identity(token_data: dict) -> tuple[str, str]:
    creds = Credentials.from_authorized_user_info(token_data, token_data.get("scopes", []))
    info = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
    email = (info.get("email") or "").strip().lower()
    name = (info.get("name") or email or "Unknown").strip()
    if not email:
        raise ValueError("Could not determine Google account email")
    return email, name


def migrate_legacy_token() -> dict | None:
    if not LEGACY_TOKEN_FILE.exists():
        return None
    token_data = json.loads(LEGACY_TOKEN_FILE.read_text(encoding="utf-8"))
    email, name = _fetch_google_identity(token_data)
    profile_id = sanitize_profile_id(email)
    save_profile(profile_id, token_data, email=email, name=name)
    LEGACY_TOKEN_FILE.rename(LEGACY_TOKEN_FILE.with_suffix(".json.migrated"))
    return {"profile_id": profile_id, "email": email, "migrated_from": str(LEGACY_TOKEN_FILE)}


def list_profiles(refresh_expired: bool = False) -> list[dict]:
    migrate_legacy_token()
    index = _load_index()
    profiles = []
    for profile_id, meta in index.get("profiles", {}).items():
        token_path = _token_path(profile_id)
        has_token = token_path.exists()
        expiry = meta.get("expiry")
        is_expired = _is_expired(expiry)
        if refresh_expired and has_token and is_expired and meta.get("has_refresh_token"):
            try:
                get_creds(profile_id)
                meta = _load_index()["profiles"].get(profile_id, meta)
                expiry = meta.get("expiry")
                is_expired = _is_expired(expiry)
            except Exception:
                pass
        profiles.append(
            {
                "profile_id": profile_id,
                "email": meta.get("email", ""),
                "name": meta.get("name", ""),
                "expiry": expiry,
                "is_expired": is_expired,
                "has_token": has_token,
                "connected_at": meta.get("connected_at"),
            }
        )
    profiles.sort(key=lambda p: (p.get("email") or p["profile_id"]).lower())
    return profiles


def resolve_profile_id(profile_id: str | None = None, email: str | None = None) -> str:
    migrate_legacy_token()
    index = _load_index()
    profiles = index.get("profiles", {})

    if profile_id:
        pid = sanitize_profile_id(profile_id)
        if pid not in profiles:
            raise ValueError(f"Profile '{pid}' not found. Use list_email_profiles to see connected accounts.")
        return pid

    if email:
        normalized = email.strip().lower()
        for pid, meta in profiles.items():
            if (meta.get("email") or "").lower() == normalized:
                return pid
        raise ValueError(f"No profile found for email '{normalized}'.")

    if len(profiles) == 1:
        return next(iter(profiles))
    if not profiles:
        raise ValueError("No Google profiles connected. Run OAuth first.")
    raise ValueError(
        "Multiple profiles connected. Pass profile_id or email to choose an account."
    )


def resolve_profile_id_for_oauth(
    requested_profile_id: str | None,
    token_data: dict,
) -> tuple[str, str, str]:
    """Choose a profile slot for OAuth without overwriting a different connected account."""
    email, name = _fetch_google_identity(token_data)
    email = email.strip().lower()

    index = _load_index()
    existing = index.get("profiles", {})

    for pid, meta in existing.items():
        if (meta.get("email") or "").lower() == email:
            return pid, email, name

    explicit = (requested_profile_id or "").strip()
    if explicit:
        pid = sanitize_profile_id(explicit)
        stored_email = (existing.get(pid, {}).get("email") or "").lower()
        if stored_email and stored_email != email:
            raise ValueError(
                f"Profile '{pid}' is already connected to {stored_email}. "
                "Choose a different profile id or sign in with that account."
            )
        return pid, email, name

    if not existing:
        return "default", email, name

    pid = sanitize_profile_id(email)
    stored_email = (existing.get(pid, {}).get("email") or "").lower()
    if stored_email and stored_email != email:
        raise ValueError(
            f"Could not auto-assign profile id for {email}. "
            "Provide a unique profile_id before connecting."
        )
    return pid, email, name


def save_profile(
    profile_id: str,
    token_data: dict,
    email: str | None = None,
    name: str | None = None,
) -> dict:
    pid = sanitize_profile_id(profile_id)
    index = _load_index()
    existing = index.get("profiles", {})

    if pid not in existing and len(existing) >= MAX_PROFILES:
        raise ValueError(f"Maximum of {MAX_PROFILES} profiles reached.")

    if not email or not name:
        fetched_email, fetched_name = _fetch_google_identity(token_data)
        email = email or fetched_email
        name = name or fetched_name

    email = email.strip().lower()
    for other_id, meta in existing.items():
        if other_id != pid and (meta.get("email") or "").lower() == email:
            raise ValueError(f"Email '{email}' is already connected as profile '{other_id}'.")

    _ensure_profiles_dir()
    _token_path(pid).write_text(json.dumps(token_data, indent=2), encoding="utf-8")

    existing[pid] = {
        "email": email,
        "name": name,
        "expiry": token_data.get("expiry"),
        "has_refresh_token": bool(token_data.get("refresh_token")),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    index["profiles"] = existing
    _save_index(index)
    return {"profile_id": pid, "email": email, "name": name}


def get_creds(profile_id: str | None = None, email: str | None = None) -> Credentials:
    pid = resolve_profile_id(profile_id=profile_id, email=email)
    token_path = _token_path(pid)
    if not token_path.exists():
        raise ValueError(f"Token file missing for profile '{pid}'. Re-run OAuth.")

    data = json.loads(token_path.read_text(encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(data, data.get("scopes", []))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        index = _load_index()
        if pid in index.get("profiles", {}):
            refreshed = json.loads(creds.to_json())
            index["profiles"][pid]["expiry"] = refreshed.get("expiry")
            index["profiles"][pid]["has_refresh_token"] = bool(refreshed.get("refresh_token"))
            _save_index(index)
    return creds


def profile_token_status(profile_id: str | None = None, email: str | None = None) -> dict:
    pid = resolve_profile_id(profile_id=profile_id, email=email)
    index = _load_index()
    meta = index.get("profiles", {}).get(pid, {})
    token_path = _token_path(pid)
    if not token_path.exists():
        return {"ok": True, "profile_id": pid, "has_token": False}

    data = json.loads(token_path.read_text(encoding="utf-8"))
    expiry = data.get("expiry") or meta.get("expiry")
    is_expired = _is_expired(expiry)
    if is_expired and data.get("refresh_token"):
        try:
            get_creds(profile_id=pid)
            data = json.loads(token_path.read_text(encoding="utf-8"))
            expiry = data.get("expiry")
            is_expired = _is_expired(expiry)
        except Exception:
            pass

    return {
        "ok": True,
        "profile_id": pid,
        "email": meta.get("email", ""),
        "name": meta.get("name", ""),
        "has_token": bool(data.get("token")),
        "expiry": expiry,
        "is_expired": is_expired,
    }

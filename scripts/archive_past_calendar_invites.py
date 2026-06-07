"""Archive inbox calendar invitation emails whose event end time is in the past."""

from __future__ import annotations

import argparse
import base64
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profiles import get_creds, list_profiles  # noqa: E402

CALENDAR_QUERIES = (
    "in:inbox filename:invite.ics",
    "in:inbox filename:ical.ics",
    "in:inbox from:calendar-notification@google.com",
    "in:inbox subject:invitation has:attachment filename:ics",
)

IST = ZoneInfo("Asia/Kolkata")

SUBJECT_RANGE = re.compile(
    r"@\s*(?:\w{3}\s+)?(?:\w{3}\s+\d{1,2}\s*-\s*)?(?:\w{3}\s+)?"
    r"(\w{3}\s+\d{1,2},\s+\d{4})(?:\s+(\d{1,2}:\d{2}))?(?:\s*-\s*(\d{1,2}:\d{2}))?",
    re.IGNORECASE,
)
TZ_RE = re.compile(r"\(([^)]+)\)\s*$")
DTEND_RE = re.compile(r"^DTEND(?:;[^:]*)?:(.+)$", re.MULTILINE | re.IGNORECASE)
DTSTART_RE = re.compile(r"^DTSTART(?:;[^:]*)?:(.+)$", re.MULTILINE | re.IGNORECASE)


def list_messages(gmail, query: str, max_results: int) -> list[dict]:
    refs: list[dict] = []
    page_token = None
    while len(refs) < max_results:
        resp = (
            gmail.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(100, max_results - len(refs)),
                pageToken=page_token,
            )
            .execute()
        )
        refs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return refs[:max_results]


def decode_b64(data: str) -> str:
    raw = base64.urlsafe_b64decode(data)
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def walk_parts(gmail, msg_id: str, payload: dict, texts: list[str]) -> None:
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    filename = (payload.get("filename") or "").lower()

    if mime == "text/calendar" and body.get("data"):
        texts.append(decode_b64(body["data"]))
    if body.get("attachmentId") and (
        "calendar" in mime or filename.endswith(".ics") or "invite" in filename
    ):
        att = (
            gmail.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=msg_id, id=body["attachmentId"])
            .execute()
        )
        texts.append(decode_b64(att["data"]))

    for part in payload.get("parts", []) or []:
        walk_parts(gmail, msg_id, part, texts)


def parse_ics_dt(value: str, line: str) -> datetime:
    value = value.strip()
    if "VALUE=DATE" in line.upper() or (len(value) == 8 and value.isdigit()):
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)


def end_from_ics(text: str) -> datetime | None:
    for regex in (DTEND_RE, DTSTART_RE):
        match = regex.search(text)
        if not match:
            continue
        try:
            return parse_ics_dt(match.group(1), match.group(0))
        except ValueError:
            continue
    return None


def tz_from_subject(subject: str) -> ZoneInfo:
    match = TZ_RE.search(subject)
    if not match:
        return IST
    token = match.group(1).strip().upper()
    mapping = {
        "IST": "Asia/Kolkata",
        "UTC": "UTC",
        "GMT": "UTC",
        "PST": "America/Los_Angeles",
        "EST": "America/New_York",
    }
    if token in mapping:
        return ZoneInfo(mapping[token])
    try:
        return ZoneInfo(token)
    except Exception:
        return IST


def end_from_subject(subject: str) -> datetime | None:
    match = SUBJECT_RANGE.search(subject)
    if not match:
        return None
    date_str, start_time, end_time = match.group(1), match.group(2), match.group(3)
    tz = tz_from_subject(subject)
    try:
        base = datetime.strptime(date_str.strip(), "%b %d, %Y")
    except ValueError:
        return None

    time_token = end_time or start_time
    if time_token:
        hour, minute = map(int, time_token.split(":"))
        dt = base.replace(hour=hour, minute=minute, tzinfo=tz)
    else:
        dt = base.replace(hour=23, minute=59, tzinfo=tz)
    return dt.astimezone(timezone.utc)


def event_end(gmail, msg: dict) -> tuple[datetime | None, str]:
    subject = next(
        (
            header["value"]
            for header in msg["payload"]["headers"]
            if header["name"].lower() == "subject"
        ),
        "",
    )
    texts: list[str] = []
    walk_parts(gmail, msg["id"], msg["payload"], texts)
    for text in texts:
        dt = end_from_ics(text)
        if dt:
            return dt, "ics"
    dt = end_from_subject(subject)
    if dt:
        return dt, "subject"
    return None, "unknown"


def archive_past_calendar_invites(
    profile_id: str,
    *,
    max_results: int = 500,
    dry_run: bool = False,
) -> dict:
    creds = get_creds(profile_id=profile_id)
    gmail = build("gmail", "v1", credentials=creds)
    now = datetime.now(timezone.utc)

    seen: set[str] = set()
    archived: list[dict] = []
    kept_future: list[dict] = []
    skipped_unknown: list[dict] = []

    for query in CALENDAR_QUERIES:
        for ref in list_messages(gmail, query, max_results):
            if ref["id"] in seen:
                continue
            seen.add(ref["id"])

            msg = (
                gmail.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            subject = next(
                (
                    header["value"]
                    for header in msg["payload"]["headers"]
                    if header["name"].lower() == "subject"
                ),
                "",
            )
            end, source = event_end(gmail, msg)
            entry = {
                "message_id": ref["id"],
                "subject": subject,
                "source": source,
                "event_end": end.isoformat() if end else None,
            }

            if end is None:
                skipped_unknown.append(entry)
                continue
            if end > now:
                kept_future.append(entry)
                continue

            if not dry_run:
                gmail.users().messages().modify(
                    userId="me",
                    id=ref["id"],
                    body={"removeLabelIds": ["INBOX", "UNREAD"]},
                ).execute()
            archived.append(entry)

    return {
        "profile_id": profile_id,
        "dry_run": dry_run,
        "scanned": len(seen),
        "archived_count": len(archived),
        "kept_future_count": len(kept_future),
        "skipped_unknown_count": len(skipped_unknown),
        "archived": archived,
        "kept_future": kept_future,
        "skipped_unknown": skipped_unknown,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-id", default="", help="Google profile id (default: all)")
    parser.add_argument("--max-results", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    profiles = list_profiles(refresh_expired=True)
    if args.profile_id:
        profiles = [p for p in profiles if p["profile_id"] == args.profile_id]

    for profile in profiles:
        if not profile.get("has_token") or profile.get("is_expired"):
            print(f"Skipping {profile['profile_id']}: token unavailable or expired")
            continue

        result = archive_past_calendar_invites(
            profile["profile_id"],
            max_results=args.max_results,
            dry_run=args.dry_run,
        )
        email = profile.get("email") or profile["profile_id"]
        print(
            f"{email}: scanned={result['scanned']} "
            f"archived={result['archived_count']} "
            f"kept_future={result['kept_future_count']} "
            f"skipped_unknown={result['skipped_unknown_count']}"
        )
        for item in result["archived"][:10]:
            print(f"  archived: {item['subject'][:90]} ({item['source']})")
        if result["archived_count"] > 10:
            print(f"  ... and {result['archived_count'] - 10} more")


if __name__ == "__main__":
    main()

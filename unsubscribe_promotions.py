"""Bulk-unsubscribe from promotional Gmail senders via List-Unsubscribe headers."""
from __future__ import annotations

import argparse
import base64
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

from googleapiclient.discovery import build

from profiles import get_creds

KEEP_SENDER_PATTERNS = (
    r"meta for business",
    r"facebook",
    r"business-updates\.facebook\.com",
    r"@facebook\.com",
    r"@fb\.com",
    r"@meta\.com",
)

KEEP_RE = re.compile("|".join(KEEP_SENDER_PATTERNS), re.I)
FROM_EMAIL_RE = re.compile(r"<([^>]+)>|([^\s<>]+@[^\s<>]+)")
UNSUB_PART_RE = re.compile(r"<([^>]+)>")


def _extract_sender_email(from_header: str) -> str:
    match = FROM_EMAIL_RE.search(from_header or "")
    if not match:
        return (from_header or "").strip().lower()
    return (match.group(1) or match.group(2) or "").strip().lower()


def _should_keep(from_header: str, sender_email: str) -> bool:
    blob = f"{from_header} {sender_email}".lower()
    return bool(KEEP_RE.search(blob))


def _parse_list_unsubscribe(value: str) -> tuple[list[str], list[str]]:
    http_links: list[str] = []
    mailto_links: list[str] = []
    for part in UNSUB_PART_RE.findall(value or ""):
        part = part.strip()
        if part.lower().startswith("mailto:"):
            mailto_links.append(part)
        elif part.lower().startswith("http"):
            http_links.append(part)
    return http_links, mailto_links


def _list_promotion_message_ids(gmail, max_messages: int) -> list[str]:
    ids: list[str] = []
    page_token = None
    while len(ids) < max_messages:
        batch = min(500, max_messages - len(ids))
        resp = (
            gmail.users()
            .messages()
            .list(
                userId="me",
                q="category:promotions",
                maxResults=batch,
                pageToken=page_token,
            )
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_messages]


def _fetch_headers(gmail, message_id: str) -> dict[str, str] | None:
    try:
        msg = (
            gmail.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "List-Unsubscribe", "List-Unsubscribe-Post"],
            )
            .execute()
        )
        return {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
    except Exception:
        return None


def _collect_sender_samples(gmail, message_ids: list[str], idle_limit: int = 60) -> tuple[dict[str, dict], list[dict], list[dict]]:
    sender_sample: dict[str, dict] = {}
    kept: list[dict] = []
    no_unsub_header: list[dict] = []
    kept_senders: set[str] = set()
    no_unsub_senders: set[str] = set()
    idle = 0

    for idx, mid in enumerate(message_ids, start=1):
        headers = _fetch_headers(gmail, mid)
        if not headers:
            continue
        from_header = headers.get("from", "")
        sender = _extract_sender_email(from_header)
        new_sender = False

        if _should_keep(from_header, sender):
            if sender not in kept_senders:
                kept.append({"from": from_header, "sender": sender})
                kept_senders.add(sender)
                new_sender = True
        elif sender not in sender_sample and headers.get("list-unsubscribe"):
            sender_sample[sender] = headers
            new_sender = True
        elif sender not in sender_sample and sender not in no_unsub_senders and not headers.get("list-unsubscribe"):
            no_unsub_header.append({"from": from_header, "sender": sender})
            no_unsub_senders.add(sender)
            new_sender = True

        idle = 0 if new_sender else idle + 1
        if idx % 50 == 0:
            print(f"  scanned {idx}/{len(message_ids)} messages, {len(sender_sample)} senders to unsub...", flush=True)
        if idle >= idle_limit and len(sender_sample) >= 10:
            print(f"  stopping early after {idx} messages (no new senders in last {idle_limit})", flush=True)
            break

    return sender_sample, kept, no_unsub_header


def _http_unsubscribe(url: str, one_click: bool) -> tuple[bool, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; HermesUnsubscribe/1.0)",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }
    try:
        if one_click:
            req = urllib.request.Request(
                url,
                data=b"List-Unsubscribe=One-Click",
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
        else:
            req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            code = resp.getcode()
            if 200 <= code < 400:
                return True, f"HTTP {code}"
            return False, f"HTTP {code}"
    except urllib.error.HTTPError as exc:
        if exc.code in (200, 201, 202, 204, 301, 302, 303, 307, 308):
            return True, f"HTTP {exc.code}"
        return False, f"HTTP error {exc.code}"
    except Exception as exc:
        return False, str(exc)


def _mailto_unsubscribe(gmail, mailto_url: str) -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(mailto_url)
    to_addr = urllib.parse.unquote(parsed.path)
    params = urllib.parse.parse_qs(parsed.query)
    subject = params.get("subject", ["unsubscribe"])[0]
    body = params.get("body", ["unsubscribe"])[0]
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to_addr
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True, f"mailto sent to {to_addr}"
    except Exception as exc:
        return False, str(exc)


def run(profile_id: str, email: str, max_messages: int, dry_run: bool) -> dict:
    gmail = build("gmail", "v1", credentials=get_creds(profile_id=profile_id or None, email=email or None))

    message_ids = _list_promotion_message_ids(gmail, max_messages)
    print(f"Listed {len(message_ids)} promotional messages, scanning senders...", flush=True)
    sender_sample, kept, no_unsub_header = _collect_sender_samples(gmail, message_ids)

    results = {
        "scanned_messages": len(message_ids),
        "unique_senders_to_unsub": len(sender_sample),
        "kept_meta_facebook": kept,
        "no_unsub_header": no_unsub_header,
        "unsubscribed": [],
        "failed": [],
    }

    for sender, headers in sender_sample.items():
        from_header = headers.get("from", "")
        unsub = headers.get("list-unsubscribe", "")
        http_links, mailto_links = _parse_list_unsubscribe(unsub)
        one_click = "list-unsubscribe=one-click" in (headers.get("list-unsubscribe-post") or "").lower()

        if dry_run:
            results["unsubscribed"].append(
                {"from": from_header, "sender": sender, "detail": "dry-run", "method": "http" if http_links else "mailto"}
            )
            continue

        ok = False
        detail = "no method worked"
        for url in http_links:
            ok, detail = _http_unsubscribe(url, one_click)
            if ok:
                break
            ok, detail = _http_unsubscribe(url, False)
            if ok:
                break

        if not ok and mailto_links:
            ok, detail = _mailto_unsubscribe(gmail, mailto_links[0])

        entry = {"from": from_header, "sender": sender, "detail": detail}
        if ok:
            results["unsubscribed"].append(entry)
        else:
            results["failed"].append(entry)
        time.sleep(0.35)

    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-id", default="default")
    parser.add_argument("--email", default="", help="Optional; use when multiple profiles are connected")
    parser.add_argument("--max-messages", type=int, default=2000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    results = run(args.profile_id, args.email, args.max_messages, args.dry_run)
    print(f"Scanned {results['scanned_messages']} promotional messages")
    print(f"Unique senders to unsubscribe: {results['unique_senders_to_unsub']}")
    print(f"Kept Meta/Facebook senders: {len(results['kept_meta_facebook'])}")
    print(f"Unsubscribed: {len(results['unsubscribed'])}")
    print(f"Failed: {len(results['failed'])}")
    print(f"No List-Unsubscribe header: {len(results['no_unsub_header'])}")

    if results["kept_meta_facebook"]:
        print("\nKept (Meta/Facebook):")
        for item in results["kept_meta_facebook"]:
            print(f"  - {item['from']}")

    if results["unsubscribed"]:
        print("\nUnsubscribed from:")
        for item in results["unsubscribed"]:
            print(f"  - {item['from']} ({item['detail']})")

    if results["failed"]:
        print("\nFailed:")
        for item in results["failed"]:
            print(f"  - {item['from']}: {item['detail']}")

    if results["no_unsub_header"]:
        print("\nNo List-Unsubscribe header (manual action may be needed):")
        for item in results["no_unsub_header"]:
            print(f"  - {item['from']}")


if __name__ == "__main__":
    main()

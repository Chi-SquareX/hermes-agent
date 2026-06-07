import base64
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from profiles import get_creds

EXPENSE_DATA_DIR = Path(__file__).with_name("expense_data")
IST = ZoneInfo("Asia/Kolkata")

_DATE_FORMATS = [
    "%d-%m-%Y at %I:%M:%S %p",
    "%d-%m-%Y at %H:%M:%S",
    "%d-%m-%Y",
    "%d %b, %Y at %H:%M:%S",
    "%d %b, %Y at %I:%M:%S %p",
    "%d %b, %Y",
    "%d-%b-%Y",
    "%d/%m/%Y",
    "%d/%m/%Y at %H:%M:%S",
]

_AMOUNT = r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)"
_BARE_AMOUNT = r"(?:Amount\s*(?:\(\s*Rs\.?\s*\))?\s*|(?:₹|Rs\.?|INR)\s*)([\d,]+(?:\.\d{1,2})?)"

_PARSERS = [
    {
        "type": "debit",
        "category": "Credit Card",
        "pattern": re.compile(
            rf"(?P<amount>{_AMOUNT})\s+has been spent on your YES BANK Credit Card.*?at\s+(?P<merchant>[^.\n]+?)\s+on\s+(?P<date>[\d-]+(?:\s+at\s+[\d:]+\s*(?:am|pm)?)?)",
            re.I,
        ),
        "source": "YES BANK",
    },
    {
        "type": "debit",
        "category": "Credit Card",
        "pattern": re.compile(
            rf"(?P<amount>{_AMOUNT})\s+has been debited from your HDFC Bank Credit Card.*?towards\s+(?P<merchant>[^.\n]+?)\s+on\s+(?P<date>[\d]{{1,2}}\s+[A-Za-z]{{3}},?\s+\d{{4}}(?:\s+at\s+[\d:]+\s*(?:am|pm)?)?)",
            re.I,
        ),
        "source": "HDFC Bank",
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            rf"purchase in\s+(?P<merchant>[^.\n]+?)\s+for value date\s+(?P<date>[\d-]+-[A-Za-z]{{3}}-\d{{4}})\s+for\s+(?P<amount>{_AMOUNT})",
            re.I,
        ),
        "source": "SBI Mutual Fund",
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            rf"Systematic Investment Purchase of units in\s+(?P<merchant>[^.\n]+?)\s+for transaction date\s+(?P<date>[\d/]+)",
            re.I,
        ),
        "source": "Motilal Oswal MF",
        "amount_anywhere": True,
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            rf"purchase request in\s+(?P<merchant>[^.]+?)\s+for\s+(?P<amount>{_AMOUNT})\s+has been processed.*? on\s+(?P<date>[\d-]+-[A-Za-z]+-\d{{4}})",
            re.I | re.S,
        ),
        "source": "Kotak Mutual Fund",
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            r"Scheme\s+(?P<merchant>Bandhan.+?)\s+Amount\s+(?P<amount>[\d,]+(?:\.\d{1,2})?)",
            re.I,
        ),
        "source": "Bandhan Mutual Fund",
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            r"Scheme Name\s+(?P<merchant>.*?)Amount\s*\(Rs\.\)\s+(?P<amount>[\d,]+(?:\.\d{1,2})?).*?Value Date\s+(?P<date>[\d-]+-[A-Za-z]+-\d{4})",
            re.I | re.S,
        ),
        "source": "ICICI Prudential MF",
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            r"invest in\s+(?P<merchant>[^.!]+?)\..*?Amount\s*\(Rs\.\)\s+(?P<amount>[\d,]+(?:\.\d{1,2})?).*?Value Date\s+(?P<date>[\d-]+-[A-Za-z]+-\d{4})",
            re.I | re.S,
        ),
        "source": "ICICI Prudential MF",
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            r"invest in\s+(?P<merchant>[^.!]+?)\.\s+The allotment details.*?Amount\s+(?P<amount>[\d,]+(?:\.\d{1,2})?).*?Value Date\s+(?P<date>[\d-]+-[A-Za-z]+-\d{4})",
            re.I | re.S,
        ),
        "source": "Mutual Fund",
    },
    {
        "type": "debit",
        "category": "Investment",
        "pattern": re.compile(
            r"SIP transaction has been processed.*?details are as follows:\s*(?P<merchant>.+?)\s+(?:₹|Rs\.?|INR)\s*(?P<amount>[\d,]+(?:\.\d{1,2})?)",
            re.I | re.S,
        ),
        "source": "Mutual Fund",
    },
    {
        "type": "credit",
        "category": "Refund",
        "pattern": re.compile(
            rf"refund of\s+(?P<amount>{_AMOUNT})",
            re.I,
        ),
        "source": "Refund Alert",
    },
    {
        "type": "credit",
        "category": "Credit Card",
        "pattern": re.compile(
            rf"(?P<amount>{_AMOUNT})\s+(?:has been credited|was credited|credited to your)",
            re.I,
        ),
        "source": "Bank Alert",
    },
    {
        "type": "debit",
        "category": "UPI / Bank",
        "pattern": re.compile(
            rf"(?P<amount>{_AMOUNT})\s+(?:has been debited|is debited|debited from your)",
            re.I,
        ),
        "source": "Bank Alert",
    },
    {
        "type": "credit",
        "category": "UPI / Bank",
        "pattern": re.compile(
            rf"(?P<amount>{_AMOUNT})\s+(?:has been credited|is credited|credited to your)",
            re.I,
        ),
        "source": "Bank Alert",
    },
]

_FINANCIAL_HINT = re.compile(
    r"(?:debited|credited|has been spent|transaction alert|payment was made|"
    r"purchase transaction|sip transaction|bill payment|statement|"
    r"systematic investment|confirmation of purchase|processing of purchase|"
    r"allotment details|sip transaction has been processed)",
    re.I,
)

_BANK_SENDER = re.compile(
    r"(?:yes\.bank|hdfcbank|hdfc bank|icici|sbi mutual|motilal oswal|"
    r"axis bank|kotak|idfc|indusind|federal bank|hsbc|amex|citibank|"
    r"camsonline|kfintech|nse\.co\.in|axismf|ppfas|bandhan)",
    re.I,
)

_MF_SUBJECT = re.compile(
    r"(?:purchase transaction|processing of(?: systematic)? purchase|confirmation of purchase|"
    r"sip transaction|systematic purchase)",
    re.I,
)


def _parse_amount(raw: str) -> float:
    cleaned = re.sub(r"^(?:Rs\.?|INR|₹)\s*", "", raw.strip(), flags=re.I)
    return float(cleaned.replace(",", ""))


def _parse_transaction_datetime(date_str: str | None, email_date: str | None) -> datetime | None:
    if date_str:
        cleaned = re.sub(r"\s+", " ", date_str.strip())
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(cleaned, fmt).replace(tzinfo=IST)
            except ValueError:
                continue

    if email_date:
        cleaned = re.sub(r"\s*\([^)]+\)$", "", email_date.strip())
        try:
            parsed = parsedate_to_datetime(cleaned)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=IST)
            return parsed
        except (TypeError, ValueError, IndexError):
            pass

    return None


def _transaction_sort_key(transaction: dict) -> datetime:
    raw = transaction.get("transaction_at")
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    parsed = _parse_transaction_datetime(transaction.get("date"), transaction.get("email_date"))
    if parsed:
        return parsed
    synced = transaction.get("synced_at")
    if synced:
        try:
            return datetime.fromisoformat(synced)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _normalize_transaction(transaction: dict) -> dict:
    normalized = dict(transaction)
    parsed = _parse_transaction_datetime(normalized.get("date"), normalized.get("email_date"))
    if parsed:
        normalized["transaction_at"] = parsed.isoformat()
    elif normalized.get("transaction_at"):
        try:
            normalized["transaction_at"] = datetime.fromisoformat(
                normalized["transaction_at"]
            ).isoformat()
        except ValueError:
            normalized.pop("transaction_at", None)
    return normalized


def _sort_transactions(transactions: list[dict]) -> list[dict]:
    return sorted(transactions, key=_transaction_sort_key, reverse=True)


def _extract_email_body(msg: dict) -> tuple[str, str, str]:
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    parts: list[bytes] = []

    def walk(part: dict) -> None:
        if part.get("body", {}).get("data"):
            parts.append(base64.urlsafe_b64decode(part["body"]["data"] + "=="))
        for child in part.get("parts", []):
            walk(child)

    walk(msg.get("payload", {}))
    text = b" ".join(parts).decode("utf-8", errors="replace")
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    snippet = msg.get("snippet", "") or ""
    return headers.get("From", ""), headers.get("Subject", ""), f"{snippet} {text}"


def _infer_source(from_header: str, subject: str) -> str:
    combined = f"{from_header} {subject}"
    lowered = combined.lower()
    if "yes.bank" in lowered:
        return "YES BANK"
    if "hdfc" in lowered and "mutual" not in lowered:
        return "HDFC Bank"
    if "sbi mutual" in lowered:
        return "SBI Mutual Fund"
    if "motilal oswal" in lowered:
        return "Motilal Oswal MF"
    if "icici prudential" in lowered or "icicipru" in lowered:
        return "ICICI Prudential MF"
    if "ppfas" in lowered or "parag parikh" in lowered:
        return "PPFAS Mutual Fund"
    if "bandhan" in lowered:
        return "Bandhan Mutual Fund"
    if "kotak mutual" in lowered or "kotak" in lowered and "mutual" in lowered:
        return "Kotak Mutual Fund"
    if "canara robeco" in lowered:
        return "Canara Robeco MF"
    if "axis mutual" in lowered or "axismf" in lowered:
        return "AXIS Mutual Fund"
    if "icici" in lowered:
        return "ICICI Bank"
    match = re.search(r"^([^<]+)", from_header.strip())
    if match:
        name = match.group(1).strip()
        if name and "@" not in name:
            return name[:40]
    match = re.search(r"<([^>]+)>", from_header)
    return (match.group(1) if match else from_header).split("@")[0][:40]


def _is_excluded_financial(subject: str, text: str) -> bool:
    subject_lower = subject.lower()
    if re.search(
        r"(?:idcw|dividend|redemption|cancellation notification|"
        r"portfolio disclosure|month-end valuation|monthly portfolio|"
        r"statement of account)",
        subject_lower,
        re.I,
    ):
        return True
    combined = f"{subject} {text}".lower()
    return bool(
        re.search(
            r"(?:idcw intimation|dividend is declared|dividend amount|"
            r"redemption confirmation|auto cancellation notification|"
            r"month-end valuation|monthly portfolio disclosure)",
            combined,
            re.I,
        )
    )


def _has_money_signal(text: str) -> bool:
    return bool(re.search(_AMOUNT, text, re.I) or re.search(_BARE_AMOUNT, text, re.I))


def _looks_financial(from_header: str, subject: str, text: str) -> bool:
    if _is_excluded_financial(subject, text):
        return False
    combined = f"{from_header} {subject} {text}"
    if _MF_SUBJECT.search(subject) and _BANK_SENDER.search(combined) and _has_money_signal(combined):
        return True
    if _BANK_SENDER.search(combined):
        return bool(_FINANCIAL_HINT.search(combined) and _has_money_signal(combined))
    return bool(
        _FINANCIAL_HINT.search(combined)
        and _has_money_signal(combined)
        and re.search(r"(?:debited|credited|spent|purchase)", combined, re.I)
    )


def parse_transaction(from_header: str, subject: str, text: str) -> dict | None:
    combined = f"{subject} {text}"
    inferred = _infer_source(from_header, subject)

    for spec in _PARSERS:
        match = spec["pattern"].search(combined)
        if not match:
            continue
        groups = match.groupdict()
        amount_raw = groups.get("amount")
        if not amount_raw and spec.get("amount_from_subject"):
            amount_match = re.search(_AMOUNT, combined, re.I)
            amount_raw = amount_match.group(1) if amount_match else None
        if not amount_raw and spec.get("amount_anywhere"):
            amount_match = re.search(_AMOUNT, combined, re.I) or re.search(
                _BARE_AMOUNT, combined, re.I
            )
            amount_raw = amount_match.group(1) if amount_match else None
        if not amount_raw:
            continue

        merchant = (groups.get("merchant") or subject).strip()
        merchant = re.sub(r"\s+", " ", merchant)[:80]
        date_str = (groups.get("date") or "").strip()
        if not date_str:
            for date_pattern in (
                r"trade date of\s+([\d-]+-[A-Za-z]+-\d{4})",
                r"Value Date\s+([\d-]+-[A-Za-z]+-\d{4})",
                r"transaction date\s+([\d/]+(?:\d{4})?)",
            ):
                date_match = re.search(date_pattern, combined, re.I)
                if date_match:
                    date_str = date_match.group(1).strip()
                    break

        raw_source = spec.get("source") or inferred
        resolved_source = inferred if raw_source == "Mutual Fund" else raw_source

        return {
            "type": spec["type"],
            "category": spec["category"],
            "amount": _parse_amount(amount_raw),
            "currency": "INR",
            "merchant": merchant,
            "date": date_str or None,
            "source": resolved_source,
            "subject": subject[:120],
            "from": from_header[:120],
        }
    return None


def _data_path(profile_id: str) -> Path:
    EXPENSE_DATA_DIR.mkdir(exist_ok=True)
    return EXPENSE_DATA_DIR / f"{profile_id}.json"


def _empty_store(profile_id: str) -> dict:
    return {
        "profile_id": profile_id,
        "last_sync_at": None,
        "processed_message_ids": [],
        "transactions": [],
        "summary": _build_summary([]),
        "last_scan": None,
    }


def _is_investment(transaction: dict) -> bool:
    return transaction.get("category") == "Investment"


def _section_summary(transactions: list[dict]) -> dict:
    debits = [t for t in transactions if t.get("type") == "debit"]
    credits = [t for t in transactions if t.get("type") == "credit"]
    total_debit = round(sum(t["amount"] for t in debits), 2)
    total_credit = round(sum(t["amount"] for t in credits), 2)
    return {
        "total_debit": total_debit,
        "total_credit": total_credit,
        "net": round(total_debit - total_credit, 2),
        "debit_count": len(debits),
        "credit_count": len(credits),
        "transaction_count": len(transactions),
    }


def _build_summary(transactions: list[dict]) -> dict:
    spend_txns = [t for t in transactions if not _is_investment(t)]
    invest_txns = [t for t in transactions if _is_investment(t)]
    spend = _section_summary(spend_txns)
    investments = _section_summary(invest_txns)
    return {
        "total_debit": spend["total_debit"],
        "total_credit": spend["total_credit"],
        "net_spend": spend["net"],
        "debit_count": spend["debit_count"],
        "credit_count": spend["credit_count"],
        "transaction_count": len(transactions),
        "spend": {
            **spend,
            "net_spend": spend["net"],
        },
        "investments": {
            **investments,
            "net_invested": investments["net"],
        },
    }


def load_expense_report(profile_id: str) -> dict | None:
    path = _data_path(profile_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if "processed_message_ids" not in data:
        data["processed_message_ids"] = [
            t["message_id"] for t in data.get("transactions", []) if t.get("message_id")
        ]
    transactions = [_normalize_transaction(t) for t in data.get("transactions", [])]
    data["transactions"] = _sort_transactions(transactions)
    data["summary"] = _build_summary(data["transactions"])
    save_expense_report(profile_id, data)
    return data


def save_expense_report(profile_id: str, report: dict) -> None:
    _data_path(profile_id).write_text(json.dumps(report, indent=2), encoding="utf-8")


def _list_messages(gmail, query: str, max_results: int) -> list[dict]:
    refs: list[dict] = []
    page_token = None
    while len(refs) < max_results:
        batch_size = min(100, max_results - len(refs))
        resp = (
            gmail.users()
            .messages()
            .list(userId="me", q=query, maxResults=batch_size, pageToken=page_token)
            .execute()
        )
        refs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return refs[:max_results]


def _process_message(
    gmail,
    message_id: str,
    archive_processed: bool,
    in_inbox: bool,
) -> tuple[dict | None, bool]:
    msg = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    from_header, subject, text = _extract_email_body(msg)

    if not _looks_financial(from_header, subject, text):
        return None, False

    parsed = parse_transaction(from_header, subject, text)
    if not parsed:
        return None, False

    parsed["message_id"] = message_id
    parsed["email_date"] = next(
        (
            h["value"]
            for h in msg.get("payload", {}).get("headers", [])
            if h["name"].lower() == "date"
        ),
        None,
    )
    parsed["synced_at"] = datetime.now(timezone.utc).isoformat()
    parsed = _normalize_transaction(parsed)

    archived = False
    if archive_processed and in_inbox:
        gmail.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["INBOX", "UNREAD"]},
        ).execute()
        archived = True

    return parsed, archived


def scan_expense_emails(
    profile_id: str,
    max_results: int = 100,
    archive_processed: bool = True,
) -> dict:
    creds = get_creds(profile_id=profile_id)
    gmail = build("gmail", "v1", credentials=creds)

    store = load_expense_report(profile_id) or _empty_store(profile_id)
    known_ids = set(store.get("processed_message_ids", []))
    existing = {t["message_id"]: t for t in store.get("transactions", []) if t.get("message_id")}

    message_refs = _list_messages(gmail, "in:inbox", max(1, min(max_results, 100)))

    new_transactions: list[dict] = []
    archived_ids: list[str] = []
    skipped = 0
    already_known = 0

    for ref in message_refs:
        if ref["id"] in known_ids:
            already_known += 1
            continue

        parsed, archived = _process_message(
            gmail, ref["id"], archive_processed=archive_processed, in_inbox=True
        )
        if not parsed:
            skipped += 1
            continue

        new_transactions.append(parsed)
        known_ids.add(ref["id"])
        existing[ref["id"]] = parsed
        if archived:
            archived_ids.append(ref["id"])

    all_transactions = _sort_transactions(list(existing.values()))

    report = {
        "profile_id": profile_id,
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "processed_message_ids": sorted(known_ids),
        "transactions": all_transactions,
        "summary": _build_summary(all_transactions),
        "last_scan": {
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "emails_scanned": len(message_refs),
            "new_transactions": len(new_transactions),
            "already_known": already_known,
            "emails_archived": len(archived_ids),
            "emails_skipped": skipped,
        },
        # Back-compat fields for UI
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "emails_scanned": len(message_refs),
        "transactions_found": len(new_transactions),
        "total_transactions": len(all_transactions),
        "emails_archived": len(archived_ids),
        "emails_skipped": skipped,
    }
    save_expense_report(profile_id, report)
    return report


def recover_archived_transactions(profile_id: str, max_results: int = 500) -> dict:
    """Re-import transactions from already-archived bank alert emails."""
    creds = get_creds(profile_id=profile_id)
    gmail = build("gmail", "v1", credentials=creds)

    store = load_expense_report(profile_id) or _empty_store(profile_id)
    known_ids = set(store.get("processed_message_ids", []))
    existing = {t["message_id"]: t for t in store.get("transactions", []) if t.get("message_id")}

    query = (
        "(from:alerts@yes.bank.in OR from:alerts@hdfcbank.bank.in OR from:camsonline.com OR "
        "from:kfintech.com OR from:sbimf.com OR "
        'subject:"transaction alert" OR subject:"payment was made" OR '
        'subject:"purchase transaction" OR subject:"processing of" OR '
        'subject:"confirmation of purchase" OR subject:"sip transaction confirmation") '
        "-in:inbox"
    )
    message_refs = _list_messages(gmail, query, max_results)

    recovered: list[dict] = []
    for ref in message_refs:
        if ref["id"] in known_ids:
            continue
        parsed, _ = _process_message(
            gmail, ref["id"], archive_processed=False, in_inbox=False
        )
        if not parsed:
            continue
        recovered.append(parsed)
        known_ids.add(ref["id"])
        existing[ref["id"]] = parsed

    all_transactions = _sort_transactions(list(existing.values()))

    report = {
        "profile_id": profile_id,
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "processed_message_ids": sorted(known_ids),
        "transactions": all_transactions,
        "summary": _build_summary(all_transactions),
        "last_scan": store.get("last_scan"),
        "recovered_count": len(recovered),
        "total_transactions": len(all_transactions),
        "transactions_found": len(recovered),
        "emails_scanned": len(message_refs),
        "emails_archived": 0,
        "emails_skipped": 0,
    }
    save_expense_report(profile_id, report)
    return report


_RECORDING_QUERIES = (
    'in:inbox from:fathom.video',
    'in:inbox subject:"Cloud Recording -"',
    'in:inbox "wants to record your upcoming meeting"',
    'in:inbox "record Google Meet meetings"',
)


def archive_recording_emails(profile_id: str, max_results: int = 500) -> dict:
    """Archive meeting-recording notification emails from the inbox."""
    creds = get_creds(profile_id=profile_id)
    gmail = build("gmail", "v1", credentials=creds)

    seen: set[str] = set()
    archived_ids: list[str] = []

    for query in _RECORDING_QUERIES:
        for ref in _list_messages(gmail, query, max_results):
            if ref["id"] in seen:
                continue
            seen.add(ref["id"])
            gmail.users().messages().modify(
                userId="me",
                id=ref["id"],
                body={"removeLabelIds": ["INBOX", "UNREAD"]},
            ).execute()
            archived_ids.append(ref["id"])

    return {
        "profile_id": profile_id,
        "archived_count": len(archived_ids),
        "archived_message_ids": archived_ids,
    }


def resync_investment_transactions(profile_id: str, max_results: int = 500) -> dict:
    """Rebuild investment transactions from mutual fund purchase confirmation emails."""
    creds = get_creds(profile_id=profile_id)
    gmail = build("gmail", "v1", credentials=creds)

    store = load_expense_report(profile_id) or _empty_store(profile_id)
    spend_txns = [t for t in store.get("transactions", []) if t.get("category") != "Investment"]
    spend_ids = {t["message_id"] for t in spend_txns if t.get("message_id")}

    query = (
        "(from:camsonline.com OR from:kfintech.com) "
        '(subject:"purchase transaction" OR subject:"processing of" OR '
        'subject:"confirmation of purchase" OR subject:"sip transaction confirmation")'
    )
    message_refs = _list_messages(gmail, query, max_results)

    investments: list[dict] = []
    invest_ids: set[str] = set()
    skipped = 0

    for ref in message_refs:
        parsed, _ = _process_message(
            gmail, ref["id"], archive_processed=False, in_inbox=False
        )
        if not parsed or parsed.get("category") != "Investment":
            skipped += 1
            continue
        investments.append(parsed)
        invest_ids.add(ref["id"])

    all_transactions = _sort_transactions(spend_txns + investments)
    known_ids = spend_ids | invest_ids

    report = {
        "profile_id": profile_id,
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "processed_message_ids": sorted(known_ids),
        "transactions": all_transactions,
        "summary": _build_summary(all_transactions),
        "last_scan": store.get("last_scan"),
        "investment_resync": {
            "resynced_at": datetime.now(timezone.utc).isoformat(),
            "emails_scanned": len(message_refs),
            "investments_imported": len(investments),
            "emails_skipped": skipped,
        },
        "total_transactions": len(all_transactions),
        "transactions_found": len(investments),
        "emails_scanned": len(message_refs),
    }
    save_expense_report(profile_id, report)
    return report

"""app/comms.py — Comms lane: inbound/outbound email + SMS.

Two external channels feed (and are fed by) the local ``messages`` table:

  1. **Gmail** (consumer accounts via IMAP/SMTP with app passwords). The
     practice runs THREE mailboxes (mainlinesurgery@, mainlinepain@,
     mainsurgical@ — referrals mostly land at mainlinepain@); ``poll_gmail``
     iterates ALL configured accounts, reads each INBOX read-only (BODY.PEEK —
     never marks a message read) and pins each row's ``recipient`` to the
     account it arrived at. ``send_email`` sends via SMTP-SSL from the first
     account by default (or an explicit ``from_account``), optionally threading
     a reply and attaching Google Drive files.
  2. **RingCentral** (OAuth 2.0 JWT credentials flow, reusing the PROVEN token
     exchange from ``ringcentral_sync.py``). ``poll_ringcentral`` reads the
     last week of inbound SMS, fax and voicemail from the message-store —
     ``channel`` per type ('sms' | 'fax' | 'voicemail'); fax/voicemail store a
     short summary + attachment metadata (never the binary). ``send_sms`` posts
     a new outbound text.

DESIGN / HONESTY NOTES (house rules — kept in lockstep with referrals.py):

  * **Every arm degrades to ``'not configured'`` and NEVER raises** when its
    credentials are absent. The app runs identically on a box with no mail or
    telephony creds. ``poll_all`` wraps each arm in its own try/except so one
    channel's exception can never take down the other or the caller.
  * **No PHI in logger calls.** Message bodies, sender addresses, subjects and
    patient content are patient data; the Python logger / launchd .err.log
    sits OUTSIDE the access-controlled DB. Loggers here carry ids and counts
    ONLY — never a body, subject, address, or phone number.
  * **Fail closed on attachments.** ``send_email`` with ``drive_file_ids``
    downloads every promised file BEFORE sending; if ANY attachment cannot be
    fetched it returns an error and sends NOTHING. A records-request reply that
    silently drops its promised records is worse than no reply at all.
  * **``external_id`` dedupes.** IMAP ``Message-ID`` / RingCentral message id
    is the UNIQUE key; every insert is ``INSERT OR IGNORE`` so a re-poll can
    never double-store the same message.
  * All functions are **synchronous** — callers (server routes, approvals)
    wrap them in ``asyncio.to_thread``.

Optional third-party deps (``googleapiclient`` for Drive attachments) are
imported INSIDE the function that needs them, so importing this module never
drags in a dependency the box may not have.
"""

from __future__ import annotations

import base64
import email
import imaplib
import json
import logging
import smtplib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

from app.config import SETTINGS
from app import db, audit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """UTC timestamp, ISO-8601 — matches the rest of the app's ``ts`` cols."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    """Convert a ``messages`` ``sqlite3.Row`` to a plain dict.

    The ``detail`` column is JSON text (or NULL) → decoded to a Python object
    when possible, else returned as the raw string; NULL → ``None``.
    """
    d = dict(row)
    raw = d.get("detail")
    if raw:
        try:
            d["detail"] = json.loads(raw)
        except (TypeError, ValueError):
            # A non-JSON detail (e.g. plain text tag) is fine to surface raw.
            d["detail"] = raw
    else:
        d["detail"] = None
    return d


def _insert_message(
    *,
    channel: str,
    direction: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    external_id: str,
    thread_ref: str = "",
    status: str = "new",
    detail: Optional[dict] = None,
) -> int:
    """Insert one message row (``INSERT OR IGNORE`` on ``external_id``).

    Returns the new row id, or ``0`` when the row was ignored as a duplicate
    (``external_id`` already present). Never logs the body/subject/sender.
    """
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    conn = db.get_conn()
    try:
        with conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (ts, channel, direction, sender, recipient, subject, body,
                     external_id, thread_ref, status, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_iso(), channel, direction, sender, recipient,
                    subject, body, external_id, thread_ref, status,
                    detail_json,
                ),
            )
            # rowcount == 0 when OR IGNORE skipped a duplicate external_id.
            return int(cur.lastrowid) if cur.rowcount else 0
    finally:
        conn.close()


def _decode_header(value: Optional[str]) -> str:
    """Decode an RFC-2047 encoded header to a plain str (never raises)."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # malformed header → best-effort raw string
        return str(value)


def _email_creation_time(date_header: Optional[str]) -> str:
    """Parse an email ``Date:`` header to a UTC ISO-8601 string, or "".

    Best-effort and never raises — a missing/malformed Date returns "" so the
    caller simply omits ``creationTime`` and the age classifier falls back.
    """
    if not date_header:
        return ""
    try:
        dt = parsedate_to_datetime(str(date_header))
        if dt is None:
            return ""
        if dt.tzinfo is None:  # naive → assume UTC rather than guess local
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Gmail — poll (IMAP, read-only) + send (SMTP)
# ---------------------------------------------------------------------------

def _gmail_accounts() -> list:
    """Return the configured Gmail accounts as ``list[(address, app_password)]``.

    Sourced from ``SETTINGS.GMAIL_ACCOUNTS`` (already deduped by config, with
    the legacy single-account pair folded in). Empty list => Gmail not
    configured. The FIRST account is the default sender for ``send_email``.
    """
    accounts = getattr(SETTINGS, "GMAIL_ACCOUNTS", None) or []
    # Defensive: only well-formed (addr, pw) pairs with both halves present.
    out = []
    for item in accounts:
        try:
            addr, pw = item
        except (TypeError, ValueError):
            continue
        if addr and pw:
            out.append((str(addr), str(pw)))
    return out


def _gmail_configured() -> bool:
    return bool(_gmail_accounts())


def _extract_email_parts(msg) -> tuple[str, str]:
    """Return ``(body_text, detail_note)`` from a parsed ``email.message``.

    Prefers the first ``text/plain`` part. If there is no plain part, the raw
    ``text/html`` is stored and tagged in ``detail`` so nothing is lost and the
    UI knows it is looking at HTML (which it renders textContent-only anyway).
    Attachments are skipped for the body.
    """
    plain = None
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and plain is None:
                plain = text
            elif ctype == "text/html" and html is None:
                html = text
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        if payload is not None:
            try:
                text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                html = text
            else:
                plain = text

    if plain is not None:
        return plain, ""
    if html is not None:
        return html, "html_body"  # stored raw, tagged so the UI knows.
    return "", ""


def _poll_gmail_account(address: str, password: str, since_days: int,
                        limit: int) -> int:
    """Poll ONE Gmail INBOX; return the number of NEW rows inserted.

    Read-only (``BODY.PEEK[]`` — never marks a message read). Every stored
    row's ``recipient`` is pinned to *address* (this account's own mailbox)
    rather than the raw ``To:`` header, so a message that arrived only at
    mainlinepain@ is attributed to that mailbox even when ``To:`` lists an alias
    or a group address. Global dedupe stays on ``Message-ID`` via
    ``_insert_message``'s ``INSERT OR IGNORE`` — the same referral delivered to
    two mailboxes is stored ONCE.

    ATTRIBUTION CAVEAT for cross-delivered mail: because dedupe is global and
    the accounts are polled in config order, a message delivered to two of our
    mailboxes (e.g. To: mainlinepain@, Cc: mainlinesurgery@) is attributed to
    whichever of those mailboxes is polled FIRST — its copy inserts, the second
    is ``INSERT OR IGNORE``d. Single-delivery mail (the common referral case) is
    always attributed correctly to the mailbox it landed in.

    Raises on IMAP/parse failure; the caller (``poll_gmail``) isolates each
    account so one bad mailbox cannot abort the others.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=int(since_days))
             ).strftime("%d-%b-%Y")

    inserted = 0
    # Socket timeout (seconds): a firewall/NAT that blackholes the TCP
    # connection mid-session (no RST/FIN) would otherwise block this worker
    # thread FOREVER — permanently stalling the scheduler tick that awaits it
    # and hanging process exit on the un-cancellable executor thread. 60s caps
    # each blocking IMAP call so a wedged connection surfaces as a normal
    # (timeout) error the per-account isolation already handles.
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=60)
    try:
        imap.login(address, password)
        imap.select("INBOX", readonly=True)
        # UID search/fetch (NOT sequence numbers): UIDs are stable across
        # expunge, so the synthesised nomsgid key below can't shift or collide
        # when older mail is deleted between polls.
        typ, data = imap.uid("SEARCH", None, "SINCE", since)
        if typ != "OK":
            # A NO/BAD SEARCH is a real failure, NOT an empty mailbox. Raise so
            # poll_gmail surfaces an honest 'error: ...' marker rather than a
            # success-looking count of 0 the UI would report as "polled clean".
            raise RuntimeError(f"IMAP UID SEARCH returned {typ}")
        ids = data[0].split() if data and data[0] else []
        # Newest first, capped at ``limit``.
        for num in reversed(ids):
            if inserted >= limit:
                break
            typ, msg_data = imap.uid("FETCH", num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not raw:
                continue
            msg = email.message_from_bytes(raw)

            message_id = (msg.get("Message-ID") or "").strip()
            if not message_id:
                # No Message-ID → synthesise a stable key from the IMAP UID
                # (stable across expunge, unlike the sequence number). Scope it
                # by account so the same synthesised UID in two mailboxes cannot
                # collide. Never fabricate content, only an id.
                message_id = (f"nomsgid:{address}:"
                              f"{num.decode('ascii', 'ignore')}")
            subject = _decode_header(msg.get("Subject"))
            sender = _decode_header(msg.get("From"))
            thread_ref = (msg.get("References") or msg.get("In-Reply-To")
                          or "").strip()
            body, body_tag = _extract_email_parts(msg)
            # Store the email's own Date header as creationTime so the Legacy
            # 48h split ages an email by when it was SENT, not when we ingested
            # it (mirrors the RC creationTime). Best-effort: an unparseable /
            # missing Date just leaves it out (classifier then falls back).
            detail = {"format": body_tag} if body_tag else {}
            email_date = _email_creation_time(msg.get("Date"))
            if email_date:
                detail["creationTime"] = email_date
            detail = detail or None

            rid = _insert_message(
                channel="email",
                direction="in",
                sender=sender,
                # Recipient = THIS account's own address (the mailbox that
                # received it), not the raw To: header.
                recipient=address,
                subject=subject,
                body=body,
                external_id=message_id,
                thread_ref=thread_ref,
                status="new",
                detail=detail,
            )
            if rid:
                inserted += 1
    finally:
        try:
            imap.logout()
        except Exception:  # logout failure is non-fatal
            pass

    return inserted


def poll_gmail(since_days: int = 7, limit: int = 25):
    """Poll EVERY configured Gmail INBOX for recent mail; store any new rows.

    Returns the TOTAL number of NEW rows inserted across all accounts, or the
    string ``'not configured'`` when zero Gmail accounts are configured. This
    return shape is BACKWARD COMPATIBLE with the previous single-account
    version ('not configured' | int).

    The practice runs three mailboxes; each account is polled independently and
    its rows are tagged with that account's own address as the ``recipient``.
    Global dedupe stays on ``Message-ID`` (``INSERT OR IGNORE`` on
    ``external_id``), so a referral CC'd to two mailboxes is stored once and
    the ``limit`` (NEW rows per account) is applied per account.

    Per-account isolation: each mailbox is polled inside its own try/except so
    one account's IMAP failure cannot abort the others. If EVERY account fails,
    the last error is re-raised so ``poll_all`` records an honest ``'error:'``
    marker rather than a success-looking count of 0.
    """
    accounts = _gmail_accounts()
    if not accounts:
        return "not configured"

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 25
    if limit < 0:
        limit = 0

    total = 0
    errors: list = []
    for address, password in accounts:
        try:
            total += _poll_gmail_account(address, password, since_days, limit)
        except Exception as e:
            # ids/counts/class only — never a body/subject/sender/address.
            logger.error("comms.poll_gmail: account arm failed (%s)",
                         type(e).__name__)
            errors.append(e)

    # If nothing was inserted AND every account errored, surface the failure
    # (don't report a clean-looking 0). A partial failure with any success is
    # tolerated: the successful counts are returned honestly.
    if total == 0 and errors and len(errors) == len(accounts):
        raise errors[-1]

    if total:
        # ids/counts only — never the body/subject/sender.
        audit.log(
            actor="system", kind="comms", action="poll_gmail",
            detail={"inserted": total, "accounts": len(accounts)},
            outcome="ok",
        )
    return total


def poll_gmail_by_account(since_days: int = 7, limit: int = 25) -> dict:
    """Poll every Gmail account, returning a per-account breakdown.

    Shape: ``{addr: <int inserted> | 'error: <ClassName>'}`` — or an empty dict
    when no accounts are configured. Each account is isolated so one mailbox's
    failure cannot abort the others; a failed account maps to
    ``'error: <ExceptionClassName>'`` (class only — no PHI, no address leak
    beyond the key, which is the operator's own configured mailbox address).

    ``poll_all`` uses this to fill its ``email_accounts`` detail while keeping
    the top-level ``email`` value a backward-compatible total.
    """
    accounts = _gmail_accounts()
    if not accounts:
        return {}

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 25
    if limit < 0:
        limit = 0

    per: dict = {}
    total = 0
    for address, password in accounts:
        try:
            n = _poll_gmail_account(address, password, since_days, limit)
            per[address] = n
            total += n
        except Exception as e:
            logger.error("comms.poll_gmail_by_account: %s arm failed (%s)",
                         address, type(e).__name__)
            per[address] = f"error: {type(e).__name__}"

    if total:
        audit.log(
            actor="system", kind="comms", action="poll_gmail",
            detail={"inserted": total, "accounts": len(accounts)},
            outcome="ok",
        )
    return per


def _fetch_drive_attachments(drive_file_ids) -> tuple[list, Optional[str]]:
    """Download each Drive file id; return ``(attachments, error)``.

    ``attachments`` is a list of ``(filename, mime_type, bytes)`` tuples.
    On the FIRST failure (missing service account, googleapiclient absent,
    fetch error) returns ``([], "<reason>")`` so the caller fails closed —
    a records reply must never send with a promised attachment missing.
    """
    from pathlib import Path

    raw = list(drive_file_ids or [])
    ids = [str(x).strip() for x in raw if str(x).strip()]
    if not ids:
        # FAIL CLOSED: if the caller promised attachments (non-empty list) but
        # every id normalizes to empty/whitespace, treat it as an unfetchable
        # promised attachment — a records reply must NEVER send with zero
        # attachments when it claimed some. Only a genuinely-empty input list
        # (no attachments promised) is the no-op ([], None) case.
        if raw:
            return [], "one or more drive_file_ids were empty/blank"
        return [], None

    sa_path = Path(SETTINGS.SERVICE_ACCOUNT_JSON)
    if not sa_path.exists():
        return [], f"service account file missing ({sa_path})"

    try:
        from google.oauth2.service_account import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaIoBaseDownload  # type: ignore
    except Exception as e:  # ImportError or transitive failure
        return [], f"googleapiclient unavailable ({e})"

    import io

    try:
        creds = Credentials.from_service_account_file(
            str(sa_path),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds,
                        cache_discovery=False)
    except Exception as e:
        return [], f"drive auth failed ({e})"

    attachments = []
    for fid in ids:
        try:
            meta = service.files().get(
                fileId=fid, fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
            name = meta.get("name") or fid
            mime = meta.get("mimeType") or "application/octet-stream"
            buf = io.BytesIO()
            request = service.files().get_media(
                fileId=fid, supportsAllDrives=True)
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
            attachments.append((name, mime, buf.getvalue()))
        except Exception as e:
            # Fail closed on the FIRST missing/unfetchable file.
            return [], f"could not fetch drive file {fid} ({e})"

    return attachments, None


def send_email(
    to: str,
    subject: str,
    body: str,
    in_reply_to_external_id: Optional[str] = None,
    drive_file_ids=None,
    from_account: Optional[str] = None,
) -> dict:
    """Send an email via Gmail SMTP-SSL; store an outbound ``messages`` row.

    Returns ``{'ok': True, 'external_id': <Message-ID>}`` on success, or
    ``{'ok': False, 'error': <reason>}`` on any failure (not configured, no
    recipient, unknown from_account, attachment fetch failure, SMTP error).

    Single-sender by default: sends from the FIRST configured account
    (``SETTINGS.GMAIL_ADDRESS``). Pass ``from_account`` to send from another
    configured mailbox — e.g. reply to a mainlinepain@ referral from that same
    address. ``from_account`` MUST be one of the configured addresses
    (case-insensitive); an unrecognised address is an error and NOTHING is
    sent (we never fall back to a different sender silently).

    Threading: when ``in_reply_to_external_id`` is given, sets ``In-Reply-To``
    and ``References`` so the reply lands in the same Gmail thread.

    FAIL CLOSED on attachments: if ``drive_file_ids`` are given and ANY cannot
    be downloaded, NOTHING is sent and an error is returned — a records-request
    reply without its promised records is worse than no send.
    """
    accounts = _gmail_accounts()
    if not accounts:
        return {"ok": False, "error": "not configured"}
    if not to or not str(to).strip():
        return {"ok": False, "error": "no recipient"}

    # Pick the sending account. Default = first configured (the practice's
    # primary). If from_account is given it MUST match a configured address
    # (case-insensitive); an unknown address is a hard error — never silently
    # send from a different mailbox than the caller asked for.
    address, password = accounts[0]
    requested = (from_account or "").strip()
    if requested:
        match = next(
            ((a, p) for a, p in accounts if a.lower() == requested.lower()),
            None,
        )
        if match is None:
            return {"ok": False,
                    "error": f"from_account not configured: {requested}"}
        address, password = match

    # Resolve attachments FIRST — fail closed before touching SMTP.
    attachments, attach_err = _fetch_drive_attachments(drive_file_ids)
    if attach_err:
        logger.warning("comms.send_email: attachment fetch failed")
        return {"ok": False, "error": f"attachment fetch failed: {attach_err}"}

    msg = EmailMessage()
    msg["From"] = address
    msg["To"] = str(to).strip()
    msg["Subject"] = str(subject or "")
    if in_reply_to_external_id:
        ref = str(in_reply_to_external_id).strip()
        if ref:
            msg["In-Reply-To"] = ref
            msg["References"] = ref
    msg.set_content(str(body or ""))

    for name, mime, data in attachments:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=name)

    try:
        # timeout=60: a blackholed SMTP connection must not hang the executor
        # thread (and thus the scheduler / process exit) indefinitely — same
        # rationale as the IMAP timeout in _poll_gmail_account.
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
            smtp.login(address, password)
            smtp.send_message(msg)
    except Exception as e:
        # No PHI in logger: SMTPRecipientsRefused stringifies to the recipient
        # dict (the patient's email address). Log the exception CLASS only; the
        # detailed reason rides the returned error dict, which stays in the DB.
        logger.error("comms.send_email: SMTP send failed (%s)",
                     type(e).__name__)
        return {"ok": False, "error": f"send failed: {e}"}

    # Message-ID is generated by EmailMessage only if we set it; capture what
    # went on the wire so the outbound row + caller can thread on it.
    external_id = msg.get("Message-ID") or ""
    if not external_id:
        # smtplib does not add a Message-ID; synthesise a stable one and
        # (best-effort) record it. We do NOT re-send; this only identifies the
        # stored row for threading/dedup.
        external_id = (f"<sent-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
                       f"@{address.split('@')[-1]}>")

    _insert_message(
        channel="email",
        direction="out",
        sender=address,
        recipient=str(to).strip(),
        subject=str(subject or ""),
        body=str(body or ""),
        external_id=external_id,
        thread_ref=str(in_reply_to_external_id or "").strip(),
        status="sent",
        detail={"attachments": len(attachments)} if attachments else None,
    )
    audit.log(
        actor="system", kind="comms", action="send_email",
        detail={"attachments": len(attachments)}, outcome="ok",
    )
    return {"ok": True, "external_id": external_id}


def drive_search(q: str, limit: int = 10):
    """Search Drive by name/full-text via the service account.

    Returns a list of ``{id, name, mimeType, modifiedTime}`` dicts, or
    ``{'error': <reason>}`` when the arm is unavailable (no service account,
    googleapiclient absent, API error). Single quotes in ``q`` are escaped so
    the Drive ``q`` expression cannot be broken.

    Honesty: on failure we return an explicit error object — never a fake or
    empty-but-successful result that would imply "no documents match".
    """
    from pathlib import Path

    query = str(q or "").strip()
    if not query:
        return {"error": "empty query"}

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    if limit < 1:
        limit = 1

    sa_path = Path(SETTINGS.SERVICE_ACCOUNT_JSON)
    if not sa_path.exists():
        return {"error": f"service account file missing ({sa_path})"}

    try:
        from google.oauth2.service_account import Credentials  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except Exception as e:
        return {"error": f"googleapiclient unavailable ({e})"}

    # Escape single quotes for the Drive query expression.
    safe = query.replace("'", "\\'")
    drive_q = f"(name contains '{safe}' or fullText contains '{safe}')"

    try:
        creds = Credentials.from_service_account_file(
            str(sa_path),
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds,
                        cache_discovery=False)
        resp = service.files().list(
            q=drive_q,
            pageSize=limit,
            fields="files(id,name,mimeType,modifiedTime)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        # No PHI in logger: googleapiclient's HttpError repr embeds the request
        # URI including the q= expression (the search text — typically a patient
        # name). Log the exception CLASS only; the detailed reason rides the
        # returned error dict, which stays in the access-controlled DB.
        logger.error("comms.drive_search failed (%s)", type(e).__name__)
        return {"error": f"drive search failed: {e}"}

    files = resp.get("files", []) or []
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mimeType": f.get("mimeType"),
            "modifiedTime": f.get("modifiedTime"),
        }
        for f in files
    ]


# ---------------------------------------------------------------------------
# RingCentral SMS — poll + send (JWT flow reused from ringcentral_sync.py)
# ---------------------------------------------------------------------------

def _rc_configured() -> bool:
    return bool(
        getattr(SETTINGS, "RC_CLIENT_ID", "")
        and getattr(SETTINGS, "RC_CLIENT_SECRET", "")
        and getattr(SETTINGS, "RC_JWT", "")
    )


def _rc_http(method, url, headers=None, data=None, timeout=30):
    """Minimal urllib JSON request helper (mirrors ringcentral_sync._http)."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _rc_access_token() -> str:
    """OAuth 2.0 JWT credentials flow (RFC 7523) — verbatim-in-spirit from
    ``ringcentral_sync.get_access_token``: Basic auth of client:secret, POST
    the jwt-bearer assertion to the token endpoint, return ``access_token``.

    Raises on missing creds / auth failure — the poll/send wrappers catch it
    and return ``'not configured'`` / ``{'ok': False, ...}`` respectively.
    """
    server = (getattr(SETTINGS, "RC_SERVER_URL", "")
              or "https://platform.ringcentral.com").rstrip("/")
    client_id = SETTINGS.RC_CLIENT_ID
    client_secret = SETTINGS.RC_CLIENT_SECRET
    jwt = SETTINGS.RC_JWT
    basic = base64.b64encode(
        f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }).encode()
    tok = _rc_http(
        "POST", f"{server}/restapi/oauth/token",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    return tok["access_token"]


# RingCentral messageType -> our local ``channel`` label. We ingest all three
# inbound types; anything else RC might return is ignored (channel None).
_RC_CHANNEL_BY_TYPE = {
    "SMS": "sms",
    "Fax": "fax",
    "VoiceMail": "voicemail",
}


def _rc_attachment_meta(rec) -> list:
    """Extract lightweight attachment metadata (uri, id) — NEVER the bytes.

    Returns a list of ``{'id':..., 'uri':...}`` dicts for each attachment on a
    message-store record. We deliberately do NOT download: the poll stays fast
    and no PHI-bearing fax/voicemail binary lands on disk from this path. The
    stored uri lets a later, access-controlled step fetch on demand.
    """
    out = []
    for att in (rec.get("attachments") or []):
        if not isinstance(att, dict):
            continue
        out.append({
            "id": att.get("id"),
            "uri": att.get("uri") or att.get("contentUri"),
        })
    return out


def _rc_build_row(rec, channel: str) -> tuple[str, str, Optional[dict]]:
    """Return ``(subject, body, detail)`` for one inbound RC record.

    Body is a short, PHI-light human summary per channel; attachment metadata
    (uri/id — never bytes) rides ``detail`` for fax/voicemail so the binary can
    be fetched later by an access-controlled step:

      * sms       -> body = the SMS text (message-store ``subject`` field).
      * fax       -> body = "Inbound fax, N page(s) from <number>"; detail
                     carries pageCount + attachment metadata.
      * voicemail -> body = "Voicemail from <number>, Ds"; detail carries
                     duration + attachment metadata.
    """
    from_info = rec.get("from", {}) or {}
    from_num = from_info.get("phoneNumber", "") or ""

    # RC's own creationTime (when the patient actually sent it) rides ``detail``
    # for EVERY channel so downstream age logic (the Legacy Requests split) can
    # use the real message time, not our ingest time.
    created = rec.get("creationTime")

    if channel == "sms":
        # SMS text lives in the message-store ``subject`` field.
        return "", (rec.get("subject", "") or ""), {"creationTime": created}

    if channel == "fax":
        # RC exposes the page count as ``faxPageCount`` (fall back to
        # ``pageCount`` if a future API rev renames it). Cast defensively.
        page_count = rec.get("faxPageCount")
        if page_count is None:
            page_count = rec.get("pageCount")
        try:
            page_count = int(page_count)
        except (TypeError, ValueError):
            page_count = None
        pages_txt = page_count if page_count is not None else "?"
        body = f"Inbound fax, {pages_txt} page(s) from {from_num}"
        detail = {"creationTime": created,
                  "pageCount": page_count,
                  "attachments": _rc_attachment_meta(rec)}
        return "", body, detail

    if channel == "voicemail":
        # Voicemail duration lives on the AudioRecording attachment; RC also
        # sometimes exposes ``vmDuration`` at the record root. Try both.
        duration = rec.get("vmDuration")
        if duration is None:
            for att in (rec.get("attachments") or []):
                if isinstance(att, dict) and att.get("duration") is not None:
                    duration = att.get("duration")
                    break
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = None
        dur_txt = duration if duration is not None else "?"
        body = f"Voicemail from {from_num}, {dur_txt}s"
        detail = {"creationTime": created,
                  "duration": duration,
                  "attachments": _rc_attachment_meta(rec)}
        return "", body, detail

    # Unknown channel — should not happen (callers gate on _RC_CHANNEL_BY_TYPE).
    return "", "", None


def poll_ringcentral(limit: int = 30):
    """Poll RingCentral for recent INBOUND messages (SMS + fax + voicemail).

    Returns the number of NEW rows inserted, or the string ``'not configured'``
    when the RC creds are absent. Reads the last 7 days of inbound messages
    from the extension message-store, ingesting all three inbound message types
    (SMS, Fax, VoiceMail); each row's ``channel`` reflects its type
    ('sms' | 'fax' | 'voicemail'). Deduped by the RC message id (row
    ``external_id``) across all types.

    Fax/voicemail bodies are short PHI-light summaries and their attachment
    metadata (uri/id — never bytes) is stored in ``detail``; the binary is NOT
    downloaded here.

    Any HTTP/parse failure is logged (counts only) and re-raised — ``poll_all``
    turns an arm's exception into an honest ``'error: ...'`` marker.
    """
    if not _rc_configured():
        return "not configured"

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    if limit < 1:
        limit = 1

    server = (getattr(SETTINGS, "RC_SERVER_URL", "")
              or "https://platform.ringcentral.com").rstrip("/")
    token = _rc_access_token()
    date_from = (datetime.now(timezone.utc) - timedelta(days=7)
                 ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # Poll each message type in its OWN capped walk. A single combined stream
    # with one shared cap let a burst of >limit SMS consume the whole budget and
    # break before reaching any fax/voicemail records further down the newest-
    # first stream — so a voicemail cancellation could be perpetually starved and
    # eventually age past the 7-day window unseen. Per-channel caps guarantee
    # each type gets its own budget. Dedupe by rc_id means anything above a
    # channel's cap is picked up on the next poll (never silently dropped).
    inserted = 0
    for rc_type in ("SMS", "Fax", "VoiceMail"):
        ch_inserted = 0
        page = 1
        while ch_inserted < limit:
            qs = urllib.parse.urlencode({
                "messageType": rc_type,
                "direction": "Inbound",
                "dateFrom": date_from,
                "perPage": 250,
                "page": page,
            })
            url = (f"{server}/restapi/v1.0/account/~/extension/~/"
                   f"message-store?{qs}")
            data = _rc_http("GET", url,
                            headers={"Authorization": f"Bearer {token}"})

            for rec in (data.get("records", []) or []):
                if ch_inserted >= limit:
                    break
                rc_id = rec.get("id")
                if rc_id is None:
                    continue
                # Inbound only (defensive: we asked for Inbound, but never trust
                # the server to filter — a mislabelled outbound row must not be
                # stored as an inbound patient message).
                if str(rec.get("direction", "")).lower() != "inbound":
                    continue
                channel = _RC_CHANNEL_BY_TYPE.get(rec.get("type"))
                if channel is None:
                    continue  # unknown/unsupported type — skip.

                from_info = rec.get("from", {}) or {}
                to_list = rec.get("to", []) or []
                to_info = to_list[0] if to_list else {}
                sender = from_info.get("phoneNumber", "") or ""
                recipient = to_info.get("phoneNumber", "") or ""

                subject, body, detail = _rc_build_row(rec, channel)
                rid = _insert_message(
                    channel=channel,
                    direction="in",
                    sender=sender,
                    recipient=recipient,
                    subject=subject,
                    body=body,
                    external_id=str(rc_id),
                    thread_ref="",
                    status="new",
                    detail=detail,
                )
                if rid:
                    ch_inserted += 1

            paging = data.get("paging", {}) or {}
            if page >= (paging.get("totalPages", 1) or 1):
                break
            page += 1
        inserted += ch_inserted

    if inserted:
        audit.log(
            actor="system", kind="comms", action="poll_ringcentral",
            detail={"inserted": inserted}, outcome="ok",
        )
    return inserted


def send_sms(to: str, text: str, from_number: str = "") -> dict:
    """Send an SMS via RingCentral; store an outbound ``messages`` row.

    ``from_number`` (optional) selects which office RC number the text is sent
    FROM — pass it to reply on the same number the patient contacted (thread
    continuity). Falls back to ``RC_FROM_NUMBER`` when omitted.

    Returns ``{'ok': True, 'external_id': <rc id>}`` on success, or
    ``{'ok': False, 'error': <reason>}`` on any failure (not configured, no
    recipient/text, no from number, API error).
    """
    if not _rc_configured():
        return {"ok": False, "error": "not configured"}
    if not to or not str(to).strip():
        return {"ok": False, "error": "no recipient"}
    from_number = (str(from_number or "").strip()
                   or getattr(SETTINGS, "RC_FROM_NUMBER", "") or "")
    if not from_number:
        return {"ok": False, "error": "no from number (pass from_number or set RC_FROM_NUMBER)"}

    server = (getattr(SETTINGS, "RC_SERVER_URL", "")
              or "https://platform.ringcentral.com").rstrip("/")
    try:
        token = _rc_access_token()
    except Exception as e:
        logger.error("comms.send_sms: token exchange failed: %s", e)
        return {"ok": False, "error": f"auth failed: {e}"}

    payload = json.dumps({
        "from": {"phoneNumber": from_number},
        "to": [{"phoneNumber": str(to).strip()}],
        "text": str(text or ""),
    }).encode("utf-8")
    url = f"{server}/restapi/v1.0/account/~/extension/~/sms"
    try:
        resp = _rc_http(
            "POST", url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            data=payload,
        )
    except Exception as e:
        logger.error("comms.send_sms: send failed: %s", e)
        return {"ok": False, "error": f"send failed: {e}"}

    rc_id = resp.get("id")
    external_id = str(rc_id) if rc_id is not None else (
        f"sent-sms-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}")

    _insert_message(
        channel="sms",
        direction="out",
        sender=from_number,
        recipient=str(to).strip(),
        subject="",
        body=str(text or ""),
        external_id=external_id,
        thread_ref="",
        status="sent",
        detail=None,
    )
    audit.log(
        actor="system", kind="comms", action="send_sms",
        detail={"external_id": external_id}, outcome="ok",
    )
    return {"ok": True, "external_id": external_id}


# ---------------------------------------------------------------------------
# Poll all channels
# ---------------------------------------------------------------------------

def poll_all() -> dict:
    """Poll every inbound channel; return per-channel results.

    Shape (backward compatible):
        ``{'email': <int|'not configured'>, 'sms': <int|'error: ...'>}``

    When Gmail accounts ARE configured, a NEW ``email_accounts`` key is added:
        ``{'email_accounts': {addr: <int inserted> | 'error: <ClassName>'}}``
    giving a per-mailbox breakdown across the three practice mailboxes. The
    top-level ``email`` value stays the backward-compatible TOTAL (an int, or
    ``'not configured'`` when zero accounts, or ``'error: ...'`` only if the
    whole arm blew up). ``email_accounts`` is omitted entirely when no accounts
    are configured, so existing callers see the exact old shape.

    Each arm is independently try/excepted so ONE channel's failure can never
    break the other or raise to the caller: an arm's exception becomes the
    string ``'error: <reason>'`` for that key. Errors are logged with the
    channel name only — never any message content.
    """
    result: dict = {}

    # Email: poll every account ONCE via poll_gmail_by_account, then derive the
    # backward-compatible total from that same breakdown (no double-poll).
    if _gmail_configured():
        try:
            per = poll_gmail_by_account()
            result["email_accounts"] = per
            # poll_gmail_by_account swallows each account's exception into an
            # 'error: <Class>' STRING, so a naive sum() of the int values would
            # report a clean-looking 0 even when EVERY mailbox failed — the
            # exact "don't report a clean 0" honesty rule this module lives by,
            # and the all-fail re-raise poll_gmail does. Mirror that guard here:
            # if there are accounts and EVERY one came back an error string (no
            # int success anywhere), surface an honest 'error: ...' so the
            # scheduler records outcome='error' instead of a silent quiet-inbox.
            int_counts = [v for v in per.values() if isinstance(v, int)]
            error_vals = [v for v in per.values() if isinstance(v, str)]
            if per and not int_counts and error_vals:
                result["email"] = (
                    f"error: all {len(error_vals)} account(s) failed"
                )
            else:
                result["email"] = sum(int_counts)
        except Exception as e:
            logger.error("comms.poll_all: email arm failed: %s",
                         type(e).__name__)
            result["email"] = f"error: {e}"
    else:
        result["email"] = "not configured"

    try:
        result["sms"] = poll_ringcentral()
    except Exception as e:
        logger.error("comms.poll_all: sms arm failed: %s", type(e).__name__)
        result["sms"] = f"error: {e}"
    return result


# ---------------------------------------------------------------------------
# Local message store — list / get / update
# ---------------------------------------------------------------------------

def list_messages(channel: Optional[str] = None,
                  status: Optional[str] = None,
                  limit: int = 50) -> list:
    """Return message rows (newest first) as dicts, optionally filtered.

    ``channel`` / ``status`` are optional exact-match filters; either may be
    ``None`` (or empty) to skip that filter. ``detail`` is JSON-decoded by
    ``_row_to_dict``.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    if limit < 0:
        limit = 0

    clauses = []
    args: list = []
    if channel:
        clauses.append("channel = ?")
        args.append(channel)
    if status:
        clauses.append("status = ?")
        args.append(status)
    sql = "SELECT * FROM messages"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    conn = db.get_conn()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_message(message_id: int) -> Optional[dict]:
    """Return one message row as a dict, or ``None`` if the id is unknown."""
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def update_message(message_id: int, status: str) -> bool:
    """Set a message's ``status``; return ``True`` if a row was updated.

    Returns ``False`` for an unknown id (so the server can answer 404 honestly,
    matching the referral-update pattern). Writes an audit row (ids only) on a
    real update.
    """
    conn = db.get_conn()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE messages SET status = ? WHERE id = ?",
                (str(status), message_id),
            )
            updated = cur.rowcount > 0
    finally:
        conn.close()

    if updated:
        audit.log(
            actor="system", kind="comms", action="update_message",
            detail={"message_id": message_id, "status": str(status)},
            outcome="ok",
        )
    return updated

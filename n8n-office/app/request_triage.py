"""app/request_triage.py — surface patient REQUESTS from inbound SMS/fax.

The referral audit (``app.referral_audit``) only scans *email* for new-patient
referrals. But patients also text and fax the office with actionable requests —
"Schedule appointment", "Do you have anything next week?", "I need to
reschedule", "cancel my visit", "please send my records" — and those inbound
messages were being stored (``messages`` table, ``status='new'``) with nothing
lifting them into the human action queue. They drowned in a flat inbox of
hundreds of messages (delivery receipts, confirmations, spam, faxes).

This module closes that gap with the SAME deterministic, keyword-gated approach
``_looks_like_referral`` uses for email:

  * A POSITIVE signal gate (``_REQUEST_SIGNALS``) — a message is only lifted
    when it carries a real scheduling / care-coordination intent. A
    false-negative (a request phrased with none of these words) merely stays in
    the inbox unflagged — safe. The false-positive it prevents is a queue flood.
  * One deduped ``manual_task`` approval card per matching message, shaped like
    the referral audit's cards so the UI renders them identically.
  * The matched message is marked ``status='triaged'`` (an existing status) so
    it leaves the unread inbox and is never re-scanned. Non-matching messages
    stay ``'new'`` and remain visible in the inbox for reference.

Deterministic and free (regex only) — no LLM call per message, so it fits the
office's tight API budget and can run on every 5-minute comms poll.
"""

from __future__ import annotations

import logging
import re

from app import approvals, comms

logger = logging.getLogger(__name__)

# Channels we triage. Email has its own dedicated path (referral_audit); voicemail
# bodies are short transcripts and are included because a voicemail asking to
# reschedule is exactly the kind of request we must not miss.
_TRIAGE_CHANNELS = ("sms", "fax", "voicemail")

# Ordered (label, pattern) intents. First match wins and becomes the card's
# request-type hint. Ordered so the more specific/actionable intents (cancel,
# reschedule) are reported ahead of the generic "schedule".
_REQUEST_SIGNALS: list[tuple[str, re.Pattern]] = [
    ("reschedule", re.compile(r"\bre-?schedul|\bmove (?:my|the) appoint|\bchange (?:my|the) appoint|\bpush (?:it|my|the) back|\bdifferent (?:day|time)\b", re.I)),
    ("cancel", re.compile(r"\bcancel|\bcan'?t make it\b|\bwon'?t make it\b|\bnot (?:going to|gonna) make", re.I)),
    ("records", re.compile(r"\brecords?\b|\bpaperwork\b|\bforms?\b|\breferral (?:form|paper)|\bfax (?:my|the|over)|\bmedical record", re.I)),
    ("confirm_slot", re.compile(r"\bi'?ll take (?:it|the|that|\d)|\btake (?:the|that) (?:slot|time|appoint)|\bthat works\b|\bworks for me\b|\bsee you (?:then|on)\b", re.I)),
    ("availability", re.compile(r"\bavailab|\bopening|\bany (?:openings?|slots?|times?)\b|\bdo (?:you|u) have (?:any|anything|something)|\bwhat(?:'?s| is) open|\bnext week\b|\bsooner\b|\bearlier appoint", re.I)),
    ("callback", re.compile(r"\bcall me\b|\bcall (?:me )?back\b|\bgive me a call\b|\bplease call\b|\breach me\b", re.I)),
    ("schedule", re.compile(r"\bschedul|\bappoint|\bappt\b|\bbook (?:an?|my)?\s*(?:appoint|visit)?|\bget (?:me )?in\b|\bset up (?:an?|my) (?:appoint|visit)|\bwhen can i (?:be seen|come in)", re.I)),
]


# RingCentral delivers SMS "tapback" reactions as pseudo-messages that QUOTE the
# original text — e.g. '👍 to "Good day! …your appointment…"' or 'Removed 👍 from
# "…"'. The quoted original often contains request words, so without this guard a
# thumbs-up on an old thread would raise a bogus "schedule" card. Reactions always
# have the shape <emoji> to|from "…"  (optionally led by "Removed").
#
# The pattern REQUIRES an actual reaction glyph before to/from (emoji or the word
# "Removed") — it must NOT match a plain message that merely starts with the word
# "to"/"from" followed by a quote (e.g. 'from "my prior clinic" send my records'),
# which would silently drop a real patient request (audit finding #3).
#
# It is also anchored and non-backtracking: the old `\W*\s*` had two overlapping
# quantifiers (a space matches BOTH \W and \s), causing catastrophic backtracking
# (ReDoS) on bodies that begin with a long run of whitespace/punctuation and never
# reach a `to/from "` — fax-to-text/OCR and multi-line SMS routinely do. A single
# emoji run (no \s* beside it) and the body[:400] length guard below keep it O(n)
# and sub-millisecond even on multi-thousand-char whitespace input (audit #1).
_REACTION_RE = re.compile(
    r'^\s*(?:removed\s+)?[\U0001F000-\U0001FAFF☀-➿️‍]+\s*'
    r'(?:to|from)\s+["“]',
    re.I,
)


def _is_reaction(body: str) -> bool:
    """True when a message body is a RingCentral tapback reaction, not real text."""
    # Only inspect the head of the body: reactions always lead with the glyph, and
    # bounding the input is defense-in-depth against pathological long inputs.
    return bool(_REACTION_RE.match(str(body or "")[:400]))


def detect_request(subject: str, body: str) -> str:
    """Return the first matching request-intent label, or "" if none.

    Matches case-insensitively against ``subject`` + ``body`` as one blob, the
    same way ``_looks_like_referral`` does. Tapback reactions are never requests.
    """
    if _is_reaction(body):
        return ""
    blob = f"{subject or ''}\n{body or ''}"
    for label, pattern in _REQUEST_SIGNALS:
        if pattern.search(blob):
            return label
    return ""


def _external_id_already_carded(external_id: str) -> bool:
    """True if a PENDING manual_task card already references this message.

    Belt-and-suspenders against a crash between enqueue and the status update:
    even though we only scan ``status='new'`` messages, a card raised on a
    previous run that failed to mark the message must not be raised twice.
    Matched on ``params.source_external_id`` set below.
    """
    if not external_id:
        return False
    for card in approvals.list_approvals(status="pending", limit=500):
        if card.get("action_type") != "manual_task":
            continue
        params = card.get("params") or {}
        if isinstance(params, dict) and params.get("source_external_id") == external_id:
            return True
    return False


def _snippet(body: str, limit: int = 160) -> str:
    """One-line, whitespace-collapsed preview of the message body."""
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    return text[:limit]


def scan_inbound_requests(created_by: str = "scheduler",
                          per_channel_limit: int = 500) -> dict:
    """Scan new inbound SMS/fax/voicemail for patient requests → action cards.

    For each inbound message with ``status='new'`` that carries a request signal,
    raise ONE ``manual_task`` approval card and mark the message ``'triaged'``.
    Idempotent: only ``'new'`` messages are scanned and each is flipped to
    ``'triaged'`` once carded, so re-running never stacks duplicates.

    Returns a summary dict:
      ``{scanned, matched, cards_created, skipped_dupe, errors, by_type}``.
    """
    scanned = 0
    matched = 0
    cards_created = 0
    skipped_dupe = 0
    errors = 0
    by_type: dict[str, int] = {}

    for channel in _TRIAGE_CHANNELS:
        try:
            rows = comms.list_messages(channel=channel, status="new",
                                       limit=per_channel_limit)
        except Exception as exc:
            logger.error("request_triage: list_messages(%s) failed: %s",
                         channel, exc)
            errors += 1
            continue

        for row in rows:
            # Inbound only — never triage our own outbound texts as requests.
            if str(row.get("direction", "")).lower() != "in":
                continue
            scanned += 1

            subject = row.get("subject") or ""
            body = row.get("body") or ""
            intent = detect_request(subject, body)
            if not intent:
                continue  # routine / non-request message — leave in inbox
            matched += 1

            external_id = str(row.get("external_id") or "")
            if _external_id_already_carded(external_id):
                skipped_dupe += 1
                continue

            sender = row.get("sender") or "unknown number"
            msg_id = row.get("id")
            snippet = _snippet(body)
            label = intent.replace("_", " ")

            try:
                approvals.enqueue(
                    action_type="manual_task",
                    params={
                        "title": f"Patient {channel} — {label}: {sender}",
                        "detail": (f"Inbound {channel} from {sender} looks like a "
                                   f"'{label}' request:\n\n\"{snippet}\"\n\n"
                                   f"Reply / act via RingCentral, then mark done."),
                        "request_type": intent,
                        "channel": channel,
                        "patient_phone": sender,
                        "source_message_id": msg_id,
                        "source_external_id": external_id,
                        "snippet": snippet,
                    },
                    reason=(f"Inbound {channel} patient request ({label}) from "
                            f"{sender}: {snippet[:80]}"),
                    requested_by=created_by,
                )
                # Lift it out of the unread inbox BEFORE counting so a mark
                # failure lands in `errors` rather than a silently-counted card;
                # dedup by source_external_id still prevents re-carding next poll.
                if msg_id is not None:
                    comms.update_message(int(msg_id), "triaged")
                cards_created += 1
                by_type[intent] = by_type.get(intent, 0) + 1
            except Exception as exc:
                logger.error("request_triage: enqueue/mark failed for msg %s: %s",
                             msg_id, exc)
                errors += 1

    summary = {
        "scanned": scanned,
        "matched": matched,
        "cards_created": cards_created,
        "skipped_dupe": skipped_dupe,
        "errors": errors,
        "by_type": by_type,
    }
    if cards_created or errors:
        logger.info("request_triage: %s", summary)
    return summary

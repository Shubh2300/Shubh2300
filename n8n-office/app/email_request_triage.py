"""app/email_request_triage.py — surface actionable REQUESTS from inbound EMAIL.

Sibling to ``app.request_triage`` (which triages inbound SMS/fax/voicemail). The
referral audit (``app.referral_audit``) already scans email, but ONLY for
new-patient *referrals* — "please see this patient" leads it searches against
the live EMR. That leaves the biggest slice of the inbox uncarded: the office's
mailboxes are dominated by law-firm and patient EMAIL asks that are NOT
referrals —

  * "Please send all records for the client…" / "REQ: <name> - Records"
    (records release — by FAR the biggest category);
  * "Please send the itemized bill" / "per the ledger…" (billing);
  * "Would it be possible to schedule Mr. X…" (scheduling).

Those messages were stored (``messages`` table, ``channel='email'``,
``status='new'``) with nothing lifting them into the human action queue, so they
drowned in a flat inbox of 800+ rows (newsletters, marketing, delivery/fax
receipts, the app's own Intake Digest, out-of-office auto-replies).

This module closes that gap with the SAME deterministic, keyword-gated approach
``request_triage`` uses for SMS — one deduped ``manual_task`` approval card per
matching message, the message flipped to ``status='triaged'`` so it leaves the
unread inbox and is never re-scanned.

PRECISION IS THE WHOLE JOB. Email is a firehose of non-actionable mail, so a
false positive here floods the human queue and defeats the point. The classifier
is therefore built as a **negative gate FIRST, positive gate SECOND**:

  1. ``_is_excluded`` drops the noise up front — newsletters/substack, marketing
     blasts, delivery/fax/receipt bots, the practice's OWN self-sent mail
     (Intake Digest / Intake Alert / AI Ops proofs, the three practice
     mailboxes), out-of-office auto-replies, and pure "thank you" replies with
     no ask. A message that trips the exclusion gate is NEVER carded, even if it
     also contains a request word ("your bill is ready" marketing, a newsletter
     that says "schedule a demo", etc.).
  2. ``_positive_intent`` only then looks for a real actionable ask, ordered
     records → billing → scheduling → other-actionable so the biggest, most
     specific category wins the label.

A false NEGATIVE (a genuine ask phrased with none of the signal words) merely
stays in the inbox unflagged — safe. The false POSITIVE it prevents is a queue
flood, which is the failure that matters.

CRITICAL SAFETY: detecting a records request here NEVER sends anything. This
module only STAGES a ``manual_task`` card for human approval — identical to
every other lane. No EMR call, no Drive fetch, no ``send_email``. The actual
release is a separate, human-approved step (``comms.send_email`` fails closed on
missing attachments; that is deliberately not reached from here).

Deterministic and free (regex only) — no LLM call per message, so it fits the
office's tight API budget and can run on every 5-minute comms poll.
"""

from __future__ import annotations

import logging
import re

from app import approvals, comms

logger = logging.getLogger(__name__)

# Only inbound email is triaged here. (SMS/fax/voicemail → request_triage;
# new-patient referrals → referral_audit.)
_CHANNEL = "email"


# ---------------------------------------------------------------------------
# NEGATIVE GATE — exclusions (checked FIRST; a hit here is never carded)
# ---------------------------------------------------------------------------

# The practice's OWN mailboxes. Gmail's IMAP poll stores a copy of self-sent /
# self-addressed mail as direction='in' (see comms._poll_gmail_account), so the
# app's own Intake Digest, Intake Alerts and "live send proof" land in this
# backlog with our own address as sender. Never card our own automated mail.
_SELF_SENDERS = (
    "mainlinesurgery@gmail.com",
    "mainlinepain@gmail.com",
    "mainsurgical@gmail.com",
    # The practice owner's own outbound thought-leadership / vendor-eval threads
    # (self-authored from a personal domain) get stored direction='in' too and
    # are not inbound patient asks.
    "sanjay@healthtimetv.com",
    "healthtimetv.com",
)

# Subjects the app itself generates (Apps Script intake + this hub). These are
# self-notifications, not inbound asks — carding them would be an infinite loop
# of the queue notifying itself. Matched case-insensitively at the START of the
# subject (after any Re:/Fwd: prefix is stripped by _clean_subject).
_SELF_SUBJECT_PREFIXES = (
    "[intake]",
    "[intake alert]",
    "[intake digest]",
    "[ai ops]",
)

# Bulk/marketing/newsletter/receipt senders — substring match on the raw From.
# These are the high-volume noise sources in the real backlog. Kept as plain
# substrings (not regex) so the list reads like an allow/deny file. A domain
# here is excluded outright regardless of body content.
_EXCLUDE_SENDER_SUBSTRINGS = (
    # transactional / notification bots
    "noreply", "no-reply", "donotreply", "do-not-reply", "notify@",
    "notifications@", "mailer-daemon", "postmaster@",
    # newsletters / content platforms
    "substack.com", "@mail.", "@e.mail.", "@email.", "@e1.", "@em.",
    "news@", "newsletter", "@us-news.",
    # known marketing / vendor blast domains seen in the backlog
    "gopro", "vistaprint", "grammarly", "instagram", "punchbowl",
    "beatthebomb", "stackcommerce", "goamsive", "squareup", "flyfrontier",
    "email-whitepages", "metamail", "mailerlite", "hubspot", "nextiva",
    "angi.com", "@em.angi", "keychain.com", "washingtonpost", "theathletic",
    "pseg.com", "toast-restaurants", "sensingfuture", "cloudhq", "alison.com",
    "olympiapharm", "adp.com",
    # automated alerts / support-desk / job-board / vendor-billing bots that
    # are NOT patient/legal record/billing asks (real-backlog false positives):
    "conversation-",          # Indeed applicant-relay address prefix
    "alerts@tdbank", "tdbank.com", "brainhq", "positscience", "billingnest",
    "as-sprink", "@phc4.", "healthtechconnex", "swaymedical",
    "@aetna.com", "aetna.com", "cclrinc.com",
    "balaplazamgmt",          # building/property management (COI reminders)
)

# Subject/body markers of bulk mail — an unsubscribe footer, "view in browser",
# a verification code, a scheduled-report header. A message carrying one of
# these AND lacking any human 1:1 framing is treated as bulk and excluded.
_BULK_MARKER_RE = re.compile(
    r"unsubscribe|view (?:this )?(?:post|email) (?:on the web|in (?:your )?browser)|"
    r"manage your (?:preferences|subscription)|"
    r"verification code|sign[- ]?in (?:link|code)|authentication code|"
    r"daily (?:sales|fast|digest)|sales summary|"
    r"you (?:have )?received a newsletter|exclusive offer|% off\b|"
    r"special offer|deal days|loan offer",
    re.I,
)

# Out-of-office / vendor auto-replies — never an ask.
_AUTO_REPLY_RE = re.compile(
    r"\bautomatic reply\b|\bout of (?:the )?office\b|\bauto[- ]?reply\b|"
    r"\baway from my (?:desk|email)\b|\bwill (?:be )?out of the office\b",
    re.I,
)

# A pure acknowledgement with no ask — "thank you", "thanks!", "received, thx".
# Only excludes when the WHOLE (short) body is an ack and no request word is
# present; the positive gate would not fire on these anyway, but excluding them
# explicitly keeps the dry-run's "why-excluded" honest.
_THANKS_ONLY_RE = re.compile(
    r"^\s*(?:thanks?|thank you|ty|much appreciated|great,? thanks?|"
    r"perfect,? thank you|received,? thank you|got it,? thanks?)"
    r"[\s.!,]*$",
    re.I,
)


def _clean_subject(subject: str) -> str:
    """Lowercased subject with leading Re:/Fwd: reply prefixes stripped.

    So a forwarded/replied self-notification ("Re: [Intake] …") still matches
    the self-subject prefixes below.
    """
    s = str(subject or "").strip().lower()
    # Strip any run of leading "re:" / "fwd:" / "fw:" prefixes.
    while True:
        m = re.match(r"\s*(?:re|fwd?|aw)\s*:\s*", s)
        if not m:
            break
        s = s[m.end():]
    return s


def _sender_excluded(sender: str) -> bool:
    """True when the From address is a self/bulk/marketing/notification sender."""
    s = str(sender or "").lower()
    if not s:
        return False
    for self_addr in _SELF_SENDERS:
        if self_addr in s:
            return True
    for sub in _EXCLUDE_SENDER_SUBSTRINGS:
        if sub in s:
            return True
    return False


def exclusion_reason(sender: str, subject: str, body: str) -> str:
    """Return a short reason this message is excluded, or "" if not excluded.

    The reason string powers the dry-run's ``why-excluded`` samples and keeps
    the exclusion logic auditable. Order matters: most-specific/self first.
    """
    clean_subj = _clean_subject(subject)
    for pref in _SELF_SUBJECT_PREFIXES:
        if clean_subj.startswith(pref):
            return "self-generated app notification (Intake/AI Ops)"

    s = str(sender or "").lower()
    for self_addr in _SELF_SENDERS:
        if self_addr in s:
            return "self-sent from a practice mailbox"

    blob = f"{subject or ''}\n{body or ''}"
    if _AUTO_REPLY_RE.search(blob):
        return "out-of-office / automatic reply"

    if _sender_excluded(sender):
        return "bulk / marketing / notification sender"

    if _BULK_MARKER_RE.search(blob):
        return "bulk-mail marker (newsletter / receipt / offer)"

    # Pure ack with no ask — evaluate on a whitespace-normalised body so a
    # trailing signature/newlines don't defeat the anchored match.
    norm_body = re.sub(r"\s+", " ", str(body or "")).strip()
    if norm_body and _THANKS_ONLY_RE.match(norm_body):
        return "acknowledgement only (no request)"

    return ""


# ---------------------------------------------------------------------------
# POSITIVE GATE — actionable intents (checked only after exclusions pass)
# ---------------------------------------------------------------------------

# A legal/records-request sender domain is a STRONG standalone signal: mail from
# a personal-injury / workers-comp firm to a medical office is, in practice,
# almost always a records or billing ask. Seen in the real backlog. Used to
# upgrade confidence, never as the sole trigger (a positive-intent phrase or a
# DOB/patient-id line must also be present — see _positive_intent).
_LAW_FIRM_RE = re.compile(
    r"law\.com|lawyers?\.com|legal\.com|@.*law@|"
    r"mylosflaw|tesslerlaw|jfinelaw|pearsonkoutcherlaw|hgsklawyers|"
    r"s2firm|missicklaw|mrgordonlaw|dionsolomon|"
    r"\blaw\b|\bl\.?l\.?p\b|\besq\b|attorney|paralegal",
    re.I,
)

# A DOB / date-of-injury / patient-identifier line — the fingerprint of a
# records/billing request about a specific patient ("DOB: 01/07/1988",
# "D/BIRTH: 8/30/79", "DOA: 4/23/2025"). Strong corroborating signal.
_PATIENT_ID_RE = re.compile(
    r"\bd(?:ate)?\.?\s*(?:o(?:f)?\.?\s*)?b(?:irth)?\.?\s*[:#]|"
    r"\bd/?birth\b|\bd\.?o\.?b\.?\b|\bd\.?o\.?a\.?\b|"
    r"\bdate of (?:birth|injury|accident|service)\b|"
    r"\bour client\b|\bclaim(?:ant)?\s*(?:no|number|#)",
    re.I,
)

# Ordered (label, pattern). First positive match wins the card's intent label.
# Ordered records → billing → scheduling → other so the biggest / most specific
# actionable category is reported first when several words co-occur.
#
# records/billing patterns are SELF-STANDING (specific enough to card on their
# own — "itemized bill", "medical records request", "HCFA form" are
# unambiguous). scheduling and other_actionable are DELIBERATELY weaker phrases
# that also appear in building-management / HR / vendor mail ("schedule a
# demo", "please send the COI"), so they require a clinical/legal CONTEXT
# corroboration (see _has_clinical_context) before they card. That split is the
# core of the precision filter.
_STRONG_INTENT_SIGNALS: list[tuple[str, re.Pattern]] = [
    ("records_request", re.compile(
        r"\bmedical records?\b|\brelease of (?:information|records)\b|"
        r"\breq(?:uest)?\s*(?:for)?\s*records?\b|\brecords?\s+request\b|"
        r"\brequest for (?:new )?records?\b|"
        r"\bsend (?:me |us |over )?(?:all |the |his |her |their |complete )?"
        r"(?:medical )?records?\b|"
        r"\brecs?\b[^.\n]{0,20}\bbills?\b|\bbills?\b[^.\n]{0,20}\brecs?\b|"
        r"\bsubpoena\b|\bchart\s+request\b|\bcopy of (?:the )?(?:chart|file)\b|"
        r"\bop(?:erative)?\s+notes?\b|\bprogress notes?\b|\boffice notes?\b|"
        r"\bmri(?:'?s)?\b|\bxr-?rec|\bfilms?\b|\bbooking packet\b",
        re.I)),
    ("billing", re.compile(
        r"\bitemized (?:bill|statement|ledger)\b|"
        r"\bsend (?:me |us |over )?(?:the |an? )?(?:itemized )?bill\b|"
        r"\bbilling (?:question|sheet|sheets|statement|records?|dispute|request)\b|"
        r"\bper the ledger\b|\bthe ledger\b|"
        r"\bstatement of (?:account|charges)\b|\boutstanding balance\b|"
        r"\bhcfa\b|\bcms[- ]?1500\b|\bub[- ]?04\b|\bexplanation of benefits\b|"
        r"\bcase status\s*-?\s*lien\b|\blien\b",
        re.I)),
]

# Weaker phrases that only card WITH clinical/legal context (below).
_SCHEDULING_RE = re.compile(
    r"\bre-?schedul|\bschedule (?:an?|the|this|his|her|them|patient|client)\b|"
    r"\breq(?:uest)?\s*(?:for)?\s*(?:an?\s*)?appointment\b|"
    r"\breq for appointment\b|\bnew appointment\b|"
    r"\b(?:would it be )?possible to (?:schedule|book|see)\b|"
    r"\bwas (?:this|the) (?:client|patient) (?:called and )?scheduled\b|"
    r"\bbook (?:him|her|them|the client|the patient|an appointment)\b|"
    r"\bget (?:him|her|them|the client|the patient) in\b|"
    r"\bfollow[- ]?up (?:visit|appointment)\b",
    re.I,
)

_OTHER_ACTIONABLE_RE = re.compile(
    r"\bplease (?:send|forward|provide|complete|fill out|sign|reach out|advise)\b|"
    r"\bcould you (?:please )?(?:send|provide|forward|complete)\b|"
    r"\bkindly (?:send|provide|forward)\b|"
    r"\bawaiting your\b|\bplease respond\b",
    re.I,
)


def _has_clinical_context(sender: str, subject: str, body: str) -> bool:
    """True when a message is clearly ABOUT a patient / legal case.

    The weak-intent lanes (scheduling / other_actionable) only card when this
    holds, so a "schedule a demo" newsletter or a "please send the COI"
    building-management reminder never reaches the queue. Context = a law-firm
    sender, OR a patient-identifier / DOB / date-of-injury line, OR an explicit
    patient/case token (a case number, "MLS###", "MAIN LINE ASC", "our
    client", "the patient").
    """
    blob = f"{subject or ''}\n{body or ''}"
    if _LAW_FIRM_RE.search(str(sender or "")):
        return True
    if _PATIENT_ID_RE.search(blob):
        return True
    # Case / patient tokens the practice's own case threads use. Kept TIGHT: a
    # real account/case id ("MLS142", "Account #MLS60"), the surgical-schedule
    # GRID header ("<date> - Main Line - <Name>" / "MAIN LINE ASC - <Name>"),
    # or an explicit patient/client phrase. Deliberately NO bare practice-name
    # match — "Main Line Surgical Center" appears in quoted signatures of
    # unrelated credentialing/HR mail, which we must NOT card.
    if re.search(r"\bMLS\s?\d{2,4}\b|"
                 r"\bMAIN LINE (?:ASC|-)\s*[A-Za-z.]|"  # grid: "MAIN LINE - X"
                 r"\bthe patient\b|\bour (?:patient|client)\b|\bmy client\b|"
                 r"\breferral for\b", blob, re.I):
        return True
    return False


def _positive_intent(sender: str, subject: str, body: str) -> str:
    """Return the actionable intent label, or "" when the message has no ask.

    Assumes exclusions have already passed. Two tiers:
      * STRONG signals (records/billing) card on their own — the phrases are
        specific enough that a false positive is unlikely.
      * WEAK signals (scheduling/other) card ONLY with clinical/legal context,
        so generic "schedule"/"please send" language in HR/vendor/building mail
        is not carded.
      * As a final records fallback, a law-firm sender + a patient-id line with
        no explicit phrase is still a records ask (the dominant real category).
    """
    blob = f"{subject or ''}\n{body or ''}"

    for label, pattern in _STRONG_INTENT_SIGNALS:
        if pattern.search(blob):
            return label

    ctx = _has_clinical_context(sender, subject, body)
    if ctx and _SCHEDULING_RE.search(blob):
        return "scheduling"
    if ctx and _OTHER_ACTIONABLE_RE.search(blob):
        return "other_actionable"

    # Law-firm sender + patient-id line, no explicit phrase → records_request
    # ("OUR CLIENT: ARIF PRICE, D/BIRTH: 8/30/79 … please reach out"). Requires
    # BOTH — never the domain alone.
    if _LAW_FIRM_RE.search(str(sender or "")) and _PATIENT_ID_RE.search(blob):
        return "records_request"

    return ""


def classify(sender: str, subject: str, body: str) -> dict:
    """Classify one inbound email → ``{carded, intent, reason}``.

    ``carded`` is True only when the message survives the negative gate AND
    matches a positive intent. ``reason`` is the exclusion reason when excluded,
    else a short "why-carded" note. Pure function, no side effects — used by
    both the live scan and the read-only dry-run.
    """
    excl = exclusion_reason(sender, subject, body)
    if excl:
        return {"carded": False, "intent": "", "reason": excl}

    # Defer genuine new-patient referrals to referral_audit — the canonical owner
    # — so a "please evaluate and schedule the patient" referral is not
    # double-carded by BOTH this module and the audit (their dedup guards are
    # disjoint: this keys on source_external_id, the audit on patient_name). The
    # message stays status='new' so the referral audit still cards it. Mirrors
    # how request_triage leaves all email to referral_audit.
    from app import referral_audit as _ra
    if _ra._looks_like_referral(subject, body):
        return {"carded": False, "intent": "",
                "reason": "deferred to referral audit (referral signal)"}

    intent = _positive_intent(sender, subject, body)
    if not intent:
        return {"carded": False, "intent": "",
                "reason": "no actionable request signal"}

    # A corroborating note for the audit trail (never fabricated — only states
    # which signals fired).
    corro = []
    if _LAW_FIRM_RE.search(str(sender or "")):
        corro.append("law-firm sender")
    if _PATIENT_ID_RE.search(f"{subject or ''}\n{body or ''}"):
        corro.append("patient-id/DOB line")
    note = f"matched {intent.replace('_', ' ')}"
    if corro:
        note += " (" + ", ".join(corro) + ")"
    return {"carded": True, "intent": intent, "reason": note}


# ---------------------------------------------------------------------------
# Dedup + helpers (mirrors request_triage)
# ---------------------------------------------------------------------------

def _external_id_already_carded(external_id: str) -> bool:
    """True if a PENDING manual_task card already references this message.

    Belt-and-suspenders against a crash between enqueue and the status update:
    even though we only scan ``status='new'`` messages, a card raised on a
    previous run that failed to mark the message must not be raised twice.
    Matched on ``params.source_external_id`` (set below).
    """
    if not external_id:
        return False
    for card in approvals.list_approvals(status="pending", limit=500):
        if card.get("action_type") != "manual_task":
            continue
        params = card.get("params") or {}
        if isinstance(params, dict) and \
                params.get("source_external_id") == external_id:
            return True
    return False


def _snippet(body: str, limit: int = 200) -> str:
    """One-line, whitespace-collapsed preview of the message body."""
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    return text[:limit]


def _sender_label(sender: str) -> str:
    """Human label for the card: display name or bare address, never empty."""
    s = str(sender or "").strip()
    return s or "unknown sender"


# ---------------------------------------------------------------------------
# Live scan — enqueue cards + mark triaged (mirrors request_triage)
# ---------------------------------------------------------------------------

def scan_inbound_email_requests(created_by: str = "scheduler",
                                limit: int = 500) -> dict:
    """Scan new inbound EMAIL for actionable requests → manual_task cards.

    For each ``channel='email'`` / ``direction='in'`` message with
    ``status='new'`` that survives the exclusion gate and matches a positive
    intent, raise ONE ``manual_task`` approval card and mark the message
    ``'triaged'``. Idempotent: only ``'new'`` messages are scanned and each is
    flipped to ``'triaged'`` once carded, so re-running never stacks duplicates.

    SAFETY: this stages a human-approval card ONLY. It never sends email, never
    fetches records, never touches the EMR. The intent label (records_request/
    billing/scheduling/other_actionable) is a routing hint for the human.

    Returns a summary dict:
      ``{scanned, matched, cards_created, skipped_dupe, excluded, errors,
         by_intent}``.
    """
    scanned = 0
    matched = 0
    cards_created = 0
    skipped_dupe = 0
    excluded = 0
    errors = 0
    by_intent: dict[str, int] = {}

    try:
        rows = comms.list_messages(channel=_CHANNEL, status="new", limit=limit)
    except Exception as exc:
        logger.error("email_request_triage: list_messages failed: %s", exc)
        return {"scanned": 0, "matched": 0, "cards_created": 0,
                "skipped_dupe": 0, "excluded": 0, "errors": 1, "by_intent": {}}

    for row in rows:
        # Inbound only — never triage our own outbound mail as a request.
        if str(row.get("direction", "")).lower() != "in":
            continue
        scanned += 1

        sender = row.get("sender") or ""
        subject = row.get("subject") or ""
        body = row.get("body") or ""

        result = classify(sender, subject, body)
        if not result["carded"]:
            excluded += 1
            continue
        intent = result["intent"]
        matched += 1

        external_id = str(row.get("external_id") or "")
        if _external_id_already_carded(external_id):
            skipped_dupe += 1
            continue

        msg_id = row.get("id")
        snippet = _snippet(body)
        label = intent.replace("_", " ")
        who = _sender_label(sender)

        try:
            approvals.enqueue(
                action_type="manual_task",
                params={
                    "title": f"Email — {label}: {who}",
                    "detail": (
                        f"Inbound email from {who} looks like a "
                        f"'{label}' request.\n\n"
                        f"Subject: {subject or '—'}\n\n"
                        f"\"{snippet}\"\n\n"
                        "REVIEW then act manually. Nothing has been sent — a "
                        "records/billing release is a separate human-approved "
                        "step; verify the requester's authorization first."
                    ),
                    "request_type": intent,
                    "channel": _CHANNEL,
                    "sender": who,
                    "subject": subject or "",
                    "source_message_id": msg_id,
                    "source_external_id": external_id,
                    "snippet": snippet,
                },
                reason=(f"Inbound email {label} from {who}: {snippet[:80]}"),
                requested_by=created_by,
            )
            # Mark BEFORE counting so a mark failure lands in `errors`, not a
            # silently-counted card; dedup by source_external_id still prevents
            # re-carding on the next poll.
            if msg_id is not None:
                comms.update_message(int(msg_id), "triaged")
            cards_created += 1
            by_intent[intent] = by_intent.get(intent, 0) + 1
        except Exception as exc:
            logger.error("email_request_triage: enqueue/mark failed for msg "
                         "%s: %s", msg_id, exc)
            errors += 1

    summary = {
        "scanned": scanned,
        "matched": matched,
        "cards_created": cards_created,
        "skipped_dupe": skipped_dupe,
        "excluded": excluded,
        "errors": errors,
        "by_intent": by_intent,
    }
    if cards_created or errors:
        logger.info("email_request_triage: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# READ-ONLY dry-run — classify the live backlog, WRITE NOTHING
# ---------------------------------------------------------------------------

def dry_run(limit: int = 5000, sample_size: int = 8) -> dict:
    """Classify the CURRENT inbound-email backlog WITHOUT writing anything.

    Reads every ``channel='email'`` / ``direction='in'`` message (any status)
    up to ``limit`` and runs the same ``classify`` used by the live scan, but
    enqueues NO cards and marks NO messages. Purely diagnostic — safe to run
    against the live DB.

    Returns:
      ``{total_email_scanned, would_card, by_intent,
         sample_carded:[{from, snippet, intent}...],
         sample_excluded:[{from, snippet, why}...]}``.

    Sender addresses / snippets ARE included in the RETURNED dict (the caller is
    a trusted operator surface); they are NEVER logged.
    """
    rows = comms.list_messages(channel=_CHANNEL, status=None, limit=limit)

    total = 0
    would_card = 0
    by_intent: dict[str, int] = {}
    sample_carded: list[dict] = []
    sample_excluded: list[dict] = []

    for row in rows:
        if str(row.get("direction", "")).lower() != "in":
            continue
        total += 1

        sender = row.get("sender") or ""
        subject = row.get("subject") or ""
        body = row.get("body") or ""
        result = classify(sender, subject, body)

        if result["carded"]:
            would_card += 1
            intent = result["intent"]
            by_intent[intent] = by_intent.get(intent, 0) + 1
            if len(sample_carded) < sample_size:
                sample_carded.append({
                    "from": _sender_label(sender),
                    "snippet": _snippet(f"{subject} — {body}", 140),
                    "intent": intent,
                })
        else:
            # Collect a diverse-ish excluded sample (cap at ~6 for the report).
            if len(sample_excluded) < max(sample_size - 2, 6):
                sample_excluded.append({
                    "from": _sender_label(sender),
                    "snippet": _snippet(f"{subject} — {body}", 140),
                    "why": result["reason"],
                })

    return {
        "total_email_scanned": total,
        "would_card": would_card,
        "by_intent": by_intent,
        "sample_carded": sample_carded,
        "sample_excluded": sample_excluded,
    }

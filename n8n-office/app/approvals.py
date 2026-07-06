"""
app/approvals.py — Human-in-the-loop approval queue for EMR writes.

Every state-changing action the assistant proposes (book / cancel /
reschedule an appointment, or a manual task for a human) is first ENQUEUED
here as a pending card. A human then approves or denies it on screen. Only on
approval does the app touch the EMR, and only through the audited
``EMRSessionManager`` write wrappers.

This is the single choke-point that keeps the house rules honest:
  - The agent NEVER executes an EMR write itself — it can only ``enqueue``.
  - A write is executed only after an explicit human ``decide(approve=True)``.
  - The stored ``result`` is the RAW EMR/scraper response, so the card can
    never claim a booking/cancel "worked" unless the scraper said so. Booking
    has no auto-verify path, so an executed booking card always carries the
    scraper's warning and says the write is unverified.

PUBLIC API
  ACTION_TYPES : set[str]
  enqueue(action_type, params, reason, requested_by) -> card dict
  list_approvals(status=None, limit=50) -> list[card]
  async decide(approval_id, approve, decided_by, note='') -> card dict
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app import audit, db
from app.config import SETTINGS

logger = logging.getLogger(__name__)


# The only actions a human may queue/approve. Anything else is rejected at
# enqueue time so a typo can never silently become an un-dispatchable card.
ACTION_TYPES = {
    "book_appointment",
    "cancel_appointment",
    "reschedule_appointment",
    "create_patient",
    "manual_task",
    "send_email",
    "send_sms",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_card(row) -> dict:
    """Convert a sqlite Row into a card dict with params/result JSON-decoded.

    ``params`` and ``result`` are stored as JSON text; decode them back to
    Python objects for the caller. A malformed/NULL value decodes to ``{}``
    (params) or ``None`` (result) rather than raising.
    """
    card = dict(row)
    raw_params = card.get("params")
    try:
        card["params"] = json.loads(raw_params) if raw_params else {}
    except (TypeError, ValueError):
        card["params"] = {}
    raw_result = card.get("result")
    if raw_result:
        try:
            card["result"] = json.loads(raw_result)
        except (TypeError, ValueError):
            card["result"] = raw_result
    else:
        card["result"] = None
    return card


def _fetch_card(conn, approval_id: int):
    row = conn.execute(
        "SELECT * FROM approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

def enqueue(action_type: str, params: dict, reason: str,
            requested_by: str) -> dict:
    """Create a new PENDING approval card and return it.

    Args:
        action_type:  one of ``ACTION_TYPES``.
        params:       action parameters (dict; JSON-serialised for storage).
        reason:       REQUIRED non-empty justification (why this change is
                      being requested). Empty/whitespace → ``ValueError``.
        requested_by: username/agent that requested it.

    Raises:
        ValueError: unknown ``action_type`` or missing ``reason``.

    Every enqueue writes an audit row (kind='approval').
    """
    if action_type not in ACTION_TYPES:
        raise ValueError(
            f"unknown action_type {action_type!r}; "
            f"must be one of {sorted(ACTION_TYPES)}"
        )
    if reason is None or not str(reason).strip():
        raise ValueError("reason is required and must be non-empty")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    ts = _now_iso()
    params_json = json.dumps(params, default=str, ensure_ascii=False)

    conn = db.get_conn()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO approvals "
                "(ts_created, ts_decided, status, action_type, params, "
                " reason, requested_by, decided_by, decision_note, result) "
                "VALUES (?, NULL, 'pending', ?, ?, ?, ?, NULL, NULL, NULL)",
                (ts, action_type, params_json, str(reason).strip(),
                 str(requested_by)),
            )
            approval_id = cur.lastrowid
        row = _fetch_card(conn, approval_id)
    finally:
        conn.close()

    card = _row_to_card(row)
    audit.log(
        actor=requested_by, kind="approval", action="enqueue",
        detail={"approval_id": approval_id, "action_type": action_type,
                "reason": str(reason).strip()},
        outcome="ok",
    )
    return card


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def get_approval(approval_id: int) -> dict | None:
    """Return one approval card as a dict, or None if the id is unknown."""
    conn = db.get_conn()
    try:
        row = _fetch_card(conn, approval_id)
        return _row_to_card(row) if row else None
    finally:
        conn.close()


def list_approvals(status: str | None = None, limit: int = 50) -> list[dict]:
    """Return approval cards (newest first) as dicts with decoded JSON.

    Args:
        status: if given, filter to that status ('pending', 'approved',
                'denied', 'executed', 'failed').
        limit:  maximum rows to return.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    if limit < 0:
        limit = 0

    sql = "SELECT * FROM approvals"
    args: list = []
    if status is not None:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    conn = db.get_conn()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_row_to_card(r) for r in rows]


# ---------------------------------------------------------------------------
# Decide (approve / deny) + execute
# ---------------------------------------------------------------------------

async def decide(approval_id: int, approve: bool, decided_by: str,
                 note: str = "") -> dict:
    """Approve or deny a pending card; on approve, execute the EMR write.

    Deny  → status 'denied' (no EMR contact).
    Approve → status 'approved', then ``await _execute(card)`` flips it to
              'executed' or 'failed' with the RAW EMR response stored in
              ``result``.

    Every transition writes an audit row (kind='approval'). Returns the final
    card dict. Re-deciding a card that is not pending raises ``ValueError``.
    """
    conn = db.get_conn()
    try:
        row = _fetch_card(conn, approval_id)
        if row is None:
            raise ValueError(f"approval {approval_id} not found")
        card = _row_to_card(row)
        if card["status"] != "pending":
            raise ValueError(
                f"approval {approval_id} is {card['status']!r}, "
                "not pending — cannot re-decide"
            )

        ts_decided = _now_iso()

        if not approve:
            with conn:
                conn.execute(
                    "UPDATE approvals SET status='denied', ts_decided=?, "
                    "decided_by=?, decision_note=? WHERE id=?",
                    (ts_decided, str(decided_by), str(note or ""),
                     approval_id),
                )
            final_row = _fetch_card(conn, approval_id)
            final = _row_to_card(final_row)
            audit.log(
                actor=decided_by, kind="approval", action="denied",
                detail={"approval_id": approval_id,
                        "action_type": card["action_type"],
                        "note": str(note or "")},
                outcome="ok",
            )
            return final

        # Approve: mark approved first (auditable intermediate state), then
        # execute. The UPDATE is CONDITIONAL on status still being 'pending' and
        # we check rowcount: the read-check above only guards a single event
        # loop, but if this app is ever run multi-worker (a real scaling step),
        # two approvers could both pass that check. The atomic compare-and-set
        # closes the window — a losing writer sees rowcount==0 and aborts BEFORE
        # _execute, so one card can never become two Svigg writes.
        with conn:
            cur = conn.execute(
                "UPDATE approvals SET status='approved', ts_decided=?, "
                "decided_by=?, decision_note=? WHERE id=? AND status='pending'",
                (ts_decided, str(decided_by), str(note or ""), approval_id),
            )
        if cur.rowcount == 0:
            raise ValueError(
                f"approval {approval_id} was already decided concurrently — "
                "not executing again"
            )
        audit.log(
            actor=decided_by, kind="approval", action="approved",
            detail={"approval_id": approval_id,
                    "action_type": card["action_type"]},
            outcome="ok",
        )

        # Re-read the now-approved card and execute it. Guard the whole
        # execution: an unexpected exception must NEVER leave the card wedged
        # at 'approved' (decide() rejects re-deciding a non-pending card and
        # the UI only offers controls on 'pending', so a stuck 'approved' card
        # is unrecoverable on screen). Any escape flips it to 'failed' with an
        # honest error, so staff can see it did not complete.
        approved_card = _row_to_card(_fetch_card(conn, approval_id))
        try:
            result, ok = await _execute(approved_card)
        except Exception as exc:  # noqa: BLE001 - honest failure, no wedge
            logger.error("approval %s execution raised: %s", approval_id, exc)
            result, ok = (
                {"error": f"execution raised: {exc}",
                 "action_type": card["action_type"]},
                False,
            )

        final_status = "executed" if ok else "failed"
        result_json = json.dumps(result, default=str, ensure_ascii=False)
        with conn:
            conn.execute(
                "UPDATE approvals SET status=?, result=? WHERE id=?",
                (final_status, result_json, approval_id),
            )
        final = _row_to_card(_fetch_card(conn, approval_id))
    finally:
        conn.close()

    audit.log(
        actor=decided_by, kind="approval", action=final_status,
        detail={"approval_id": approval_id,
                "action_type": card["action_type"],
                "result": result},
        outcome="ok" if ok else "error",
    )
    return final


# ---------------------------------------------------------------------------
# Execution dispatch
# ---------------------------------------------------------------------------

async def _execute(card: dict) -> tuple[dict, bool]:
    """Dispatch an approved card to the real EMR write wrappers.

    Returns ``(result_dict, ok)`` where ``ok`` decides executed vs failed.
    The ``EMRSessionManager`` is imported INSIDE this function so the module
    imports cleanly even when Playwright / the scraper stack is unavailable
    (e.g. in unit tests or on a machine without a browser).

    HONESTY:
      - If ``EMR_ENABLED`` is off, nothing is attempted; the card fails with
        an explicit error string (never a fake success).
      - Booking has no auto-verify: an executed booking result always
        surfaces the scraper's warning and says the write is unverified.
      - The stored ``result`` is the RAW scraper response (never re-shaped
        into a claim the scraper did not make).
    """
    action_type = card.get("action_type")
    params = card.get("params") or {}

    # manual_task is human-acknowledged work; no EMR contact needed, so it
    # executes even when the EMR is disabled.
    if action_type == "manual_task":
        return (
            {
                "status": "executed",
                "title": params.get("title", ""),
                "detail": params.get("detail", ""),
                "note": ("manual task acknowledged by the approver; "
                         "no EMR write performed"),
            },
            True,
        )

    # Outbound patient communications (email / SMS) are NOT EMR writes — they
    # go out over Gmail / RingCentral, so they are dispatched here BEFORE the
    # EMR_ENABLED gate (an approved records-request reply must still send even
    # when the EMR scraper stack is off). comms.py degrades to a clean error
    # dict when the channel is not configured, so this never fakes a send.
    if action_type == "send_email":
        return await _execute_send_email(params)
    if action_type == "send_sms":
        return await _execute_send_sms(params)

    # All remaining action types touch the EMR. If it is disabled, fail
    # honestly rather than pretending the write happened.
    if not SETTINGS.EMR_ENABLED:
        return ({"error": "EMR disabled (EMR_ENABLED=0)",
                 "action_type": action_type}, False)

    # Import the EMR manager lazily — only reached for real EMR actions. Use
    # the PROCESS SINGLETON (get_instance), NOT a fresh EMRSessionManager():
    #   * a fresh instance would open a SECOND concurrent logged-in Svigg/SIS
    #     browser fighting the same portal (the exact hazard the manager's
    #     connect-locks exist to prevent), and
    #   * it is never disconnected, so every approval would leak an
    #     authenticated headless Chromium.
    # The singleton is what agent.py and referrals.py already share, so its
    # per-operation locks serialize this write with any in-flight chat read.
    # Construction is inside the try so a constructor/import error becomes an
    # honest failed card (caught upstream and committed as 'failed') rather
    # than an unhandled 500 that leaves the card wedged at 'approved'.
    try:
        from emr_session_manager import EMRSessionManager

        mgr = EMRSessionManager.get_instance()
    except Exception as exc:  # import/environment failure → honest failure
        logger.error("EMRSessionManager unavailable: %s", exc)
        return ({"error": f"EMR unavailable: {exc}",
                 "action_type": action_type}, False)

    if action_type == "book_appointment":
        return await _execute_book(mgr, params)
    if action_type == "cancel_appointment":
        return await _execute_cancel(mgr, params)
    if action_type == "reschedule_appointment":
        return await _execute_reschedule(mgr, params)
    if action_type == "create_patient":
        return await _execute_create_patient(mgr, params)

    return ({"error": f"unknown action_type {action_type!r}"}, False)


# --- outbound comms (email / SMS) -----------------------------------------

def _resolve_reply_external_id(reply_to_message_id) -> str | None:
    """Map a stored inbound ``messages.id`` to its ``external_id`` for threading.

    Returns the external_id string (IMAP Message-ID) when the row exists and
    carries one, else ``None`` (send goes out un-threaded rather than failing —
    a missing thread ref must never block an approved reply). No PHI is logged.
    """
    if reply_to_message_id in (None, ""):
        return None
    try:
        rid = int(reply_to_message_id)
    except (TypeError, ValueError):
        return None
    conn = db.get_conn()
    try:
        # direction='in' ONLY: an outbound row's external_id is the locally
        # synthesised '<sent-…>' Message-ID that never went on the wire, so
        # threading a reply on it silently breaks threading AND would let
        # _mark_inbound_replied flip an outbound 'sent' row to 'replied'
        # (a transition the schema reserves for inbound messages).
        row = conn.execute(
            "SELECT external_id FROM messages WHERE id = ? AND direction = 'in'",
            (rid,),
        ).fetchone()
    except Exception as exc:  # messages table absent / query error → un-threaded
        logger.warning("reply-thread lookup failed for message %s: %s",
                       rid, exc)
        return None
    finally:
        conn.close()
    if row is None:
        return None
    ext = row["external_id"] if "external_id" in row.keys() else None
    return ext or None


def _mark_inbound_replied(reply_to_message_id) -> None:
    """Flip the replied-to inbound message's status to 'replied'. Best-effort.

    Called only after a send succeeds. A failure here must not turn a truly
    sent message into a 'failed' card, so it is swallowed (logged, ids only).
    """
    if reply_to_message_id in (None, ""):
        return
    try:
        rid = int(reply_to_message_id)
    except (TypeError, ValueError):
        return
    try:
        from app import comms
        # 'replied' is an INBOUND-only status (schema comment). Only flip the
        # row if it is actually inbound — never corrupt an outbound 'sent' row
        # to 'replied' when the agent threaded off the practice's own prior
        # reply. A single-row status UPDATE, run inline (never threaded so a
        # thread-pool hiccup can't strand a genuinely-sent card).
        row = comms.get_message(rid)
        if row and row.get("direction") == "in":
            comms.update_message(rid, "replied")
    except Exception as exc:  # never wedge a genuinely-sent card
        logger.warning("could not mark message %s replied: %s", rid, exc)


async def _execute_send_email(params: dict) -> tuple[dict, bool]:
    """Send an approved outbound email via comms.send_email (fail-closed).

    params: {to, subject, body, reply_to_message_id?, drive_file_ids?,
    from_account?}. The body the approver read on the card is sent VERBATIM. If
    any promised Drive attachment cannot be fetched, comms.send_email returns ok
    False and NOTHING is sent (a records reply without its records is worse than
    no send). ``from_account`` (when present) picks WHICH configured mailbox the
    reply is sent from — so a reply to a mainlinepain@ referral goes out from
    mainlinepain@, not the default first account; comms.send_email hard-errors
    (sends nothing) if it is not a configured address. On success the replied-to
    inbound message is flipped to 'replied'.
    """
    import asyncio

    to = params.get("to", "")
    subject = params.get("subject", "")
    body = params.get("body", "")
    reply_to = params.get("reply_to_message_id")
    drive_file_ids = params.get("drive_file_ids") or None
    from_account = params.get("from_account") or None

    in_reply_to = _resolve_reply_external_id(reply_to)

    try:
        from app import comms
    except Exception as exc:  # comms module unavailable → honest failure
        logger.error("comms module unavailable: %s", exc)
        return ({"error": f"comms unavailable: {exc}"}, False)

    try:
        resp = await asyncio.to_thread(
            comms.send_email, to, subject, body,
            in_reply_to_external_id=in_reply_to,
            drive_file_ids=drive_file_ids,
            from_account=from_account,
        )
    except Exception as exc:
        logger.error("send_email raised: %s", exc)
        return ({"error": f"send_email failed: {exc}"}, False)

    resp = resp if isinstance(resp, dict) else {"raw": resp}
    if resp.get("ok"):
        _mark_inbound_replied(reply_to)
        return (
            {"external_id": resp.get("external_id"),
             "note": "email sent"},
            True,
        )
    if "error" not in resp:
        resp["error"] = "email not sent (send_email returned ok=False)"
    return (resp, False)


async def _execute_send_sms(params: dict) -> tuple[dict, bool]:
    """Send an approved outbound SMS via comms.send_sms.

    params: {to, text, reply_to_message_id?}. The text the approver read on the
    card is sent VERBATIM. On success the replied-to inbound message is flipped
    to 'replied'.
    """
    import asyncio

    to = params.get("to", "")
    text = params.get("text", "")
    reply_to = params.get("reply_to_message_id")
    from_number = params.get("from_number", "")

    try:
        from app import comms
    except Exception as exc:
        logger.error("comms module unavailable: %s", exc)
        return ({"error": f"comms unavailable: {exc}"}, False)

    try:
        resp = await asyncio.to_thread(comms.send_sms, to, text, from_number)
    except Exception as exc:
        logger.error("send_sms raised: %s", exc)
        return ({"error": f"send_sms failed: {exc}"}, False)

    resp = resp if isinstance(resp, dict) else {"raw": resp}
    if resp.get("ok"):
        _mark_inbound_replied(reply_to)
        return (
            {"external_id": resp.get("external_id"),
             "note": "sms sent"},
            True,
        )
    if "error" not in resp:
        resp["error"] = "sms not sent (send_sms returned ok=False)"
    return (resp, False)


# --- book -----------------------------------------------------------------

def _map_book_params(params: dict) -> dict:
    """Translate contract booking params to svigg_book_appointment kwargs.

    Contract param ``date_mdy`` (MM/DD/YYYY) maps to the scraper's ``date``;
    all other keys pass through by the same name. ``execute=True`` and
    ``confirm_unverified=True`` are added here (the human approval IS the
    confirmation), and never relaxed elsewhere.
    """
    return {
        "acct": params.get("acct", ""),
        "rowid": params.get("rowid", ""),
        "last_name": params.get("last_name", ""),
        "first_name": params.get("first_name", ""),
        "date": params.get("date_mdy", ""),          # date_mdy -> date
        "start_time": params.get("start_time", ""),
        "duration_min": params.get("duration_min", 15),
        "appt_type": params.get("appt_type", "EST"),
        "provider": params.get("provider", ""),
        # Overbook policy: this office routinely double-books every slot, so an
        # approved booking should force the overbook rather than stall at Svigg's
        # "Overbook to force" confirm. We DEFAULT to True (the office norm) but
        # HONOR the card's own value so the behavior always matches the
        # allow_overbook the approver actually saw on the card — the agent stages
        # bookings with allow_overbook=True, so display and behavior agree (no
        # silent override). The scraper only clicks Overbook when the slot is
        # genuinely full and reports status='submitted_overbooked' when it does.
        "allow_overbook": bool(params.get("allow_overbook", True)),
        "execute": True,
        "confirm_unverified": True,
    }


async def _execute_book(mgr, params: dict) -> tuple[dict, bool]:
    """Run a booking write and classify the scraper status.

    submitted | submitted_overbooked -> executed
    overbook_required                 -> failed (note: re-request w/ overbook)
    anything else                     -> failed

    ALWAYS appends the scraper's warning and NEVER claims verification —
    booking has no auto-verify path.
    """
    kwargs = _map_book_params(params)
    # PATIENT-SAFETY GUARD: never book on a name-only card. Svigg resolves a
    # name-only booking to the FIRST last-name match in Patient View, so a card
    # with no acct AND no rowid can silently book the WRONG patient (a real risk
    # once the acct allowlist widens past the test account). Require a positive
    # patient identifier; refuse honestly and tell staff to resolve the patient.
    if not str(kwargs.get("acct", "")).strip() and not str(kwargs.get("rowid", "")).strip():
        return ({"error": "booking card has no patient account (acct/rowid) — "
                          "refusing to book by name alone (could hit the wrong "
                          "patient). Re-stage the booking after looking the "
                          "patient up so their account is attached.",
                 "status": "no_patient_id"}, False)
    try:
        resp = await mgr.svigg_book_appointment(**kwargs)
    except Exception as exc:
        logger.error("svigg_book_appointment raised: %s", exc)
        return ({"error": f"book_appointment failed: {exc}"}, False)

    resp = resp if isinstance(resp, dict) else {"raw": resp}
    status = resp.get("status")

    # Preserve the raw response; layer on our honest framing without
    # overwriting anything the scraper reported.
    result = dict(resp)
    scraper_warning = resp.get("warning")

    if status in ("submitted", "submitted_overbooked"):
        note = ("booking submitted to Svigg — NOT auto-verified; "
                "re-read the calendar to confirm the real date/time")
        if status == "submitted_overbooked":
            note = "forced overbook " + note
        result["note"] = note
        result["verified"] = False
        if scraper_warning:
            result["scraper_warning"] = scraper_warning
        return (result, True)

    if status == "overbook_required":
        result["note"] = ("slot is over-allocated — re-request this booking "
                           "with allow_overbook=true to force it")
        if scraper_warning:
            result["scraper_warning"] = scraper_warning
        return (result, False)

    # execute_blocked, error, overbook_click_failed, or anything unexpected.
    if "error" not in result:
        result["error"] = (
            f"booking not submitted (status={status!r})"
        )
    if scraper_warning:
        result["scraper_warning"] = scraper_warning
    return (result, False)


# --- create patient -------------------------------------------------------

# The demographic keys carried on a create_patient approval card. These pass
# straight through to the scraper's create_patient(demographics=...) — the
# card's params ARE the demographics dict (no rename needed).
_CREATE_DEMOGRAPHIC_KEYS = (
    "last_name", "first_name", "mi", "dob", "ssn", "sex",
    "address", "address2", "city", "state", "zip",
    "home_phone", "cell_phone", "work_phone", "email",
)


def _map_create_patient_params(params: dict) -> dict:
    """Extract the demographics dict from a create_patient card's params.

    Only the known demographic keys are forwarded; anything absent stays "" so
    the scraper renders an honest blank rather than fabricating a value.
    """
    return {k: params.get(k, "") for k in _CREATE_DEMOGRAPHIC_KEYS}


async def _execute_create_patient(mgr, params: dict) -> tuple[dict, bool]:
    """Run an approved new-patient creation and classify the scraper status.

    IMPORTANT — HONESTY: the Save POST contract is UNVERIFIED (not in any HAR),
    so this executor runs the scraper in DRY-RUN even after human approval. It
    de-dupes, discovers the add-form fields, and returns what it WOULD submit —
    it does NOT write a chart. This mirrors booking's "no fake success": we
    never claim a chart was created when the Save step cannot yet be verified.
    Once a live HAR of the Save is captured, flip this to pass
    ``confirm_unverified=True`` (with SVIGG_CREATE_EXECUTE=1 set) to commit.

    Status classification:
      prepared            -> executed (fields discovered; nothing written) —
                             surfaced as executed-but-unwritten via 'verified'
      duplicate_suspected -> failed (a human must resolve the duplicate)
      anything else       -> failed
    """
    demographics = _map_create_patient_params(params)
    try:
        # dry_run=True is DELIBERATE and fail-closed: approval authorizes the
        # discovery pass, not an unverified Save.
        resp = await mgr.svigg_create_patient(demographics, dry_run=True)
    except Exception as exc:
        logger.error("svigg_create_patient raised: %s", exc)
        return ({"error": f"create_patient failed: {exc}"}, False)

    resp = resp if isinstance(resp, dict) else {"raw": resp}
    status = resp.get("status")
    result = dict(resp)
    scraper_warning = resp.get("warning")

    if status == "prepared":
        # The discovery pass succeeded. This is NOT a written chart — say so
        # plainly. 'executed' here means "the approved discovery ran", and
        # verified=False makes clear no Save happened.
        result["note"] = (
            "add-form fields discovered and duplicate check passed — NO chart "
            "was written (Save step unverified pending HAR). Capture the Save "
            "POST, then re-run to commit."
        )
        result["verified"] = False
        if scraper_warning:
            result["scraper_warning"] = scraper_warning
        return (result, True)

    if status == "duplicate_suspected":
        result["note"] = (
            "an existing chart matched — not created. A human must confirm "
            "whether this is genuinely a new person."
        )
        if scraper_warning:
            result["scraper_warning"] = scraper_warning
        return (result, False)

    # execute_blocked, save_unverified, error, or anything unexpected.
    if "error" not in result:
        result["error"] = f"patient not created (status={status!r})"
    if scraper_warning:
        result["scraper_warning"] = scraper_warning
    return (result, False)


# --- cancel ---------------------------------------------------------------

def _map_cancel_params(params: dict) -> dict:
    """Translate contract cancel params to svigg_cancel_appointment kwargs.

    ``date_iso`` (YYYY-MM-DD) -> ``date``; ``reason_code`` ('or'|'pr') ->
    ``reason``. ``confirm=True`` is added (the human approval is the
    confirmation).
    """
    return {
        "acct": params.get("acct", ""),
        "last_name": params.get("last_name", ""),
        "first_name": params.get("first_name", ""),
        "date": params.get("date_iso", ""),           # date_iso -> date
        "time": params.get("time", ""),
        "reason": params.get("reason_code", "or"),     # reason_code -> reason
        "confirm": True,
    }


async def _execute_cancel(mgr, params: dict) -> tuple[dict, bool]:
    """Run a cancel write and classify the scraper status.

    cancelled + verified True  -> executed
    cancelled + verified False -> executed, but surface the warning
    anything else              -> failed
    """
    kwargs = _map_cancel_params(params)
    try:
        resp = await mgr.svigg_cancel_appointment(**kwargs)
    except Exception as exc:
        logger.error("svigg_cancel_appointment raised: %s", exc)
        return ({"error": f"cancel_appointment failed: {exc}"}, False)

    resp = resp if isinstance(resp, dict) else {"raw": resp}
    result = dict(resp)
    status = resp.get("status")

    if status == "cancelled":
        verified = bool(resp.get("verified"))
        if verified:
            result["note"] = "cancel confirmed by post-cancel calendar re-read"
        else:
            # Executed (the cancel was submitted) but unverified — bubble the
            # scraper's warning up so the human knows to check manually.
            result["note"] = (
                resp.get("warning")
                or "cancel submitted but could NOT be verified — check manually"
            )
        return (result, True)

    if "error" not in result:
        result["error"] = f"cancel not completed (status={status!r})"
    return (result, False)


# --- reschedule -----------------------------------------------------------

async def _execute_reschedule(mgr, params: dict) -> tuple[dict, bool]:
    """Cancel first, then book the new slot ONLY IF the cancel VERIFIED.

    params = {cancel: {...cancel params}, book: {...book params}}.

    A reschedule that books before the original is confirmed gone can leave the
    patient holding TWO live appointments. So we do NOT book on a merely
    "submitted" cancel — we require the scraper's post-cancel calendar re-read
    to CONFIRM the original is gone (``verified == True``). Both of the
    unverified cases block the booking:
      * re-read failed        → we can't prove the cancel landed, don't book;
      * re-read still shows it → the original AFFIRMATIVELY still exists, don't
        book (this is the exact double-booking case).
    In every non-verified case the card fails and the cancel's warning is
    surfaced at the TOP level (not buried in result.cancel.warning) so staff
    can act. The result records BOTH raw responses.
    """
    cancel_params = params.get("cancel") or {}
    book_params = params.get("book") or {}

    cancel_result, cancel_ok = await _execute_cancel(mgr, cancel_params)
    cancel_verified = bool(
        isinstance(cancel_result, dict) and cancel_result.get("verified")
    )
    cancel_warning = (
        cancel_result.get("warning") or cancel_result.get("note")
        if isinstance(cancel_result, dict) else None
    )

    if not cancel_ok or not cancel_verified:
        # Either the cancel outright failed, or it was submitted but NOT
        # verified (re-read failed OR the appointment is still on the grid).
        # Do NOT book — booking now risks a live double-booking.
        return (
            {
                "error": (
                    "reschedule aborted — the original appointment's "
                    "cancellation is NOT verified, so the new slot was NOT "
                    "booked (booking now could double-book the patient); "
                    "verify the cancel manually, then re-request"
                    if cancel_ok else
                    "reschedule aborted — cancel step did not succeed; "
                    "booking NOT attempted"
                ),
                "cancel_warning": cancel_warning,
                "cancel": cancel_result,
                "book": None,
            },
            False,
        )

    book_result, book_ok = await _execute_book(mgr, book_params)
    combined = {
        "cancel": cancel_result,
        "book": book_result,
    }
    if not book_ok:
        combined["error"] = (
            "reschedule partial — original appointment was cancelled (verified) "
            "but the new booking did NOT succeed; re-book manually"
        )
    else:
        combined["note"] = (
            "rescheduled: original cancelled (verified) and new slot submitted "
            "(booking unverified — re-read the calendar to confirm)"
        )
    return (combined, book_ok)

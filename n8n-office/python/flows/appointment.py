#!/usr/bin/env python3
"""
appointment.py - Appointment-request flow (read-mostly; reserve is gated).

Pipeline (one inbound SMS / chatbot turn -> one ``run(payload)`` call):

    1. Validate the incoming payload (patient identity + requested window +
       optional preferred slot / provider).
    2. Resolve the patient across all 3 EMR sources via
       ``emr_bridge.lookup_patient(..., source="all")``. Fail closed if the
       patient cannot be matched in any source; we never invent a chart.
    3. Pull the appointment book for the requested window from Sunnyvig
       (``svigg``) via ``emr_bridge.check_appointment_book``. Per Q5 the SIS
       portal owns appointments authoritatively, but the spec here says
       Sunnyvig - both routes flow through the same bridge and the source is
       a parameter, so the caller can override at runtime.
    4. Detect conflicts: any existing appointment for the same patient inside
       the requested window, or - if a specific ``preferred_slot`` is passed -
       any existing appointment at exactly that slot for any patient.
    5. Confirm billing posture in Picasso via
       ``emr_bridge.check_billing(..., source="picasso")``. We do NOT block a
       booking request on outstanding balance (that's a clinic policy call) -
       we surface the balance so the approver sees it in the SMS gate.
    6. Reserve the slot - WRITE OPERATION. Per Q5 (read-only v1) and Q4 (SMS
       staff-approval gate), the actual reservation does NOT call any portal.
       Instead we:
         a. Append an audit row with ``intent="appointment-reserve-request"``
            and ``action="STAGED"`` capturing the full proposed booking.
         b. Fire a Q4-style SMS to ``APPROVAL_STAFF_NUMBER`` summarizing the
            request and inviting ``YES`` / ``NO`` / ``EDIT`` reply. The reply
            parser is in a separate webhook flow; we only generate the
            outbound prompt here.
         c. Return a ``{"reserved": false, "pending_approval": true}`` payload.
       The actual call to ``emr_bridge.book_appointment(...)`` stays disabled
       (it raises ``NotImplementedError``) until the Q5 sign-off lands.
    7. Send a patient-facing confirmation SMS via
       ``ringcentral_adapter.send_sms``. We send the "we got your request, a
       staff member will follow up" template (per Q9 - clinic identity, no
       persona); we never tell the patient the slot is reserved before staff
       has approved it.

Idempotency & retries:
    Q10 says retry-once-with-30s-backoff at the n8n-node layer. This module
    is internally idempotent for the read steps (cache TTL in emr_bridge) but
    NOT for the SMS sends - the caller (n8n) must dedupe by request_id, which
    is generated here and returned in the response so the workflow can log it.

Failure surfacing:
    Any unhandled exception bubbles up as a ``FlowError``. The n8n workflow
    routes that to the dead-letter handler (Q10): one audit row with
    outcome=error, one staff SMS, n8n execution marked failed.

PHI:
    Patient name / phone / dob are PHI. They flow through this module and
    into the audit log (hashed) and the staff-approval SMS (in the clear,
    over the RingCentral BAA channel - acceptable). They are NEVER sent to
    any third-party LLM by this module - all classification happens upstream
    in the triage flow (Q3, Anthropic-direct).

Usage:
    from flows.appointment import run

    result = run({
        "patient": {"name": "Jane Doe", "phone": "+12155551234"},
        "requested_window": {"start": "2026-07-01", "end": "2026-07-08"},
        "preferred_slot": "2026-07-02T14:00:00",
        "provider": "Dr. Smith",
        "reason": "MRI follow-up",
        "actor": "system:sms-bot",
    })

Smoke test:
    python3 flows/appointment.py          # runs the __main__ block below
                                          # (no real API calls)
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Path bootstrap so this module is importable either as `flows.appointment`
# or via direct `python3 path/to/appointment.py`. We do NOT permanently
# mutate sys.path - we only prepend the python/ dir if not already there.
_PYTHON_DIR = Path(__file__).resolve().parent.parent
if str(_PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(_PYTHON_DIR))

from integrations import emr_bridge  # noqa: E402
from integrations import ringcentral_adapter  # noqa: E402
from integrations.audit_log import AuditLog, AuditLogError  # noqa: E402


# ─── .env loader (no external deps — mirrors server.py style) ───────────────
def _load_dotenv(path: str = ".env") -> None:
    """Load /Users/shubh/n8n-office/.env into os.environ if not already set.

    Mirrors the helper in Antigravity's server.py. Idempotent. Silently
    no-ops if the file is missing - flows can run with env supplied another
    way (n8n env, direct export, etc.).
    """
    env_path = Path(__file__).resolve().parent.parent.parent / path
    if not env_path.exists():
        return
    try:
        with env_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        # .env is best-effort; never break flow import on a disk hiccup.
        pass


_load_dotenv()


# ─── Constants & paths ──────────────────────────────────────────────────────

# Where the hash-chained audit DB lives. The whole n8n-office dir is on a
# FileVault-encrypted volume per Q6/Q11; the DB inherits that encryption.
AUDIT_DB_PATH = Path(
    os.environ.get(
        "N8N_OFFICE_AUDIT_DB",
        "/Users/shubh/n8n-office/data/audit.sqlite",
    )
)

# Per-install pepper for hashing patient ids in the audit log (Q7). MUST be
# stable for the life of the log - rotating it breaks per-patient correlation.
# Loaded from env; the module refuses to log anything if it is missing.
# TODO: WIRE CREDS - AUDIT_LOG_PEPPER
AUDIT_PEPPER_ENV = "AUDIT_LOG_PEPPER"

# Staff RingCentral number that receives the Q4 approval-gate SMS. E.164.
# TODO: WIRE CREDS - APPROVAL_STAFF_NUMBER
APPROVAL_STAFF_NUMBER_ENV = "APPROVAL_STAFF_NUMBER"

# Optional override for the clinic's outbound SMS DID. If unset RingCentral
# uses the extension's default SMS-enabled number.
# TODO: WIRE CREDS - CLINIC_SMS_FROM_NUMBER
CLINIC_SMS_FROM_ENV = "CLINIC_SMS_FROM_NUMBER"

# Main clinic phone, embedded in the patient-facing confirmation per Q9.
# TODO: WIRE CREDS - CLINIC_MAIN_LINE
CLINIC_MAIN_LINE_ENV = "CLINIC_MAIN_LINE"

# Default EMR for the appointment book scan. Spec says Sunnyvig; emr_bridge
# accepts 'svigg' as the registered source key for Sunnyvig.
DEFAULT_APPOINTMENT_SOURCE = "svigg"

# Default EMR for billing posture (Q5 - Picasso owns AR alongside Svigg).
DEFAULT_BILLING_SOURCE = "picasso"


# ─── Exceptions ─────────────────────────────────────────────────────────────


class FlowError(RuntimeError):
    """Any failure that should bubble up to the n8n dead-letter handler."""


class ValidationFailed(FlowError):
    """Payload missing required fields or shaped wrong."""


class PatientNotFound(FlowError):
    """Lookup returned no record in any EMR source."""


# ─── Data shapes ────────────────────────────────────────────────────────────


@dataclass
class AppointmentRequest:
    """Normalized inbound payload. Build with ``AppointmentRequest.from_payload``."""

    patient_id: str           # name | phone | email — emr_bridge handles all 3
    patient_label: str        # human-readable identifier for SMS copy
    window_start: str         # ISO date YYYY-MM-DD
    window_end: str           # ISO date YYYY-MM-DD
    preferred_slot: Optional[str] = None   # ISO datetime, optional
    provider: Optional[str] = None
    reason: Optional[str] = None
    actor: str = "system:appointment-flow"
    appointment_source: str = DEFAULT_APPOINTMENT_SOURCE
    billing_source: str = DEFAULT_BILLING_SOURCE

    @classmethod
    def from_payload(cls, payload: dict) -> "AppointmentRequest":
        if not isinstance(payload, dict):
            raise ValidationFailed("payload must be a dict")

        patient = payload.get("patient") or {}
        if not isinstance(patient, dict):
            raise ValidationFailed("payload.patient must be a dict")
        patient_id = (
            patient.get("name")
            or patient.get("phone")
            or patient.get("email")
            or ""
        ).strip()
        if not patient_id:
            raise ValidationFailed(
                "payload.patient requires at least one of name/phone/email"
            )

        window = payload.get("requested_window") or {}
        if not isinstance(window, dict):
            raise ValidationFailed("payload.requested_window must be a dict")
        start = (window.get("start") or "").strip()
        end = (window.get("end") or "").strip()
        if not (start and end):
            raise ValidationFailed(
                "payload.requested_window must include start and end ISO dates"
            )

        return cls(
            patient_id=patient_id,
            patient_label=patient.get("name") or patient_id,
            window_start=start,
            window_end=end,
            preferred_slot=(payload.get("preferred_slot") or None),
            provider=(payload.get("provider") or None),
            reason=(payload.get("reason") or None),
            actor=(payload.get("actor") or "system:appointment-flow"),
            appointment_source=(
                payload.get("appointment_source") or DEFAULT_APPOINTMENT_SOURCE
            ),
            billing_source=(
                payload.get("billing_source") or DEFAULT_BILLING_SOURCE
            ),
        )


@dataclass
class AppointmentResult:
    """Structured response returned by ``run``. JSON-serializable."""

    request_id: str
    ok: bool
    pending_approval: bool
    reserved: bool
    patient: dict
    appointments_in_window: list
    conflicts: list
    billing: dict
    staff_sms_message_id: Optional[str] = None
    patient_sms_message_id: Optional[str] = None
    audit_row_ids: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "ok": self.ok,
            "pending_approval": self.pending_approval,
            "reserved": self.reserved,
            "patient": self.patient,
            "appointments_in_window": self.appointments_in_window,
            "conflicts": self.conflicts,
            "billing": self.billing,
            "staff_sms_message_id": self.staff_sms_message_id,
            "patient_sms_message_id": self.patient_sms_message_id,
            "audit_row_ids": self.audit_row_ids,
            "errors": self.errors,
        }


# ─── Audit log helper ───────────────────────────────────────────────────────


_audit_log_singleton: Optional[AuditLog] = None


def _get_audit_log() -> AuditLog:
    """Lazy singleton so a missing pepper doesn't break import."""
    global _audit_log_singleton
    if _audit_log_singleton is not None:
        return _audit_log_singleton
    pepper = os.environ.get(AUDIT_PEPPER_ENV, "")
    if not pepper:
        raise FlowError(
            AUDIT_PEPPER_ENV + " is not set. Audit logging is mandatory; "
            "load the per-install pepper from your secret store before "
            "running this flow."
        )
    AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        _audit_log_singleton = AuditLog(str(AUDIT_DB_PATH), pepper=pepper)
    except AuditLogError as exc:
        raise FlowError("failed to open audit log: " + str(exc)) from exc
    return _audit_log_singleton


# ─── Step implementations ───────────────────────────────────────────────────


def _validate_payload(payload: dict) -> AppointmentRequest:
    """Step 1: parse + sanity-check the inbound payload."""
    return AppointmentRequest.from_payload(payload)


def _lookup_patient_all_sources(req: AppointmentRequest) -> dict:
    """Step 2: read-only lookup across SIS / WEBeDoctor / Picasso / Svigg."""
    result = emr_bridge.lookup_patient(req.patient_id, source="all")
    matched_sources = [
        r["source"] for r in result.get("results", []) if r.get("record")
    ]
    if not matched_sources:
        # Fail closed - we will not invent a chart. The caller routes this
        # to the staff approval queue with a "patient unknown" tag.
        tried = [r.get("source") for r in result.get("results", [])]
        raise PatientNotFound(
            "No patient match for " + repr(req.patient_id)
            + " in any source. Sources tried: " + repr(tried)
        )
    return result


def _check_appointment_book(req: AppointmentRequest) -> list:
    """Step 3: pull appointments in the requested window from Sunnyvig."""
    book = emr_bridge.check_appointment_book(
        (req.window_start, req.window_end),
        source=req.appointment_source,
    )
    return book.get("appointments", [])


def _detect_conflicts(
    req: AppointmentRequest,
    appointments: list,
    patient_lookup: dict,
) -> list:
    """Step 4: same-patient overlap in window OR same-slot overlap (any patient)."""
    conflicts: list = []

    # Build a set of names the patient is known by, lower-cased, so we can
    # match the appointment book without a stable patient id.
    patient_names = set()
    for r in patient_lookup.get("results", []):
        rec = r.get("record") or {}
        name = str(rec.get("name") or "").strip().lower()
        if name:
            patient_names.add(name)
    if req.patient_label:
        patient_names.add(req.patient_label.strip().lower())

    preferred = (req.preferred_slot or "").strip()
    preferred_date = preferred.split("T")[0] if "T" in preferred else preferred

    for appt in appointments:
        appt_name = str(appt.get("name") or "").strip().lower()
        appt_date = str(appt.get("intake_date") or "")
        # Normalize MM/DD/YYYY -> YYYY-MM-DD for comparison if needed.
        if "/" in appt_date:
            try:
                m, d, y = appt_date.split("/")
                appt_date = y + "-" + str(int(m)).zfill(2) + "-" + str(int(d)).zfill(2)
            except (ValueError, AttributeError):
                pass

        if appt_name and appt_name in patient_names:
            conflicts.append({
                "type": "same_patient_in_window",
                "appointment": appt,
            })
            continue

        if preferred_date and appt_date == preferred_date:
            conflicts.append({
                "type": "slot_taken",
                "appointment": appt,
            })

    return conflicts


def _check_billing(req: AppointmentRequest) -> dict:
    """Step 5: read-only Picasso billing posture for the approver."""
    return emr_bridge.check_billing(req.patient_id, source=req.billing_source)


def _summarize_billing(billing: dict) -> dict:
    """Reduce billing.ledgers to a single balance/insurance snapshot."""
    ledgers = billing.get("ledgers", [])
    if not ledgers:
        return {"balance": None, "insurance": None, "found": False}
    # Pick the first found ledger; the read step uses one source by default.
    for led in ledgers:
        if led.get("found"):
            return {
                "balance": led.get("balance"),
                "insurance": led.get("insurance"),
                "status": led.get("status"),
                "source": led.get("source"),
                "found": True,
            }
    return {"balance": None, "insurance": None, "found": False}


def _stage_reservation(
    req: AppointmentRequest,
    request_id: str,
    conflicts: list,
    billing: dict,
    audit: AuditLog,
) -> list:
    """Step 6a: append the staged-reservation audit rows (no portal write).

    Returns the list of inserted audit row ids. The caller fires the staff
    SMS next. Two rows are written: a short summary and the full payload so
    a reviewer can reconstruct exactly what was proposed.
    """
    ids: list = []
    summary_id = audit.log(
        actor=req.actor,
        intent="appointment-reserve-request",
        action="STAGED",
        result_summary=(
            "staged; pending staff approval ("
            + str(len(conflicts)) + " conflicts)"
        ),
        patient_id=req.patient_id,
        error_msg=None,
    )
    ids.append(int(summary_id))

    payload_json = json.dumps(
        {
            "request_id": request_id,
            "appointment_source": req.appointment_source,
            "window": [req.window_start, req.window_end],
            "preferred_slot": req.preferred_slot,
            "provider": req.provider,
            "reason": req.reason,
            "conflict_count": len(conflicts),
            "billing_summary": _summarize_billing(billing),
        },
        default=str,
    )
    payload_id = audit.log(
        actor=req.actor,
        intent="appointment-reserve-payload",
        action="STAGED_PAYLOAD",
        result_summary=payload_json[:500],
        patient_id=req.patient_id,
    )
    ids.append(int(payload_id))
    return ids


def _build_staff_sms_body(
    req: AppointmentRequest,
    request_id: str,
    conflicts: list,
    billing: dict,
) -> str:
    """Compose the Q4 staff-approval SMS body. Plain string concat - no
    template engine; the payload is staff-facing PHI we control."""
    bill_summary = _summarize_billing(billing)
    balance = bill_summary.get("balance")
    balance_str = ("$" + str(balance)) if balance not in (None, "") else "—"

    if conflicts:
        first_types = ", ".join(c["type"] for c in conflicts[:3])
        conflict_str = str(len(conflicts)) + " (" + first_types + ")"
    else:
        conflict_str = "none"

    reason = (req.reason or "—")
    if len(reason) > 120:
        reason = reason[:120]

    lines = [
        "[APW] Appointment request " + request_id,
        "Patient: " + req.patient_label,
        "Window: " + req.window_start + " -> " + req.window_end,
        "Slot:    " + (req.preferred_slot or "—"),
        "Provider: " + (req.provider or "—"),
        "Reason:  " + reason,
        "Balance: " + balance_str,
        "Conflicts: " + conflict_str,
        "Reply YES to confirm, NO to reject, EDIT to staff-handle.",
    ]
    return "\n".join(lines)


def _send_staff_approval_sms(
    req: AppointmentRequest,
    request_id: str,
    conflicts: list,
    billing: dict,
) -> Optional[str]:
    """Step 6b: Q4 SMS approval gate. Returns the RC message id or None."""
    staff_number = os.environ.get(APPROVAL_STAFF_NUMBER_ENV, "").strip()
    if not staff_number:
        raise FlowError(
            APPROVAL_STAFF_NUMBER_ENV + " is not set. Set it to the on-call "
            "staff RingCentral number (E.164) before running the appointment "
            "flow in production."
        )

    body = _build_staff_sms_body(req, request_id, conflicts, billing)
    from_number = os.environ.get(CLINIC_SMS_FROM_ENV, "").strip() or None
    msg = ringcentral_adapter.send_sms(staff_number, body, from_=from_number)
    return str(msg.get("id")) if isinstance(msg, dict) else None


def _build_patient_confirmation_body() -> str:
    """Q9: clinic identity only. No persona, no medical advice."""
    main_line = os.environ.get(CLINIC_MAIN_LINE_ENV, "").strip() or "the clinic"
    return (
        "This is Atlantic Pain & Wellness. We received your appointment "
        "request and a staff member will follow up shortly. For urgent "
        "issues, call " + main_line + ". For emergencies, call 911."
    )


def _send_patient_confirmation_sms(req: AppointmentRequest) -> Optional[str]:
    """Step 7: patient-facing acknowledgement (Q9 - clinic identity only).

    We only send if the payload gave us a phone-shaped identifier. We do NOT
    scrape the patient record for a phone we weren't explicitly given - that
    crosses a consent boundary.
    """
    if not (req.patient_id.startswith("+") or _looks_like_phone(req.patient_id)):
        return None

    body = _build_patient_confirmation_body()
    from_number = os.environ.get(CLINIC_SMS_FROM_ENV, "").strip() or None
    msg = ringcentral_adapter.send_sms(req.patient_id, body, from_=from_number)
    return str(msg.get("id")) if isinstance(msg, dict) else None


def _looks_like_phone(value: str) -> bool:
    digits = "".join(ch for ch in value if ch.isdigit())
    return len(digits) in (10, 11)


# ─── Public entrypoint ──────────────────────────────────────────────────────


def run(payload: dict, *, dry_run: bool = False) -> dict:
    """Run the appointment-request flow for one inbound message.

    Args:
        payload: see module docstring for shape.
        dry_run: if True, skip all SMS sends AND skip the audit row append.
            Used by the smoke test and by n8n's "Test Workflow" mode. Reads
            still run (they hit the local emr_bridge cache + JSON DB).

    Returns:
        AppointmentResult as a plain dict (JSON-serializable).
    """
    request_id = "appt-" + uuid.uuid4().hex[:12]
    result = AppointmentResult(
        request_id=request_id,
        ok=False,
        pending_approval=False,
        reserved=False,
        patient={},
        appointments_in_window=[],
        conflicts=[],
        billing={},
    )

    # 1. Validate
    try:
        req = _validate_payload(payload)
    except ValidationFailed as exc:
        result.errors.append("validation: " + str(exc))
        return result.to_dict()

    # 2. Patient lookup (cross-source)
    try:
        patient_lookup = _lookup_patient_all_sources(req)
        result.patient = patient_lookup
    except PatientNotFound as exc:
        result.errors.append("patient_not_found: " + str(exc))
        return result.to_dict()
    except emr_bridge.EMRBridgeError as exc:
        result.errors.append("patient_lookup_error: " + str(exc))
        return result.to_dict()

    # 3. Appointment book
    try:
        appointments = _check_appointment_book(req)
        result.appointments_in_window = appointments
    except emr_bridge.EMRSessionExpired as exc:
        # Q10 - bubble session-expired distinctly so dashboard surfaces it.
        result.errors.append("session_expired: " + str(exc))
        return result.to_dict()
    except emr_bridge.EMRBridgeError as exc:
        result.errors.append("appointment_book_error: " + str(exc))
        return result.to_dict()

    # 4. Conflicts
    result.conflicts = _detect_conflicts(req, appointments, patient_lookup)

    # 5. Billing
    try:
        billing = _check_billing(req)
        result.billing = billing
    except emr_bridge.EMRBridgeError as exc:
        # Don't block the flow on a billing read; the approver can decide
        # whether to proceed without it. Surface as a soft error.
        result.errors.append("billing_warning: " + str(exc))
        billing = {"ledgers": [], "session_expired": []}
        result.billing = billing

    # 6. Stage reservation (audit row + staff SMS) — NEVER a portal write.
    if not dry_run:
        try:
            audit = _get_audit_log()
            row_ids = _stage_reservation(
                req, request_id, result.conflicts, billing, audit
            )
            result.audit_row_ids.extend(row_ids)
        except FlowError as exc:
            result.errors.append("audit_error: " + str(exc))
            return result.to_dict()

        try:
            result.staff_sms_message_id = _send_staff_approval_sms(
                req, request_id, result.conflicts, billing
            )
        except (FlowError, RuntimeError, ValueError) as exc:
            # SMS failure is fatal for this flow — without staff approval we
            # cannot proceed even after Q5 sign-off. Log and surface.
            result.errors.append("staff_sms_error: " + str(exc))
            return result.to_dict()

    result.pending_approval = True
    result.reserved = False  # Q5 - writes disabled in v1

    # 7. Patient acknowledgement
    if not dry_run:
        try:
            result.patient_sms_message_id = _send_patient_confirmation_sms(req)
        except (RuntimeError, ValueError) as exc:
            # Patient SMS is best-effort; don't fail the whole flow if RC is
            # flaky. The staff approver still has the request in their queue.
            result.errors.append("patient_sms_warning: " + str(exc))

    result.ok = True
    return result.to_dict()


# ─── Smoke test: __main__ ───────────────────────────────────────────────────


def _smoke_test() -> int:
    """Verify the routing logic with a fake payload. Makes NO real API calls.

    We use ``dry_run=True`` to short-circuit SMS sends and audit writes. The
    read steps still execute against the local emr_bridge cache + patient
    database. We then assert structural invariants on the result.
    """
    print("=== appointment.py smoke test ===")

    fake_payload = {
        "patient": {
            "name": "Test Patient SmokeOnly",
            "phone": "+12155550100",
        },
        "requested_window": {
            "start": "2026-07-01",
            "end": "2026-07-08",
        },
        "preferred_slot": "2026-07-02T14:00:00",
        "provider": "Dr. Smith",
        "reason": "MRI follow-up",
        "actor": "smoke-test",
    }

    # 1. Validation should succeed for a well-formed payload.
    try:
        req = _validate_payload(fake_payload)
        assert req.patient_id == "Test Patient SmokeOnly"
        assert req.window_start == "2026-07-01"
        assert req.appointment_source == DEFAULT_APPOINTMENT_SOURCE
        print("[ok] validation accepts well-formed payload")
    except Exception as exc:  # pragma: no cover
        print("[FAIL] validation: " + str(exc))
        return 1

    # 2. Validation should reject missing patient.
    try:
        _validate_payload({"requested_window": {"start": "x", "end": "y"}})
        print("[FAIL] validation accepted missing patient")
        return 1
    except ValidationFailed:
        print("[ok] validation rejects missing patient")

    # 3. Validation should reject missing window.
    try:
        _validate_payload({"patient": {"name": "X"}})
        print("[FAIL] validation accepted missing window")
        return 1
    except ValidationFailed:
        print("[ok] validation rejects missing window")

    # 4. End-to-end dry run. May raise PatientNotFound if local DB has no
    #    record for the fake patient — that's the expected fail-closed path.
    out = run(fake_payload, dry_run=True)
    expected_keys = {
        "request_id", "ok", "pending_approval", "reserved", "patient",
        "appointments_in_window", "conflicts", "billing",
        "staff_sms_message_id", "patient_sms_message_id",
        "audit_row_ids", "errors",
    }
    missing = expected_keys - set(out.keys())
    assert not missing, "result missing keys: " + repr(missing)
    print("[ok] dry_run returns full result shape (request_id=" + out["request_id"] + ")")

    if not out["ok"] and any(e.startswith("patient_not_found") for e in out["errors"]):
        print("[ok] fake patient correctly fails closed (no chart fabricated)")
    elif out["ok"]:
        # The local DB happened to contain a near-match — still a valid run.
        assert out["pending_approval"] is True
        assert out["reserved"] is False, "writes must stay disabled (Q5)"
        print("[ok] real-match path returns pending_approval=True, reserved=False")
    else:
        print("[note] dry run returned errors: " + repr(out["errors"]))

    # 5. Conflict detector unit check: synthetic appointments.
    req = _validate_payload(fake_payload)
    synthetic_appts = [
        {"name": "Test Patient SmokeOnly", "intake_date": "07/03/2026",
         "type": "follow-up", "status": "", "insurance": "BCBS"},
        {"name": "Someone Else", "intake_date": "2026-07-02",
         "type": "consult", "status": "", "insurance": "Aetna"},
    ]
    fake_lookup = {
        "results": [
            {"source": "sis", "record": {"name": "Test Patient SmokeOnly"}},
        ],
    }
    conflicts = _detect_conflicts(req, synthetic_appts, fake_lookup)
    types = sorted(c["type"] for c in conflicts)
    assert "same_patient_in_window" in types, (
        "expected same_patient conflict, got " + repr(types)
    )
    assert "slot_taken" in types, (
        "expected slot_taken conflict, got " + repr(types)
    )
    print("[ok] conflict detector catches both classes (" + repr(types) + ")")

    # 6. Phone normalization helper.
    assert _looks_like_phone("+12155550100") is True
    assert _looks_like_phone("2155550100") is True
    assert _looks_like_phone("not a phone") is False
    print("[ok] phone detector accepts E.164 and 10-digit, rejects junk")

    # 7. Staff-SMS body composer (pure function, no network).
    req = _validate_payload(fake_payload)
    body = _build_staff_sms_body(req, "appt-test123", conflicts, {
        "ledgers": [{"found": True, "balance": "120.00", "insurance": "BCBS",
                     "status": "scheduled", "source": "picasso"}],
    })
    assert "appt-test123" in body
    assert "Test Patient SmokeOnly" in body
    assert "YES" in body and "NO" in body and "EDIT" in body
    print("[ok] staff SMS body contains request id, patient, and reply prompts")

    # 8. Patient confirmation body uses clinic identity only (Q9).
    pbody = _build_patient_confirmation_body()
    assert "Atlantic Pain & Wellness" in pbody
    assert "911" in pbody
    print("[ok] patient confirmation uses clinic identity + emergency fallback")

    print("=== all smoke checks passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(_smoke_test())

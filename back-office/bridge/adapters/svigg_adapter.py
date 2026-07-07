"""svigg_adapter.py — wraps the vendored SviggScraper (browser RPA EMR).

Svigg/WEBeDoctor has no API, so every action is deterministic Playwright RPA
(see bridge/integrations/svigg_scraper.py). Reads (search, summary,
appointments) are implemented; writes (book, cancel, create-patient) are
implemented but triple-gated in the vendored client (env kill-switch + explicit
confirm flag + test-account allowlist) and, for booking/create, tagged
UNVERIFIED until their POST contracts are HAR-verified.

This adapter:
  * runs the credential + implementation gates (bridge/adapters/_common.py),
  * for writes, halts on a weak patient match (human review),
  * passes name+DOB into book/cancel so the client's fail-closed IDENTITY GUARD
    can refuse a mismatched write (status=identity_mismatch -> honest failure),
  * hands the client a proof_path so it self-captures a proof screenshot on
    EVERY terminal outcome (verified success AND rejection/failure), surfaced
    via proof_captured + proof_kind ('completed' vs 'rejected') + screenshot_id,
  * maps the client's honest terminal vocabulary onto the envelope so ONLY a
    verified re-read (submitted_verified / created_verified / cancelled+verified)
    is success; unverified/not_found/ambiguous -> needs_human_review; gate-closed
    -> blocked; identity_mismatch / slot & callback conflicts -> failed,
  * NEVER fabricates a result — a missing selector/credential or a gated commit
    yields a structured blocked / needs_human_review / honest failure response.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .. import evidence
from ..models import (
    BookAppointmentRequest,
    BridgeResponse,
    CancelAppointmentRequest,
    CreatePatientRequest,
    PatientIdentifiers,
    Status,
    System,
)
from . import _common

SYS = System.svigg


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_client():
    """Lazily construct + start a SviggScraper (login performed by caller path).
    Imported lazily so the module imports cleanly without Playwright."""
    from ..integrations.svigg_scraper import SviggScraper

    client = SviggScraper(headless=True)
    await client.start()
    await client.login()
    return client


def _ok(action, data, match, source, *, verified=True, screenshot_id=None,
        trace_id=None, warnings=None) -> BridgeResponse:
    return BridgeResponse(
        status=Status.success,
        verified=verified,
        system=SYS,
        action=action,
        patient_match=match,
        data=data,
        source=source,
        as_of=_now_iso(),
        screenshot_id=screenshot_id,
        trace_id=trace_id,
        warnings=warnings or [],
    )


def _err(action, reason, *, screenshot_id=None) -> BridgeResponse:
    return BridgeResponse(
        status=Status.failed, system=SYS, action=action,
        failure_reason=reason, screenshot_id=screenshot_id,
    )


# The vendored client's terminal status vocabulary (svigg_scraper.py), grouped
# by how the bridge must classify each — read against the REAL source, not
# guessed. A "verified change" is the ONLY thing that becomes success.
#   book:   submitted_verified | submit_unconfirmed | date_not_on_grid |
#           no_free_slots | slot_conflict | callback_conflict |
#           overbook_click_failed | identity_mismatch | execute_blocked |
#           prepared | error
#   cancel: cancelled(+verified bool) | not_found | ambiguous |
#           identity_mismatch | execute_blocked | error
#   create: prepared | duplicate_suspected | execute_blocked |
#           created_verified | created_unverified | save_unverified | error
_VERIFIED_SUCCESS = {"submitted_verified", "created_verified", "updated_verified"}
_DRYRUN_OK = {"prepared"}  # dry-run/discovery completed — honest, nothing written
_UNVERIFIED_REVIEW = {
    "submit_unconfirmed", "created_unverified", "updated_unverified",
    "cancel_submitted_unverified", "cancel_unconfirmed",
    "not_found", "ambiguous", "ambiguous_name", "duplicate_suspected",
}
_GATE_BLOCKED = {"execute_blocked", "save_unverified"}
_HONEST_FAILURE = {
    "identity_mismatch", "date_not_on_grid", "no_free_slots", "slot_conflict",
    "callback_conflict", "overbook_click_failed", "slot_column_date_mismatch",
    "validation_bounced",
}


def _from_client_result(action, match, result: dict, source: str,
                        screenshot_id=None, trace_id=None) -> BridgeResponse:
    """Map a vendored client result-dict onto the standard envelope, honestly.

    Only a genuinely VERIFIED change (a positive post-action re-read inside the
    client) becomes ``success``. The new client also self-captures a proof
    screenshot on EVERY terminal outcome (success and rejection); we surface it
    via ``proof_captured`` + ``proof_kind`` ('completed' on a verified success,
    'rejected' otherwise). ``screenshot_id`` is the id we reserved for the file
    the client wrote — attached ONLY when the client reports ``proof_captured``.
    """
    rstatus = str(result.get("status", "")).lower()
    verified_flag = bool(result.get("verified"))
    proof_captured = bool(result.get("proof_captured"))
    # Only trust the reserved id if a shot was actually written.
    shot_id = screenshot_id if (screenshot_id and proof_captured) else None

    warnings: list[str] = []
    if result.get("warning"):
        warnings.append(str(result["warning"]))
    if rstatus == "identity_mismatch":
        warnings.append(
            "identity guard REFUSED the write (fail-closed name/DOB match) — "
            f"{result.get('error') or result.get('reason') or 'name/DOB did not match the resolved chart'}"
        )

    # A verified change: submitted_verified / created_verified, OR a cancel
    # whose own post-cancel re-read confirmed the row is gone (status
    # 'cancelled' + verified True).
    is_verified_success = (
        rstatus in _VERIFIED_SUCCESS
        or (rstatus == "cancelled" and verified_flag)
    )
    # A cancel that submitted but did NOT verify is NOT a success.
    is_unverified_cancel = rstatus == "cancelled" and not verified_flag

    proof_kind = None
    if shot_id:
        proof_kind = "completed" if is_verified_success else "rejected"

    def _envelope(status: Status, *, verified: bool, reason=None,
                  human=False) -> BridgeResponse:
        return BridgeResponse(
            status=status, verified=verified, system=SYS, action=action,
            patient_match=match, data=result, source=source, as_of=_now_iso(),
            screenshot_id=shot_id, trace_id=trace_id,
            proof_captured=proof_captured, proof_kind=proof_kind,
            requires_human_review=human, failure_reason=reason, warnings=warnings,
        )

    if is_verified_success:
        return _envelope(Status.success, verified=True)

    if rstatus in _DRYRUN_OK:
        warnings.append("dry-run: nothing was committed to the EMR.")
        return _envelope(Status.success, verified=False)

    if rstatus in _GATE_BLOCKED:
        return _envelope(
            Status.blocked, verified=False,
            reason=result.get("reason") or rstatus or "execute blocked",
        )

    if rstatus in _UNVERIFIED_REVIEW or is_unverified_cancel:
        # Submitted-but-unconfirmed, not-found (resurface w/ next route),
        # ambiguous, or duplicate — never reported as success.
        detail = (result.get("detail") or result.get("reason")
                  or result.get("warning") or rstatus)
        return _envelope(
            Status.needs_human_review, verified=False, human=True,
            reason=(f"Svigg returned '{rstatus}' — not a verified change; "
                    f"routed to human review ({detail})."),
        )

    if rstatus in _HONEST_FAILURE:
        return _envelope(
            Status.failed, verified=False,
            reason=result.get("error") or result.get("reason") or rstatus,
        )

    # error / unknown
    return _envelope(
        Status.failed, verified=False,
        reason=result.get("error") or rstatus or "unknown svigg result",
    )


# ---------------------------------------------------------------------------
# READS (implemented)
# ---------------------------------------------------------------------------
async def find_patient(patient: PatientIdentifiers, query: Optional[str]) -> BridgeResponse:
    action = "find_patient"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    last = patient.last_name or (query or "")
    if not last.strip():
        return _err(action, "Svigg search requires at least a last name")
    match = _common.compute_match(patient)
    try:
        client = await _get_client()
        try:
            rows = await client.search_patient(last.strip(), patient.first_name or "")
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_search_failed: {exc}")
    return _ok(action, rows, match, "svigg:search_patient")


async def get_patient_demographics(patient: PatientIdentifiers) -> BridgeResponse:
    action = "get_patient_demographics"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    if not patient.last_name:
        return _err(action, "Svigg demographics requires at least a last name")
    match = _common.compute_match(patient)
    try:
        client = await _get_client()
        try:
            rows = await client.search_and_summarize(
                patient.last_name, patient.first_name or ""
            )
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_demographics_failed: {exc}")
    return _ok(action, rows, match, "svigg:search_and_summarize")


async def get_upcoming_appointments(patient: PatientIdentifiers) -> BridgeResponse:
    action = "get_upcoming_appointments"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    if not patient.emr_id:
        return _err(action, "Svigg appointment history requires acct (emr_id)")
    match = _common.compute_match(patient)
    try:
        client = await _get_client()
        try:
            rows = await client.get_patient_appointments(patient.emr_id)
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_appointments_failed: {exc}")
    return _ok(action, rows, match, "svigg:get_patient_appointments")


# ---------------------------------------------------------------------------
# WRITES (implemented, gated; post-action screenshot mandatory)
# ---------------------------------------------------------------------------
async def book_appointment(req: BookAppointmentRequest) -> BridgeResponse:
    action = "book_appointment"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    match = _common.compute_match(req.patient)
    review = _common.weak_match_block(SYS, action, match)
    if review:
        return review
    if not req.patient.emr_id:
        return _err(action, "booking requires acct (emr_id) — strong match only")
    # Reserve a proof path; the client captures the screenshot ITSELF on every
    # terminal outcome (verified booking AND every rejection/refusal — see
    # svigg_scraper._capture_proof), so proof exists even when the write fails.
    proof = evidence.allocate_proof("svigg", "book")
    try:
        client = await _get_client()
        try:
            result = await client.book_appointment(
                acct=req.patient.emr_id,
                last_name=req.patient.last_name or "",
                first_name=req.patient.first_name or "",
                dob=req.patient.dob or "",     # identity-guard binding (name+DOB)
                date=req.date,
                start_time=req.time,
                duration_min=req.duration_minutes,
                appt_type=req.cpt00,           # cpt00 select value (EST/NP/...)
                provider=req.provider,
                note=req.note or "",
                execute=True,                  # still triple-gated in the client
                confirm_unverified=True,
                proof_path=proof["file_path"],
            )
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_book_failed: {exc}")
    return _from_client_result(
        action, match, result, "svigg:book_appointment",
        screenshot_id=proof["screenshot_id"],
    )


async def cancel_appointment(req: CancelAppointmentRequest) -> BridgeResponse:
    action = "cancel_appointment"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    match = _common.compute_match(req.patient)
    review = _common.weak_match_block(SYS, action, match)
    if review:
        return review
    if not (req.patient.emr_id and req.appointment_date):
        return _err(action, "cancel requires acct (emr_id) + appointment_date (YYYY-MM-DD)")
    # The vendored client now runs the HAR-faithful ENCOUNTER-keyed cancel
    # (resched.htm?enc -> resched_p Delete -> cancel2_p Yes) with a fresh
    # TFORMCOUNT read from each rendered form; the old grid-cell path is gone.
    # It self-captures proof on every terminal outcome (cancelled/not_found/
    # identity_mismatch/error). appointment_ref (enc) disambiguates when given.
    proof = evidence.allocate_proof("svigg", "cancel")
    try:
        client = await _get_client()
        try:
            result = await client.cancel_appointment(
                acct=req.patient.emr_id,
                date=req.appointment_date,
                last_name=req.patient.last_name or "",
                first_name=req.patient.first_name or "",
                dob=req.patient.dob or "",     # identity-guard binding (name+DOB)
                time=req.appointment_time or "",
                appointment_ref=req.encounter_id or "",
                reason=req.cancel_reason or "or",
                confirm=True,                  # still allowlist-gated in the client
                proof_path=proof["file_path"],
            )
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_cancel_failed: {exc}")
    return _from_client_result(
        action, match, result, "svigg:cancel_appointment",
        screenshot_id=proof["screenshot_id"],
    )


async def create_new_patient(req: CreatePatientRequest) -> BridgeResponse:
    action = "create_new_patient"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    if not (req.demographics.get("last_name") and req.demographics.get("first_name")):
        return _err(action, "create_new_patient requires demographics.last_name + first_name")
    # A create is not tied to an existing record: match is computed from the
    # proposed demographics only, and the de-dupe gate in the client protects
    # against double-create. No weak-match halt here (there is no prior record
    # to strong-match), but the commit stays fail-closed + unverified.
    match = _common.compute_match(
        PatientIdentifiers(
            first_name=req.demographics.get("first_name"),
            last_name=req.demographics.get("last_name"),
            dob=req.demographics.get("dob"),
        )
    )
    # create_patient is DRY-RUN by default and does NOT self-capture proof (no
    # proof_path param); on the triple-gated commit path (SVIGG_CREATE_EXECUTE +
    # dry_run=False + confirm_unverified) we take an adapter-level proof shot of
    # the end-state ourselves and mark the result so the mapper surfaces it.
    shot: dict = {}
    try:
        client = await _get_client()
        try:
            result = await client.create_patient(
                req.demographics,
                dry_run=req.dry_run,
                confirm_unverified=req.confirm_unverified,
            )
            if not req.dry_run:
                shot = await evidence.capture_screenshot(
                    client._page, system="svigg", label="create_verify"
                )
                # Surface the shot through the same proof_captured path the
                # book/cancel client results use.
                result["proof_captured"] = bool(shot.get("screenshot_id")
                                                 and not shot.get("error"))
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_create_failed: {exc}")
    return _from_client_result(
        action, match, result, "svigg:create_patient",
        screenshot_id=shot.get("screenshot_id"),
    )


# ---------------------------------------------------------------------------
# NOT IMPLEMENTED on Svigg — structured block, never a fake success.
# ---------------------------------------------------------------------------
async def get_referral_status(patient: PatientIdentifiers) -> BridgeResponse:
    return _common.preflight(SYS, "get_referral_status") or \
        _common.contract_block_response(SYS, "get_referral_status")


async def retrieve_notes(patient: PatientIdentifiers) -> BridgeResponse:
    return _common.preflight(SYS, "retrieve_notes") or \
        _common.contract_block_response(SYS, "retrieve_notes")


async def update_unsigned_note(_req) -> BridgeResponse:
    return _common.preflight(SYS, "update_unsigned_note") or \
        _common.contract_block_response(SYS, "update_unsigned_note")


async def append_signed_note_addendum(_req) -> BridgeResponse:
    return _common.preflight(SYS, "append_signed_note_addendum") or \
        _common.contract_block_response(SYS, "append_signed_note_addendum")

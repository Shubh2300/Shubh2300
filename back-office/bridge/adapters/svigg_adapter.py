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
  * captures a POST-ACTION verification screenshot on every write,
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


def _from_client_result(action, match, result: dict, source: str,
                        screenshot_id=None, trace_id=None) -> BridgeResponse:
    """Map a vendored client result-dict onto the standard envelope, honestly.

    The vendored methods return status strings like 'submitted_verified',
    'validation_bounced', 'execute_blocked', 'save_unverified', 'duplicate_
    suspected'. Only a verified terminal status maps to success; everything
    else maps to failed / blocked / needs_human_review with the detail intact.
    """
    rstatus = str(result.get("status", "")).lower()
    warnings = []
    if "unverified" in rstatus or result.get("warning"):
        warnings.append(str(result.get("warning") or rstatus))

    verified_terminal = rstatus in {"submitted_verified", "cancelled_verified", "verified"}
    if verified_terminal:
        return _ok(action, result, match, source, verified=True,
                   screenshot_id=screenshot_id, trace_id=trace_id, warnings=warnings)

    if rstatus in {"execute_blocked", "save_unverified"}:
        return BridgeResponse(
            status=Status.blocked, system=SYS, action=action,
            patient_match=match, data=result, source=source,
            screenshot_id=screenshot_id, trace_id=trace_id,
            failure_reason=result.get("reason", rstatus or "execute blocked"),
            warnings=warnings,
        )

    if rstatus in {"duplicate_suspected", "ambiguous", "ambiguous_name",
                   "cancel_submitted_unverified", "cancel_unconfirmed",
                   "prepared", "submitted"}:
        # Not a confirmed change — must not be reported as success.
        return BridgeResponse(
            status=Status.needs_human_review, system=SYS, action=action,
            patient_match=match, data=result, source=source,
            requires_human_review=True, screenshot_id=screenshot_id,
            trace_id=trace_id,
            failure_reason=(
                f"Svigg returned '{rstatus}' — not a verified change; "
                "routed to human review."
            ),
            warnings=warnings,
        )

    # error / unknown
    return BridgeResponse(
        status=Status.failed, system=SYS, action=action, patient_match=match,
        data=result, source=source, screenshot_id=screenshot_id, trace_id=trace_id,
        failure_reason=result.get("error") or rstatus or "unknown svigg result",
        warnings=warnings,
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
    try:
        client = await _get_client()
        try:
            result = await client.book_appointment(
                acct=req.patient.emr_id,
                last_name=req.patient.last_name or "",
                first_name=req.patient.first_name or "",
                date=req.date,
                start_time=req.time,
                duration_min=req.duration_minutes,
                appt_type=req.cpt00,           # cpt00 select value (EST/NP/...)
                provider=req.provider,
                note=req.note or "",
                execute=True,                  # still triple-gated in the client
                confirm_unverified=True,
            )
            shot = await evidence.capture_screenshot(
                client._page, system="svigg", label="book_verify"
            )
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_book_failed: {exc}")
    return _from_client_result(
        action, match, result, "svigg:book_appointment",
        screenshot_id=shot.get("screenshot_id"),
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
    warnings = []
    if req.encounter_id:
        warnings.append(
            "encounter-keyed cancel path (enc) is in svigg_reliability_fix.py, "
            "MERGE PENDING — grid path used."
        )
    try:
        client = await _get_client()
        try:
            result = await client.cancel_appointment(
                acct=req.patient.emr_id,
                date=req.appointment_date,
                last_name=req.patient.last_name or "",
                first_name=req.patient.first_name or "",
                time=req.appointment_time or "",
                appointment_ref=req.encounter_id or "",
                reason=req.cancel_reason or "or",
                confirm=True,                  # still allowlist-gated in the client
            )
            shot = await evidence.capture_screenshot(
                client._page, system="svigg", label="cancel_verify"
            )
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"svigg_cancel_failed: {exc}")
    resp = _from_client_result(
        action, match, result, "svigg:cancel_appointment",
        screenshot_id=shot.get("screenshot_id"),
    )
    resp.warnings.extend(warnings)
    return resp


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
    shot = {}
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

"""sis_adapter.py — wraps the vendored SISClient (read-only EMR).

SIS Complete is reached via its REST API through an authenticated Playwright
page (see bridge/integrations/sis_client.py). It is READ-ONLY: no booking,
cancel, patient-create, or note-write path exists in the vendored client, so
every write action returns a structured ``blocked`` response here — never a
fabricated success.

The adapter never fabricates data: it either returns what the live client read,
or a blocked / needs_human_review response. It performs the credential + match
gates via bridge/adapters/_common.py before touching the client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models import (
    BridgeResponse,
    PatientIdentifiers,
    Status,
    System,
)
from . import _common

SYS = System.sis


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_client():
    """Lazily construct + start a SISClient. Imported lazily so the module
    imports cleanly without Playwright installed."""
    from ..integrations.sis_client import SISClient

    client = SISClient(headless=True)
    await client.start()
    return client


def _ok(action: str, data, match, source: str) -> BridgeResponse:
    return BridgeResponse(
        status=Status.success,
        verified=True,
        system=SYS,
        action=action,
        patient_match=match,
        data=data,
        source=source,
        as_of=_now_iso(),
    )


def _err(action: str, reason: str) -> BridgeResponse:
    return BridgeResponse(
        status=Status.failed, system=SYS, action=action, failure_reason=reason
    )


# ---------------------------------------------------------------------------
# READS (implemented)
# ---------------------------------------------------------------------------
async def find_patient(patient: PatientIdentifiers, query: Optional[str]) -> BridgeResponse:
    action = "find_patient"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    q = query or " ".join(x for x in [patient.first_name, patient.last_name] if x) \
        or patient.emr_id or patient.phone or ""
    if not q.strip():
        return _err(action, "no query terms supplied")
    match = _common.compute_match(patient)
    try:
        client = await _get_client()
        try:
            rows = await client.search_patient(q.strip())
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover - live browser
        return _err(action, f"sis_search_failed: {exc}")
    return _ok(action, rows, match, "sis:rest/search_patient")


async def get_patient_demographics(patient: PatientIdentifiers) -> BridgeResponse:
    action = "get_patient_demographics"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    if not patient.emr_id:
        return _err(action, "SIS demographics requires emr_id (SIS patient_id)")
    match = _common.compute_match(patient)
    try:
        client = await _get_client()
        try:
            data = await client.get_patient_demographics(int(patient.emr_id))
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"sis_demographics_failed: {exc}")
    return _ok(action, data, match, "sis:rest/get_patient_demographics")


async def get_upcoming_appointments(patient: PatientIdentifiers) -> BridgeResponse:
    action = "get_upcoming_appointments"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    match = _common.compute_match(patient)
    try:
        client = await _get_client()
        try:
            data = await client.get_schedule_day()
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"sis_schedule_failed: {exc}")
    return _ok(action, data, match, "sis:rest/get_schedule_day")


async def retrieve_notes(patient: PatientIdentifiers) -> BridgeResponse:
    action = "retrieve_notes"
    blocked = _common.preflight(SYS, action)
    if blocked:
        return blocked
    if not patient.emr_id:
        return _err(action, "SIS notes retrieval requires emr_id (SIS patient_id)")
    match = _common.compute_match(patient)
    try:
        client = await _get_client()
        try:
            data = await client.get_patient_notes(int(patient.emr_id))
        finally:
            await client.stop()
    except Exception as exc:  # pragma: no cover
        return _err(action, f"sis_notes_failed: {exc}")
    return _ok(action, data, match, "sis:rest/get_patient_notes")


# ---------------------------------------------------------------------------
# NOT IMPLEMENTED on SIS — return a structured block, never a fake success.
# ---------------------------------------------------------------------------
async def get_referral_status(patient: PatientIdentifiers) -> BridgeResponse:
    return _common.preflight(SYS, "get_referral_status") or \
        _common.contract_block_response(SYS, "get_referral_status")


async def book_appointment(_req) -> BridgeResponse:
    return _common.preflight(SYS, "book_appointment") or \
        _common.contract_block_response(SYS, "book_appointment")


async def cancel_appointment(_req) -> BridgeResponse:
    return _common.preflight(SYS, "cancel_appointment") or \
        _common.contract_block_response(SYS, "cancel_appointment")


async def create_new_patient(_req) -> BridgeResponse:
    return _common.preflight(SYS, "create_new_patient") or \
        _common.contract_block_response(SYS, "create_new_patient")


async def update_unsigned_note(_req) -> BridgeResponse:
    return _common.preflight(SYS, "update_unsigned_note") or \
        _common.contract_block_response(SYS, "update_unsigned_note")


async def append_signed_note_addendum(_req) -> BridgeResponse:
    return _common.preflight(SYS, "append_signed_note_addendum") or \
        _common.contract_block_response(SYS, "append_signed_note_addendum")

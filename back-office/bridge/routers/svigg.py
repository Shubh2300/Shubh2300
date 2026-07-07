"""Svigg router — POST endpoints for Svigg/WEBeDoctor (browser RPA EMR).

Every endpoint: Pydantic-validate the input -> (writes) check the approval
token -> dispatch to bridge/adapters/svigg_adapter.py. Reads and the gated
writes (book/cancel/create) are implemented; notes are blocked (no verified
selector). Nothing here fabricates a result.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..adapters import svigg_adapter
from ..models import (
    AppendSignedNoteAddendumRequest,
    BookAppointmentRequest,
    BridgeResponse,
    CancelAppointmentRequest,
    CreatePatientRequest,
    FindPatientRequest,
    PatientRefRequest,
    System,
    UpdateUnsignedNoteRequest,
)
from ._deps import require_approval_token

router = APIRouter(prefix="/svigg", tags=["svigg"])
SYS = System.svigg


# ---- reads ----------------------------------------------------------------
@router.post("/find_patient", response_model=BridgeResponse)
async def find_patient(req: FindPatientRequest) -> BridgeResponse:
    return await svigg_adapter.find_patient(req.patient, req.query)


@router.post("/get_patient_demographics", response_model=BridgeResponse)
async def get_patient_demographics(req: PatientRefRequest) -> BridgeResponse:
    return await svigg_adapter.get_patient_demographics(req.patient)


@router.post("/get_referral_status", response_model=BridgeResponse)
async def get_referral_status(req: PatientRefRequest) -> BridgeResponse:
    return await svigg_adapter.get_referral_status(req.patient)


@router.post("/get_upcoming_appointments", response_model=BridgeResponse)
async def get_upcoming_appointments(req: PatientRefRequest) -> BridgeResponse:
    return await svigg_adapter.get_upcoming_appointments(req.patient)


@router.post("/retrieve_notes", response_model=BridgeResponse)
async def retrieve_notes(req: PatientRefRequest) -> BridgeResponse:
    return await svigg_adapter.retrieve_notes(req.patient)


# ---- writes (risk >= 2): approval-token gated, then dispatched -------------
@router.post("/book_appointment", response_model=BridgeResponse)
async def book_appointment(req: BookAppointmentRequest) -> BridgeResponse:
    gate = require_approval_token(SYS, "book_appointment", req.approval_token)
    if gate:
        return gate
    return await svigg_adapter.book_appointment(req)


@router.post("/cancel_appointment", response_model=BridgeResponse)
async def cancel_appointment(req: CancelAppointmentRequest) -> BridgeResponse:
    gate = require_approval_token(SYS, "cancel_appointment", req.approval_token)
    if gate:
        return gate
    return await svigg_adapter.cancel_appointment(req)


@router.post("/create_new_patient", response_model=BridgeResponse)
async def create_new_patient(req: CreatePatientRequest) -> BridgeResponse:
    gate = require_approval_token(SYS, "create_new_patient", req.approval_token)
    if gate:
        return gate
    return await svigg_adapter.create_new_patient(req)


@router.post("/update_unsigned_note", response_model=BridgeResponse)
async def update_unsigned_note(req: UpdateUnsignedNoteRequest) -> BridgeResponse:
    gate = require_approval_token(SYS, "update_unsigned_note", req.approval_token)
    if gate:
        return gate
    return await svigg_adapter.update_unsigned_note(req)


@router.post("/append_signed_note_addendum", response_model=BridgeResponse)
async def append_signed_note_addendum(req: AppendSignedNoteAddendumRequest) -> BridgeResponse:
    gate = require_approval_token(SYS, "append_signed_note_addendum", req.approval_token)
    if gate:
        return gate
    return await svigg_adapter.append_signed_note_addendum(req)

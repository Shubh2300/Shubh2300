"""Patients router — internal workflow-layer patient index (list/detail).

The platform's OWN minimal demographic index (decision #4). Full charts live in
the EMRs, not here. External EMR identifiers live in patient_external_ids.

patients columns: id, organization_id, first_name, last_name, dob, phone,
email, is_test_patient, created_at, updated_at.
patient_external_ids: patient_id, emr_system_id, external_id, external_id_kind.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from db import get_connection
from schemas import PatientOut

router = APIRouter(prefix="/patients", tags=["patients"])

_SELECT = """
    SELECT id, first_name, last_name, dob, phone, email, is_test_patient,
           created_at
    FROM patients
"""


def _external_ids(cur, patient_id: str) -> dict:
    cur.execute(
        """
        SELECT s.system_key AS system_code, x.external_id, x.external_id_kind
        FROM patient_external_ids x
        JOIN emr_systems s ON s.id = x.emr_system_id
        WHERE x.patient_id = %s
        """,
        (patient_id,),
    )
    out: dict = {}
    for r in cur.fetchall():
        key = r["system_code"]
        if r["external_id_kind"] and r["external_id_kind"] != "primary":
            key = f"{key}:{r['external_id_kind']}"
        out[key] = r["external_id"]
    return out


def _to_out(row: dict, external_ids: dict) -> PatientOut:
    return PatientOut(
        id=row["id"],
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        dob=str(row["dob"]) if row.get("dob") is not None else None,
        phone=row.get("phone"),
        email=row.get("email"),
        is_test_patient=row.get("is_test_patient", False),
        external_ids=external_ids,
        created_at=row["created_at"],
    )


@router.get("", response_model=List[PatientOut])
def list_patients(limit: int = 100) -> List[PatientOut]:
    limit = max(1, min(limit, 500))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
            result = [_to_out(r, _external_ids(cur, r["id"])) for r in rows]
    return result


@router.get("/{patient_id}", response_model=PatientOut)
def get_patient(patient_id: str) -> PatientOut:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE id = %s", (patient_id,))
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="patient not found")
            ext = _external_ids(cur, patient_id)
    return _to_out(row, ext)

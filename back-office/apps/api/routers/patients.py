"""Patients router — internal workflow-layer patient records (list/detail).

These are the platform's OWN patient index (decision #4: the platform builds
its own database; old patient DBs are not imported). They are the records the
workflow layer resolves against; they are not a copy of either EMR.

Assumed ``patients`` table (owned by db/schema.sql):
    id (uuid/text pk), first_name, last_name, dob (date/text),
    system_ids (jsonb: {"sis": "...", "svigg": "..."}),
    created_at (timestamptz)
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from db import get_connection
from schemas import PatientOut

router = APIRouter(prefix="/patients", tags=["patients"])


def _to_out(row: dict) -> PatientOut:
    return PatientOut(
        id=row["id"],
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        dob=str(row["dob"]) if row.get("dob") is not None else None,
        system_ids=row.get("system_ids") or {},
        created_at=row["created_at"],
    )


@router.get("", response_model=List[PatientOut])
def list_patients(limit: int = 100) -> List[PatientOut]:
    limit = max(1, min(limit, 500))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, first_name, last_name, dob, system_ids, created_at
                FROM patients ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return [_to_out(r) for r in rows]


@router.get("/{patient_id}", response_model=PatientOut)
def get_patient(patient_id: str) -> PatientOut:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, first_name, last_name, dob, system_ids, created_at
                FROM patients WHERE id = %s
                """,
                (patient_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    return _to_out(row)

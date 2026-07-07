"""Tasks router — staff submit a free-text task; list/detail.

A task is the raw staff request before parsing. The free-text prompt is stored
in ``description``; ``title`` is a short derived label (tasks.title is NOT NULL).

tasks columns: id, organization_id, patient_id, created_by, assigned_to,
title, description, status (task_status), due_at, created_at, updated_at.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException

from db import get_connection
from repositories import resolve_org_id
from schemas import TaskCreate, TaskOut

router = APIRouter(prefix="/tasks", tags=["tasks"])

_SELECT = """
    SELECT id, title, description, status, created_by, patient_id,
           created_at, updated_at
    FROM tasks
"""


def _derive_title(prompt: str) -> str:
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else "Task"
    return first_line[:117] + "..." if len(first_line) > 120 else first_line


@router.post("", response_model=TaskOut, status_code=201)
def create_task(payload: TaskCreate) -> TaskOut:
    title = payload.title or _derive_title(payload.prompt)
    with get_connection() as conn:
        with conn.cursor() as cur:
            org_id = resolve_org_id(cur)
            cur.execute(
                """
                INSERT INTO tasks
                    (organization_id, patient_id, created_by, title,
                     description, status)
                VALUES (%s, %s, %s, %s, %s, 'open')
                RETURNING id, title, description, status, created_by,
                          patient_id, created_at, updated_at
                """,
                (org_id, payload.patient_id, payload.created_by, title, payload.prompt),
            )
            row = cur.fetchone()
    return TaskOut(**row)


@router.get("", response_model=List[TaskOut])
def list_tasks(status: str | None = None, limit: int = 100) -> List[TaskOut]:
    limit = max(1, min(limit, 500))
    with get_connection() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    _SELECT + " WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute(_SELECT + " ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
    return [TaskOut(**r) for r in rows]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str) -> TaskOut:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE id = %s", (task_id,))
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskOut(**row)

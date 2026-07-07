"""Tasks router — staff submit a free-text task; CRUD/list.

A task is the raw staff request before parsing. Assumed ``tasks`` table
(owned by db/schema.sql):
    id (uuid/text pk), prompt (text), created_by (text),
    status (text), created_at (timestamptz), updated_at (timestamptz)
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, HTTPException

from db import get_connection
from schemas import TaskCreate, TaskOut

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskOut, status_code=201)
def create_task(payload: TaskCreate) -> TaskOut:
    task_id = str(uuid.uuid4())
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (id, prompt, created_by, status)
                VALUES (%s, %s, %s, 'open')
                RETURNING id, prompt, created_by, status, created_at, updated_at
                """,
                (task_id, payload.prompt, payload.created_by),
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
                    """
                    SELECT id, prompt, created_by, status, created_at, updated_at
                    FROM tasks WHERE status = %s
                    ORDER BY created_at DESC LIMIT %s
                    """,
                    (status, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, prompt, created_by, status, created_at, updated_at
                    FROM tasks ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()
    return [TaskOut(**r) for r in rows]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str) -> TaskOut:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, prompt, created_by, status, created_at, updated_at
                FROM tasks WHERE id = %s
                """,
                (task_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskOut(**row)

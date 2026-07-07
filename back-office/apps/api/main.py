"""Back Office API — staff-facing FastAPI backend.

Flow: staff task -> parse (AI) -> human approval -> Temporal workflow ->
EMR bridge -> verify + screenshot -> audit. This service owns everything up to
"start the workflow"; the worker owns execution; the bridge owns the EMR.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from db import close_pool, init_pool
from routers import action_runs, approvals, audit, intents, patients, tasks

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backoffice.api")

app = FastAPI(title="Back Office API", version="0.1.0")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks.router)
app.include_router(intents.router)
app.include_router(approvals.router)
app.include_router(action_runs.router)
app.include_router(audit.router)
app.include_router(patients.router)


@app.on_event("startup")
def _startup() -> None:
    try:
        init_pool()
    except Exception:  # noqa: BLE001 - startup should not crash on cold DB
        logger.warning("db pool not initialized at startup; will retry lazily")


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}

"""main.py — Local EMR Bridge (FastAPI).

Runs ON THE OFFICE MACHINE, bound to localhost, inside the office network. It is
NEVER exposed publicly. It is the only component that talks to a real EMR, and
it only does so through the vendored, deterministic Playwright clients wrapped
by the adapter layer.

Security posture (see README.md):
  * bind 127.0.0.1 only (see the __main__ block / run command);
  * writes require an approval_token (risk_level >= 2);
  * no PHI is logged; no mock data is ever returned;
  * missing selectors/credentials -> structured `blocked`, never a fake success.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

from .models import System
from .routers import sis as sis_router
from .routers import svigg as svigg_router

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "action-registry"
    / "actions.json"
)

app = FastAPI(
    title="Back Office — Local EMR Bridge",
    version="0.1.0",
    description=(
        "Localhost-only bridge to SIS + Svigg EMRs. Deterministic Playwright "
        "only; two-phase, verified, audited. No mock data, ever."
    ),
)

app.include_router(sis_router.router)
app.include_router(svigg_router.router)


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness only — does NOT open a browser or touch an EMR."""
    return {"status": "ok", "service": "emr-bridge"}


@app.get("/registry")
async def registry() -> dict:
    """Return the action registry (contracts) so callers can discover which
    actions are IMPLEMENTED_VENDORED vs BLOCKED before invoking them."""
    with _REGISTRY_PATH.open() as fh:
        doc = json.load(fh)
    summary = {
        a["action_name"]: {
            "target_system": a["target_system"],
            "risk_level": a["risk_level"],
            "implementation_status": a["implementation_status"],
        }
        for a in doc["actions"]
    }
    return {"count": len(summary), "actions": summary}


@app.get("/systems")
async def systems() -> dict:
    return {"systems": [s.value for s in System if s not in (System.both, System.unknown)]}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    # Localhost-only bind. Do NOT change host to 0.0.0.0 — this service must
    # never be reachable off the office machine.
    uvicorn.run("bridge.main:app", host="127.0.0.1", port=8901, reload=False)

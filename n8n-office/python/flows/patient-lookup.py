#!/usr/bin/env python3
"""
patient-lookup.py - Patient Lookup flow (n8n-office, flow #1).

Per Q2 (locked): Patient Lookup is the first flow built end-to-end, because
every other flow (Appointment, File Request, General Message) keys off
"which patient is this?".

What this module does
---------------------
1. Accepts a lookup payload (name / phone / email / dob).
2. Checks the short-TTL cache in emr_bridge first (cache-first per spec).
3. Fans out a parallel read-only lookup across Sunnyvig (Svigg),
   SysComplete (SIS) and Google Drive via `emr_bridge.lookup_patient()`.
4. Decides new-vs-existing using a deterministic rule:
       existing  iff  any source returned a record AND the per-source
                      confidence rolls up to >= EXISTING_CONFIDENCE_THRESHOLD.
5. Returns a merged result with per-source confidence + an aggregate
   `decision` block the chatbot/n8n flow can branch on.
6. Writes a tamper-evident audit row via `audit_log.AuditLog` (Q7).

What this module does NOT do
----------------------------
- No writes to any EMR (Q5: read-only for v1).
- No PHI leaves the box. The Anthropic Haiku triage layer (Q3) is upstream
  of this flow; we get a sanitized lookup payload, not raw SMS body.
- No appointment / billing logic — that's a separate flow.

Style note: mirrors server.py's plain-stdlib, route-by-`elif`, no-extra-deps
posture. FastAPI is wired only if it's importable so a missing dep can never
brick the module's `__main__` smoke test or the n8n subprocess invocation
path. The n8n workflow JSON (../n8n-workflows/patient-lookup.json) calls
this script directly via the `Execute Command` node and parses stdout JSON;
the FastAPI app is for the human-debug HTTP path only.

Usage
-----
    # As a CLI (what n8n invokes):
    python3 patient-lookup.py '{"name":"Dorca Jones","phone":"5551234"}'

    # As a FastAPI service (manual debugging):
    uvicorn flows.patient_lookup:app --port 8011

    # As an importable module (for tests / other Python flows):
    from flows.patient_lookup import run_lookup
    result = run_lookup({"name": "Dorca Jones"})

Dashes in the filename: import via importlib if the caller needs the
function, or invoke as a script. n8n uses the script form.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

# ─── Path bootstrap so `from integrations import ...` works no matter how ─────
# this file is invoked (script, importlib, uvicorn, n8n Execute Command).
_HERE = Path(__file__).resolve().parent
_PY_ROOT = _HERE.parent            # /Users/shubh/n8n-office/python
_REPO_ROOT = _PY_ROOT.parent       # /Users/shubh/n8n-office
if str(_PY_ROOT) not in sys.path:
    sys.path.insert(0, str(_PY_ROOT))


# ─── Load .env file at startup (mirrors server.py — no python-dotenv dep) ────
def _load_dotenv(path: str = ".env") -> None:
    env_path = _REPO_ROOT / path
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


# ─── Logger ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("PATIENT_LOOKUP_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
)
logger = logging.getLogger("patient-lookup")


# ─── Integrations (the locked decision: import from integrations/) ───────────
from integrations import emr_bridge  # noqa: E402  (after sys.path bootstrap)
from integrations.audit_log import AuditLog, AuditLogError  # noqa: E402


# ─── FastAPI optional import — keeps the CLI / n8n path dep-free ─────────────
try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    FastAPI = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    BaseModel = object  # type: ignore[assignment,misc]

    def Field(*_args, **_kwargs):  # type: ignore[no-redef]
        return None


# ─── Configuration ───────────────────────────────────────────────────────────
# Sources we fan out to. Drive is included per spec; for v1 the "drive" probe
# is a metadata lookup against the patient folder index (read-only).
DEFAULT_SOURCES = ("svigg", "sis", "drive")

# Roll-up rule: a patient is treated as "existing" when at least one source
# returns a hit AND the strongest source confidence is >= this threshold.
EXISTING_CONFIDENCE_THRESHOLD = float(
    os.environ.get("PATIENT_LOOKUP_EXISTING_THRESHOLD", "0.70")
)

# Per-source confidence weighting when multiple sources agree.
# SIS holds the clinical chart of record (Q5), so it wins ties.
SOURCE_WEIGHTS = {
    "sis": 1.00,
    "svigg": 0.90,
    "picasso": 0.85,
    "webedoctor": 0.75,
    "drive": 0.50,  # files prove existence but not identity by themselves
}

# Parallel fan-out budget. EMR RPA calls are slow; the cache layer makes the
# 2nd+ call sub-millisecond, so 6s per source is plenty in the cached path
# and forces a fail-fast on cold portals (n8n's retry policy — Q10 — handles
# the second attempt).
PER_SOURCE_TIMEOUT_S = float(os.environ.get("PATIENT_LOOKUP_TIMEOUT_S", "6.0"))


# ─── Credential pointers (locked Q6: .env on FileVault, chmod 0600) ──────────
# These are referenced lazily so the module imports cleanly even before the
# operator has provisioned every credential. The audit-log pepper is the only
# hard requirement at construction time — without it we cannot honestly log
# PHI access, so we refuse to start.
#
# TODO: WIRE CREDS - AUDIT_LOG_PEPPER          (>=32 random bytes, hex/base64)
# TODO: WIRE CREDS - ANTHROPIC_API_KEY         (Q3 Haiku triage; upstream)
# TODO: WIRE CREDS - GOOGLE_SERVICE_ACCOUNT_JSON  (Drive read-only scope)
# TODO: WIRE CREDS - SIS_USERNAME, SIS_PASSWORD     (RPA session bootstrap)
# TODO: WIRE CREDS - SVIGG_USERNAME, SVIGG_PASSWORD (RPA session bootstrap)
# TODO: WIRE CREDS - RINGCENTRAL_JWT           (Q4 approval-gate SMS DM)
AUDIT_DB_PATH = os.environ.get(
    "AUDIT_DB_PATH",
    str(_REPO_ROOT / "data" / "audit.sqlite"),
)


def _get_audit() -> Optional[AuditLog]:
    """
    Return a constructed AuditLog if the pepper is present, else None.
    A missing pepper is a config error in prod (we'll refuse to handle PHI),
    but in dev / smoke-test we want the module to still load and report it.
    """
    pepper = os.environ.get("AUDIT_LOG_PEPPER", "")
    if not pepper:
        return None
    try:
        return AuditLog(AUDIT_DB_PATH, pepper=pepper)
    except AuditLogError as exc:
        logger.error("AuditLog init failed: %s", exc)
        return None


# ─── Lookup-key normalization & confidence scoring ───────────────────────────


def _normalize_key(payload: dict) -> str:
    """
    Build the single string passed to emr_bridge.lookup_patient(). The bridge
    matches by name / phone / email, in that order, so we pick the strongest
    identifier the payload contains. Order = "stable id first".
    """
    if not payload:
        return ""
    # Prefer phone (10-digit-normalized) > email > name. DOB on its own is
    # never a lookup key — too many collisions.
    phone = (payload.get("phone") or "").strip()
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        # Antigravity stores phone as the raw user-entered string; pass digits
        # through — the bridge does a substring/equality match.
        return digits or phone
    email = (payload.get("email") or "").strip().lower()
    if email:
        return email
    name = (payload.get("name") or "").strip()
    return name


def _score_record(payload: dict, record: Optional[dict]) -> float:
    """
    Score 0.0–1.0 for how confidently `record` is the patient described by
    `payload`. Deterministic, no ML. Returns 0.0 when there is no record.

    Weights (sum=1.0):
        phone match     0.45
        email match     0.20
        DOB match       0.20
        name exact      0.10
        name substring  0.05
    """
    if not record:
        return 0.0

    score = 0.0

    want_phone = "".join(c for c in (payload.get("phone") or "") if c.isdigit())
    have_phone = "".join(c for c in str(record.get("phone") or "") if c.isdigit())
    if want_phone and have_phone and want_phone[-10:] == have_phone[-10:]:
        score += 0.45

    want_email = (payload.get("email") or "").strip().lower()
    have_email = str(record.get("email") or "").strip().lower()
    if want_email and have_email and want_email == have_email:
        score += 0.20

    want_dob = (payload.get("dob") or "").strip()
    have_dob = str(record.get("dob") or "").strip()
    if want_dob and have_dob and want_dob == have_dob:
        score += 0.20

    want_name = (payload.get("name") or "").strip().lower()
    have_name = str(record.get("name") or "").strip().lower()
    if want_name and have_name:
        if want_name == have_name:
            score += 0.10
        elif want_name in have_name or have_name in want_name:
            score += 0.05

    return round(min(score, 1.0), 3)


# ─── The per-source worker (runs inside the ThreadPoolExecutor) ──────────────


def _lookup_one_source(payload: dict, source: str) -> dict:
    """
    Run a single-source lookup. Returns a dict shaped for the merged result.
    Never raises — errors are flattened into the dict so one bad source can't
    cancel the others. n8n's IF-node branches on `error`.
    """
    key = _normalize_key(payload)
    started = time.monotonic()

    if not key:
        return {
            "source": source,
            "label": emr_bridge.EMR_SOURCES.get(source, {}).get("label", source),
            "record": None,
            "confidence": 0.0,
            "cached": False,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": "no_lookup_key",
        }

    # "drive" is not in emr_bridge.EMR_SOURCES today. For v1 we treat a Drive
    # hit as "patient folder exists by name", served from the Antigravity
    # scratch layout (and later swapped for a real Drive Files.list call once
    # the service-account credentials are wired). Until then it's a deliberate
    # stub that reports back with confidence 0.0 and no record.
    if source == "drive":
        return {
            "source": "drive",
            "label": "Google Drive (patient folder index)",
            "record": None,
            "confidence": 0.0,
            "cached": False,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            # TODO: WIRE CREDS - GOOGLE_SERVICE_ACCOUNT_JSON
            "note": "drive probe stubbed — wire GOOGLE_SERVICE_ACCOUNT_JSON",
        }

    try:
        # emr_bridge.lookup_patient itself fans out to all known sources when
        # source="all"; we drive it one source at a time so we can score each
        # independently and so the ThreadPoolExecutor below actually buys us
        # parallelism (rather than every thread blocking on a single
        # subprocess inside the bridge).
        resp = emr_bridge.lookup_patient(key, source=source)
        # resp["results"] is a list of one in single-source mode.
        first = (resp.get("results") or [{}])[0]
        record = first.get("record")
        confidence = _score_record(payload, record)
        # Weight by source so e.g. a perfect-info Drive hit can't outscore a
        # weak SIS hit when they disagree.
        weight = SOURCE_WEIGHTS.get(source, 0.5)
        weighted = round(confidence * weight, 3)
        return {
            "source": source,
            "label": first.get("label", source),
            "record": record,
            "confidence": confidence,
            "confidence_weighted": weighted,
            "cached": bool(first.get("cached")),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": first.get("error"),
        }
    except emr_bridge.EMRSessionExpired as exc:
        return {
            "source": source,
            "label": emr_bridge.EMR_SOURCES.get(source, {}).get("label", source),
            "record": None,
            "confidence": 0.0,
            "cached": False,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": "session_expired",
            "detail": str(exc),
        }
    except emr_bridge.EMRBridgeError as exc:
        return {
            "source": source,
            "label": emr_bridge.EMR_SOURCES.get(source, {}).get("label", source),
            "record": None,
            "confidence": 0.0,
            "cached": False,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": "bridge_error",
            "detail": str(exc),
        }
    except Exception as exc:  # never let a single source crash the flow
        logger.exception("Unexpected error in source=%s", source)
        return {
            "source": source,
            "label": emr_bridge.EMR_SOURCES.get(source, {}).get("label", source),
            "record": None,
            "confidence": 0.0,
            "cached": False,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": "unexpected",
            "detail": str(exc),
        }


# ─── Cache-first check (the spec calls this out explicitly) ──────────────────


def _all_sources_cached(payload: dict, sources: list[str]) -> Optional[list[dict]]:
    """
    Probe emr_bridge's internal cache for every source. If every probe hits,
    return the synthesized per-source list and skip the ThreadPool entirely.
    A single miss returns None and we fall through to the parallel path.
    """
    key = _normalize_key(payload)
    if not key:
        return None
    out: list[dict] = []
    for src in sources:
        if src == "drive":
            # drive is stubbed; never claim a cache hit so we always re-stub.
            return None
        cached = emr_bridge._cache_get(src, "lookup_patient", key.lower())
        if cached is None:
            return None
        out.append(
            {
                "source": src,
                "label": emr_bridge.EMR_SOURCES.get(src, {}).get("label", src),
                "record": cached,
                "confidence": _score_record(payload, cached),
                "confidence_weighted": round(
                    _score_record(payload, cached) * SOURCE_WEIGHTS.get(src, 0.5),
                    3,
                ),
                "cached": True,
                "elapsed_ms": 0,
                "error": None,
            }
        )
    return out


# ─── Decision / merge logic ──────────────────────────────────────────────────


def _merge_decision(per_source: list[dict]) -> dict:
    """
    Roll the per-source results up into a single decision the n8n IF-node
    can branch on:

        decision.status        = "existing" | "new" | "ambiguous" | "error"
        decision.confidence    = highest weighted source confidence
        decision.winning_source= source whose record we keep, or None
        decision.merged_record = the canonical record we hand downstream
        decision.session_expired = [sources, ...]   (Q10 escalation hook)
    """
    expired = [r["source"] for r in per_source if r.get("error") == "session_expired"]
    hits = [r for r in per_source if r.get("record")]

    if not hits:
        # Distinguish "everyone returned cleanly, no record" from "all errored".
        all_errored = all(r.get("error") for r in per_source)
        return {
            "status": "error" if all_errored else "new",
            "confidence": 0.0,
            "winning_source": None,
            "merged_record": None,
            "session_expired": expired,
        }

    hits.sort(key=lambda r: r.get("confidence_weighted", 0.0), reverse=True)
    top = hits[0]
    top_conf = top.get("confidence_weighted", 0.0)

    # Ambiguity check: two near-equal hits with different records.
    if len(hits) >= 2:
        runner = hits[1]
        same_record = (
            (top.get("record") or {}).get("name", "").lower()
            == (runner.get("record") or {}).get("name", "").lower()
        )
        if (
            not same_record
            and top_conf - runner.get("confidence_weighted", 0.0) < 0.05
        ):
            return {
                "status": "ambiguous",
                "confidence": top_conf,
                "winning_source": None,
                "merged_record": None,
                "candidates": [top.get("record"), runner.get("record")],
                "session_expired": expired,
            }

    status = "existing" if top_conf >= EXISTING_CONFIDENCE_THRESHOLD else "new"
    return {
        "status": status,
        "confidence": top_conf,
        "winning_source": top["source"],
        "merged_record": top.get("record"),
        "session_expired": expired,
    }


# ─── Public entrypoint ───────────────────────────────────────────────────────


def run_lookup(
    payload: dict,
    sources: Optional[list[str]] = None,
    actor: str = "n8n-flow:patient-lookup",
) -> dict:
    """
    Top-level entrypoint. Cache-first; parallel fan-out on miss; merge; audit.

    Args:
        payload : { name?, phone?, email?, dob?, intent? }
        sources : optional source list; defaults to DEFAULT_SOURCES.
        actor   : audit-log actor string (Q11 unique-identity requirement).

    Returns:
        {
          ok: bool,
          patient_lookup_key: str,
          sources: [ {source, record, confidence, cached, ...}, ... ],
          decision: { status, confidence, winning_source, merged_record, ... },
          elapsed_ms: int,
          audit_row_id: int | None,
        }
    """
    started = time.monotonic()
    src_list = list(sources or DEFAULT_SOURCES)

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "payload must be a dict",
            "elapsed_ms": 0,
        }

    key = _normalize_key(payload)
    if not key:
        return {
            "ok": False,
            "error": "no lookup key (need name, phone, or email)",
            "elapsed_ms": 0,
        }

    # Cache-first per spec.
    cached_all = _all_sources_cached(payload, src_list)
    if cached_all is not None:
        per_source = cached_all
        cache_path = "all_cached"
    else:
        per_source = []
        cache_path = "fanout"
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(src_list)
        ) as pool:
            futures = {
                pool.submit(_lookup_one_source, payload, src): src
                for src in src_list
            }
            for fut in concurrent.futures.as_completed(
                futures, timeout=PER_SOURCE_TIMEOUT_S * len(src_list) + 5
            ):
                src = futures[fut]
                try:
                    per_source.append(
                        fut.result(timeout=PER_SOURCE_TIMEOUT_S)
                    )
                except concurrent.futures.TimeoutError:
                    per_source.append(
                        {
                            "source": src,
                            "label": emr_bridge.EMR_SOURCES.get(src, {}).get(
                                "label", src
                            ),
                            "record": None,
                            "confidence": 0.0,
                            "cached": False,
                            "error": "timeout",
                        }
                    )

    # Stable order for downstream consumers (n8n nodes prefer deterministic JSON).
    per_source.sort(key=lambda r: src_list.index(r["source"]))

    decision = _merge_decision(per_source)

    # Audit (Q7). Hash patient id is automatic inside AuditLog. The patient_id
    # we pass is the lookup key, not raw PHI from the SMS body.
    audit_row_id: Optional[int] = None
    audit = _get_audit()
    if audit is not None:
        try:
            audit_row_id = audit.log(
                actor=actor,
                intent="patient-lookup",
                action="READ",
                result_summary=(
                    f"status={decision['status']} "
                    f"conf={decision['confidence']} path={cache_path}"
                ),
                patient_id=key,
                error_msg=(
                    None
                    if decision["status"] != "error"
                    else ",".join(
                        r.get("error", "?")
                        for r in per_source
                        if r.get("error")
                    )
                ),
            )
        except AuditLogError as exc:
            logger.error("audit.log failed: %s", exc)

    return {
        "ok": True,
        "patient_lookup_key": key,
        "cache_path": cache_path,
        "sources": per_source,
        "decision": decision,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "audit_row_id": audit_row_id,
    }


# ─── FastAPI surface (optional — for human debug, not the n8n hot path) ──────

if FASTAPI_AVAILABLE:

    class LookupPayload(BaseModel):  # type: ignore[misc]
        name: Optional[str] = Field(default=None)
        phone: Optional[str] = Field(default=None)
        email: Optional[str] = Field(default=None)
        dob: Optional[str] = Field(default=None)
        intent: Optional[str] = Field(default=None)
        actor: Optional[str] = Field(default="n8n-flow:patient-lookup")

    app = FastAPI(
        title="patient-lookup (n8n-office)",
        version="1.0.0",
        description=(
            "Read-only patient lookup across SIS, Svigg, and Drive. "
            "Q5 lock: no write paths exposed on this surface."
        ),
    )

    @app.get("/health")
    def health() -> dict:  # noqa: D401
        return {
            "ok": True,
            "fastapi": True,
            "audit_ready": _get_audit() is not None,
            "sources": list(DEFAULT_SOURCES),
        }

    @app.post("/lookup")
    def lookup(body: LookupPayload) -> dict:  # type: ignore[valid-type]
        payload = body.model_dump(exclude_none=True)  # type: ignore[attr-defined]
        actor = payload.pop("actor", "n8n-flow:patient-lookup")
        result = run_lookup(payload, actor=actor)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error"))  # type: ignore[misc]
        return result

else:
    app = None  # type: ignore[assignment]


# ─── Convenience: importable-by-dashes helper ────────────────────────────────


def load_self_as_module(alias: str = "patient_lookup"):
    """
    Helper for callers that need to `import` this file but cannot use the
    dashed filename directly. Returns the loaded module object.

        mod = load_self_as_module()
        mod.run_lookup({"name": "Dorca Jones"})
    """
    spec = importlib.util.spec_from_file_location(alias, __file__)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not build importlib spec for self")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── CLI / smoke test ────────────────────────────────────────────────────────


def _parse_cli_payload(argv: list[str]) -> dict:
    """
    n8n's Execute Command node passes a single JSON arg. The CLI also accepts
    --name/--phone/--email/--dob for human use.
    """
    if len(argv) == 1 and argv[0].startswith("{"):
        try:
            return json.loads(argv[0])
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSON payload: {exc}")
    parser = argparse.ArgumentParser(
        description="Patient Lookup flow (n8n-office)"
    )
    parser.add_argument("--name")
    parser.add_argument("--phone")
    parser.add_argument("--email")
    parser.add_argument("--dob")
    parser.add_argument("--intent", default="lookup")
    parser.add_argument(
        "--sources",
        default=",".join(DEFAULT_SOURCES),
        help="comma-separated source list",
    )
    parser.add_argument(
        "--actor",
        default="cli:patient-lookup",
        help="audit-log actor string",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="run the offline routing smoke test (no real API calls)",
    )
    ns = parser.parse_args(argv)
    return {
        "_argparse": ns,
        "name": ns.name,
        "phone": ns.phone,
        "email": ns.email,
        "dob": ns.dob,
        "intent": ns.intent,
    }


def _smoke_test() -> int:
    """
    Verify routing/merge logic with a fake payload — no real API calls.
    Exercises:
      - empty payload rejection
      - normalization order (phone > email > name)
      - decision rollup (new / existing / ambiguous)
      - audit logger is optional
    Exits non-zero on failure so CI / verify.sh can wire it in.
    """
    print("=== patient-lookup smoke test (offline) ===")
    failures: list[str] = []

    # 1. Empty payload returns ok=False with a clear error.
    r = run_lookup({})
    assert r["ok"] is False, "empty payload should be rejected"
    assert "no lookup key" in r["error"], r
    print("  [ok] empty payload rejected")

    # 2. Normalization picks phone over name when both present.
    assert _normalize_key({"name": "X", "phone": "(555) 123-4567"}) == "5551234567"
    assert _normalize_key({"name": "Jane Doe"}) == "Jane Doe"
    assert _normalize_key({"email": "A@B.com"}) == "a@b.com"
    print("  [ok] key normalization")

    # 3. Score record sanity.
    payload = {"name": "Dorca Jones", "phone": "5551234567"}
    score = _score_record(
        payload, {"name": "Dorca Jones", "phone": "(555) 123-4567"}
    )
    assert score >= 0.55, f"expected confident match, got {score}"
    print(f"  [ok] _score_record exact match -> {score}")

    score_none = _score_record(payload, None)
    assert score_none == 0.0
    print("  [ok] _score_record on None -> 0.0")

    # 4. _merge_decision: no hits -> status="new".
    per_source = [
        {"source": "sis", "record": None, "confidence_weighted": 0.0,
         "error": None},
        {"source": "svigg", "record": None, "confidence_weighted": 0.0,
         "error": None},
        {"source": "drive", "record": None, "confidence_weighted": 0.0,
         "error": None},
    ]
    d = _merge_decision(per_source)
    assert d["status"] == "new", d
    print("  [ok] merge_decision empty -> new")

    # 5. _merge_decision: strong SIS hit -> existing.
    per_source = [
        {"source": "sis",
         "record": {"name": "Dorca Jones", "phone": "5551234567"},
         "confidence": 0.95, "confidence_weighted": 0.95,
         "cached": False, "error": None},
        {"source": "svigg", "record": None,
         "confidence_weighted": 0.0, "error": None},
        {"source": "drive", "record": None,
         "confidence_weighted": 0.0, "error": None},
    ]
    d = _merge_decision(per_source)
    assert d["status"] == "existing", d
    assert d["winning_source"] == "sis"
    print("  [ok] merge_decision SIS hit -> existing")

    # 6. _merge_decision: weak hit only -> new (below threshold).
    per_source = [
        {"source": "drive",
         "record": {"name": "Some Folder"},
         "confidence": 0.10, "confidence_weighted": 0.05,
         "cached": False, "error": None},
    ]
    d = _merge_decision(per_source)
    assert d["status"] == "new", f"weak-only hit should be 'new', got {d}"
    print("  [ok] merge_decision weak-only -> new")

    # 7. _merge_decision: ambiguity (two near-equal hits, different records).
    per_source = [
        {"source": "sis",
         "record": {"name": "John Smith Sr"},
         "confidence_weighted": 0.80, "error": None},
        {"source": "svigg",
         "record": {"name": "John Smith Jr"},
         "confidence_weighted": 0.78, "error": None},
    ]
    d = _merge_decision(per_source)
    assert d["status"] == "ambiguous", d
    print("  [ok] merge_decision conflicting hits -> ambiguous")

    # 8. _merge_decision: every source errored -> status=error.
    per_source = [
        {"source": "sis", "record": None, "confidence_weighted": 0.0,
         "error": "session_expired"},
        {"source": "svigg", "record": None, "confidence_weighted": 0.0,
         "error": "bridge_error"},
        {"source": "drive", "record": None, "confidence_weighted": 0.0,
         "error": "no_lookup_key"},
    ]
    d = _merge_decision(per_source)
    assert d["status"] == "error", d
    assert d["session_expired"] == ["sis"], d
    print("  [ok] merge_decision all-errored -> error + session_expired list")

    # 9. _get_audit gracefully returns None when pepper missing.
    saved = os.environ.pop("AUDIT_LOG_PEPPER", None)
    try:
        assert _get_audit() is None
        print("  [ok] missing pepper -> _get_audit() returns None")
    finally:
        if saved is not None:
            os.environ["AUDIT_LOG_PEPPER"] = saved

    if failures:
        print("FAIL:", failures)
        return 1
    print("=== all smoke checks passed ===")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Plain `python3 patient-lookup.py` with no args runs the smoke test —
    # matches the project's pattern (server.py serves; smaller utilities
    # default to a self-check when invoked bare).
    if not argv or "--smoke" in argv:
        return _smoke_test()

    parsed = _parse_cli_payload(argv)
    # Drop the argparse handle if present.
    sources = None
    actor = "cli:patient-lookup"
    if "_argparse" in parsed:
        ns = parsed.pop("_argparse")
        sources = [s.strip() for s in (ns.sources or "").split(",") if s.strip()]
        actor = ns.actor or actor
    # Strip Nones so the merger sees a clean payload.
    payload = {k: v for k, v in parsed.items() if v is not None}
    result = run_lookup(payload, sources=sources, actor=actor)
    # Emit a single JSON line so n8n's Execute Command node can parse stdout.
    print(json.dumps(result, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())

"""Temporal activities for ExecuteActionWorkflow.

Order (per spec): validate_intent -> call_bridge -> verify_result ->
record_outcome. Activities run OUTSIDE the workflow sandbox so they may do
real I/O (HTTP to the bridge, Postgres writes).

Safety rules encoded here:
  * A write whose outcome is unknown (network failure mid-request) is NEVER
    retried — it raises a non-retryable error tagged so the workflow routes it
    to human review.
  * Reads may be retried on transient network errors.
  * The bridge envelope is the source of truth: success AND verified are both
    required to complete; blocked / needs_human_review route to human review.
  * No PHI in logs — ids/counts only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shared import ActionOutcome, BridgeEnvelope

logger = logging.getLogger("backoffice.worker.activities")

GENESIS_PREV_HASH = "0" * 64
_HASHED_FIELDS = (
    "ts",
    "actor",
    "action",
    "intent",
    "target_patient_id_hash",
    "result_summary",
)


# --------------------------------------------------------------------------- #
# validate_intent
# --------------------------------------------------------------------------- #
@dataclass
class ValidatedAction:
    action: str
    system: Optional[str]
    endpoint: str
    method: str
    write: bool
    inputs: Dict[str, Any]


def _load_registry() -> Dict[str, Dict[str, Any]]:
    path = os.environ.get(
        "ACTION_REGISTRY_PATH", "/app/packages/action-registry/actions.json"
    )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = data["actions"] if isinstance(data, dict) and "actions" in data else data
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(actions, list):
        for raw in actions:
            name = raw.get("action") or raw.get("name")
            if name:
                out[name] = raw
    elif isinstance(actions, dict):
        out = actions
    return out


@activity.defn
async def validate_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Re-validate the intent against the registry inside the workflow.

    Raises a non-retryable error if the action is unknown or required inputs
    are missing — the workflow catches this and routes to human review.
    """
    registry = _load_registry()
    action = intent.get("action")
    contract = registry.get(action) if action else None
    if contract is None:
        raise ApplicationError(
            f"unknown action '{action}'",
            type="IntentRejected",
            non_retryable=True,
        )

    required = list(contract.get("required_inputs") or [])
    inputs = intent.get("inputs") or {}
    missing = [r for r in required if inputs.get(r) in (None, "")]
    if missing:
        raise ApplicationError(
            "missing required inputs: " + ", ".join(missing),
            type="IntentRejected",
            non_retryable=True,
        )

    endpoint = contract.get("endpoint") or contract.get("path")
    if not endpoint:
        raise ApplicationError(
            f"action '{action}' has no bridge endpoint in the registry",
            type="IntentRejected",
            non_retryable=True,
        )

    validated = ValidatedAction(
        action=action,
        system=contract.get("system"),
        endpoint=endpoint,
        method=str(contract.get("method") or "POST").upper(),
        write=bool(contract.get("write", False)),
        inputs=inputs,
    )
    activity.logger.info(
        "validate_intent ok action=%s write=%s", validated.action, validated.write
    )
    return validated.__dict__


# --------------------------------------------------------------------------- #
# call_bridge
# --------------------------------------------------------------------------- #
@dataclass
class CallBridgeInput:
    endpoint: str
    method: str
    write: bool
    payload: Dict[str, Any]


@activity.defn
async def call_bridge(req: Dict[str, Any]) -> Dict[str, Any]:
    """POST/GET the EMR bridge and return its response envelope as a dict.

    Retry semantics are enforced by the workflow (different RetryPolicy for
    reads vs writes). Here we translate failure modes into typed errors:
      * A network error on a WRITE -> non-retryable ``WriteOutcomeUnknown``
        (never retry a write whose outcome we cannot confirm).
      * A network error on a READ  -> retryable ``BridgeNetworkError``.
      * Any HTTP response received (even 4xx/5xx) is returned as the envelope;
        the bridge is expected to speak the standard envelope even on failure.
    """
    base = os.environ.get("BRIDGE_URL", "http://host.docker.internal:8600")
    url = base.rstrip("/") + "/" + req["endpoint"].lstrip("/")
    method = req.get("method", "POST").upper()
    is_write = bool(req.get("write", False))
    payload = req.get("payload") or {}

    try:
        with httpx.Client(timeout=60.0) as client:
            if method == "GET":
                resp = client.get(url, params=payload)
            else:
                resp = client.request(method, url, json=payload)
    except httpx.HTTPError as exc:
        activity.logger.warning(
            "call_bridge network_error write=%s type=%s", is_write, type(exc).__name__
        )
        if is_write:
            # Outcome is unknown; must not retry. Route to human review.
            raise ApplicationError(
                "write outcome unknown after network error",
                type="WriteOutcomeUnknown",
                non_retryable=True,
            )
        raise ApplicationError(
            f"bridge network error: {type(exc).__name__}",
            type="BridgeNetworkError",
            non_retryable=False,
        )

    try:
        envelope = resp.json()
    except ValueError:
        # Non-JSON body: the bridge failed to speak the envelope. Treat as
        # ambiguous -> caller will route to human review.
        raise ApplicationError(
            f"bridge returned non-envelope body (http {resp.status_code})",
            type="BridgeBadResponse",
            non_retryable=True,
        )

    activity.logger.info(
        "call_bridge ok http=%s status=%s", resp.status_code, envelope.get("status")
    )
    return envelope


# --------------------------------------------------------------------------- #
# verify_result
# --------------------------------------------------------------------------- #
@activity.defn
async def verify_result(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect the bridge envelope and decide the terminal state.

    Rule: status == success AND verified is True is required to complete.
    blocked / needs_human_review (or requires_human_review flag) route to human
    review. Anything ambiguous also routes to human review — never a silent
    success.
    """
    env = BridgeEnvelope.from_dict(envelope)

    common = dict(
        status=env.status,
        verified=env.verified,
        screenshot_id=env.screenshot_id,
        trace_id=env.trace_id,
        warnings=env.warnings,
    )

    if env.status == "success" and env.verified and not env.requires_human_review:
        outcome = ActionOutcome(state="verified", failure_reason=None, **common)
    elif env.status in ("blocked", "needs_human_review") or env.requires_human_review:
        outcome = ActionOutcome(
            state="needs_human_review",
            failure_reason=env.failure_reason or f"routed: status={env.status}",
            **common,
        )
    elif env.status == "failed":
        outcome = ActionOutcome(
            state="failed",
            failure_reason=env.failure_reason or "bridge reported failed",
            **common,
        )
    else:
        # success-but-unverified, unknown status, or any other ambiguity.
        outcome = ActionOutcome(
            state="needs_human_review",
            failure_reason=(
                env.failure_reason
                or f"ambiguous result: status={env.status} verified={env.verified}"
            ),
            **common,
        )

    activity.logger.info("verify_result state=%s", outcome.state)
    return outcome.__dict__


# --------------------------------------------------------------------------- #
# record_outcome
# --------------------------------------------------------------------------- #
def _compute_entry_hash(entry: Dict[str, Any], prev_hash: str) -> str:
    preimage = {k: entry.get(k) for k in _HASHED_FIELDS}
    preimage["prev_hash"] = prev_hash
    canonical = json.dumps(
        preimage, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class RecordOutcomeInput:
    action_run_id: str
    approval_id: str
    action: str
    outcome: Dict[str, Any]


@activity.defn
async def record_outcome(req: Dict[str, Any]) -> None:
    """Update action_runs and append to the hash-chained audit_logs table.

    Uses psycopg3. No PHI written: audit stores hashed patient id only (the
    worker does not carry patient ids into the audit here — actor/action/result
    counts only)."""
    import psycopg
    from psycopg.types.json import Json

    dsn = os.environ["DATABASE_URL"]
    outcome = req["outcome"]
    warnings = outcome.get("warnings") or []

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE action_runs
                SET state = %s,
                    status = %s,
                    verified = %s,
                    screenshot_id = %s,
                    trace_id = %s,
                    failure_reason = %s,
                    warnings = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    outcome.get("state"),
                    outcome.get("status"),
                    outcome.get("verified"),
                    outcome.get("screenshot_id"),
                    outcome.get("trace_id"),
                    outcome.get("failure_reason"),
                    Json(warnings),
                    req["action_run_id"],
                ),
            )

            # Append to the audit chain (serialized against concurrent writers).
            cur.execute("LOCK TABLE audit_logs IN EXCLUSIVE MODE")
            cur.execute(
                "SELECT entry_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            prev_hash = row[0] if row else GENESIS_PREV_HASH
            ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            entry = {
                "ts": ts,
                "actor": "worker",
                "action": f"ACTION_{str(outcome.get('state', 'unknown')).upper()}",
                "intent": req.get("action"),
                "target_patient_id_hash": None,
                "result_summary": (
                    f"run={req['action_run_id']} "
                    f"status={outcome.get('status')} "
                    f"verified={outcome.get('verified')}"
                ),
            }
            entry_hash = _compute_entry_hash(entry, prev_hash)
            cur.execute(
                """
                INSERT INTO audit_logs
                    (ts, actor, action, intent, target_patient_id_hash,
                     result_summary, prev_hash, entry_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    entry["ts"],
                    entry["actor"],
                    entry["action"],
                    entry["intent"],
                    entry["target_patient_id_hash"],
                    entry["result_summary"],
                    prev_hash,
                    entry_hash,
                ),
            )
        conn.commit()

    activity.logger.info(
        "record_outcome run=%s state=%s", req["action_run_id"], outcome.get("state")
    )

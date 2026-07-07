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

Bridge interface assumption (bridge sibling still in progress): the bridge
exposes POST /{system}/{action_name} (system in {sis, svigg}) accepting the
ActionIntent JSON and returning the standard BridgeResponse envelope.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from shared import ActionOutcome, BridgeEnvelope

# run_status enum values the DB expects for a finished run.
_STATE_TO_RUN_STATUS = {
    "verified": "success",
    "failed": "failed",
    "needs_human_review": "needs_human_review",
}


# --------------------------------------------------------------------------- #
# validate_intent
# --------------------------------------------------------------------------- #
def _load_registry() -> Dict[str, Dict[str, Any]]:
    import json
    from pathlib import Path

    path = os.environ.get(
        "ACTION_REGISTRY_PATH", "/app/packages/action-registry/actions.json"
    )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    actions = data["actions"] if isinstance(data, dict) and "actions" in data else data
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(actions, list):
        for raw in actions:
            name = raw.get("action_name") or raw.get("action") or raw.get("name")
            if name:
                out[name] = raw
    elif isinstance(actions, dict):
        out = actions
    return out


@activity.defn
async def validate_intent(intent: Dict[str, Any]) -> Dict[str, Any]:
    """Re-validate against the registry and derive the bridge endpoint.

    Raises non-retryable ``IntentRejected`` for unknown actions, incomplete
    intents (non-empty missing_fields), or an unresolved target_system — the
    workflow catches these and routes to human review, never a silent success.
    """
    registry = _load_registry()
    action_name = intent.get("action_name") or intent.get("action")
    contract = registry.get(action_name) if action_name else None
    if contract is None:
        raise ApplicationError(
            f"unknown action '{action_name}'", type="IntentRejected", non_retryable=True
        )

    missing = list(intent.get("missing_fields") or [])
    if missing:
        raise ApplicationError(
            "intent incomplete; missing: " + ", ".join(missing),
            type="IntentRejected",
            non_retryable=True,
        )

    system = intent.get("target_system")
    if system not in ("sis", "svigg"):
        # 'both' / 'unknown' must be resolved to a single system before execute.
        raise ApplicationError(
            f"target_system '{system}' is not a single executable EMR",
            type="IntentRejected",
            non_retryable=True,
        )

    risk_level = contract.get("risk_level") or intent.get("risk_level") or 1
    try:
        is_write = int(risk_level) >= 2
    except (TypeError, ValueError):
        is_write = bool(intent.get("requires_approval", False))

    endpoint = f"/{system}/{action_name}"
    activity.logger.info(
        "validate_intent ok action=%s system=%s write=%s", action_name, system, is_write
    )
    return {
        "action_name": action_name,
        "system": system,
        "endpoint": endpoint,
        "method": "POST",
        "write": is_write,
        "payload": intent,
    }


# --------------------------------------------------------------------------- #
# call_bridge
# --------------------------------------------------------------------------- #
@activity.defn
async def call_bridge(req: Dict[str, Any]) -> Dict[str, Any]:
    """POST/GET the EMR bridge and return its response envelope as a dict.

    Retry semantics are enforced by the workflow (reads vs writes). Here we map
    failure modes to typed errors:
      * network error on a WRITE -> non-retryable ``WriteOutcomeUnknown``.
      * network error on a READ  -> retryable ``BridgeNetworkError``.
      * any HTTP response received (even 4xx/5xx) is returned as the envelope.
    """
    base = os.environ.get("BRIDGE_URL", "http://host.docker.internal:8600")
    url = base.rstrip("/") + "/" + req["endpoint"].lstrip("/")
    method = req.get("method", "POST").upper()
    is_write = bool(req.get("write", False))
    payload = req.get("payload") or {}

    try:
        with httpx.Client(timeout=120.0) as client:
            if method == "GET":
                resp = client.get(url, params=payload)
            else:
                resp = client.request(method, url, json=payload)
    except httpx.HTTPError as exc:
        activity.logger.warning(
            "call_bridge network_error write=%s type=%s", is_write, type(exc).__name__
        )
        if is_write:
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
    blocked / needs_human_review (or requires_human_review) route to human
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
@dataclass
class RecordOutcomeInput:
    action_run_id: str
    approval_id: str
    action: str
    outcome: Dict[str, Any]


@activity.defn
async def record_outcome(req: Dict[str, Any]) -> None:
    """Update action_runs and append to audit_logs (hash chain via DB trigger).

    Routes needs_human_review outcomes to human_review_queue. No PHI written."""
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Json

    dsn = os.environ["DATABASE_URL"]
    outcome = req["outcome"]
    state = outcome.get("state", "needs_human_review")
    run_status = _STATE_TO_RUN_STATUS.get(state, "needs_human_review")
    verified = bool(outcome.get("verified", False))
    requires_review = state == "needs_human_review"
    screenshot_key = outcome.get("screenshot_id")
    trace_key = outcome.get("trace_id")

    result_json = {
        "bridge_status": outcome.get("status"),
        "verified": verified,
        "screenshot_id": screenshot_key,
        "trace_id": trace_key,
        "warnings": outcome.get("warnings") or [],
        "failure_reason": outcome.get("failure_reason"),
    }

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Context: org + target_system + action_name for the audit row.
            cur.execute(
                """
                SELECT ar.organization_id, ar.target_system, reg.action_name
                FROM action_runs ar
                JOIN action_registry reg ON reg.id = ar.action_registry_id
                WHERE ar.id = %s
                """,
                (req["action_run_id"],),
            )
            ctx = cur.fetchone() or {}
            org_id = ctx.get("organization_id")
            target_system = ctx.get("target_system") or "unknown"
            action_name = ctx.get("action_name") or req.get("action")

            # Resolve evidence keys to FK ids if the rows exist; else leave null
            # (the key is preserved in result JSONB regardless).
            screenshot_id = _resolve_evidence(cur, "screenshots", "screenshot_key", screenshot_key)
            trace_id = _resolve_evidence(cur, "traces", "trace_key", trace_key)

            cur.execute(
                """
                UPDATE action_runs
                SET status = %s,
                    verified = %s,
                    requires_human_review = %s,
                    failure_reason = %s,
                    result = %s,
                    screenshot_id = %s,
                    trace_id = %s,
                    finished_at = now()
                WHERE id = %s
                """,
                (
                    run_status,
                    verified,
                    requires_review,
                    outcome.get("failure_reason"),
                    Json(result_json),
                    screenshot_id,
                    trace_id,
                    req["action_run_id"],
                ),
            )

            if requires_review and org_id is not None:
                cur.execute(
                    """
                    INSERT INTO human_review_queue
                        (organization_id, action_run_id, reason_code,
                         reason_detail, payload)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        org_id,
                        req["action_run_id"],
                        _reason_code(outcome),
                        outcome.get("failure_reason"),
                        Json(result_json),
                    ),
                )

            # Append to the hash-chained audit log (trigger computes the hashes).
            cur.execute(
                """
                INSERT INTO audit_logs
                    (organization_id, action_run_id, actor_label, action,
                     target_system, result, failure_reason)
                VALUES (%s, %s, 'worker', %s, %s, %s, %s)
                """,
                (
                    org_id,
                    req["action_run_id"],
                    action_name,
                    target_system,
                    outcome.get("status") or run_status,
                    outcome.get("failure_reason"),
                ),
            )
        conn.commit()

    activity.logger.info(
        "record_outcome run=%s state=%s", req["action_run_id"], state
    )


def _resolve_evidence(cur, table: str, key_col: str, key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    cur.execute(f"SELECT id FROM {table} WHERE {key_col} = %s", (key,))
    row = cur.fetchone()
    return row["id"] if row else None


def _reason_code(outcome: Dict[str, Any]) -> str:
    status = outcome.get("status")
    if status == "blocked":
        return "blocked"
    if not outcome.get("verified", False):
        return "unverified_write"
    return "needs_human_review"

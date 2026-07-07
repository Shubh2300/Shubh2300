"""ExecuteActionWorkflow — the durable execution of an approved ActionIntent.

Sequence: validate_intent -> call_bridge -> verify_result -> record_outcome.

Retry policy:
  * Reads: up to 3 attempts, exponential backoff, only for transient network
    errors (BridgeNetworkError). All other errors are non-retryable.
  * Writes: a single attempt (max_attempts=1). A write whose outcome is unknown
    is NEVER retried; it is routed to human review.

Any ambiguity, validation rejection, or unknown-outcome write completes the
workflow as ``needs_human_review`` — it NEVER silently succeeds.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from shared import ExecuteActionInput

_READ_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
    non_retryable_error_types=[
        "IntentRejected",
        "BridgeBadResponse",
        "WriteOutcomeUnknown",
    ],
)

# Writes: never retried. A single attempt; unknown outcomes go to human review.
_WRITE_RETRY = RetryPolicy(maximum_attempts=1)

_VALIDATE_RETRY = RetryPolicy(
    maximum_attempts=1,
    non_retryable_error_types=["IntentRejected"],
)


@workflow.defn(name="ExecuteActionWorkflow")
class ExecuteActionWorkflow:
    @workflow.run
    async def run(self, arg: dict) -> dict:
        data = ExecuteActionInput(**arg)
        action = data.intent.get("action", "unknown")

        # 1) validate_intent — reject unknown/incomplete intents into review.
        try:
            validated = await workflow.execute_activity(
                "validate_intent",
                data.intent,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_VALIDATE_RETRY,
            )
        except ActivityError as exc:
            return await self._to_review(
                data, action, self._reason(exc, "intent validation failed")
            )

        is_write = bool(validated.get("write", False))

        # 2) call_bridge — reads may retry on network errors; writes never do.
        call_input = {
            "endpoint": validated["endpoint"],
            "method": validated["method"],
            "write": is_write,
            "payload": validated["inputs"],
        }
        try:
            envelope = await workflow.execute_activity(
                "call_bridge",
                call_input,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=_WRITE_RETRY if is_write else _READ_RETRY,
            )
        except ActivityError as exc:
            # e.g. WriteOutcomeUnknown, BridgeBadResponse, exhausted read retries.
            return await self._to_review(
                data, action, self._reason(exc, "bridge call failed")
            )

        # 3) verify_result — envelope inspection decides the terminal state.
        outcome = await workflow.execute_activity(
            "verify_result",
            envelope,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )

        # 4) record_outcome — persist + audit.
        await self._record(data, action, outcome)
        return outcome

    async def _to_review(self, data: ExecuteActionInput, action: str, reason: str) -> dict:
        outcome = {
            "state": "needs_human_review",
            "status": None,
            "verified": False,
            "screenshot_id": None,
            "trace_id": None,
            "failure_reason": reason,
            "warnings": [],
        }
        await self._record(data, action, outcome)
        return outcome

    async def _record(self, data: ExecuteActionInput, action: str, outcome: dict) -> None:
        await workflow.execute_activity(
            "record_outcome",
            {
                "action_run_id": data.action_run_id,
                "approval_id": data.approval_id,
                "action": action,
                "outcome": outcome,
            },
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    @staticmethod
    def _reason(exc: ActivityError, default: str) -> str:
        cause = exc.cause
        if isinstance(cause, ApplicationError):
            return f"{cause.type or default}: {cause.message}"
        return default

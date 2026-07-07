"""Thin client wrapper to start the ExecuteActionWorkflow in Temporal.

The API does NOT import the worker's workflow/activity code. It starts the
workflow by its registered name and passes the approved ActionIntent plus the
approval/run identifiers as the argument. The worker owns execution.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from temporalio.client import Client

logger = logging.getLogger("backoffice.temporal")

WORKFLOW_NAME = "ExecuteActionWorkflow"


async def get_client(address: str, namespace: str = "default") -> Client:
    return await Client.connect(address, namespace=namespace)


async def start_execute_action_workflow(
    *,
    address: str,
    namespace: str,
    task_queue: str,
    approval_id: str,
    action_run_id: str,
    intent: Dict[str, Any],
    client: Optional[Client] = None,
) -> str:
    """Start ExecuteActionWorkflow for an approved intent. Returns workflow id.

    The workflow id is derived from the action_run_id so a given run is
    idempotent — Temporal rejects a duplicate start for the same id.
    """
    client = client or await get_client(address, namespace)
    workflow_id = f"execute-action-{action_run_id}"
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        args=[
            {
                "approval_id": approval_id,
                "action_run_id": action_run_id,
                "intent": intent,
            }
        ],
        id=workflow_id,
        task_queue=task_queue,
    )
    logger.info(
        "workflow.started workflow_id=%s run_id=%s", workflow_id, handle.result_run_id
    )
    return workflow_id

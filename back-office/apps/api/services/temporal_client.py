"""Thin client wrapper to start ExecuteActionWorkflow in Temporal.

The API does NOT import the worker's workflow/activity code. It starts the
workflow by its registered name and passes the approved intent plus the
identifiers as the argument. The worker owns execution.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

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
    workflow_id: str,
    approval_id: str,
    action_run_id: str,
    intent: Dict[str, Any],
    approval_token: Optional[str] = None,
    client: Optional[Client] = None,
) -> Tuple[str, Optional[str]]:
    """Start ExecuteActionWorkflow. Returns (temporal_workflow_id, run_id).

    ``workflow_id`` is the workflow_runs UUID — deterministic and unique, so a
    duplicate start for the same run is rejected by Temporal (idempotent).

    ``approval_token`` is the single-use raw token (its hash is stored on the
    approval row); the worker attaches it to write requests to the bridge.
    """
    client = client or await get_client(address, namespace)
    handle = await client.start_workflow(
        WORKFLOW_NAME,
        args=[
            {
                "approval_id": approval_id,
                "action_run_id": action_run_id,
                "intent": intent,
                "approval_token": approval_token,
            }
        ],
        id=workflow_id,
        task_queue=task_queue,
    )
    logger.info(
        "workflow.started workflow_id=%s run_id=%s", workflow_id, handle.result_run_id
    )
    return workflow_id, handle.result_run_id

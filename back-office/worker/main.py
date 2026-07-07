"""Temporal worker bootstrap.

Registers ExecuteActionWorkflow and its four activities on the task queue and
runs until interrupted. Configuration comes from the environment
(TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, TEMPORAL_TASK_QUEUE, BRIDGE_URL,
DATABASE_URL, ACTION_REGISTRY_PATH).
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from activities import (
    call_bridge,
    mark_run_status,
    record_outcome,
    validate_intent,
    verify_result,
)
from workflows import ExecuteActionWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backoffice.worker")


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", "emr-actions")

    client = await Client.connect(address, namespace=namespace)
    logger.info("worker connecting queue=%s namespace=%s", task_queue, namespace)

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ExecuteActionWorkflow],
        activities=[
            validate_intent,
            call_bridge,
            mark_run_status,
            verify_result,
            record_outcome,
        ],
    )
    logger.info("worker started")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())

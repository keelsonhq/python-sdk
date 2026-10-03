"""Keelson Tasks SDK (``tasks``).

Enqueue a run of a command declared under ``tasks:`` in ``keelson.yaml`` and
read its state. See the module docstring in :mod:`keelson_tasks.client`.

    from keelson import tasks

    task_id = tasks.enqueue("generate-pdf", {"order_id": 1},
                            idempotency_key="order-1-pdf")
    status = tasks.get(task_id)   # status.status == "queued" / ... / "failed"
"""

from keelson_tasks.client import (
    MAX_BODY_BYTES,
    TasksError,
    TaskStatus,
    enqueue,
    get,
)

__all__ = [
    "MAX_BODY_BYTES",
    "TasksError",
    "TaskStatus",
    "enqueue",
    "get",
]

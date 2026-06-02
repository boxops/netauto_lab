"""
RabbitMQ task bus for the AI agent pipeline.

When RABBITMQ_URL is set, task creation publishes a message to a durable
exchange so the assigned agent can consume it immediately instead of polling.

When RABBITMQ_URL is not set (default / lab mode), all operations are no-ops
and agents fall back to their existing SQLite polling loops automatically.

Exchange topology (all direct, durable):
    task.rca           → ops_agent queue       (not consumed here; alert poller drives RCA)
    task.fix_proposal  → eng_fix_queue
    task.validation    → chaos_val_queue
    task.approval_gate → eng_gate_queue

Message payload: {"task_id": "<id>", "priority": "<priority>"}
Message priority: 10 for critical, 7 for high, 4 for normal, 1 for low
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "")

# Maps task type → (exchange, routing_key, queue_name)
_ROUTES: dict[str, tuple[str, str, str]] = {
    "rca":           ("task.rca",           "rca",           "ops_rca_queue"),
    "fix_proposal":  ("task.fix_proposal",  "fix_proposal",  "eng_fix_queue"),
    "validation":    ("task.validation",    "validation",    "chaos_val_queue"),
    "approval_gate": ("task.approval_gate", "approval_gate", "eng_gate_queue"),
}

_PRIORITY_MAP = {"critical": 10, "high": 7, "normal": 4, "low": 1}


# ── helpers ────────────────────────────────────────────────────────────────────

def _is_enabled() -> bool:
    return bool(RABBITMQ_URL)


def _run_async(coro: Awaitable) -> None:
    """Run a coroutine from a synchronous context (task_store.create_task)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an async context (e.g. FastAPI) — schedule as task
            asyncio.ensure_future(coro)
        else:
            loop.run_until_complete(coro)
    except RuntimeError:
        # No event loop — run in a fire-and-forget thread
        def _run():
            asyncio.run(coro)
        threading.Thread(target=_run, daemon=True).start()


# ── publish ────────────────────────────────────────────────────────────────────

async def _publish_async(task_type: str, task_id: str, priority: str) -> None:
    try:
        import aio_pika
    except ImportError:
        logger.debug("task_bus: aio_pika not installed; skipping publish")
        return

    route = _ROUTES.get(task_type)
    if not route:
        return

    exchange_name, routing_key, _queue = route
    msg_priority = _PRIORITY_MAP.get(priority, 4)

    try:
        conn = await aio_pika.connect_robust(RABBITMQ_URL, timeout=5)
        async with conn:
            channel = await conn.channel()
            exchange = await channel.declare_exchange(
                exchange_name,
                aio_pika.ExchangeType.DIRECT,
                durable=True,
            )
            body = json.dumps({"task_id": task_id, "priority": priority}).encode()
            await exchange.publish(
                aio_pika.Message(
                    body=body,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    priority=msg_priority,
                ),
                routing_key=routing_key,
            )
            logger.debug(
                "task_bus: published task_id=%s type=%s priority=%s",
                task_id, task_type, priority,
            )
    except Exception as exc:
        logger.warning("task_bus: publish failed for %s: %s", task_id, exc)


def publish_task(task_type: str, task_id: str, priority: str) -> None:
    """
    Fire-and-forget publish from synchronous code (task_store.create_task).
    No-op when RABBITMQ_URL is not configured.
    """
    if not _is_enabled():
        return
    _run_async(_publish_async(task_type, task_id, priority))


# ── consume ────────────────────────────────────────────────────────────────────

async def consume_tasks(
    task_type: str,
    handler: Callable[[str, str], None],
    prefetch: int = 1,
) -> None:
    """
    Subscribe to the queue for task_type.  For each message, call:
        handler(task_id, priority)

    This coroutine runs forever (until the process exits) and handles
    reconnections automatically via aio_pika's robust connection.

    Args:
        task_type:  One of rca / fix_proposal / validation / approval_gate.
        handler:    Synchronous callback; called in the current thread.
        prefetch:   Max messages in-flight at once (default 1 = one at a time).
    """
    try:
        import aio_pika
    except ImportError:
        logger.warning("task_bus: aio_pika not installed; consume_tasks is a no-op")
        return

    route = _ROUTES.get(task_type)
    if not route:
        raise ValueError(f"Unknown task_type {task_type!r}")

    exchange_name, routing_key, queue_name = route

    logger.info("task_bus: connecting to RabbitMQ for task_type=%s queue=%s", task_type, queue_name)

    conn = await aio_pika.connect_robust(RABBITMQ_URL, timeout=15)
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=prefetch)

    exchange = await channel.declare_exchange(
        exchange_name,
        aio_pika.ExchangeType.DIRECT,
        durable=True,
    )
    queue = await channel.declare_queue(
        queue_name,
        durable=True,
        arguments={"x-max-priority": 10},
    )
    await queue.bind(exchange, routing_key=routing_key)

    async with queue.iterator() as it:
        async for message in it:
            async with message.process():
                try:
                    payload = json.loads(message.body)
                    tid      = payload.get("task_id", "")
                    prio     = payload.get("priority", "normal")
                    if tid:
                        handler(tid, prio)
                except Exception as exc:
                    logger.exception("task_bus: handler failed for message: %s", exc)


def start_consumer(
    task_type: str,
    handler: Callable[[str, str], None],
) -> None:
    """
    Start a background thread that runs consume_tasks() forever.
    No-op when RABBITMQ_URL is not configured (agents use polling instead).
    """
    if not _is_enabled():
        return

    def _thread():
        asyncio.run(consume_tasks(task_type, handler))

    t = threading.Thread(target=_thread, name=f"consumer-{task_type}", daemon=True)
    t.start()
    logger.info("task_bus: consumer thread started for task_type=%s", task_type)

"""
sync_manager.py — Automatic reconcile-on-reconnect
======================================================
Subscribed to ConnectivityMonitor.on_online(). The moment the probe (or
the demo's forced toggle) reports the network is back, this drains the
DurableQueue in the order operations were created, replays each one
against the *real* async function for its op_type, and marks it done.

Idempotency: each row has a unique op_id and is only marked 'done'
after the real call returns successfully, so a crash mid-drain just
means the same op is retried on the next reconnect — never silently
dropped, and (assuming the real endpoint is itself idempotent per
op_id, which is the standard contract for an outbox pattern) never
double-applied in effect.
"""

from __future__ import annotations
import asyncio
import time
from typing import Awaitable, Callable, Dict

from connectivity_guard import ConnectivityMonitor
from durable_queue import DurableQueue

AsyncFn = Callable[..., Awaitable[None]]


class SyncManager:
    def __init__(self, monitor: ConnectivityMonitor, queue: DurableQueue,
                 replay_handlers: Dict[str, AsyncFn], max_retries: int = 3):
        self.monitor = monitor
        self.queue = queue
        self.replay_handlers = replay_handlers
        self.max_retries = max_retries
        monitor.on_online(self._drain)

    async def _drain(self) -> None:
        pending = self.queue.pending()
        if not pending:
            print("[SYNC] reconnected — outbox already empty")
            return

        print(f"[SYNC] reconnected — replaying {len(pending)} queued op(s)")
        t0 = time.monotonic()
        synced, failed = 0, 0

        for op in pending:
            handler = self.replay_handlers.get(op.op_type)
            if handler is None:
                print(f"  [SYNC] no handler for op_type={op.op_type}, skipping {op.op_id}")
                self.queue.mark_failed(op.op_id, permanent=True)
                failed += 1
                continue
            try:
                await handler(op.agent_id, op.resource, op.payload)
                self.queue.mark_done(op.op_id)
                synced += 1
                print(f"  [SYNC] replayed {op.op_type} for {op.agent_id} "
                      f"(queued {round(time.time() - op.created_at, 1)}s ago)")
            except Exception as e:
                permanent = op.attempts + 1 >= self.max_retries
                self.queue.mark_failed(op.op_id, permanent=permanent)
                failed += 1
                print(f"  [SYNC] FAILED {op.op_type} for {op.agent_id}: {e} "
                      f"({'giving up' if permanent else 'will retry next reconnect'})")

        dt = time.monotonic() - t0
        print(f"[SYNC] reconciliation complete: {synced} synced, {failed} failed, "
              f"{dt:.2f}s — outbox counts={self.queue.counts()}")

    async def force_drain(self) -> None:
        """Manual trigger, e.g. for a demo or a health-check endpoint."""
        await self._drain()

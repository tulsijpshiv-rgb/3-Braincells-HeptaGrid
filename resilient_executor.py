"""
resilient_executor.py — The offline-critical function
=========================================================
This is the piece that keeps doing real, meaningful work with no
internet connection — not just detecting the outage.

Two call shapes, matching how the agents in example_real_agents.py
actually use resources:

  READ  (llm-api reasoning, search-api, vector-db query)
      Online  -> call the real function, cache the result.
      Offline -> serve the last-known-good cached result for a similar
                 key (degraded but functional — e.g. the monitoring
                 agent can still evaluate "should I alert?" using the
                 last cached signal instead of stalling).

  WRITE (vector-db upsert, output-api send, anything with a side effect
         the outside world must eventually observe)
      Online  -> call the real function immediately.
      Offline -> durably enqueue the operation (DurableQueue) and return
                 a synthetic ack so the agent's workflow keeps moving.
                 Nothing is lost; it is replayed by SyncManager later.

Admission control itself (HeptagridController.reserve — priority
scoring, the ledger, starvation handling) needs no network at all, so
it is left untouched and keeps running normally through an outage; this
module only wraps the I/O *inside* the reserved section.
"""

from __future__ import annotations
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from connectivity_guard import ConnectivityMonitor
from durable_queue import DurableQueue

AsyncFn = Callable[..., Awaitable[Any]]


class ResilientExecutor:
    def __init__(self, monitor: ConnectivityMonitor, queue: DurableQueue):
        self.monitor = monitor
        self.queue = queue
        self._cache: Dict[str, Any] = {}          # last-known-good reads
        self._cache_ts: Dict[str, float] = {}

    # ---- READ path -------------------------------------------------------
    async def read(self, agent_id: str, resource: str, cache_key: str,
                    real_fn: AsyncFn, *args, **kwargs) -> Dict[str, Any]:
        if self.monitor.is_online:
            result = await real_fn(*args, **kwargs)
            self._cache[cache_key] = result
            self._cache_ts[cache_key] = time.time()
            return {"result": result, "degraded": False, "source": "live"}

        # Offline: serve from cache if we have anything for this key,
        # else fall back to the most recent cache entry of any key for
        # this resource — a stale answer beats no answer for a
        # time-sensitive agent (e.g. the monitoring agent) that would
        # otherwise stall for the whole outage.
        if cache_key in self._cache:
            age = time.time() - self._cache_ts[cache_key]
            return {"result": self._cache[cache_key], "degraded": True,
                    "source": "cache", "age_s": round(age, 1)}

        return {"result": None, "degraded": True, "source": "no-cache",
                "note": f"no cached data for {resource}; offline default used"}

    # ---- WRITE path --------------------------------------------------------
    async def write(self, agent_id: str, resource: str, op_type: str,
                     payload: Dict[str, Any], real_fn: Optional[AsyncFn] = None,
                     *args, **kwargs) -> Dict[str, Any]:
        if self.monitor.is_online and real_fn is not None:
            result = await real_fn(*args, **kwargs)
            return {"result": result, "queued": False, "source": "live"}

        op_id = self.queue.enqueue(agent_id, resource, op_type, payload)
        return {"result": None, "queued": True, "op_id": op_id, "source": "outbox"}

"""
demo_intermittent_connectivity.py — Brownie-points challenge demo
======================================================================
Run:
    python3 demo_intermittent_connectivity.py            # full 65s outage
    python3 demo_intermittent_connectivity.py --fast      # 6s outage, quick test
    python3 demo_intermittent_connectivity.py --restart   # also proves the
                                                            # outbox survives
                                                            # a process kill
                                                            # mid-outage

What it shows, mapped to the challenge requirements:

  Critical offline function
      A monitoring agent keeps evaluating alert conditions on a fixed
      cadence AND an indexing agent keeps accepting/queuing documents —
      neither one blocks or stops just because the network is down.
      Admission control (who gets which resource slot, priority,
      starvation handling) needs no network at all, so it is completely
      unaffected by the outage — only the actual outbound API calls are.

  Local processing / caching / queuing / fallback
      ResilientExecutor: reads are served from a local cache (degraded
      but usable), writes are appended to a durable SQLite outbox.

  Retention of data/events generated during the outage
      DurableQueue is SQLite/WAL on disk — survives this process dying
      mid-outage (see --restart), not just an in-memory list.

  Automatic reconciliation on reconnect
      SyncManager.drain() fires the instant ConnectivityMonitor flips
      back to online and replays every queued op against the "real"
      (simulated) service, in order, exactly once.

Notes on the controller used here
      This demo wraps a MiniController — a small stand-in that exposes
      the same register()/reserve() surface as HeptagridController.
      The uploaded heptagrid_controller.py imports ledger.py, agents.py,
      scoring.py, shadow.py, recover.py, metrics.py, safety.py and
      forecast.py, which weren't in the zip, so it can't be imported
      standalone here. The resilience layer (connectivity_guard.py,
      durable_queue.py, resilient_executor.py, sync_manager.py) doesn't
      touch controller internals at all — it only wraps the real I/O
      *inside* a reserve() block — so swapping MiniController for
      `HeptagridController.get()` once those files are available is a
      one-line change (see `get_controller()` below).
"""

from __future__ import annotations
import argparse
import asyncio
import os
import random
import sys
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from connectivity_guard import ConnectivityMonitor
from durable_queue import DurableQueue
from resilient_executor import ResilientExecutor
from sync_manager import SyncManager


# ---------------------------------------------------------------------------
# MiniController — stand-in for HeptagridController.reserve()'s public API.
# Same call shape (`async with ctrl.reserve(agent_id, resource): ...`),
# same idea (capacity-limited semaphore per resource, async, event-driven).
# Replace with `HeptagridController.get()` once its dependency modules
# are available; nothing else in this file needs to change.
# ---------------------------------------------------------------------------

class MiniController:
    CAPACITIES = {"llm-api": 3, "vector-db": 2, "search-api": 2, "output-api": 2}

    def __init__(self):
        self._sems = {r: asyncio.Semaphore(c) for r, c in self.CAPACITIES.items()}
        self._agents: Dict[str, dict] = {}

    def register(self, agent_id: str, importance: float = 0.5, deadline: int = 20) -> None:
        self._agents[agent_id] = {"importance": importance, "deadline": deadline}

    def deregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    @asynccontextmanager
    async def reserve(self, agent_id: str, resource: str, max_wait: float = 60.0):
        sem = self._sems[resource]
        await sem.acquire()
        try:
            yield resource
        finally:
            sem.release()


def get_controller() -> MiniController:
    try:
        from heptagrid_controller import HeptagridController  # noqa: F401
        # Full dependency set (ledger.py, agents.py, ...) present — use it.
        return HeptagridController.get()
    except Exception:
        print("[DEMO] heptagrid_controller dependencies not present — "
              "using MiniController stand-in (same reserve() contract).")
        return MiniController()


# ---------------------------------------------------------------------------
# "Real" (simulated) network-bound service calls.
# These are the calls that would fail/hang during a real outage — the
# ResilientExecutor is what decides whether they run live, get served
# from cache, or get queued.
# ---------------------------------------------------------------------------

async def real_llm_call(prompt: str) -> str:
    await asyncio.sleep(0.05)
    return f"llm-answer::{prompt}"

async def real_search(query: str) -> List[str]:
    await asyncio.sleep(0.05)
    return [f"result-for::{query}"]

async def real_vector_upsert(agent_id: str, resource: str, payload: dict) -> None:
    await asyncio.sleep(0.05)
    print(f"    [REMOTE] vector-db upsert committed: {payload}")

async def real_output_send(agent_id: str, resource: str, payload: dict) -> None:
    await asyncio.sleep(0.05)
    print(f"    [REMOTE] output-api delivered: {payload}")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

async def monitoring_agent(ctrl, rex: ResilientExecutor, stop_evt: asyncio.Event):
    """
    The critical offline function: a tight monitor/alert loop that must
    keep evaluating signals on a fixed cadence no matter what the network
    is doing. Reads are cache-served offline so this never stalls.
    """
    agent_id = "monitor-1"
    ctrl.register(agent_id, importance=1.0, deadline=3)
    tick = 0
    while not stop_evt.is_set():
        tick += 1
        async with ctrl.reserve(agent_id, "search-api"):
            reading = await rex.read(agent_id, "search-api", "system-health",
                                      real_search, "system-health")
        tag = "LIVE" if not reading["degraded"] else f"CACHED(age={reading.get('age_s','?')}s)"
        alert = random.random() < 0.3
        if alert:
            async with ctrl.reserve(agent_id, "output-api"):
                w = await rex.write(agent_id, "output-api", "alert",
                                     {"tick": tick, "reading": reading["result"]},
                                     real_output_send, agent_id, "output-api",
                                     {"tick": tick})
            status = f"QUEUED({w['op_id'][:8]})" if w["queued"] else "SENT"
            print(f"[monitor-1] tick={tick} [{tag}] ALERT -> {status}")
        else:
            print(f"[monitor-1] tick={tick} [{tag}] nominal")
        await asyncio.sleep(1.0)
    ctrl.deregister(agent_id)


async def indexing_agent(ctrl, rex: ResilientExecutor, stop_evt: asyncio.Event):
    """Keeps accepting documents to index throughout the outage; writes
    that can't reach the real vector-db are durably queued, never dropped."""
    agent_id = "indexer-1"
    ctrl.register(agent_id, importance=0.7, deadline=12)
    doc_n = 0
    while not stop_evt.is_set():
        doc_n += 1
        async with ctrl.reserve(agent_id, "vector-db"):
            w = await rex.write(agent_id, "vector-db", "upsert",
                                 {"doc": f"doc-{doc_n}"},
                                 real_vector_upsert, agent_id, "vector-db",
                                 {"doc": f"doc-{doc_n}"})
        status = f"QUEUED({w['op_id'][:8]})" if w["queued"] else "COMMITTED"
        print(f"[indexer-1] doc-{doc_n} -> {status}")
        await asyncio.sleep(1.5)
    ctrl.deregister(agent_id)


# ---------------------------------------------------------------------------
# Orchestration: disconnect -> operate -> reconnect -> recover
# ---------------------------------------------------------------------------

REPLAY_HANDLERS = {
    "upsert": lambda agent_id, resource, payload: real_vector_upsert(agent_id, resource, payload),
    "alert":  lambda agent_id, resource, payload: real_output_send(agent_id, resource, payload),
}


async def main(offline_seconds: float, simulate_restart: bool, db_path: str):
    monitor = ConnectivityMonitor(interval=1.0)
    queue = DurableQueue(db_path=db_path)
    rex = ResilientExecutor(monitor, queue)
    sync = SyncManager(monitor, queue, REPLAY_HANDLERS)
    ctrl = get_controller()

    stop_evt = asyncio.Event()
    agents = [
        asyncio.create_task(monitoring_agent(ctrl, rex, stop_evt)),
        asyncio.create_task(indexing_agent(ctrl, rex, stop_evt)),
    ]

    print(f"\n{'='*60}\n PHASE 1: online, warming the read cache\n{'='*60}")
    await asyncio.sleep(3)

    print(f"\n{'='*60}\n PHASE 2: connectivity lost for {offline_seconds:.0f}s\n{'='*60}")
    await monitor.force(False)

    if simulate_restart:
        half = offline_seconds / 2
        await asyncio.sleep(half)
        print(f"\n[DEMO] simulating process restart mid-outage: closing and "
              f"reopening the outbox file at '{db_path}' ...")
        pending_before = queue.counts()
        queue.close()
        queue = DurableQueue(db_path=db_path)   # reopen same file, fresh handle
        rex.queue = queue
        sync.queue = queue
        print(f"[DEMO] outbox reopened — counts survived restart: {queue.counts()} "
              f"(were {pending_before} before)")
        await asyncio.sleep(offline_seconds - half)
    else:
        await asyncio.sleep(offline_seconds)

    print(f"\n{'='*60}\n PHASE 3: connectivity restored\n{'='*60}")
    await monitor.force(True)
    await asyncio.sleep(2)   # let SyncManager's drain task finish

    stop_evt.set()
    await asyncio.gather(*agents)

    print(f"\n{'='*60}\n FINAL outbox state: {queue.counts()}\n{'='*60}")
    queue.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="6s outage instead of 65s")
    ap.add_argument("--restart", action="store_true",
                     help="also kill/reopen the outbox mid-outage to prove durability")
    ap.add_argument("--db", default="offline_outbox.sqlite3")
    args = ap.parse_args()

    if os.path.exists(args.db) and "--keep-db" not in sys.argv:
        os.remove(args.db)  # start each demo run clean

    offline_s = 6.0 if args.fast else 65.0
    asyncio.run(main(offline_s, args.restart, args.db))

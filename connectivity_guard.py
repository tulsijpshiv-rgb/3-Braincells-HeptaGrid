"""
connectivity_guard.py — Online/offline detection for HeptagridController
==========================================================================
Runs a lightweight background probe (TCP connect, no HTTP payload) against
a small set of well-known hosts. Flips `is_online` on state *change* only,
and fires registered async callbacks so other components (the durable
queue, the sync manager) can react immediately instead of polling a flag.

Two ways to drive it:
  1. Real network: `ConnectivityMonitor().start()` — probes every
     `interval` seconds using a raw socket connect (fast, no deps).
  2. Forced/demo: `await monitor.force(False)` / `await monitor.force(True)`
     — used by the demo script (and by tests) to deterministically
     simulate "pull the cable" / "plug it back in" without needing to
     actually toggle the host's network interface.

A forced state pins the monitor (probing pauses) until `force(None)` is
called to release it back to real probing.
"""

from __future__ import annotations
import asyncio
import socket
import time
from typing import Awaitable, Callable, List, Optional

Callback = Callable[[], Awaitable[None]]

# (host, port) pairs — small, diverse, unlikely to all be blocked/down together
_PROBE_TARGETS = [
    ("1.1.1.1", 53),   # Cloudflare DNS
    ("8.8.8.8", 53),   # Google DNS
    ("9.9.9.9", 53),   # Quad9 DNS
]


class ConnectivityMonitor:
    def __init__(self, interval: float = 3.0, probe_timeout: float = 1.5):
        self.interval = interval
        self.probe_timeout = probe_timeout
        self.is_online: bool = True
        self._forced: Optional[bool] = None
        self._on_offline: List[Callback] = []
        self._on_online: List[Callback] = []
        self._task: Optional[asyncio.Task] = None
        self._last_change_ts: float = time.monotonic()

    # -- subscription -----------------------------------------------------
    def on_offline(self, cb: Callback) -> None:
        self._on_offline.append(cb)

    def on_online(self, cb: Callback) -> None:
        self._on_online.append(cb)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    # -- forced state (demo / tests) ----------------------------------------
    async def force(self, state: Optional[bool]) -> None:
        """state=False -> simulate outage, state=True -> simulate recovery,
        state=None -> release back to real probing."""
        self._forced = state
        if state is not None:
            await self._set_state(state)

    # -- probing --------------------------------------------------------------
    def _probe_once(self) -> bool:
        for host, port in _PROBE_TARGETS:
            try:
                with socket.create_connection((host, port), timeout=self.probe_timeout):
                    return True
            except OSError:
                continue
        return False

    async def _loop(self) -> None:
        while True:
            if self._forced is None:
                online = await asyncio.to_thread(self._probe_once)
                await self._set_state(online)
            await asyncio.sleep(self.interval)

    async def _set_state(self, online: bool) -> None:
        if online == self.is_online:
            return
        self.is_online = online
        self._last_change_ts = time.monotonic()
        callbacks = self._on_online if online else self._on_offline
        label = "ONLINE" if online else "OFFLINE"
        print(f"[CONNECTIVITY] state -> {label}")
        for cb in callbacks:
            asyncio.create_task(cb())

    def seconds_in_state(self) -> float:
        return time.monotonic() - self._last_change_ts

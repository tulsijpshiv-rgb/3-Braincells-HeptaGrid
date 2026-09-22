"""
durable_queue.py — Crash-safe local event/operation queue
=============================================================
Every side-effecting call an agent makes while offline (a vector-db
upsert, an output-api write, a tool invocation the outside world needs
to eventually see) is appended here instead of being dropped or blocked
on forever. Backed by SQLite in WAL mode:

  * Survives an outage that outlasts the process (kill -9 mid-outage,
    restart, queue is still there) — not just an in-memory list.
  * Each row gets a UUID op_id, so replay on reconnect is idempotent:
    the sync manager can safely retry without double-applying an op
    if a partial replay happened before a crash.

This module has zero knowledge of *what* the operations mean — it is a
generic durable outbox. `resilient_executor.py` decides what goes in;
`sync_manager.py` decides how to replay it.
"""

from __future__ import annotations
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    op_id       TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    agent_id    TEXT NOT NULL,
    resource    TEXT NOT NULL,
    op_type     TEXT NOT NULL,
    payload     TEXT NOT NULL,   -- JSON
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|done|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    synced_at   REAL
);
"""


@dataclass
class QueuedOp:
    op_id: str
    created_at: float
    agent_id: str
    resource: str
    op_type: str
    payload: Dict[str, Any]
    status: str
    attempts: int


class DurableQueue:
    def __init__(self, db_path: str = "offline_outbox.sqlite3"):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    # -- writer side (called while offline) --------------------------------
    def enqueue(self, agent_id: str, resource: str, op_type: str,
                payload: Dict[str, Any]) -> str:
        op_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO outbox (op_id, created_at, agent_id, resource, "
            "op_type, payload, status, attempts) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)",
            (op_id, time.time(), agent_id, resource, op_type, json.dumps(payload)),
        )
        self._conn.commit()
        return op_id

    # -- reader / replay side (called on reconnect) --------------------------
    def pending(self) -> List[QueuedOp]:
        cur = self._conn.execute(
            "SELECT op_id, created_at, agent_id, resource, op_type, payload, "
            "status, attempts FROM outbox WHERE status='pending' ORDER BY created_at ASC"
        )
        return [
            QueuedOp(op_id=r[0], created_at=r[1], agent_id=r[2], resource=r[3],
                     op_type=r[4], payload=json.loads(r[5]), status=r[6], attempts=r[7])
            for r in cur.fetchall()
        ]

    def mark_done(self, op_id: str) -> None:
        self._conn.execute(
            "UPDATE outbox SET status='done', synced_at=? WHERE op_id=?",
            (time.time(), op_id),
        )
        self._conn.commit()

    def mark_failed(self, op_id: str, permanent: bool = False) -> None:
        self._conn.execute(
            "UPDATE outbox SET status=?, attempts=attempts+1 WHERE op_id=?",
            ("failed" if permanent else "pending", op_id),
        )
        self._conn.commit()

    def counts(self) -> Dict[str, int]:
        cur = self._conn.execute(
            "SELECT status, COUNT(*) FROM outbox GROUP BY status"
        )
        out = {"pending": 0, "done": 0, "failed": 0}
        out.update(dict(cur.fetchall()))
        return out

    def close(self) -> None:
        self._conn.close()

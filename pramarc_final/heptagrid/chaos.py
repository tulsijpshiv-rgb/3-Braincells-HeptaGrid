"""Controlled fault injection for PRAMARC demos."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class ChaosEvent:
    resource: str
    kind: str
    start_tick: int
    end_tick: int
    previous_limit: int
    active: bool = True


class ChaosController:
    def __init__(self) -> None:
        self.events: List[ChaosEvent] = []
        self.recovery_events: int = 0

    def fail_resource(self, ledger, resource: str, tick: int, duration: int = 3) -> ChaosEvent:
        prev = ledger.capacity_limit(resource)
        ledger.set_capacity_limit(resource, 0)
        ledger.set_health(resource, "down")
        event = ChaosEvent(resource, "failure", tick, tick + max(1, duration), prev)
        self.events.append(event)
        return event

    def squeeze_capacity(self, ledger, resource: str, tick: int, limit: int = 1, duration: int = 3) -> ChaosEvent:
        prev = ledger.capacity_limit(resource)
        ledger.set_capacity_limit(resource, max(0, min(limit, ledger.total_capacity(resource))))
        ledger.set_health(resource, "degraded")
        event = ChaosEvent(resource, "capacity", tick, tick + max(1, duration), prev)
        self.events.append(event)
        return event

    def tick(self, ledger, tick: int) -> List[ChaosEvent]:
        restored: List[ChaosEvent] = []
        for event in self.events:
            if event.active and tick >= event.end_tick:
                ledger.set_capacity_limit(event.resource, event.previous_limit)
                ledger.set_health(event.resource, "up")
                event.active = False
                self.recovery_events += 1
                restored.append(event)
        return restored

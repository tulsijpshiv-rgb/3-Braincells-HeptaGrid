"""Central reservation ledger for PRAMARC.

This version fixes the original single-owner bug by tracking every reservation
individually and adds resource health/capacity controls used by Chaos Mode.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Reservation:
    agent: str
    lock_type: str
    ttl: int


@dataclass
class ResourceEntry:
    total_capacity: int
    capacity_limit: int
    health: str = "up"          # up | degraded | down
    reservations: List[Reservation] = field(default_factory=list)

    @property
    def capacity(self) -> int:
        if self.health == "down":
            return 0
        return max(0, self.capacity_limit - len(self.reservations))

    @property
    def state(self) -> str:
        if self.health == "down":
            return "down"
        if not self.reservations:
            return "free"
        if any(r.lock_type == "hard" for r in self.reservations):
            return "hard"
        return "soft"

    @property
    def owner(self) -> Optional[str]:
        return self.reservations[-1].agent if self.reservations else None

    @property
    def owners(self) -> List[str]:
        return [r.agent for r in self.reservations]

    @property
    def ttl(self) -> int:
        ttls = [r.ttl for r in self.reservations if r.lock_type == "soft"]
        return min(ttls) if ttls else (999 if self.reservations else 0)


class Ledger:
    DEFAULT_RESOURCES: Dict[str, int] = {
        "llm-api": 3,
        "vector-db": 2,
        "search-api": 2,
        "output-api": 2,
        "llm-api-backup": 2,
    }

    def __init__(self, resources: Optional[Dict[str, int]] = None) -> None:
        resource_map = resources or self.DEFAULT_RESOURCES
        self._data: Dict[str, ResourceEntry] = {
            name: ResourceEntry(total_capacity=cap, capacity_limit=cap)
            for name, cap in resource_map.items()
        }
        self.soft_reservations_made = 0
        self.soft_reservations_wasted = 0

    def reserve(self, resource: str, agent_name: str, lock_type: str, ttl: int = 5) -> bool:
        if resource not in self._data:
            raise KeyError(f"Unknown resource: {resource!r}")
        if lock_type not in ("soft", "hard"):
            raise ValueError("reserve() accepts only soft or hard locks")
        entry = self._data[resource]
        if entry.capacity <= 0:
            return False
        entry.reservations.append(Reservation(agent_name, lock_type, ttl if lock_type == "soft" else 999))
        if lock_type == "soft":
            self.soft_reservations_made += 1
        return True

    def release(self, resource: str, agent_name: str, used: bool = True) -> bool:
        if resource not in self._data:
            raise KeyError(f"Unknown resource: {resource!r}")
        entry = self._data[resource]
        for i, reservation in enumerate(entry.reservations):
            if reservation.agent == agent_name:
                if reservation.lock_type == "soft" and not used:
                    self.soft_reservations_wasted += 1
                entry.reservations.pop(i)
                return True
        return False

    def release_all_for_agent(self, agent_name: str, used: bool = False) -> int:
        released = 0
        for name in self._data:
            while self.release(name, agent_name, used=used):
                released += 1
        return released

    def tick(self) -> None:
        for entry in self._data.values():
            kept: List[Reservation] = []
            for reservation in entry.reservations:
                if reservation.lock_type != "soft":
                    kept.append(reservation)
                    continue
                reservation.ttl -= 1
                if reservation.ttl <= 0:
                    self.soft_reservations_wasted += 1
                else:
                    kept.append(reservation)
            entry.reservations = kept

    def is_available(self, resource: str) -> bool:
        return resource in self._data and self._data[resource].capacity > 0

    def available_capacity(self, resource: str) -> int:
        return self._data[resource].capacity if resource in self._data else 0

    def capacity_limit(self, resource: str) -> int:
        return self._data[resource].capacity_limit

    def total_capacity(self, resource: str) -> int:
        return self._data[resource].total_capacity

    def set_capacity_limit(self, resource: str, limit: int) -> None:
        if resource not in self._data:
            raise KeyError(resource)
        entry = self._data[resource]
        entry.capacity_limit = max(0, min(int(limit), entry.total_capacity))

    def set_health(self, resource: str, health: str) -> None:
        if resource not in self._data:
            raise KeyError(resource)
        if health not in ("up", "degraded", "down"):
            raise ValueError("health must be up, degraded or down")
        self._data[resource].health = health

    def get_health(self, resource: str) -> str:
        return self._data[resource].health

    def effective_capacity(self, resource: str) -> int:
        if resource not in self._data:
            return 0
        entry = self._data[resource]
        return 0 if entry.health == "down" else entry.capacity_limit

    def get_state(self, resource: str) -> str:
        return self._data[resource].state

    def get_entry(self, resource: str) -> ResourceEntry:
        return self._data[resource]

    def all_resources(self) -> Dict[str, ResourceEntry]:
        return self._data

    def print_state(self) -> None:
        print("\n--- LEDGER STATE ---")
        for name, entry in self._data.items():
            used = min(len(entry.reservations), entry.capacity_limit)
            bar = "█" * used + "░" * max(0, entry.capacity_limit - used)
            print(
                f"  {name:<18} [{bar:<3}] avail={entry.capacity}/{entry.capacity_limit} "
                f"health={entry.health:<8} state={entry.state:<5} owners={entry.owners}"
            )
        print(f"  Soft reservations made: {self.soft_reservations_made} | wasted: {self.soft_reservations_wasted}")

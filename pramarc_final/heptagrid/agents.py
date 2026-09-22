"""
agents.py — Person 1: Service Graph + System Backend
=====================================================
Defines the Agent data model and the default agent pool.

Each Agent tracks:
  - Its full resource workflow (steps)
  - Current position in that workflow
  - Priority metadata (importance, deadline, wait_time)
  - Execution state: running | waiting | done | failed
  - Checkpoint for mid-workflow save/resume
  - Reserved resource (set during execution by Person 4)

Integration notes for teammates:
  - Person 2 reads: agent.wait_time, agent.importance, agent.deadline, agent.future_steps()
  - Person 3 reads: agent.current_resource(), agent.priority (written by Person 2)
  - Person 4 reads/writes: agent.status, agent.reserved_resource, agent.save_checkpoint(),
                           agent.resume_from_checkpoint(), agent.advance()
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    name: str
    steps: List[str]
    importance: float = 1.0    # 0.0–1.0; set at creation, bumped by anti-starvation
    deadline: int = 10         # ticks until task must be complete
    task: str = ""             # optional natural-language task used by LLM graph generation
    prediction_confidence: Dict[str, float] = field(default_factory=dict)

    # Runtime state — do not set at construction
    current_step: int = field(default=0, init=False)
    wait_time: int = field(default=0, init=False)
    status: str = field(default="running", init=False)  # running | waiting | done | failed
    checkpoint: Optional[int] = field(default=None, init=False)
    reserved_resource: Optional[str] = field(default=None, init=False)
    priority: float = field(default=0.0, init=False)  # written by Person 2 each tick

    # ------------------------------------------------------------------ #
    # Workflow helpers                                                      #
    # ------------------------------------------------------------------ #

    def current_resource(self) -> Optional[str]:
        """Return the resource needed at the current workflow step, or None if done."""
        if self.current_step < len(self.steps):
            return self.steps[self.current_step]
        return None

    def future_steps(self, n: int = 3) -> List[str]:
        """
        Return the next n resource names starting from the current step.
        Used by Person 2 for demand forecasting and Person 4 for shadow execution.
        """
        start = self.current_step
        end = min(start + n, len(self.steps))
        return self.steps[start:end]

    def steps_remaining(self) -> int:
        """How many steps are left in this agent's workflow."""
        return max(0, len(self.steps) - self.current_step)

    def advance(self) -> None:
        """
        Move to the next workflow step.
        Called by Person 4 after a resource is successfully used.
        Automatically marks the agent done when all steps are consumed.
        """
        self.current_step += 1
        self.reserved_resource = None
        if self.current_step >= len(self.steps):
            self.status = "done"

    # ------------------------------------------------------------------ #
    # Checkpoint / resume (Person 4)                                       #
    # ------------------------------------------------------------------ #

    def save_checkpoint(self) -> None:
        """
        Persist the current step so the agent can resume here after waiting.
        Called by Person 4 when a resource is unavailable mid-workflow.
        """
        self.checkpoint = self.current_step

    def resume_from_checkpoint(self) -> None:
        """
        Restore current_step from the last saved checkpoint and flip status
        back to running. If no checkpoint exists, position is unchanged.
        """
        if self.checkpoint is not None:
            self.current_step = self.checkpoint
        self.status = "running"

    def confidence_for_current_step(self, default: float = 0.80) -> float:
        """Return confidence for the current predicted graph node if available."""
        if not self.prediction_confidence:
            return default
        resource = self.current_resource()
        if resource is None:
            return default
        occurrence = sum(1 for r in self.steps[: self.current_step + 1] if r == resource) - 1
        return float(self.prediction_confidence.get(f"{resource}#{occurrence}", default))

    # ------------------------------------------------------------------ #
    # Display                                                              #
    # ------------------------------------------------------------------ #

    def __repr__(self) -> str:
        remaining = self.steps[self.current_step:] if self.current_step < len(self.steps) else []
        return (
            f"Agent({self.name!r} | status={self.status} | "
            f"step={self.current_step}/{len(self.steps)} | "
            f"next={self.current_resource()!r} | "
            f"remaining={remaining} | "
            f"importance={self.importance} | deadline={self.deadline} | "
            f"wait={self.wait_time} | priority={self.priority:.3f})"
        )


# ---------------------------------------------------------------------------
# Default agent pool
# ---------------------------------------------------------------------------

def create_agents() -> List[Agent]:
    """
    Return the 8-agent pool used in all simulations.

    Resources available in the system:
        llm-api, vector-db, search-api, output-api, llm-api-backup
    """
    return [
        Agent("Agent-A", ["llm-api", "vector-db", "llm-api", "output-api"],
              importance=0.9, deadline=8),

        Agent("Agent-B", ["search-api", "llm-api", "vector-db"],
              importance=0.7, deadline=10),

        Agent("Agent-C", ["llm-api", "llm-api", "output-api"],
              importance=0.5, deadline=12),

        Agent("Agent-D", ["vector-db", "search-api", "llm-api"],
              importance=0.8, deadline=6),

        Agent("Agent-E", ["llm-api", "vector-db", "search-api", "output-api"],
              importance=0.6, deadline=9),

        Agent("Agent-F", ["search-api", "vector-db", "llm-api"],
              importance=0.4, deadline=15),

        Agent("Agent-G", ["llm-api", "output-api"],
              importance=1.0, deadline=3),

        Agent("Agent-H", ["vector-db", "llm-api", "output-api", "search-api"],
              importance=0.3, deadline=20),
    ]

"""
graph.py — Person 1: Service Graph + System Backend
====================================================
Builds a directed service-dependency graph for each agent and exposes
helper queries used by the scoring and forecasting modules.

Each node in the graph is a resource name (e.g. "llm-api").
Each directed edge A → B means "A must be acquired before B in this workflow."
Because the same resource can appear multiple times in a workflow (e.g.
Agent-A uses llm-api twice), repeated occurrences are distinguished by
appending an occurrence index: "llm-api#0", "llm-api#1", etc.

Public API
----------
build_service_graph(agent)      → nx.DiGraph
get_dependency_risk(agent, all) → float  0.0–1.0
get_resource_contention(resource, agents) → int
print_graph_summary(agent)      → None  (debug)

Integration notes:
  - Person 2 calls get_dependency_risk() inside priority_score()
  - Person 3 may call get_resource_contention() for reservation scoring
  - The graph itself is mainly used for visualisation and shadow simulation (Person 4)
"""

from __future__ import annotations
from typing import List, Dict, Optional
import networkx as nx

# Type alias to avoid circular imports
AgentLike = object   # duck-typed: needs .name, .steps, .current_step, .current_resource()


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_service_graph(agent: AgentLike) -> nx.DiGraph:
    """
    Build a directed graph representing the agent's full resource workflow.

    Nodes are labelled "resource#occurrence" so repeated resources are
    distinct nodes (e.g. "llm-api#0" → "vector-db#0" → "llm-api#1").
    Each node carries a 'resource' attribute with the plain resource name.

    Example for Agent-A ["llm-api", "vector-db", "llm-api", "output-api"]:
        llm-api#0 → vector-db#0 → llm-api#1 → output-api#0
    """
    G = nx.DiGraph()
    G.graph["agent"] = agent.name

    # Track occurrence counts to create unique node ids
    occurrence: Dict[str, int] = {}
    node_ids: List[str] = []

    for resource in agent.steps:
        idx = occurrence.get(resource, 0)
        node_id = f"{resource}#{idx}"
        occurrence[resource] = idx + 1
        G.add_node(node_id, resource=resource, occurrence=idx, step=len(node_ids))
        node_ids.append(node_id)

    for i in range(len(node_ids) - 1):
        G.add_edge(node_ids[i], node_ids[i + 1])

    return G


def get_current_node(agent: AgentLike, graph: nx.DiGraph) -> Optional[str]:
    """
    Return the graph node id that corresponds to the agent's current step.
    Returns None if the agent is done.
    """
    step = agent.current_step
    nodes_in_order = list(nx.topological_sort(graph))
    if step < len(nodes_in_order):
        return nodes_in_order[step]
    return None


def get_successors(agent: AgentLike, graph: nx.DiGraph, n: int = 3) -> List[str]:
    """
    Return up to n successor resource names (plain names, not node ids)
    starting from the agent's current step. Used by Person 2 for lookahead.
    """
    nodes_in_order = list(nx.topological_sort(graph))
    start = agent.current_step
    window = nodes_in_order[start: start + n]
    return [graph.nodes[nid]["resource"] for nid in window]


# ---------------------------------------------------------------------------
# Dependency / contention helpers
# ---------------------------------------------------------------------------

def get_dependency_risk(agent: AgentLike, all_agents: List[AgentLike]) -> float:
    """
    Compute a contention risk score [0.0, 1.0] for the resource the given
    agent needs right now, based on how many other active agents need the
    same resource at their current step.

    Formula:
        risk = competitors / (total_agents - 1)   clipped to [0, 1]

    Called by Person 2 inside priority_score().
    """
    current = agent.current_resource()
    if current is None:
        return 0.0

    active_others = [
        a for a in all_agents
        if a.name != agent.name and getattr(a, "status", "running") == "running"
    ]
    if not active_others:
        return 0.0

    competitors = sum(1 for a in active_others if a.current_resource() == current)
    return round(min(competitors / len(active_others), 1.0), 3)


def get_resource_contention(resource: str, agents: List[AgentLike]) -> int:
    """
    Return the raw count of running agents whose *current* step requires
    the given resource. Useful for Person 3's reservation scoring.
    """
    return sum(
        1 for a in agents
        if getattr(a, "status", "running") == "running"
        and a.current_resource() == resource
    )


def get_future_contention(resource: str, agents: List[AgentLike], lookahead: int = 3) -> int:
    """
    Return the count of running agents that will need the given resource
    within the next `lookahead` steps. Used by Person 2 for demand forecasting.
    """
    return sum(
        1 for a in agents
        if getattr(a, "status", "running") == "running"
        and resource in a.future_steps(lookahead)
    )


# ---------------------------------------------------------------------------
# Debug / display
# ---------------------------------------------------------------------------

def print_graph_summary(agent: AgentLike) -> None:
    """Print a human-readable summary of the agent's service graph."""
    G = build_service_graph(agent)
    nodes = list(nx.topological_sort(G))
    print(f"\nService Graph — {agent.name}")
    print("  Steps : " + " → ".join(nodes))
    print(f"  Nodes : {G.number_of_nodes()}  |  Edges : {G.number_of_edges()}")
    current_node = nodes[agent.current_step] if agent.current_step < len(nodes) else "—"
    print(f"  Current node: {current_node}  (step {agent.current_step})")
    remaining = nodes[agent.current_step:]
    print(f"  Remaining   : {' → '.join(remaining) if remaining else 'none (done)'}")

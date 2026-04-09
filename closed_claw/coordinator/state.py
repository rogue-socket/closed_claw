# Purpose: Shared coordinator state shapes and helpers.
#
# CoordinatorState documents every key the graph nodes read/write.
# The graph itself uses StateGraph(dict) for LangGraph compatibility,
# but this TypedDict serves as the authoritative schema reference.

from __future__ import annotations

from typing import Any, TypedDict


class Candidate(TypedDict):
    agent_id: str
    score: float
    reason: str


class CoordinatorState(TypedDict, total=False):
    """Authoritative schema for the coordinator graph state dict.

    All keys are optional (``total=False``) because each graph node only
    reads/writes a subset.  The graph is wired with ``StateGraph(dict)``
    for LangGraph compatibility — this TypedDict exists as documentation
    and can be used for static type-checking where desired.
    """

    # ---- identity
    run_id: str
    session_id: str

    # ---- input
    task: str
    context: dict[str, Any]
    runtime_policies: dict[str, Any]

    # ---- embedding
    query_vector: list[float]

    # ---- planning
    task_plan: dict[str, Any]
    task_complexity: str
    subtask_pool: list[dict[str, Any]]
    role_agent_map: dict[str, str]

    # ---- selection
    candidates: list[Candidate]
    low_confidence: bool
    human_create_approved: bool
    decision: str
    selected_agent_id: str

    # ---- execution
    discovery_results: dict[str, str]
    tool_events: list[dict[str, Any]]
    approvals: list[dict[str, Any]]

    # ---- results
    response_status: str
    response_result: str
    response_error: str
    response_latency_ms: float
    artifacts: list[dict[str, Any]]
    memory_updates: list[dict[str, Any]]
    failed_agents: list[str]
    subtask_pool: list[dict[str, Any]]
    discovery_subtask_pool: list[dict[str, Any]]
    execution_subtask_pool: list[dict[str, Any]]
    role_agent_map: dict[str, str]
    discovery_results: dict[str, str]
    execution_results: dict[str, str]
    subtask_results: dict[str, str]

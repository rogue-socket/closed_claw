from __future__ import annotations

from typing import Any, TypedDict


class Candidate(TypedDict):
    agent_id: str
    score: float
    reason: str


class CoordinatorState(TypedDict, total=False):
    run_id: str
    session_id: str
    task: str
    context: dict[str, Any]
    query_vector: list[float]
    candidates: list[Candidate]
    low_confidence: bool
    human_create_approved: bool
    decision: str
    selected_agent_id: str
    response_status: str
    response_result: str
    response_error: str
    response_latency_ms: float
    approvals: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    memory_updates: list[dict[str, Any]]
    failed_agents: list[str]

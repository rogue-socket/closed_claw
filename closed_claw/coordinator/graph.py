# Purpose: Coordinator graph construction and node wiring.

from __future__ import annotations

from pathlib import Path
from typing import Any

from closed_claw.agents.factory import AgentFactory
from closed_claw.config import Settings
from closed_claw.coordinator.nodes import CoordinatorNodes
from closed_claw.embeddings.provider import EmbeddingProvider
from closed_claw.policy.approval import ApprovalGate
from closed_claw.policy.audit import AuditStore
from closed_claw.registry.search import build_reranker
from closed_claw.registry.store import RegistryStore
from closed_claw.runtime.runner import AgentRunner


def build_graph(settings: Settings) -> Any:
    """Run build graph."""
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise RuntimeError("langgraph is required to run coordinator graph") from exc

    settings.ensure_dirs()
    registry = RegistryStore(
        db_path=settings.db_path,
        schema_path=Path(__file__).resolve().parent.parent / "registry" / "schema.sql",
        embedding_dim=settings.embedding_dim,
        require_sqlite_vec=settings.require_sqlite_vec,
    )
    nodes = CoordinatorNodes(
        settings=settings,
        registry=registry,
        reranker=build_reranker(settings),
        embedder=EmbeddingProvider(settings.embedding_model, settings.embedding_dim),
        runner=AgentRunner(
            timeout_sec=settings.agent_timeout_sec,
            retries=settings.agent_retries,
            max_intents=settings.max_tool_calls_per_agent,
        ),
        factory=AgentFactory(settings.agents_dir),
        approval_gate=ApprovalGate(timeout_sec=settings.api_approval_timeout_sec),
        audit=AuditStore(settings.db_path),
    )

    graph = StateGraph(dict)
    graph.add_node("ingest_task", nodes.ingest_task)
    graph.add_node("decompose_task", nodes.decompose_task)
    graph.add_node("execute_task_pool", nodes.execute_task_pool)
    graph.add_node("validate_outputs", nodes.validate_outputs)
    graph.add_node("update_registry_and_audit", nodes.update_registry_and_audit)
    graph.add_node("synthesize_final_response", nodes.synthesize_final_response)

    graph.set_entry_point("ingest_task")
    graph.add_edge("ingest_task", "decompose_task")
    graph.add_edge("decompose_task", "execute_task_pool")
    graph.add_edge("execute_task_pool", "validate_outputs")
    graph.add_edge("validate_outputs", "update_registry_and_audit")
    graph.add_edge("update_registry_and_audit", "synthesize_final_response")
    graph.add_edge("synthesize_final_response", END)

    return graph.compile()

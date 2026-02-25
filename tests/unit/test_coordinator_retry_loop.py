# Purpose: Unit tests for coordinator retry loop.

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from closed_claw.config import Settings
from closed_claw.coordinator.nodes import CoordinatorNodes
from closed_claw.runtime.protocol import AgentResponse


class _StubRunner:
    """Stub runner that fails once and then succeeds."""

    def __init__(self) -> None:
        """Initialize the instance."""
        self.calls = 0

    async def run_agent(self, **_: Any) -> AgentResponse:
        """Asynchronously run run agent."""
        self.calls += 1
        if self.calls == 1:
            return AgentResponse(status="error", error_message="first_failure")
        return AgentResponse(status="ok", result="recovered_result")


class _StubRegistry:
    """Minimal registry surface for coordinator retry tests."""

    def __init__(self) -> None:
        """Initialize the instance."""
        self._manifest = SimpleNamespace(tools_allowlist=["file_io"], status="active")

    def get_manifest(self, _agent_id: str) -> Any:
        """Run get manifest."""
        return self._manifest


class _StubEmbedder:
    """Minimal embedder surface for coordinator retry tests."""

    def embed(self, _text: str) -> list[float]:
        """Run embed."""
        return [0.0]


class _StubFactory:
    """Minimal factory surface for coordinator retry tests."""

    def create_capsule(self, **_: Any) -> Any:
        """Run create capsule."""
        raise AssertionError("create_capsule should not be called in this test")


class _StubApprovalGate:
    """Minimal approval-gate surface for coordinator retry tests."""


class _StubAudit:
    """Minimal audit surface for coordinator retry tests."""

    def record_event(self, *_: Any, **__: Any) -> None:
        """Run record event."""
        return None

    def record_approval(self, *_: Any, **__: Any) -> None:
        """Run record approval."""
        return None


def _settings(tmp_path: Path, subtask_max_attempts: int) -> Settings:
    """Run settings."""
    return Settings(
        db_path=tmp_path / "registry.db",
        agents_dir=tmp_path / "agents",
        run_logs_dir=tmp_path / "runs",
        embedding_model="all-MiniLM-L6-v2",
        embedding_dim=8,
        low_confidence_threshold=0.62,
        create_approval_required=True,
        create_approval_mode="approve",
        api_approval_mode="approve",
        paid_api_providers=set(),
        api_approval_timeout_sec=30,
        agent_timeout_sec=30,
        agent_retries=0,
        circuit_breaker_failures=3,
        circuit_breaker_reset_sec=120,
        task_pool_poll_interval_sec=1,
        require_sqlite_vec=False,
        llm_provider="heuristic",
        llm_model="local-heuristic",
        llm_timeout_sec=10,
        llm_api_key="",
        openai_api_key="",
        gemini_api_key="",
        anthropic_api_key="",
        siemens_api_key="",
        openai_base_url="https://api.openai.com",
        gemini_base_url="https://generativelanguage.googleapis.com",
        anthropic_base_url="https://api.anthropic.com",
        siemens_base_url="https://api.siemens.com/llm",
        extra_allowed_paths=[],
        subtask_max_attempts=subtask_max_attempts,
    )


def test_execute_task_pool_retries_failed_subtask(tmp_path: Path):
    """Test execute task pool retries failed subtask."""
    settings = _settings(tmp_path, subtask_max_attempts=2)
    runner = _StubRunner()
    nodes = CoordinatorNodes(
        settings=settings,
        registry=_StubRegistry(),
        reranker=SimpleNamespace(),
        embedder=_StubEmbedder(),
        runner=runner,
        factory=_StubFactory(),
        approval_gate=_StubApprovalGate(),
        audit=_StubAudit(),
    )

    state = {
        "run_id": "run-1",
        "session_id": "session-1",
        "task": "Perform resilient subtask execution",
        "context": {},
        "runtime_policies": {"api_approval_mode": "approve"},
        "approvals": [],
        "tool_events": [],
        "created_agents": [],
        "subtask_results": {},
        "role_agent_map": {"task-operator": "agent-1"},
        "subtask_pool": [
            {
                "task_id": "execute-task",
                "title": "Execute",
                "description": "Do the work",
                "role_tag": "task-operator",
                "depends_on": [],
                "acceptance_criteria": ["Done"],
                "requires_tool": False,
                "status": "pending",
                "assigned_agent_id": None,
                "result": "",
                "error": "",
            }
        ],
    }

    result = asyncio.run(nodes.execute_task_pool(state))
    task = result["subtask_pool"][0]
    assert runner.calls == 2
    assert task["status"] == "completed"
    assert task["attempts"] == 2
    assert result["response_status"] == "ok"

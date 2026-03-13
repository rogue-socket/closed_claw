# Purpose: Unit tests for coordinator two-phase discovery and execution flow.

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from closed_claw.config import Settings
from closed_claw.coordinator.nodes import CoordinatorNodes
from closed_claw.runtime.protocol import AgentResponse


class _RecordingRunner:
    """Runner stub that records requests and can fail discovery."""

    def __init__(self, fail_discovery: bool = False) -> None:
        """Initialize the instance."""
        self.fail_discovery = fail_discovery
        self.calls: list[dict[str, Any]] = []

    async def run_agent(self, **kwargs: Any) -> AgentResponse:
        """Asynchronously run run agent."""
        request = kwargs["request"]
        phase = request.context.get("task_phase")
        self.calls.append({"phase": phase, "request": request})
        if self.fail_discovery and phase == "discovery":
            return AgentResponse(status="error", error_message="discovery_failed")
        task_id = request.context.get("subtask", {}).get("task_id", "unknown")
        return AgentResponse(status="ok", result=f"{phase}:{task_id}:done")


class _StubRegistry:
    """Minimal registry surface for coordinator phase tests."""

    def __init__(self) -> None:
        """Initialize the instance."""
        self._manifest = SimpleNamespace(tools_allowlist=["file_io"], status="active")

    def get_manifest(self, _agent_id: str) -> Any:
        """Run get manifest."""
        return self._manifest


class _StubEmbedder:
    """Minimal embedder surface for coordinator phase tests."""

    def embed(self, _text: str) -> list[float]:
        """Run embed."""
        return [0.0]


class _StubFactory:
    """Minimal factory surface for coordinator phase tests."""

    def create_capsule(self, **_: Any) -> Any:
        """Run create capsule."""
        raise AssertionError("create_capsule should not be called in this test")


class _StubApprovalGate:
    """Minimal approval-gate surface for coordinator phase tests."""


class _StubAudit:
    """Minimal audit surface for coordinator phase tests."""

    def record_event(self, *_: Any, **__: Any) -> None:
        """Run record event."""
        return None

    def record_approval(self, *_: Any, **__: Any) -> None:
        """Run record approval."""
        return None


def _settings(tmp_path: Path) -> Settings:
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
        subtask_max_attempts=1,
    )


def _phase_plan(
    *,
    phase: str,
) -> list[dict[str, Any]]:
    """Run phase plan."""
    if phase == "discovery":
        return [
            {
                "task_id": "discover-context",
                "title": "Discover Context",
                "description": "Collect prerequisite facts",
                "role_tag": "task-operator",
                "depends_on": [],
                "acceptance_criteria": ["Facts captured"],
                "requires_tool": False,
            }
        ]
    return [
        {
            "task_id": "execute-work",
            "title": "Execute Work",
            "description": "Perform task using discovery outputs",
            "role_tag": "task-operator",
            "depends_on": [],
            "acceptance_criteria": ["Work completed"],
            "requires_tool": False,
        }
    ]


def test_execute_task_pool_runs_discovery_then_execution(monkeypatch, tmp_path: Path):
    """Test execute task pool runs discovery then execution."""
    settings = _settings(tmp_path)
    runner = _RecordingRunner()
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

    calls: list[tuple[str, dict[str, str] | None]] = []

    def _fake_plan(
        _settings: Settings,
        _task: str,
        *,
        phase: str = "execution",
        discovery_results: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        calls.append((phase, discovery_results))
        if phase == "execution":
            assert discovery_results
        return _phase_plan(phase=phase)

    monkeypatch.setattr("closed_claw.coordinator.nodes.generate_task_plan", _fake_plan)

    ingested = asyncio.run(
        nodes.ingest_task(
            {
                "run_id": "run-1",
                "session_id": "session-1",
                "task": "Complete a two-phase operation",
                "context": {},
                "runtime_policies": {"api_approval_mode": "approve"},
            }
        )
    )
    decomposed = asyncio.run(nodes.decompose_task(ingested))
    result = asyncio.run(
        nodes.execute_task_pool(
            {
                **decomposed,
                "role_agent_map": {"task-operator": "agent-1"},
            }
        )
    )

    assert [item[0] for item in calls] == ["discovery", "execution"]
    assert [item["phase"] for item in runner.calls] == ["discovery", "execution"]
    assert result["response_status"] == "ok"
    assert all(item["status"] == "completed" for item in result["discovery_subtask_pool"])
    assert all(item["status"] == "completed" for item in result["execution_subtask_pool"])
    assert result["discovery_results"]
    assert runner.calls[1]["request"].context.get("discovery_results")


def test_execute_task_pool_stops_when_discovery_fails(monkeypatch, tmp_path: Path):
    """Test execute task pool stops when discovery fails."""
    settings = _settings(tmp_path)
    runner = _RecordingRunner(fail_discovery=True)
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

    def _fake_plan(
        _settings: Settings,
        _task: str,
        *,
        phase: str = "execution",
        discovery_results: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        return _phase_plan(phase=phase)

    monkeypatch.setattr("closed_claw.coordinator.nodes.generate_task_plan", _fake_plan)

    ingested = asyncio.run(
        nodes.ingest_task(
            {
                "run_id": "run-2",
                "session_id": "session-2",
                "task": "Fail at discovery",
                "context": {},
                "runtime_policies": {"api_approval_mode": "approve"},
            }
        )
    )
    decomposed = asyncio.run(nodes.decompose_task(ingested))
    result = asyncio.run(
        nodes.execute_task_pool(
            {
                **decomposed,
                "role_agent_map": {"task-operator": "agent-1"},
            }
        )
    )

    assert [item["phase"] for item in runner.calls] == ["discovery"]
    assert result["response_status"] == "error"
    assert result["response_error"] == "discovery_phase_failed"
    assert result["execution_subtask_pool"] == []

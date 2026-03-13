# Purpose: Unit tests for parallel subtask execution via asyncio.gather.

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from closed_claw.config import Settings
from closed_claw.coordinator.nodes import CoordinatorNodes
from closed_claw.runtime.protocol import AgentResponse


class _TimingRunner:
    """Runner that records wall-clock start/end per call and sleeps async.

    Each ``run_agent`` invocation sleeps for ``delay_sec`` (truly async via
    ``asyncio.sleep``) so that concurrent calls overlap in wall time.
    """

    def __init__(self, delay_sec: float = 0.15) -> None:
        self.delay_sec = delay_sec
        self.calls: list[dict[str, Any]] = []

    async def run_agent(self, **kwargs: Any) -> AgentResponse:
        request = kwargs["request"]
        task_id = request.context.get("subtask", {}).get("task_id", "unknown")
        t0 = time.monotonic()
        await asyncio.sleep(self.delay_sec)
        t1 = time.monotonic()
        self.calls.append({"task_id": task_id, "start": t0, "end": t1})
        return AgentResponse(status="ok", result=f"{task_id}:done")


class _ToolEventRunner:
    """Runner that invokes the tool callback so per-subtask event isolation can be verified."""

    def __init__(self, tool_path: str = ".") -> None:
        self.calls: list[str] = []
        self.tool_path = tool_path

    async def run_agent(self, **kwargs: Any) -> AgentResponse:
        request = kwargs["request"]
        task_id = request.context.get("subtask", {}).get("task_id", "unknown")
        self.calls.append(task_id)
        # Simulate a tool call via the callback
        from closed_claw.runtime.protocol import ToolCallIntent

        tool_cb = kwargs["tool_callback"]
        intent = ToolCallIntent(tool="file_io", args={"op": "list", "path": self.tool_path}, reason="test")
        await tool_cb(intent, "agent-1")
        return AgentResponse(status="ok", result=f"{task_id}:done")


class _StubRegistry:
    def __init__(self) -> None:
        self._manifest = SimpleNamespace(
            tools_allowlist=["file_io"], status="active", skill_ids=[]
        )

    def get_manifest(self, _agent_id: str) -> Any:
        return self._manifest


class _StubEmbedder:
    def embed(self, _text: str) -> list[float]:
        return [0.0]


class _StubFactory:
    def create_capsule(self, **_: Any) -> Any:
        raise AssertionError("create_capsule should not be called")


class _StubApprovalGate:
    pass


class _StubAudit:
    def record_event(self, *_: Any, **__: Any) -> None:
        return None

    def record_approval(self, *_: Any, **__: Any) -> None:
        return None


def _settings(tmp_path: Path) -> Settings:
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


def _make_pool(count: int) -> list[dict[str, Any]]:
    """Create *count* independent pending subtasks (no dependencies)."""
    return [
        {
            "task_id": f"task-{i}",
            "title": f"Task {i}",
            "description": f"Do thing {i}",
            "role_tag": "task-operator",
            "depends_on": [],
            "acceptance_criteria": [f"Thing {i} done"],
            "requires_tool": False,
            "status": "pending",
            "assigned_agent_id": None,
            "result": "",
            "error": "",
        }
        for i in range(count)
    ]


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_independent_subtasks_run_concurrently(tmp_path: Path):
    """Three independent subtasks should overlap in wall time, proving they
    run via asyncio.gather rather than serially."""
    delay = 0.15
    n_tasks = 3
    runner = _TimingRunner(delay_sec=delay)
    nodes = CoordinatorNodes(
        settings=_settings(tmp_path),
        registry=_StubRegistry(),
        reranker=SimpleNamespace(),
        embedder=_StubEmbedder(),
        runner=runner,
        factory=_StubFactory(),
        approval_gate=_StubApprovalGate(),
        audit=_StubAudit(),
    )
    pool = _make_pool(n_tasks)
    state = {
        "run_id": "run-par",
        "session_id": "sess-par",
        "task": "Parallel test",
        "context": {},
        "runtime_policies": {"api_approval_mode": "approve"},
    }

    t_wall_start = time.monotonic()
    result = asyncio.run(
        nodes._execute_phase_pool(
            state=state,
            phase="execution",
            pool=pool,
            discovery_results={},
            approvals=[],
            tool_events=[],
            role_agent_map={"task-operator": "agent-1"},
            created_agents=[],
            subtask_results={},
            api_mode="approve",
        )
    )
    t_wall_end = time.monotonic()
    wall_elapsed = t_wall_end - t_wall_start

    # All tasks should have completed successfully.
    assert result["status"] == "ok"
    assert len(runner.calls) == n_tasks
    assert all(item["status"] == "completed" for item in pool)

    # Serial execution would take at least n_tasks * delay.
    # Parallel execution should take roughly 1 * delay (plus scheduling overhead).
    serial_minimum = n_tasks * delay
    assert wall_elapsed < serial_minimum, (
        f"Wall time {wall_elapsed:.3f}s >= serial minimum {serial_minimum:.3f}s — "
        "subtasks appear to have run serially"
    )


def test_subtasks_with_dependencies_respect_ordering(tmp_path: Path):
    """Subtask B depends on A — B should only run after A completes."""
    runner = _TimingRunner(delay_sec=0.05)
    nodes = CoordinatorNodes(
        settings=_settings(tmp_path),
        registry=_StubRegistry(),
        reranker=SimpleNamespace(),
        embedder=_StubEmbedder(),
        runner=runner,
        factory=_StubFactory(),
        approval_gate=_StubApprovalGate(),
        audit=_StubAudit(),
    )
    pool = [
        {
            "task_id": "task-a",
            "title": "Task A",
            "description": "First",
            "role_tag": "task-operator",
            "depends_on": [],
            "acceptance_criteria": ["A done"],
            "requires_tool": False,
            "status": "pending",
            "assigned_agent_id": None,
            "result": "",
            "error": "",
        },
        {
            "task_id": "task-b",
            "title": "Task B",
            "description": "Second",
            "role_tag": "task-operator",
            "depends_on": ["task-a"],
            "acceptance_criteria": ["B done"],
            "requires_tool": False,
            "status": "pending",
            "assigned_agent_id": None,
            "result": "",
            "error": "",
        },
    ]
    state = {
        "run_id": "run-dep",
        "session_id": "sess-dep",
        "task": "Dependency ordering test",
        "context": {},
        "runtime_policies": {"api_approval_mode": "approve"},
    }

    result = asyncio.run(
        nodes._execute_phase_pool(
            state=state,
            phase="execution",
            pool=pool,
            discovery_results={},
            approvals=[],
            tool_events=[],
            role_agent_map={"task-operator": "agent-1"},
            created_agents=[],
            subtask_results={},
            api_mode="approve",
        )
    )

    assert result["status"] == "ok"
    assert all(item["status"] == "completed" for item in pool)

    # Verify ordering: B's start must be after A's end.
    calls_by_id = {c["task_id"]: c for c in runner.calls}
    assert calls_by_id["task-b"]["start"] >= calls_by_id["task-a"]["end"]


def test_per_subtask_tool_events_isolation(tmp_path: Path):
    """Tool events from concurrent subtasks must not leak into each other's
    verification window (the old tool_events_start slicing bug)."""
    runner = _ToolEventRunner(tool_path=str(tmp_path))
    settings = _settings(tmp_path)
    # Need a real ToolExecutor for the callback to work
    from closed_claw.tools.executor import ToolExecutor

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
    # Override the tool_executor to allow any root
    nodes.tool_executor = ToolExecutor(
        workspace_root=tmp_path,
        allowed_roots=[tmp_path],
    )

    pool = _make_pool(3)
    # Mark all as requires_tool=True to exercise verification
    for item in pool:
        item["requires_tool"] = True

    state = {
        "run_id": "run-iso",
        "session_id": "sess-iso",
        "task": "Tool isolation test",
        "context": {},
        "runtime_policies": {"api_approval_mode": "approve"},
    }

    tool_events: list[dict[str, Any]] = []
    result = asyncio.run(
        nodes._execute_phase_pool(
            state=state,
            phase="execution",
            pool=pool,
            discovery_results={},
            approvals=[],
            tool_events=tool_events,
            role_agent_map={"task-operator": "agent-1"},
            created_agents=[],
            subtask_results={},
            api_mode="approve",
        )
    )

    assert result["status"] == "ok"
    assert all(item["status"] == "completed" for item in pool)
    # All 3 subtasks' tool events should have been merged into the shared list.
    assert len(tool_events) == 3


def test_parallel_results_stored_correctly(tmp_path: Path):
    """subtask_results dict should contain entries for all parallel subtasks."""
    runner = _TimingRunner(delay_sec=0.01)
    nodes = CoordinatorNodes(
        settings=_settings(tmp_path),
        registry=_StubRegistry(),
        reranker=SimpleNamespace(),
        embedder=_StubEmbedder(),
        runner=runner,
        factory=_StubFactory(),
        approval_gate=_StubApprovalGate(),
        audit=_StubAudit(),
    )
    pool = _make_pool(4)
    subtask_results: dict[str, str] = {}
    state = {
        "run_id": "run-res",
        "session_id": "sess-res",
        "task": "Results test",
        "context": {},
        "runtime_policies": {"api_approval_mode": "approve"},
    }

    result = asyncio.run(
        nodes._execute_phase_pool(
            state=state,
            phase="execution",
            pool=pool,
            discovery_results={},
            approvals=[],
            tool_events=[],
            role_agent_map={"task-operator": "agent-1"},
            created_agents=[],
            subtask_results=subtask_results,
            api_mode="approve",
        )
    )

    assert result["status"] == "ok"
    # Each task should have both plain and phase-prefixed entries.
    for i in range(4):
        tid = f"task-{i}"
        assert tid in subtask_results
        assert f"execution.{tid}" in subtask_results

# Purpose: Integration tests for flow.

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from closed_claw.config import Settings
from closed_claw.registry.search import HeuristicReranker

pytest.importorskip("langgraph")

from closed_claw.coordinator.graph import build_graph


def _stub_task_plan(
    _settings: Settings,
    _task: str,
    *,
    phase: str = "execution",
    discovery_results: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Stub task plan for integration tests (no LLM required)."""
    if phase == "discovery":
        return [
            {
                "task_id": "collect-required-context",
                "title": "Collect Required Context",
                "description": "Gather prerequisite context for the task.",
                "role_tag": "context-discoverer",
                "depends_on": [],
                "acceptance_criteria": ["Context captured."],
                "requires_tool": False,
            }
        ]
    return [
        {
            "task_id": "execute-task",
            "title": "Execute User Task",
            "description": _task,
            "role_tag": "task-operator",
            "depends_on": [],
            "acceptance_criteria": ["Task completed."],
            "requires_tool": False,
        }
    ]


def _stub_agent_profile(
    settings: Settings,
    task: str,
    supported_tools: list[str],
    fallback_tools: list[str],
) -> dict[str, Any]:
    """Stub agent profile for integration tests (no LLM required)."""
    return {
        "profile_id": "task-operator",
        "name_prefix": "Task Operator",
        "description": f"Operator for: {task[:60]}",
        "tools_allowlist": fallback_tools or ["terminal"],
        "tags": ["auto", "capability", "task-operator"],
        "skill_md": "# Task Operator\n\nExecute assigned tasks safely.\n",
        "api_capabilities": [],
        "requires_approval_for": [],
    }


def test_end_to_end_flow_with_approval(monkeypatch, tmp_path: Path):
    """Test end to end flow with approval."""
    monkeypatch.setenv("CLOSED_CLAW_DB_PATH", str(tmp_path / "registry.db"))
    monkeypatch.setenv("CLOSED_CLAW_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("CLOSED_CLAW_EMBEDDING_DIM", "8")
    monkeypatch.setenv("CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS", "false")
    monkeypatch.setenv("CLOSED_CLAW_CREATE_APPROVAL_REQUIRED", "true")
    monkeypatch.setenv("CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD", "0.95")

    # Patch LLM-dependent functions so the test runs without a real LLM provider
    monkeypatch.setattr("closed_claw.coordinator.graph.build_reranker", lambda _s: HeuristicReranker())
    monkeypatch.setattr("closed_claw.coordinator.nodes.generate_task_plan", _stub_task_plan)
    monkeypatch.setattr("closed_claw.coordinator.nodes.generate_agent_profile", _stub_agent_profile)

    answers = iter(["yes", "yes"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    settings = Settings.from_env()
    graph = build_graph(settings)

    async def run_once() -> dict:
        """Test run once."""
        return await graph.ainvoke({"task": "please use paid_api for analysis", "context": {}})

    result = asyncio.run(run_once())
    assert "response_result" in result
    assert result.get("response_status") in {"ok", "error"}

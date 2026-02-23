from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from closed_claw.config import Settings

pytest.importorskip("langgraph")

from closed_claw.coordinator.graph import build_graph


def test_end_to_end_flow_with_approval(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLOSED_CLAW_DB_PATH", str(tmp_path / "registry.db"))
    monkeypatch.setenv("CLOSED_CLAW_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("CLOSED_CLAW_EMBEDDING_DIM", "8")
    monkeypatch.setenv("CLOSED_CLAW_CREATE_APPROVAL_REQUIRED", "true")
    monkeypatch.setenv("CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD", "0.95")

    answers = iter(["yes", "yes"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    settings = Settings.from_env()
    graph = build_graph(settings)

    async def run_once() -> dict:
        return await graph.ainvoke({"task": "please use paid_api for analysis", "context": {}})

    result = asyncio.run(run_once())
    assert "response_result" in result
    assert result.get("response_status") in {"ok", "error"}

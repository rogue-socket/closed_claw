# Purpose: Unit tests for infinite-loop guardrails across agent, runner, and coordinator.

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from closed_claw.config import Settings
from closed_claw.runtime.protocol import (
    AgentResponse,
    ApiCallDecision,
    ApiCallIntent,
    ToolCallIntent,
    ToolCallResult,
    parse_agent_line,
)
from closed_claw.runtime.runner import AgentRunner, AgentRuntimeError


# ── Config guardrail defaults ────────────────────────────────────────────────


def test_config_defaults_include_guardrails(monkeypatch, tmp_path: Path):
    """New guardrail settings have sensible defaults."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    settings = Settings.from_env()
    assert settings.max_tool_calls_per_agent == 50
    assert settings.max_agents_per_run == 10
    assert settings.max_subtasks_per_phase == 4


def test_config_guardrails_from_env(monkeypatch, tmp_path: Path):
    """Guardrail settings can be overridden via .env."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CLOSED_CLAW_MAX_TOOL_CALLS_PER_AGENT=20\n"
        "CLOSED_CLAW_MAX_AGENTS_PER_RUN=5\n"
        "CLOSED_CLAW_MAX_SUBTASKS_PER_PHASE=8\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()
    assert settings.max_tool_calls_per_agent == 20
    assert settings.max_agents_per_run == 5
    assert settings.max_subtasks_per_phase == 8


def test_config_guardrails_floor_at_one(monkeypatch, tmp_path: Path):
    """Guardrail settings are clamped to a minimum of 1."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CLOSED_CLAW_MAX_TOOL_CALLS_PER_AGENT=0\n"
        "CLOSED_CLAW_MAX_AGENTS_PER_RUN=-5\n"
        "CLOSED_CLAW_MAX_SUBTASKS_PER_PHASE=0\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()
    assert settings.max_tool_calls_per_agent >= 1
    assert settings.max_agents_per_run >= 1
    assert settings.max_subtasks_per_phase >= 1


# ── Runner max-intents guardrail ─────────────────────────────────────────────


def test_runner_max_intents_default():
    """AgentRunner has a max_intents parameter with a default."""
    runner = AgentRunner()
    assert runner.max_intents == 50


def test_runner_max_intents_custom():
    """AgentRunner respects a custom max_intents value."""
    runner = AgentRunner(max_intents=10)
    assert runner.max_intents == 10


@pytest.mark.asyncio
async def test_runner_kills_process_on_intent_overflow(tmp_path: Path):
    """AgentRunner kills the subprocess when intents exceed max_intents."""
    # Create a minimal agent script that emits tool_call_intent in a tight loop
    agent_script = tmp_path / "loop_agent.py"
    agent_script.write_text(
        textwrap.dedent(
            """\
        import json, sys
        req = sys.stdin.readline()
        # Emit 20 tool_call_intents in a row (no final response)
        for i in range(20):
            sys.stdout.write(json.dumps({
                "type": "tool_call_intent",
                "tool": "terminal",
                "args": {"cmd": "echo hi"},
                "reason": f"loop {i}",
            }) + "\\n")
            sys.stdout.flush()
            # Read the result back
            sys.stdin.readline()
        # Should never reach here if guardrail works
        sys.stdout.write(json.dumps({
            "status": "ok", "result": "done", "memory_updates": [],
            "artifacts": [], "metrics": {},
        }) + "\\n")
        sys.stdout.flush()
        """
        ),
        encoding="utf-8",
    )

    runner = AgentRunner(timeout_sec=30, retries=0, max_intents=5)

    from closed_claw.runtime.protocol import CoordinatorRequest

    req = CoordinatorRequest(session_id="test", task="test", context={}, config={})

    async def tool_cb(intent, agent_id):
        return ToolCallResult(ok=True, result={"output": "ok"}, error="")

    async def approval_cb(intent, agent_id):
        return ApiCallDecision(approved=True, note="test")

    with pytest.raises(AgentRuntimeError, match="exceeded max intents"):
        await runner.run_agent(
            agent_id="test-agent",
            entrypoint=agent_script,
            request=req,
            approval_callback=approval_cb,
            tool_callback=tool_cb,
        )

# Purpose: Unit tests for AgentRunner subprocess runtime.

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from closed_claw.runtime.protocol import (
    AgentResponse,
    ApiCallDecision,
    ApiCallIntent,
    CoordinatorRequest,
    ToolCallIntent,
    ToolCallResult,
)
from closed_claw.runtime.runner import AgentRunner, AgentRuntimeError


def _write_agent_script(tmp_path: Path, lines: list[str]) -> Path:
    """Write a Python script that outputs the given JSON lines to stdout."""
    script = tmp_path / "agent.py"
    code_lines = [
        "import json, sys",
        "first = sys.stdin.readline()",  # read the CoordinatorRequest
    ]
    for line in lines:
        if line == "__READ_STDIN__":
            code_lines.append("response = sys.stdin.readline()")
        else:
            code_lines.append(f"print({line!r}, flush=True)")
    script.write_text("\n".join(code_lines), encoding="utf-8")
    return script


def _make_request() -> CoordinatorRequest:
    return CoordinatorRequest(session_id="test", task="do something")


@pytest.mark.asyncio
async def test_normal_execution(tmp_path: Path):
    """Agent sends a valid AgentResponse and runner returns it."""
    response_json = json.dumps({
        "type": "agent_response",
        "status": "ok",
        "result": "done",
        "memory_updates": [],
        "artifacts": [],
        "metrics": {"latency_ms": 10.0},
    })
    script = _write_agent_script(tmp_path, [response_json])
    runner = AgentRunner(timeout_sec=10, retries=0)
    result = await runner.run_agent(
        agent_id="test-agent",
        entrypoint=script,
        request=_make_request(),
        approval_callback=AsyncMock(),
        tool_callback=AsyncMock(),
    )
    assert isinstance(result, AgentResponse)
    assert result.status == "ok"
    assert result.result == "done"


@pytest.mark.asyncio
async def test_retry_on_failure(tmp_path: Path):
    """Runner retries on subprocess failure and succeeds on second attempt."""
    # Script that fails (no output = no valid response)
    fail_script = tmp_path / "fail_agent.py"
    fail_script.write_text(
        "import sys\nfirst = sys.stdin.readline()\nsys.exit(1)\n",
        encoding="utf-8",
    )
    # Script that succeeds
    response_json = json.dumps({
        "type": "agent_response",
        "status": "ok",
        "result": "retry worked",
        "memory_updates": [],
        "artifacts": [],
        "metrics": {},
    })
    ok_script = tmp_path / "ok_agent.py"
    ok_script.write_text(
        f"import sys\nfirst = sys.stdin.readline()\nprint({response_json!r}, flush=True)\n",
        encoding="utf-8",
    )

    runner = AgentRunner(timeout_sec=10, retries=1)
    call_count = 0
    original_run_once = runner._run_once

    async def patched_run_once(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise AgentRuntimeError("simulated failure")
        return await original_run_once(
            "test-agent", ok_script, _make_request(), AsyncMock(), AsyncMock()
        )

    runner._run_once = patched_run_once
    result = await runner.run_agent(
        agent_id="test-agent",
        entrypoint=ok_script,
        request=_make_request(),
        approval_callback=AsyncMock(),
        tool_callback=AsyncMock(),
    )
    assert result.status == "ok"
    assert call_count == 2


@pytest.mark.asyncio
async def test_max_retries_exceeded(tmp_path: Path):
    """Runner raises AgentRuntimeError after exhausting retries."""
    fail_script = tmp_path / "fail_agent.py"
    fail_script.write_text(
        "import sys\nfirst = sys.stdin.readline()\nsys.exit(1)\n",
        encoding="utf-8",
    )
    runner = AgentRunner(timeout_sec=10, retries=1)
    with pytest.raises(AgentRuntimeError, match="failed after retries"):
        await runner.run_agent(
            agent_id="test-agent",
            entrypoint=fail_script,
            request=_make_request(),
            approval_callback=AsyncMock(),
            tool_callback=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_tool_callback_invoked(tmp_path: Path):
    """Runner invokes tool_callback when agent emits a ToolCallIntent."""
    tool_intent = json.dumps({
        "type": "tool_call_intent",
        "tool": "terminal",
        "args": {"cmd": "echo hi"},
        "reason": "test",
    })
    response_json = json.dumps({
        "type": "agent_response",
        "status": "ok",
        "result": "used tool",
        "memory_updates": [],
        "artifacts": [],
        "metrics": {},
    })

    script = tmp_path / "tool_agent.py"
    script.write_text("\n".join([
        "import json, sys",
        "first = sys.stdin.readline()",
        f"print({tool_intent!r}, flush=True)",
        "tool_result = sys.stdin.readline()",
        f"print({response_json!r}, flush=True)",
    ]), encoding="utf-8")

    tool_cb = AsyncMock(return_value=ToolCallResult(ok=True, result={"stdout": "hi"}))
    runner = AgentRunner(timeout_sec=10, retries=0)
    result = await runner.run_agent(
        agent_id="test-agent",
        entrypoint=script,
        request=_make_request(),
        approval_callback=AsyncMock(),
        tool_callback=tool_cb,
    )
    assert result.status == "ok"
    tool_cb.assert_called_once()
    call_args = tool_cb.call_args
    assert isinstance(call_args[0][0], ToolCallIntent)
    assert call_args[0][0].tool == "terminal"


@pytest.mark.asyncio
async def test_approval_callback_invoked(tmp_path: Path):
    """Runner invokes approval_callback when agent emits an ApiCallIntent."""
    api_intent = json.dumps({
        "type": "api_call_intent",
        "call_type": "external_paid_api",
        "provider": "openai",
        "endpoint": "/v1/chat",
        "estimated_cost_usd": 0.01,
        "reason": "need llm",
    })
    response_json = json.dumps({
        "type": "agent_response",
        "status": "ok",
        "result": "api approved",
        "memory_updates": [],
        "artifacts": [],
        "metrics": {},
    })

    script = tmp_path / "api_agent.py"
    script.write_text("\n".join([
        "import json, sys",
        "first = sys.stdin.readline()",
        f"print({api_intent!r}, flush=True)",
        "decision = sys.stdin.readline()",
        f"print({response_json!r}, flush=True)",
    ]), encoding="utf-8")

    approval_cb = AsyncMock(return_value=ApiCallDecision(approved=True, note="ok"))
    runner = AgentRunner(timeout_sec=10, retries=0)
    result = await runner.run_agent(
        agent_id="test-agent",
        entrypoint=script,
        request=_make_request(),
        approval_callback=approval_cb,
        tool_callback=AsyncMock(),
    )
    assert result.status == "ok"
    approval_cb.assert_called_once()
    call_args = approval_cb.call_args
    assert isinstance(call_args[0][0], ApiCallIntent)
    assert call_args[0][0].provider == "openai"

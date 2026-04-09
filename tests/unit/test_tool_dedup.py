# Purpose: Unit tests for tool call deduplication in AgentRunner.

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from closed_claw.runtime.protocol import CoordinatorRequest
from closed_claw.runtime.runner import AgentRunner, _SIDE_EFFECT_TOOLS, _tool_cache_key


# ── Dedup for read-only tools ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_read_only_tool_calls_are_deduplicated(tmp_path: Path):
    """When an agent emits the same read-only tool call multiple times,
    only the first should invoke the callback; subsequent calls return cached."""

    # Agent script: emits the same web_fetch call 4 times, then a final response.
    agent_script = tmp_path / "dedup_agent.py"
    agent_script.write_text(
        textwrap.dedent(
            """\
        import json, sys
        req = sys.stdin.readline()
        for i in range(4):
            intent = {
                "type": "tool_call_intent",
                "tool": "web_fetch",
                "args": {"url": "https://example.com/page"},
                "reason": f"fetch attempt {i}",
            }
            sys.stdout.write(json.dumps(intent) + "\\n")
            sys.stdout.flush()
            result = json.loads(sys.stdin.readline())
            # All 4 results should be identical (dedup)
        sys.stdout.write(json.dumps({
            "type": "agent_response",
            "status": "ok",
            "result": "done after 4 fetches",
            "memory_updates": [],
            "artifacts": [],
            "metrics": {},
        }) + "\\n")
        sys.stdout.flush()
        """
        ),
        encoding="utf-8",
    )

    runner = AgentRunner(timeout_sec=30, retries=0, max_intents=50)
    req = CoordinatorRequest(session_id="s1", task="test dedup", context={}, config={})

    callback_count = 0

    async def counting_tool_callback(intent, agent_id):
        nonlocal callback_count
        callback_count += 1
        from closed_claw.runtime.protocol import ToolCallResult
        return ToolCallResult(ok=True, result={"text": "hello"}, error="")

    async def noop_approval(intent, agent_id):
        from closed_claw.runtime.protocol import ApiCallDecision
        return ApiCallDecision(approved=True)

    response = await runner.run_agent(
        agent_id="test-agent",
        entrypoint=agent_script,
        request=req,
        approval_callback=noop_approval,
        tool_callback=counting_tool_callback,
    )

    assert response.status == "ok"
    assert response.result == "done after 4 fetches"
    # The callback should have been invoked only ONCE — the remaining 3 are dedup hits.
    assert callback_count == 1, f"Expected 1 real call, got {callback_count}"


# ── Side-effect tools are NOT deduplicated ───────────────────────────────────


@pytest.mark.asyncio
async def test_side_effect_tool_calls_are_not_deduplicated(tmp_path: Path):
    """terminal and python_exec calls should always execute, even if identical."""

    agent_script = tmp_path / "sideeffect_agent.py"
    agent_script.write_text(
        textwrap.dedent(
            """\
        import json, sys
        req = sys.stdin.readline()
        for i in range(3):
            intent = {
                "type": "tool_call_intent",
                "tool": "terminal",
                "args": {"cmd": "echo hi"},
                "reason": f"run {i}",
            }
            sys.stdout.write(json.dumps(intent) + "\\n")
            sys.stdout.flush()
            sys.stdin.readline()
        sys.stdout.write(json.dumps({
            "type": "agent_response",
            "status": "ok",
            "result": "done",
            "memory_updates": [],
            "artifacts": [],
            "metrics": {},
        }) + "\\n")
        sys.stdout.flush()
        """
        ),
        encoding="utf-8",
    )

    runner = AgentRunner(timeout_sec=30, retries=0, max_intents=50)
    req = CoordinatorRequest(session_id="s1", task="test no dedup", context={}, config={})

    callback_count = 0

    async def counting_tool_callback(intent, agent_id):
        nonlocal callback_count
        callback_count += 1
        from closed_claw.runtime.protocol import ToolCallResult
        return ToolCallResult(ok=True, result={"output": "hi"}, error="")

    async def noop_approval(intent, agent_id):
        from closed_claw.runtime.protocol import ApiCallDecision
        return ApiCallDecision(approved=True)

    response = await runner.run_agent(
        agent_id="test-agent",
        entrypoint=agent_script,
        request=req,
        approval_callback=noop_approval,
        tool_callback=counting_tool_callback,
    )

    assert response.status == "ok"
    # All 3 calls should be forwarded since terminal is a side-effect tool.
    assert callback_count == 3, f"Expected 3 real calls, got {callback_count}"


# ── Different args are NOT deduplicated ──────────────────────────────────────


@pytest.mark.asyncio
async def test_different_args_are_not_deduplicated(tmp_path: Path):
    """Same tool but different args should each execute separately."""

    agent_script = tmp_path / "diffargs_agent.py"
    agent_script.write_text(
        textwrap.dedent(
            """\
        import json, sys
        req = sys.stdin.readline()
        for url in ["https://a.com", "https://b.com", "https://a.com"]:
            intent = {
                "type": "tool_call_intent",
                "tool": "web_fetch",
                "args": {"url": url},
                "reason": "fetch",
            }
            sys.stdout.write(json.dumps(intent) + "\\n")
            sys.stdout.flush()
            sys.stdin.readline()
        sys.stdout.write(json.dumps({
            "type": "agent_response",
            "status": "ok",
            "result": "done",
            "memory_updates": [],
            "artifacts": [],
            "metrics": {},
        }) + "\\n")
        sys.stdout.flush()
        """
        ),
        encoding="utf-8",
    )

    runner = AgentRunner(timeout_sec=30, retries=0, max_intents=50)
    req = CoordinatorRequest(session_id="s1", task="test", context={}, config={})

    callback_count = 0

    async def counting_tool_callback(intent, agent_id):
        nonlocal callback_count
        callback_count += 1
        from closed_claw.runtime.protocol import ToolCallResult
        return ToolCallResult(ok=True, result={"text": "ok"}, error="")

    async def noop_approval(intent, agent_id):
        from closed_claw.runtime.protocol import ApiCallDecision
        return ApiCallDecision(approved=True)

    response = await runner.run_agent(
        agent_id="test-agent",
        entrypoint=agent_script,
        request=req,
        approval_callback=noop_approval,
        tool_callback=counting_tool_callback,
    )

    assert response.status == "ok"
    # 2 unique URLs: a.com and b.com. Third call (a.com again) is deduped.
    assert callback_count == 2, f"Expected 2 real calls, got {callback_count}"


# ── Cache key helper ─────────────────────────────────────────────────────────


def test_tool_cache_key_is_deterministic():
    """Cache key should be the same regardless of dict insertion order."""
    from closed_claw.runtime.protocol import ToolCallIntent

    i1 = ToolCallIntent(tool="web_fetch", args={"url": "x", "timeout": 5}, reason="a")
    i2 = ToolCallIntent(tool="web_fetch", args={"timeout": 5, "url": "x"}, reason="b")
    assert _tool_cache_key(i1) == _tool_cache_key(i2)


def test_side_effect_tools_set():
    """Verify which tools are considered side-effect tools."""
    assert "terminal" in _SIDE_EFFECT_TOOLS
    assert "python_exec" in _SIDE_EFFECT_TOOLS
    assert "web_fetch" not in _SIDE_EFFECT_TOOLS
    assert "file_io" not in _SIDE_EFFECT_TOOLS

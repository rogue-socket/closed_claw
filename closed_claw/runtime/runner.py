# Purpose: Subprocess runtime for launching and managing agent execution.

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from closed_claw.runtime.protocol import (
    AgentResponse,
    ApiCallDecision,
    ApiCallIntent,
    CoordinatorRequest,
    ToolCallIntent,
    ToolCallResult,
    parse_agent_line,
)

logger = logging.getLogger(__name__)

ApprovalCallback = Callable[[ApiCallIntent, str], Awaitable[ApiCallDecision]]
ToolCallback = Callable[[ToolCallIntent, str], Awaitable[ToolCallResult]]

# Tools that may produce different results on repeated identical calls
# (side-effects, mutable state, etc.) and should NOT be deduplicated.
_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({"terminal", "python_exec"})


def _tool_cache_key(intent: ToolCallIntent) -> str:
    """Build a deterministic cache key from a tool call intent."""
    return f"{intent.tool}:{json.dumps(intent.args, sort_keys=True)}"


class AgentRuntimeError(RuntimeError):
    pass


class AgentRunner:
    def __init__(self, timeout_sec: int = 120, retries: int = 2, max_intents: int = 50) -> None:
        """Initialize the instance."""
        self.timeout_sec = timeout_sec
        self.retries = retries
        self.max_intents = max_intents

    async def run_agent(
        self,
        agent_id: str,
        entrypoint: Path,
        request: CoordinatorRequest,
        approval_callback: ApprovalCallback,
        tool_callback: ToolCallback,
    ) -> AgentResponse:
        """Asynchronously run run agent."""
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                return await asyncio.wait_for(
                    self._run_once(agent_id, entrypoint, request, approval_callback, tool_callback),
                    timeout=self.timeout_sec,
                )
            except Exception as exc:
                last_error = exc
        raise AgentRuntimeError(f"Agent {agent_id} failed after retries: {last_error}")

    async def _run_once(
        self,
        agent_id: str,
        entrypoint: Path,
        request: CoordinatorRequest,
        approval_callback: ApprovalCallback,
        tool_callback: ToolCallback,
    ) -> AgentResponse:
        """Asynchronously run run once."""
        start = time.time()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(entrypoint),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin and proc.stdout and proc.stderr

        proc.stdin.write((request.model_dump_json() + "\n").encode("utf-8"))
        await proc.stdin.drain()

        final: AgentResponse | None = None
        intent_count = 0
        # Per-run tool call dedup cache: cache_key -> ToolCallResult
        tool_cache: dict[str, ToolCallResult] = {}
        dedup_count = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            parsed = parse_agent_line(line.decode("utf-8"))
            if isinstance(parsed, ApiCallIntent):
                intent_count += 1
                if intent_count > self.max_intents:
                    # Kill the subprocess — it has exceeded the intent limit.
                    proc.kill()
                    raise AgentRuntimeError(
                        f"Agent {agent_id} exceeded max intents ({self.max_intents})"
                    )
                decision = await approval_callback(parsed, agent_id)
                proc.stdin.write((decision.model_dump_json() + "\n").encode("utf-8"))
                await proc.stdin.drain()
                continue
            if isinstance(parsed, ToolCallIntent):
                intent_count += 1
                if intent_count > self.max_intents:
                    proc.kill()
                    raise AgentRuntimeError(
                        f"Agent {agent_id} exceeded max intents ({self.max_intents})"
                    )
                # Dedup: return cached result for identical read-only tool calls
                cache_key = _tool_cache_key(parsed)
                if parsed.tool not in _SIDE_EFFECT_TOOLS and cache_key in tool_cache:
                    result = tool_cache[cache_key]
                    dedup_count += 1
                    logger.info(
                        "tool_call_deduplicated agent=%s tool=%s dedup_count=%d",
                        agent_id, parsed.tool, dedup_count,
                    )
                else:
                    result = await tool_callback(parsed, agent_id)
                    if parsed.tool not in _SIDE_EFFECT_TOOLS:
                        tool_cache[cache_key] = result
                proc.stdin.write((result.model_dump_json() + "\n").encode("utf-8"))
                await proc.stdin.drain()
                continue
            final = parsed
            break

        stderr = (await proc.stderr.read()).decode("utf-8").strip()
        rc = await proc.wait()
        if final is None:
            raise AgentRuntimeError(
                f"No valid final response from agent {agent_id}; rc={rc}, stderr={stderr}"
            )
        if final.metrics.latency_ms is None:
            final.metrics.latency_ms = (time.time() - start) * 1000
        if rc != 0 and final.status != "error":
            raise AgentRuntimeError(f"Agent {agent_id} exited with code {rc}: {stderr}")
        return final

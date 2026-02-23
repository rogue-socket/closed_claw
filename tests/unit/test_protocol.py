from __future__ import annotations

import pytest

from closed_claw.runtime.protocol import AgentResponse, ApiCallIntent, ToolCallIntent, parse_agent_line


def test_parse_api_intent():
    line = '{"type":"api_call_intent","provider":"demo","endpoint":"/x","estimated_cost_usd":0.1,"reason":"r"}'
    parsed = parse_agent_line(line)
    assert isinstance(parsed, ApiCallIntent)
    assert parsed.provider == "demo"


def test_parse_agent_response():
    line = '{"status":"ok","result":"done","memory_updates":[],"artifacts":[],"metrics":{"latency_ms":1.2}}'
    parsed = parse_agent_line(line)
    assert isinstance(parsed, AgentResponse)
    assert parsed.status == "ok"


def test_parse_invalid():
    with pytest.raises(ValueError):
        parse_agent_line("not json")


def test_parse_tool_intent():
    line = '{"type":"tool_call_intent","tool":"terminal","args":{"cmd":"echo hi"},"reason":"demo"}'
    parsed = parse_agent_line(line)
    assert isinstance(parsed, ToolCallIntent)
    assert parsed.tool == "terminal"

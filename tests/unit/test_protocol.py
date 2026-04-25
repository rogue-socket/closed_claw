# Purpose: Unit tests for protocol.

from __future__ import annotations

import pytest

from closed_claw.runtime.protocol import AgentResponse, ApiCallIntent, ToolCallIntent, parse_agent_line


def test_parse_api_intent():
    """Test parse api intent."""
    line = '{"type":"api_call_intent","provider":"demo","endpoint":"/x","estimated_cost_usd":0.1,"reason":"r"}'
    parsed = parse_agent_line(line)
    assert isinstance(parsed, ApiCallIntent)
    assert parsed.provider == "demo"


def test_parse_agent_response():
    """Test parse agent response."""
    line = '{"status":"ok","result":"done","memory_updates":[],"artifacts":[],"metrics":{"latency_ms":1.2}}'
    parsed = parse_agent_line(line)
    assert isinstance(parsed, AgentResponse)
    assert parsed.status == "ok"


def test_parse_invalid():
    """Test parse invalid."""
    with pytest.raises(ValueError):
        parse_agent_line("not json")


def test_parse_tool_intent():
    """Test parse tool intent."""
    line = '{"type":"tool_call_intent","tool":"terminal","args":{"cmd":"echo hi"},"reason":"demo"}'
    parsed = parse_agent_line(line)
    assert isinstance(parsed, ToolCallIntent)
    assert parsed.tool == "terminal"


def test_parse_empty_line():
    """parse_agent_line raises ValueError on empty input."""
    with pytest.raises(ValueError, match="empty agent output"):
        parse_agent_line("")
    with pytest.raises(ValueError, match="empty agent output"):
        parse_agent_line("   \n")


def test_parse_without_type_field_agent_response():
    """Backward compat: AgentResponse without explicit type field still parses via fallback."""
    line = '{"status":"ok","result":"done","memory_updates":[],"artifacts":[],"metrics":{"latency_ms":1.0}}'
    parsed = parse_agent_line(line)
    assert isinstance(parsed, AgentResponse)
    assert parsed.result == "done"


def test_parse_malformed_json():
    """Completely broken JSON raises ValueError."""
    with pytest.raises(ValueError):
        parse_agent_line("{invalid json!!}")


def test_coordinator_request_defaults():
    """CoordinatorRequest fills defaults for optional fields."""
    from closed_claw.runtime.protocol import CoordinatorRequest
    req = CoordinatorRequest(session_id="s1", task="do something")
    assert req.context == {}
    assert req.artifacts == []
    assert req.config == {}


def test_coordinator_request_roundtrip():
    """CoordinatorRequest survives JSON serialization round-trip."""
    from closed_claw.runtime.protocol import CoordinatorRequest
    req = CoordinatorRequest(
        session_id="s1",
        task="test task",
        config={"llm": {"provider": "openai"}},
    )
    json_str = req.model_dump_json()
    restored = CoordinatorRequest.model_validate_json(json_str)
    assert restored.session_id == "s1"
    assert restored.config["llm"]["provider"] == "openai"

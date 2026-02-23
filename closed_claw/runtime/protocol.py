from __future__ import annotations

from typing import Any, Literal

from closed_claw.compat import BaseModel, Field


class CoordinatorRequest(BaseModel):
    session_id: str
    task: str
    context: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class ApiCallIntent(BaseModel):
    type: Literal["api_call_intent"] = "api_call_intent"
    call_type: str = "external_paid_api"
    provider: str
    endpoint: str
    estimated_cost_usd: float
    reason: str


class ApiCallDecision(BaseModel):
    type: Literal["api_call_decision"] = "api_call_decision"
    approved: bool
    note: str = ""


class ToolCallIntent(BaseModel):
    type: Literal["tool_call_intent"] = "tool_call_intent"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class ToolCallResult(BaseModel):
    type: Literal["tool_call_result"] = "tool_call_result"
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class AgentMetrics(BaseModel):
    latency_ms: float | None = None


class AgentResponse(BaseModel):
    status: Literal["ok", "error"]
    result: str = ""
    memory_updates: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metrics: AgentMetrics = Field(default_factory=AgentMetrics)
    error_code: str | None = None
    error_message: str | None = None


def parse_agent_line(line: str) -> ApiCallIntent | ToolCallIntent | AgentResponse:
    data = line.strip()
    if not data:
        raise ValueError("empty agent output")
    try:
        return ApiCallIntent.model_validate_json(data)
    except Exception:
        pass
    try:
        return ToolCallIntent.model_validate_json(data)
    except Exception as intent_err:
        try:
            return AgentResponse.model_validate_json(data)
        except Exception as response_err:
            raise ValueError(
                f"Invalid agent protocol line: intent={intent_err}, response={response_err}"
            ) from response_err

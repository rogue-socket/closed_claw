# Purpose: Unit tests for agent profile generation.

from __future__ import annotations

from pathlib import Path

from closed_claw.config import Settings
from closed_claw.registry.search import generate_agent_profile, generate_task_plan


def _settings(
    provider: str = "heuristic",
    generic_key: str = "",
    openai_key: str = "",
) -> Settings:
    """Test settings."""
    return Settings(
        db_path=Path(".closed_claw/registry.db"),
        agents_dir=Path("agents"),
        run_logs_dir=Path(".closed_claw/runs"),
        embedding_model="all-MiniLM-L6-v2",
        embedding_dim=384,
        low_confidence_threshold=0.62,
        create_approval_required=True,
        create_approval_mode="interactive",
        api_approval_mode="interactive",
        paid_api_providers={"demo-llm"},
        api_approval_timeout_sec=30,
        agent_timeout_sec=120,
        agent_retries=2,
        circuit_breaker_failures=3,
        circuit_breaker_reset_sec=120,
        task_pool_poll_interval_sec=30,
        require_sqlite_vec=False,
        llm_provider=provider,
        llm_model="model-x",
        llm_timeout_sec=45,
        llm_api_key=generic_key,
        openai_api_key=openai_key,
        gemini_api_key="",
        anthropic_api_key="",
        openai_base_url="https://api.openai.com",
        gemini_base_url="https://generativelanguage.googleapis.com",
        anthropic_base_url="https://api.anthropic.com",
        extra_allowed_paths=[],
    )


def test_generate_agent_profile_heuristic_is_task_driven():
    """Test generate agent profile heuristic is task driven."""
    task = "Build terraform aws networking modules and validate deployment plan"
    profile = generate_agent_profile(
        settings=_settings(provider="heuristic"),
        task=task,
        supported_tools=["terminal", "file_io", "python_exec", "http_api"],
        fallback_tools=["terminal", "file_io"],
    )
    assert profile["name_prefix"] != "General Terminal Operator"
    assert "Operator" in profile["name_prefix"]
    assert "terraform" in profile["description"].lower()
    assert "terminal" in profile["tools_allowlist"]
    assert profile["profile_id"]


def test_generate_agent_profile_llm_output_sanitized(monkeypatch):
    """Test generate agent profile llm output sanitized."""
    def _fake_generate(**_: object) -> str:
        """Test fake generate."""
        return (
            '{"profile_id":"Cloud Architect!",'
            '"name_prefix":"cloud architect",'
            '"description":"Design cloud infra and automate provisioning.",'
            '"tools_allowlist":["terminal","totally_fake_tool"],'
            '"tags":["Auto","Capability","Cloud"],'
            '"skill_md":"Focus on reusable infra patterns."}'
        )

    monkeypatch.setattr("closed_claw.registry.search._generate_text_with_provider", _fake_generate)

    profile = generate_agent_profile(
        settings=_settings(provider="openai", openai_key="k"),
        task="Design reusable cloud infrastructure automation workflows",
        supported_tools=["terminal", "file_io", "python_exec"],
        fallback_tools=["file_io"],
    )
    assert profile["name_prefix"] == "Cloud Architect"
    assert profile["profile_id"] == "cloud-architect"
    assert profile["tools_allowlist"] == ["terminal"]
    assert "auto" in profile["tags"]
    assert "capability" in profile["tags"]
    assert profile["skill_md"].startswith("# ")


def test_generate_task_plan_without_llm_uses_default_single_task():
    """Test generate task plan without llm uses default single task."""
    task = (
        "Go to /tmp/project-alpha, inspect files there, read instructions, "
        "write python code from them, and save it in that directory."
    )
    plan = generate_task_plan(settings=_settings(provider="heuristic"), task=task)

    assert len(plan) == 1
    assert plan[0]["task_id"] == "execute-task"
    assert plan[0]["role_tag"] == "task-operator"


def test_generate_task_plan_discovery_phase_without_llm_uses_default_discovery_task():
    """Test generate task plan discovery phase without llm uses default discovery task."""
    plan = generate_task_plan(
        settings=_settings(provider="heuristic"),
        task="Gather context before implementing a file task.",
        phase="discovery",
    )
    assert len(plan) == 1
    assert plan[0]["task_id"] == "collect-required-context"
    assert plan[0]["role_tag"] == "context-discoverer"
    assert plan[0]["requires_tool"] is True

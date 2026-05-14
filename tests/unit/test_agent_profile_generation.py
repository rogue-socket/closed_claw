# Purpose: Unit tests for agent profile generation.

from __future__ import annotations

from pathlib import Path

import pytest

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
        siemens_api_key="",
        openai_base_url="https://api.openai.com",
        gemini_base_url="https://generativelanguage.googleapis.com",
        anthropic_base_url="https://api.anthropic.com",
        siemens_base_url="https://api.siemens.com/llm",
        extra_allowed_paths=[],
    )


def test_generate_agent_profile_heuristic_raises():
    """generate_agent_profile must raise when provider is heuristic."""
    with pytest.raises(ValueError, match="LLM provider required"):
        generate_agent_profile(
            settings=_settings(provider="heuristic"),
            task="Build terraform aws networking modules and validate deployment plan",
            supported_tools=["terminal", "file_io", "python_exec", "http_api"],
            fallback_tools=["terminal", "file_io"],
        )


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


def test_generate_agent_profile_prompt_includes_tool_constraints(monkeypatch):
    """The profile-creation prompt must include tool descriptions and constraints,
    not just bare tool names — otherwise the LLM hallucinates capabilities into
    permanent skill_md (e.g., assuming sql_query supports DDL/INSERT)."""
    captured = {}

    def _fake_generate(**kwargs: object) -> str:
        captured["prompt"] = kwargs.get("prompt", "")
        return (
            '{"profile_id":"x","name_prefix":"x","description":"x",'
            '"tools_allowlist":["sql_query"],"tags":["auto","capability"],'
            '"skill_md":"x"}'
        )

    monkeypatch.setattr("closed_claw.registry.search._generate_text_with_provider", _fake_generate)
    generate_agent_profile(
        settings=_settings(provider="openai", openai_key="k"),
        task="Count rows in a sqlite table.",
        supported_tools=["sql_query", "file_io", "python_exec"],
        fallback_tools=["python_exec"],
    )
    prompt = captured["prompt"]
    assert "Tool catalog" in prompt
    assert "SELECT" in prompt, "sql_query SELECT-only constraint missing from prompt"
    assert "args_schema" in prompt
    assert "escapes are blocked" in prompt, "file_io workspace constraint missing"
    assert "stdin is closed" in prompt, "python_exec constraint missing"


def test_generate_task_plan_heuristic_raises():
    """generate_task_plan must raise when provider is heuristic."""
    with pytest.raises(ValueError, match="LLM provider required"):
        generate_task_plan(
            settings=_settings(provider="heuristic"),
            task="Go to /tmp/project-alpha, inspect and write python code.",
        )


def test_generate_task_plan_discovery_phase_heuristic_raises():
    """generate_task_plan must raise for discovery phase when provider is heuristic."""
    with pytest.raises(ValueError, match="LLM provider required"):
        generate_task_plan(
            settings=_settings(provider="heuristic"),
            task="Gather context before implementing a file task.",
            phase="discovery",
        )


def test_canonical_tags_collapse_drift(monkeypatch):
    """Two LLM responses with drifted free-text tags but same intent and tools
    should end up sharing canonical role + tool tags after normalization.
    This is what the reuse tag-overlap filter relies on."""
    drift_a = (
        '{"profile_id":"p","name_prefix":"x","description":"Lists directory contents.",'
        '"tools_allowlist":["terminal","file_io"],"tags":["filesystem-navigator"],'
        '"skill_md":"x"}'
    )
    drift_b = (
        '{"profile_id":"p","name_prefix":"x","description":"Enumerates files in a folder.",'
        '"tools_allowlist":["terminal","file_io"],"tags":["folder-enumerator"],'
        '"skill_md":"x"}'
    )

    monkeypatch.setattr(
        "closed_claw.registry.search._generate_text_with_provider",
        lambda **_: drift_a,
    )
    pa = generate_agent_profile(
        settings=_settings(provider="openai", openai_key="k"),
        task="List the files in /tmp",
        supported_tools=["terminal", "file_io"],
        fallback_tools=["terminal"],
    )

    monkeypatch.setattr(
        "closed_claw.registry.search._generate_text_with_provider",
        lambda **_: drift_b,
    )
    pb = generate_agent_profile(
        settings=_settings(provider="openai", openai_key="k"),
        task="Enumerate top-level items under /Users/foo",
        supported_tools=["terminal", "file_io"],
        fallback_tools=["terminal"],
    )

    # Free-text tags drift — confirm
    assert "filesystem-navigator" in pa["tags"]
    assert "folder-enumerator" in pb["tags"]
    assert "filesystem-navigator" not in pb["tags"]

    # Canonical tags converge — both should carry fs.list + tool.* fingerprints
    assert "fs.list" in pa["tags"]
    assert "fs.list" in pb["tags"]
    assert "tool.terminal" in pa["tags"]
    assert "tool.terminal" in pb["tags"]
    assert "tool.file_io" in pa["tags"]
    assert "tool.file_io" in pb["tags"]

    # Reuse-gate's tag-overlap filter would now pass between these two profiles
    noise = {"auto", "capability"}
    overlap = (set(pa["tags"]) - noise) & (set(pb["tags"]) - noise)
    assert overlap, f"expected non-empty overlap after canonicalization, got {overlap}"

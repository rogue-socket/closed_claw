from __future__ import annotations

from closed_claw.config import Settings
from closed_claw.registry.search import HeuristicReranker, LLMReranker, build_reranker


def _settings(provider: str, generic_key: str = "", openai_key: str = "", gemini_key: str = "", anthropic_key: str = "") -> Settings:
    return Settings(
        db_path=__import__("pathlib").Path(".closed_claw/registry.db"),
        agents_dir=__import__("pathlib").Path("agents"),
        run_logs_dir=__import__("pathlib").Path(".closed_claw/runs"),
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
        gemini_api_key=gemini_key,
        anthropic_api_key=anthropic_key,
        openai_base_url="https://api.openai.com",
        gemini_base_url="https://generativelanguage.googleapis.com",
        anthropic_base_url="https://api.anthropic.com",
        extra_allowed_paths=[],
    )


def test_default_heuristic():
    rr = build_reranker(_settings("heuristic"))
    assert isinstance(rr, HeuristicReranker)


def test_openai_without_key_falls_back():
    rr = build_reranker(_settings("openai"))
    assert isinstance(rr, HeuristicReranker)


def test_openai_with_key_uses_llm():
    rr = build_reranker(_settings("openai", openai_key="k"))
    assert isinstance(rr, LLMReranker)


def test_gemini_with_generic_key_uses_llm():
    rr = build_reranker(_settings("gemini", generic_key="k"))
    assert isinstance(rr, LLMReranker)


def test_claude_with_key_uses_llm():
    rr = build_reranker(_settings("claude", anthropic_key="k"))
    assert isinstance(rr, LLMReranker)

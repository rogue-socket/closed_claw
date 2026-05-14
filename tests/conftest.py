# Purpose: Shared test fixtures used across the test suite.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from closed_claw.config import Settings


@pytest.fixture(autouse=True)
def _restore_environ():
    """Snapshot ``os.environ`` per test and restore after.

    ``Settings.from_env`` propagates dotenv values into ``os.environ`` via
    ``setdefault``. Once leaked, subsequent ``from_env`` calls in the same
    process see the previously-set value and ignore their own ``.env``,
    which makes any test that simulates a different ``.env`` flaky.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def make_test_settings(tmp_path: Path, **overrides) -> Settings:
    """Create a minimal Settings instance rooted in *tmp_path*.

    Any keyword argument is forwarded as a field override so individual
    tests can customise provider, keys, etc. without re-specifying all
    the boilerplate fields.
    """
    defaults = dict(
        db_path=tmp_path / "registry.db",
        agents_dir=tmp_path / "agents",
        run_logs_dir=tmp_path / "runs",
        embedding_model="all-MiniLM-L6-v2",
        embedding_dim=8,
        low_confidence_threshold=0.62,
        create_approval_required=True,
        create_approval_mode="approve",
        api_approval_mode="approve",
        paid_api_providers=set(),
        api_approval_timeout_sec=30,
        agent_timeout_sec=30,
        agent_retries=0,
        circuit_breaker_failures=3,
        circuit_breaker_reset_sec=120,
        task_pool_poll_interval_sec=1,
        require_sqlite_vec=False,
        llm_provider="heuristic",
        llm_model="local-heuristic",
        llm_timeout_sec=10,
        llm_api_key="",
        openai_api_key="",
        gemini_api_key="",
        anthropic_api_key="",
        siemens_api_key="",
        openai_base_url="https://api.openai.com",
        gemini_base_url="https://generativelanguage.googleapis.com",
        anthropic_base_url="https://api.anthropic.com",
        siemens_base_url="https://api.siemens.com/llm",
        extra_allowed_paths=[],
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Convenience fixture returning default test Settings in a temp dir."""
    return make_test_settings(tmp_path)

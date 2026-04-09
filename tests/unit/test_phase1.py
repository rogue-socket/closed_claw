# Purpose: Unit tests for Phase 1 changes — HTML stripping, profile enrichment,
# success-rate weighting, and task complexity classification.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from closed_claw.config import Settings
from closed_claw.registry.search import (
    _normalize_profile_payload,
    _narrow_tools_by_task,
    classify_task_complexity,
)
from closed_claw.registry.store import AgentManifest, RegistryStore, SearchCandidate
from closed_claw.tools.executor import ToolExecutor, _HTMLTextExtractor


# ═══════════════════════════════════════════════════════════════════════════════
# Change 1: web_fetch HTML stripping
# ═══════════════════════════════════════════════════════════════════════════════


class TestHTMLTextExtractor:
    def test_strips_tags(self):
        html = "<p>Hello <b>world</b></p>"
        text = _HTMLTextExtractor().extract(html)
        assert "Hello" in text
        assert "world" in text
        assert "<p>" not in text
        assert "<b>" not in text

    def test_removes_script_and_style(self):
        html = (
            "<html><head><style>body{color:red}</style></head>"
            "<body><script>alert('x')</script><p>Content here</p></body></html>"
        )
        text = _HTMLTextExtractor().extract(html)
        assert "Content here" in text
        assert "alert" not in text
        assert "color:red" not in text

    def test_respects_max_chars(self):
        html = "<p>" + "x" * 20000 + "</p>"
        text = _HTMLTextExtractor().extract(html, max_chars=100)
        assert len(text) <= 100

    def test_collapses_whitespace(self):
        html = "<p>  lots   of    spaces  </p>"
        text = _HTMLTextExtractor().extract(html)
        assert "  " not in text or text.count("  ") == 0
        assert "lots of spaces" in text

    def test_plain_text_passthrough(self):
        text = _HTMLTextExtractor().extract("No HTML here, just text")
        assert "No HTML here" in text

    def test_real_page_structure(self):
        html = """<!DOCTYPE html>
        <html><head><title>Math</title>
        <style>.nav{display:flex}</style>
        <script>window.onload=function(){}</script>
        </head><body>
        <nav><a href="/">Home</a><a href="/docs">Docs</a></nav>
        <article>
        <h1>math.sqrt</h1>
        <p>Return the square root of x.</p>
        </article>
        <footer>Copyright 2026</footer>
        </body></html>"""
        text = _HTMLTextExtractor().extract(html)
        assert "math.sqrt" in text
        assert "square root" in text
        assert "window.onload" not in text
        assert "display:flex" not in text


# ═══════════════════════════════════════════════════════════════════════════════
# Change 2: Enriched profile generation — tool narrowing
# ═══════════════════════════════════════════════════════════════════════════════

ALL_TOOLS = ["terminal", "http_api", "web_fetch", "file_io", "python_exec", "sql_query"]


class TestToolNarrowing:
    def test_file_task_gets_file_io(self):
        tools = _narrow_tools_by_task("Read the files in this directory", ALL_TOOLS)
        assert "file_io" in tools
        assert len(tools) <= 3

    def test_web_task_gets_web_tools(self):
        tools = _narrow_tools_by_task("Fetch the webpage at https://example.com", ALL_TOOLS)
        assert "web_fetch" in tools or "http_api" in tools
        assert len(tools) <= 3

    def test_code_task_gets_python(self):
        tools = _narrow_tools_by_task("Run this python script and check output", ALL_TOOLS)
        assert "python_exec" in tools
        assert len(tools) <= 3

    def test_sql_task_gets_sql(self):
        tools = _narrow_tools_by_task("Query the database for user records", ALL_TOOLS)
        assert "sql_query" in tools
        assert len(tools) <= 3

    def test_terminal_task_gets_terminal(self):
        tools = _narrow_tools_by_task("Install the package using pip in the terminal", ALL_TOOLS)
        assert "terminal" in tools
        assert len(tools) <= 3

    def test_unknown_task_gets_default_set(self):
        tools = _narrow_tools_by_task("Do something unrelated", ALL_TOOLS)
        assert len(tools) <= 3
        assert len(tools) > 0

    def test_normalize_narrows_fallback_profile(self):
        """When LLM returns no tools and fallback kicks in, narrowing should apply."""
        profile = _normalize_profile_payload(
            payload={"name_prefix": "Task Operator", "tools_allowlist": []},
            task="Read the README file and summarize it",
            supported_tools=ALL_TOOLS,
            fallback_tools=ALL_TOOLS,
        )
        # Should NOT have all 6 tools
        assert len(profile["tools_allowlist"]) <= 3
        assert "file_io" in profile["tools_allowlist"]


# ═══════════════════════════════════════════════════════════════════════════════
# Change 3: Success-rate weighting and agent degradation
# ═══════════════════════════════════════════════════════════════════════════════


def test_update_agent_status(tmp_path: Path):
    """RegistryStore.update_agent_status changes the status field."""
    schema_path = Path(__file__).resolve().parents[2] / "closed_claw/registry/schema.sql"
    store = RegistryStore(
        db_path=tmp_path / "test.db",
        schema_path=schema_path,
        embedding_dim=4,
        require_sqlite_vec=False,
    )
    manifest = AgentManifest(
        agent_id="agent-1",
        name="Test",
        description="test",
        embedding_model="m",
        embedding_vector=[0.1, 0.2, 0.3, 0.4],
        created_at="2026-01-01T00:00:00",
        status="active",
    )
    store.upsert_manifest(manifest)

    store.update_agent_status("agent-1", "degraded")
    updated = store.get_manifest("agent-1")
    assert updated is not None
    assert updated.status == "degraded"


def test_degraded_agent_skipped_in_search(tmp_path: Path):
    """Semantic search only returns active agents, not degraded ones."""
    schema_path = Path(__file__).resolve().parents[2] / "closed_claw/registry/schema.sql"
    store = RegistryStore(
        db_path=tmp_path / "test.db",
        schema_path=schema_path,
        embedding_dim=4,
        require_sqlite_vec=False,
    )
    # Create two agents with the same embedding
    for aid, status in [("good", "active"), ("bad", "degraded")]:
        m = AgentManifest(
            agent_id=aid,
            name=f"Agent {aid}",
            description="test agent",
            embedding_model="m",
            embedding_vector=[0.5, 0.5, 0.5, 0.5],
            created_at="2026-01-01T00:00:00",
            status="active",
        )
        store.upsert_manifest(m)
    store.update_agent_status("bad", "degraded")

    # Search should only find the active agent (fallback cosine path)
    results = store.semantic_search([0.5, 0.5, 0.5, 0.5], k=10)
    agent_ids = [r.agent_id for r in results]
    assert "good" in agent_ids
    assert "bad" not in agent_ids


# ═══════════════════════════════════════════════════════════════════════════════
# Change 4: Task complexity classifier
# ═══════════════════════════════════════════════════════════════════════════════


def test_classify_task_complexity_heuristic_raises(monkeypatch, tmp_path: Path):
    """Heuristic provider should raise ValueError."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CLOSED_CLAW_LLM_PROVIDER=heuristic\n", encoding="utf-8")
    settings = Settings.from_env()
    with pytest.raises(ValueError, match="LLM provider required"):
        classify_task_complexity(settings, "list files")


def test_classify_task_complexity_returns_complex_on_failure(monkeypatch, tmp_path: Path):
    """On LLM failure, classifier should default to 'complex' (safe default)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CLOSED_CLAW_LLM_PROVIDER=openai\nOPENAI_API_KEY=test-key\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()

    with patch("closed_claw.registry.search._generate_text_with_provider", side_effect=RuntimeError("fail")):
        result = classify_task_complexity(settings, "analyze the entire codebase")
    assert result == "complex"


def test_classify_task_complexity_parses_simple(monkeypatch, tmp_path: Path):
    """When LLM returns 'simple', classifier should return 'simple'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CLOSED_CLAW_LLM_PROVIDER=openai\nOPENAI_API_KEY=test-key\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()

    with patch(
        "closed_claw.registry.search._generate_text_with_provider",
        return_value='{"complexity": "simple"}',
    ):
        result = classify_task_complexity(settings, "list files in current directory")
    assert result == "simple"


def test_classify_task_complexity_parses_complex(monkeypatch, tmp_path: Path):
    """When LLM returns 'complex', classifier should return 'complex'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "CLOSED_CLAW_LLM_PROVIDER=openai\nOPENAI_API_KEY=test-key\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()

    with patch(
        "closed_claw.registry.search._generate_text_with_provider",
        return_value='{"complexity": "complex"}',
    ):
        result = classify_task_complexity(settings, "analyze all dependencies and find vulnerabilities")
    assert result == "complex"

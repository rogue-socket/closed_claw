# Purpose: Unit tests for RegistryStore and cosine similarity.

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from closed_claw.registry.store import AgentManifest, RegistryStore, _cosine_similarity


def _schema_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "closed_claw" / "registry" / "schema.sql"


def _make_store(tmp_path: Path) -> RegistryStore:
    db_path = tmp_path / "registry.db"
    return RegistryStore(
        db_path=db_path,
        schema_path=_schema_path(),
        embedding_dim=3,
        require_sqlite_vec=False,
    )


def _make_manifest(agent_id: str = "test-agent-0001", embedding: list[float] | None = None) -> AgentManifest:
    return AgentManifest(
        agent_id=agent_id,
        name="Test Agent",
        description="A test agent",
        embedding_model="test",
        embedding_vector=embedding or [1.0, 0.0, 0.0],
        tools_allowlist=["terminal"],
        tags=["test"],
        api_capabilities=[],
        requires_approval_for=[],
        created_at=datetime.now(UTC).isoformat(),
    )


# ── cosine_similarity ────────────────────────────────────────────────────────


def test_cosine_similarity_identical_vectors():
    """Identical vectors have similarity 1.0."""
    assert _cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 1.0


def test_cosine_similarity_orthogonal_vectors():
    """Orthogonal vectors have similarity 0.0."""
    assert _cosine_similarity([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) == 0.0


def test_cosine_similarity_mismatched_dims():
    """Mismatched dimensions return 0.0 instead of silently truncating."""
    result = _cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])
    assert result == 0.0


def test_cosine_similarity_zero_vectors():
    """Zero vector returns 0.0."""
    assert _cosine_similarity([0.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_cosine_similarity_empty():
    """Empty vectors return 0.0."""
    assert _cosine_similarity([], [1.0, 2.0]) == 0.0
    assert _cosine_similarity([], []) == 0.0


# ── record_run ───────────────────────────────────────────────────────────────


def test_record_run_updates_agent_stats(tmp_path: Path):
    """record_run atomically increments usage_count and success/failure counts."""
    store = _make_store(tmp_path)
    manifest = _make_manifest()
    store.upsert_manifest(manifest)

    store.record_run("run-1", manifest.agent_id, "task 1", "ok", 100.0)
    store.record_run("run-2", manifest.agent_id, "task 2", "error", 200.0)

    updated = store.get_manifest(manifest.agent_id)
    assert updated is not None
    assert updated.usage_count == 2
    assert updated.success_count == 1
    assert updated.failure_count == 1
    assert updated.avg_latency_ms is not None


def test_record_run_preserves_created_at(tmp_path: Path):
    """Re-recording a run with the same run_id preserves the original created_at."""
    store = _make_store(tmp_path)
    manifest = _make_manifest()
    store.upsert_manifest(manifest)

    store.record_run("run-1", manifest.agent_id, "task", "ok", 100.0)

    import sqlite3
    conn = sqlite3.connect(tmp_path / "registry.db")
    conn.row_factory = sqlite3.Row
    row1 = conn.execute("SELECT created_at FROM runs WHERE run_id = 'run-1'").fetchone()
    original_ts = row1["created_at"]

    time.sleep(0.05)

    store.record_run("run-1", manifest.agent_id, "task", "error", 200.0)

    row2 = conn.execute("SELECT created_at, status FROM runs WHERE run_id = 'run-1'").fetchone()
    conn.close()

    assert row2["created_at"] == original_ts
    assert row2["status"] == "error"


# ── delete_agent ─────────────────────────────────────────────────────────────


def test_delete_agent_returns_false_for_nonexistent(tmp_path: Path):
    """delete_agent returns False for an agent that doesn't exist."""
    store = _make_store(tmp_path)
    assert store.delete_agent("no-such-agent") is False


def test_delete_agent_removes_from_registry(tmp_path: Path):
    """delete_agent removes the agent from the agents table."""
    store = _make_store(tmp_path)
    manifest = _make_manifest()
    store.upsert_manifest(manifest)
    assert store.get_manifest(manifest.agent_id) is not None
    result = store.delete_agent(manifest.agent_id)
    assert result is True
    assert store.get_manifest(manifest.agent_id) is None


# ── semantic_search fallback ─────────────────────────────────────────────────


def test_semantic_search_fallback_ordering(tmp_path: Path):
    """Fallback semantic search ranks closer embeddings higher."""
    store = _make_store(tmp_path)

    close_agent = _make_manifest("close-agent-0001", embedding=[0.9, 0.1, 0.0])
    far_agent = _make_manifest("far-agent-0001", embedding=[0.0, 0.0, 1.0])

    store.upsert_manifest(close_agent)
    store.upsert_manifest(far_agent)

    results = store.semantic_search([1.0, 0.0, 0.0], k=5)
    assert len(results) == 2
    assert results[0].agent_id == "close-agent-0001"
    assert results[1].agent_id == "far-agent-0001"
    assert results[0].score > results[1].score


# ── upsert and get manifest ─────────────────────────────────────────────────


def test_upsert_and_get_manifest_roundtrip(tmp_path: Path):
    """Upserting then getting a manifest returns matching fields."""
    store = _make_store(tmp_path)
    manifest = _make_manifest()
    store.upsert_manifest(manifest)
    retrieved = store.get_manifest(manifest.agent_id)
    assert retrieved is not None
    assert retrieved.agent_id == manifest.agent_id
    assert retrieved.name == manifest.name
    assert retrieved.description == manifest.description
    assert retrieved.tools_allowlist == manifest.tools_allowlist
    assert retrieved.tags == manifest.tags

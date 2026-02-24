# Purpose: Unit tests for registry audit.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from closed_claw.policy.audit import AuditStore
from closed_claw.registry.store import AgentManifest, RegistryStore


def test_registry_and_audit(tmp_path: Path):
    """Test registry and audit."""
    db_path = tmp_path / "reg.db"
    schema_path = Path("closed_claw/registry/schema.sql")
    store = RegistryStore(db_path=db_path, schema_path=schema_path, embedding_dim=4, require_sqlite_vec=False)
    manifest = AgentManifest(
        agent_id="agent-1",
        name="Agent",
        description="weather specialist",
        embedding_model="model",
        embedding_vector=[0.1, 0.2, 0.3, 0.4],
        tools_allowlist=["local_fs"],
        tags=["weather"],
        api_capabilities=["external_paid_api"],
        requires_approval_for=["external_paid_api"],
        created_at=datetime.now(UTC).isoformat(),
    )
    store.upsert_manifest(manifest)
    results = store.semantic_search([0.1, 0.2, 0.3, 0.4], k=1)
    assert results
    store.record_run("run-1", "agent-1", "task", "ok", 10.0)

    audit = AuditStore(db_path)
    audit.record_event("test", {"x": 1}, run_id="run-1", agent_id="agent-1")
    assert store.list_agents(limit=5)
    assert store.list_runs(limit=5)


def test_circuit_breaker(tmp_path: Path):
    """Test circuit breaker."""
    db_path = tmp_path / "reg.db"
    schema_path = Path("closed_claw/registry/schema.sql")
    store = RegistryStore(db_path=db_path, schema_path=schema_path, embedding_dim=4, require_sqlite_vec=False)
    assert store.is_circuit_open("demo-llm", 10) is False
    opened = store.open_circuit_if_needed("demo-llm", threshold=1)
    assert opened is True
    assert store.is_circuit_open("demo-llm", 10) is True
    store.reset_circuit("demo-llm")
    assert store.is_circuit_open("demo-llm", 10) is False


def test_delete_agent(tmp_path: Path):
    """Test delete agent."""
    db_path = tmp_path / "reg.db"
    schema_path = Path("closed_claw/registry/schema.sql")
    store = RegistryStore(db_path=db_path, schema_path=schema_path, embedding_dim=4, require_sqlite_vec=False)
    manifest = AgentManifest(
        agent_id="agent-delete",
        name="Agent",
        description="desc",
        embedding_model="model",
        embedding_vector=[0.1, 0.2, 0.3, 0.4],
        tools_allowlist=[],
        tags=[],
        api_capabilities=[],
        requires_approval_for=[],
        created_at=datetime.now(UTC).isoformat(),
    )
    store.upsert_manifest(manifest)
    assert store.get_manifest("agent-delete") is not None
    assert store.delete_agent("agent-delete") is True
    assert store.get_manifest("agent-delete") is None
    assert store.delete_agent("agent-delete") is False


def test_registry_init_without_sqlite_vec_outside_pytest_env(monkeypatch, tmp_path: Path):
    """Test registry init without sqlite vec outside pytest env."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    db_path = tmp_path / "reg.db"
    schema_path = Path(__file__).resolve().parents[2] / "closed_claw/registry/schema.sql"
    store = RegistryStore(
        db_path=db_path,
        schema_path=schema_path,
        embedding_dim=4,
        require_sqlite_vec=False,
    )
    assert store.list_agents(limit=1) == []

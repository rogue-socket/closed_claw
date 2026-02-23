from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from closed_claw.agents.factory import AgentFactory
from closed_claw.cli import cmd_delete_all_agents
from closed_claw.config import Settings
from closed_claw.registry.store import RegistryStore


def test_delete_all_agents(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLOSED_CLAW_DB_PATH", str(tmp_path / ".closed_claw/registry.db"))
    monkeypatch.setenv("CLOSED_CLAW_AGENTS_DIR", "agents")
    monkeypatch.setenv("CLOSED_CLAW_REQUIRE_SQLITE_VEC", "false")
    monkeypatch.setenv("CLOSED_CLAW_EMBEDDING_DIM", "4")

    settings = Settings.from_env()
    settings.ensure_dirs()

    schema_path = Path(__file__).resolve().parents[2] / "closed_claw/registry/schema.sql"
    store = RegistryStore(
        db_path=settings.db_path,
        schema_path=schema_path,
        embedding_dim=settings.embedding_dim,
        require_sqlite_vec=False,
    )
    factory = AgentFactory(settings.agents_dir)

    m1 = factory.create_capsule(
        name="Agent One",
        description="one",
        embedding_model="m",
        embedding_vector=[0.1, 0.2, 0.3, 0.4],
        tools_allowlist=["terminal"],
        tags=[],
        api_capabilities=[],
        requires_approval_for=[],
        skill_content="# a",
    )
    m2 = factory.create_capsule(
        name="Agent Two",
        description="two",
        embedding_model="m",
        embedding_vector=[0.1, 0.2, 0.3, 0.4],
        tools_allowlist=["terminal"],
        tags=[],
        api_capabilities=[],
        requires_approval_for=[],
        skill_content="# b",
    )
    store.upsert_manifest(m1)
    store.upsert_manifest(m2)

    rc = cmd_delete_all_agents(Namespace(yes=True))
    assert rc == 0
    assert not (settings.agents_dir / m1.agent_id).exists()
    assert not (settings.agents_dir / m2.agent_id).exists()
    assert store.list_agents(limit=10) == []

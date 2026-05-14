# Purpose: Regression tests for AgentFactory.sync_registry_index.
#
# The 2026-05-09 sim crashed because two call sites in cli.py and nodes.py
# invoked AgentFactory.sync_registry_index(agents_dir) — a classmethod that
# didn't exist at the time. The fix added the classmethod. These tests lock
# in the contract so the same omission can't happen again.

from __future__ import annotations

import json
from pathlib import Path

from closed_claw.agents.factory import AgentFactory
from closed_claw.registry.store import AgentManifest


def _write_capsule(agents_dir: Path, agent_id: str, **overrides) -> AgentManifest:
    """Write a minimal valid manifest.json under agents_dir/<agent_id>/."""
    capsule = agents_dir / agent_id
    capsule.mkdir(parents=True, exist_ok=True)
    manifest = AgentManifest(
        agent_id=agent_id,
        name=overrides.get("name", agent_id),
        description=overrides.get("description", "test"),
        embedding_model="test",
        embedding_vector=overrides.get("embedding_vector", [0.1, 0.2, 0.3]),
        tools_allowlist=overrides.get("tools_allowlist", []),
        tags=overrides.get("tags", []),
        api_capabilities=[],
        requires_approval_for=[],
        created_at="2026-05-11T00:00:00+00:00",
    )
    (capsule / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


def test_classmethod_exists_with_expected_signature():
    """Regression for the original AttributeError: cli.py and nodes.py both
    call ``AgentFactory.sync_registry_index(agents_dir)`` — keep that surface."""
    assert callable(getattr(AgentFactory, "sync_registry_index", None))


def test_empty_agents_dir_writes_empty_registry(tmp_path: Path):
    """No capsules → registry.json with an empty agents list."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    AgentFactory.sync_registry_index(agents_dir)
    data = json.loads((agents_dir / "registry.json").read_text())
    assert data["version"] == "1.5"
    assert data["agents"] == []


def test_multiple_manifests_all_indexed(tmp_path: Path):
    """Two capsules → both appear in the index."""
    agents_dir = tmp_path / "agents"
    _write_capsule(agents_dir, "agent-aaa")
    _write_capsule(agents_dir, "agent-bbb")
    AgentFactory.sync_registry_index(agents_dir)
    data = json.loads((agents_dir / "registry.json").read_text())
    ids = sorted(a["agent_id"] for a in data["agents"])
    assert ids == ["agent-aaa", "agent-bbb"]


def test_invalid_manifest_is_skipped_silently(tmp_path: Path):
    """An unparseable manifest is skipped; the rest still get indexed.
    Locks in the try/except behaviour at factory.py:128-133."""
    agents_dir = tmp_path / "agents"
    _write_capsule(agents_dir, "agent-good")
    bad_capsule = agents_dir / "agent-bad"
    bad_capsule.mkdir()
    (bad_capsule / "manifest.json").write_text("{ not valid json", encoding="utf-8")
    AgentFactory.sync_registry_index(agents_dir)
    data = json.loads((agents_dir / "registry.json").read_text())
    ids = [a["agent_id"] for a in data["agents"]]
    assert ids == ["agent-good"]


def test_embedding_vector_excluded_from_index(tmp_path: Path):
    """Embedding vectors live on the per-capsule manifest, not in the index —
    otherwise registry.json grows linearly with agents * embedding_dim."""
    agents_dir = tmp_path / "agents"
    _write_capsule(agents_dir, "agent-x", embedding_vector=[0.5] * 384)
    AgentFactory.sync_registry_index(agents_dir)
    data = json.loads((agents_dir / "registry.json").read_text())
    assert "embedding_vector" not in data["agents"][0]


def test_existing_registry_is_overwritten(tmp_path: Path):
    """A stale registry.json from a previous sync is replaced, not merged."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "registry.json").write_text(
        json.dumps({"version": "stale", "agents": [{"agent_id": "ghost"}]}),
        encoding="utf-8",
    )
    _write_capsule(agents_dir, "agent-real")
    AgentFactory.sync_registry_index(agents_dir)
    data = json.loads((agents_dir / "registry.json").read_text())
    assert data["version"] == "1.5"
    ids = [a["agent_id"] for a in data["agents"]]
    assert ids == ["agent-real"]

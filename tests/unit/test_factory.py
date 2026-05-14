# Purpose: Unit tests for AgentFactory capsule creation and registry index.

from __future__ import annotations

import json
from pathlib import Path

from closed_claw.agents.factory import AgentFactory
from closed_claw.registry.store import AgentManifest


def _create_test_capsule(tmp_path: Path, **overrides) -> tuple[AgentFactory, AgentManifest]:
    factory = AgentFactory(tmp_path / "agents")
    defaults = {
        "name": "Test Agent",
        "description": "A test agent for unit tests",
        "embedding_model": "test-model",
        "embedding_vector": [0.1, 0.2, 0.3],
        "tools_allowlist": ["terminal", "file_io"],
        "tags": ["test"],
        "api_capabilities": [],
        "requires_approval_for": [],
        "skill_content": "# Test skill\nYou are a test agent.",
    }
    defaults.update(overrides)
    manifest = factory.create_capsule(**defaults)
    return factory, manifest


def test_create_capsule_files_created(tmp_path: Path):
    """create_capsule creates all expected files in the capsule directory."""
    _, manifest = _create_test_capsule(tmp_path)
    capsule_dir = tmp_path / "agents" / manifest.agent_id

    assert capsule_dir.is_dir()
    assert (capsule_dir / "manifest.json").is_file()
    assert (capsule_dir / "skill.md").is_file()
    assert (capsule_dir / "entrypoint.py").is_file()
    assert (capsule_dir / "memory.db").is_file()
    assert (capsule_dir / "logs").is_dir()


def test_create_capsule_manifest_content(tmp_path: Path):
    """manifest.json contains the correct agent metadata."""
    _, manifest = _create_test_capsule(tmp_path)
    capsule_dir = tmp_path / "agents" / manifest.agent_id

    data = json.loads((capsule_dir / "manifest.json").read_text(encoding="utf-8"))
    assert data["agent_id"] == manifest.agent_id
    assert data["name"] == "Test Agent"
    assert data["description"] == "A test agent for unit tests"
    assert data["tools_allowlist"] == ["terminal", "file_io"]
    assert data["tags"] == ["test"]


def test_create_capsule_with_skill_ids(tmp_path: Path):
    """create_capsule stores skill_ids in the manifest when provided."""
    _, manifest = _create_test_capsule(tmp_path, skill_ids=["terminal", "git"])
    assert manifest.skill_ids == ["terminal", "git"]

    capsule_dir = tmp_path / "agents" / manifest.agent_id
    data = json.loads((capsule_dir / "manifest.json").read_text(encoding="utf-8"))
    assert data["skill_ids"] == ["terminal", "git"]


def test_create_capsule_default_skill_ids(tmp_path: Path):
    """create_capsule defaults skill_ids to empty list when not provided."""
    _, manifest = _create_test_capsule(tmp_path)
    assert manifest.skill_ids == []


def test_save_registry_index(tmp_path: Path):
    """save_registry_index writes a valid JSON index excluding embedding_vector."""
    factory, m1 = _create_test_capsule(tmp_path, name="Agent One")
    _, m2 = _create_test_capsule(tmp_path, name="Agent Two")

    index_path = tmp_path / "agents" / "registry.json"
    AgentFactory.save_registry_index(index_path, [m1, m2])

    data = json.loads(index_path.read_text(encoding="utf-8"))
    assert data["version"] == "1.5"
    assert len(data["agents"]) == 2
    names = {a["name"] for a in data["agents"]}
    assert "Agent One" in names
    assert "Agent Two" in names
    # embedding_vector should be excluded from the index
    for agent in data["agents"]:
        assert "embedding_vector" not in agent


def test_agent_id_generation(tmp_path: Path):
    """Agent IDs are lowercase, space-to-dash, with a 4-char hex suffix."""
    _, manifest = _create_test_capsule(tmp_path, name="My Cool Agent")
    assert manifest.agent_id.startswith("my-cool-agent-")
    suffix = manifest.agent_id.split("-")[-1]
    assert len(suffix) == 4
    int(suffix, 16)  # should not raise — valid hex


def test_entrypoint_version_tag(tmp_path: Path):
    """Generated entrypoint.py contains the current version tag."""
    _, manifest = _create_test_capsule(tmp_path)
    capsule_dir = tmp_path / "agents" / manifest.agent_id
    content = (capsule_dir / "entrypoint.py").read_text(encoding="utf-8")
    assert "CLOSED_CLAW_ENTRYPOINT_VERSION=14" in content
    # Shim delegates to the shared runtime module.
    assert "from closed_claw.runtime.agent_loop import main" in content

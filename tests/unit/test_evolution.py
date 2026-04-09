# Purpose: Tests for the evolutionary agents system — genome, fitness, mutation, selection.

from __future__ import annotations

import json
import os
import random
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Note: do NOT set CLOSED_CLAW_LLM_PROVIDER in os.environ here — it pollutes
# other tests that rely on .env file precedence (e.g. test_phase1.py).

from closed_claw.evolution.genome import Genome, Lineage, STRATEGY_HINTS, PERSONA_TRAITS
from closed_claw.evolution.fitness import FitnessScore, evaluate_fitness, DEFAULT_WEIGHTS
from closed_claw.evolution.mutation import mutate_genome
from closed_claw.evolution.selection import select_ancestor_genome
from closed_claw.registry.store import AgentManifest, RegistryStore


# ---------------------------------------------------------------------------
# Genome tests
# ---------------------------------------------------------------------------


class TestGenome:
    def test_default_genome(self):
        g = Genome()
        assert g.strategy_hint == ""
        assert g.tool_preferences == []
        assert g.temperature == 0.3
        assert g.max_iterations == 12
        assert g.persona_traits == []

    def test_to_dict_from_dict_roundtrip(self):
        g = Genome(
            strategy_hint="step-by-step",
            tool_preferences=["file_io", "terminal"],
            temperature=0.5,
            max_iterations=10,
            persona_traits=["cautious", "precise"],
        )
        d = g.to_dict()
        g2 = Genome.from_dict(d)
        assert g2.strategy_hint == g.strategy_hint
        assert g2.tool_preferences == g.tool_preferences
        assert g2.temperature == g.temperature
        assert g2.max_iterations == g.max_iterations
        assert g2.persona_traits == g.persona_traits

    def test_from_dict_empty(self):
        g = Genome.from_dict({})
        assert g.strategy_hint == ""
        assert g.temperature == 0.3

    def test_hash_deterministic(self):
        g = Genome(strategy_hint="test", temperature=0.5)
        assert g.hash() == g.hash()
        assert len(g.hash()) == 16

    def test_hash_changes_with_mutation(self):
        g1 = Genome(strategy_hint="a", temperature=0.5)
        g2 = Genome(strategy_hint="b", temperature=0.5)
        assert g1.hash() != g2.hash()

    def test_random_genome(self):
        random.seed(42)
        g = Genome.random(tools=["file_io", "terminal", "web_fetch"])
        assert g.strategy_hint in STRATEGY_HINTS
        assert len(g.persona_traits) >= 1
        assert 0.0 <= g.temperature <= 1.0
        assert 4 <= g.max_iterations <= 20
        assert set(g.tool_preferences) == {"file_io", "terminal", "web_fetch"}

    def test_random_genome_no_tools(self):
        g = Genome.random()
        assert g.tool_preferences == []

    def test_apply_to_skill_with_strategy_and_traits(self):
        g = Genome(
            strategy_hint="Think step-by-step.",
            persona_traits=["cautious", "methodical"],
        )
        result = g.apply_to_skill("# Agent Skill\nDo stuff.")
        assert "## Approach" in result
        assert "Think step-by-step." in result
        assert "## Behavioural Traits" in result
        assert "cautious, methodical" in result
        assert "# Agent Skill" in result

    def test_apply_to_skill_empty_genome(self):
        g = Genome()
        result = g.apply_to_skill("original content")
        assert result == "original content"

    def test_apply_to_skill_empty_skill_md(self):
        g = Genome(strategy_hint="Be thorough.")
        result = g.apply_to_skill("")
        assert "## Approach" in result
        assert "Be thorough." in result


# ---------------------------------------------------------------------------
# Lineage tests
# ---------------------------------------------------------------------------


class TestLineage:
    def test_default_lineage(self):
        lin = Lineage()
        assert lin.parent_genome_hash is None
        assert lin.generation == 0
        assert lin.mutations_applied == []

    def test_to_dict_from_dict_roundtrip(self):
        lin = Lineage(
            parent_genome_hash="abc123",
            generation=3,
            mutations_applied=["strategy_hint", "temperature:0.3->0.4"],
        )
        d = lin.to_dict()
        lin2 = Lineage.from_dict(d)
        assert lin2.parent_genome_hash == "abc123"
        assert lin2.generation == 3
        assert len(lin2.mutations_applied) == 2

    def test_from_dict_empty(self):
        lin = Lineage.from_dict({})
        assert lin.generation == 0


# ---------------------------------------------------------------------------
# Fitness tests
# ---------------------------------------------------------------------------


class TestFitness:
    def test_perfect_run(self):
        score = evaluate_fitness(
            task_succeeded=True,
            tool_events=[{"ok": True}, {"ok": True}],
            verification_passed=True,
            latency_ms=5000.0,
        )
        assert score.completion == 1.0
        assert score.tool_efficiency == 1.0
        assert score.verification == 1.0
        assert score.speed == 1.0  # 30000/5000 = 6.0, capped at 1.0

    def test_failed_run(self):
        score = evaluate_fitness(
            task_succeeded=False,
            tool_events=[{"ok": False}, {"ok": True}],
            verification_passed=False,
            latency_ms=60000.0,
        )
        assert score.completion == 0.0
        assert score.tool_efficiency == 0.5
        assert score.verification == 0.0
        assert score.speed == 0.5  # 30000/60000 = 0.5

    def test_no_tool_events(self):
        score = evaluate_fitness(
            task_succeeded=True,
            tool_events=[],
            verification_passed=True,
            latency_ms=None,
        )
        assert score.tool_efficiency == 0.5  # default when no tools
        assert score.speed == 0.5  # default when no latency

    def test_aggregate_default_weights(self):
        score = FitnessScore(
            completion=1.0,
            tool_efficiency=1.0,
            verification=1.0,
            speed=1.0,
        )
        agg = score.aggregate()
        assert abs(agg - 1.0) < 0.001

    def test_aggregate_custom_weights(self):
        score = FitnessScore(completion=1.0, tool_efficiency=0.0, verification=0.0, speed=0.0)
        agg = score.aggregate({"completion": 1.0, "tool_efficiency": 0.0, "verification": 0.0, "speed": 0.0})
        assert abs(agg - 1.0) < 0.001

    def test_aggregate_zero_run(self):
        score = FitnessScore()
        assert score.aggregate() == 0.0


# ---------------------------------------------------------------------------
# Mutation tests
# ---------------------------------------------------------------------------


class TestMutation:
    def test_mutation_produces_child(self):
        random.seed(42)
        parent = Genome(
            strategy_hint="Think step-by-step.",
            tool_preferences=["file_io", "terminal"],
            temperature=0.5,
            max_iterations=12,
            persona_traits=["cautious"],
        )
        child, lineage = mutate_genome(parent, mutation_rate=1.0)
        # Child should differ from parent in at least one gene
        assert lineage.mutations_applied  # at least one mutation happened
        assert lineage.parent_genome_hash == parent.hash()

    def test_mutation_respects_bounds(self):
        random.seed(99)
        parent = Genome(temperature=0.0, max_iterations=4)
        for _ in range(50):
            child, _ = mutate_genome(parent, mutation_rate=1.0)
            assert 0.0 <= child.temperature <= 1.0
            assert 4 <= child.max_iterations <= 20

    def test_zero_mutation_rate_forces_one_change(self):
        parent = Genome(temperature=0.5, max_iterations=12)
        child, lineage = mutate_genome(parent, mutation_rate=0.0)
        # Even with 0 rate, at least one mutation is forced
        assert len(lineage.mutations_applied) >= 1

    def test_mutation_preserves_tool_set(self):
        parent = Genome(tool_preferences=["a", "b", "c"])
        child, _ = mutate_genome(parent, mutation_rate=1.0)
        assert set(child.tool_preferences) == {"a", "b", "c"}

    def test_lineage_generation_defaults_zero(self):
        parent = Genome()
        _, lineage = mutate_genome(parent)
        assert lineage.generation == 0  # caller sets real generation


# ---------------------------------------------------------------------------
# Selection tests (with real RegistryStore)
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    schema = Path(__file__).resolve().parent.parent.parent / "closed_claw" / "registry" / "schema.sql"
    db = tmp_path / "test.db"
    return RegistryStore(db_path=db, schema_path=schema, require_sqlite_vec=False)


class TestSelection:
    def test_no_ancestors(self, store):
        result = select_ancestor_genome(store, "nonexistent-profile")
        assert result is None

    def test_selects_fittest_ancestor(self, store):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        genome_a = Genome(strategy_hint="A", temperature=0.3)
        genome_b = Genome(strategy_hint="B", temperature=0.7)

        # Agent A: fitness 0.4
        manifest_a = AgentManifest(
            agent_id="agent_a",
            name="Agent A",
            description="Test A",
            embedding_model="test",
            embedding_vector=[0.1] * 384,
            tags=["profile-x"],
            created_at=now,
            genome_json=json.dumps(genome_a.to_dict()),
            lineage_json=json.dumps({"generation": 1}),
            fitness_score=0.4,
        )
        store.upsert_manifest(manifest_a)

        # Agent B: fitness 0.8 (should win)
        manifest_b = AgentManifest(
            agent_id="agent_b",
            name="Agent B",
            description="Test B",
            embedding_model="test",
            embedding_vector=[0.2] * 384,
            tags=["profile-x"],
            created_at=now,
            genome_json=json.dumps(genome_b.to_dict()),
            lineage_json=json.dumps({"generation": 2}),
            fitness_score=0.8,
        )
        store.upsert_manifest(manifest_b)

        result = select_ancestor_genome(store, "profile-x")
        assert result is not None
        genome, generation = result
        assert genome.strategy_hint == "B"
        assert genome.temperature == 0.7
        assert generation == 2

    def test_ignores_non_matching_tags(self, store):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        manifest = AgentManifest(
            agent_id="agent_z",
            name="Agent Z",
            description="Wrong tag",
            embedding_model="test",
            embedding_vector=[0.1] * 384,
            tags=["other-profile"],
            created_at=now,
            genome_json=json.dumps(Genome(strategy_hint="Z").to_dict()),
            fitness_score=0.9,
        )
        store.upsert_manifest(manifest)
        assert select_ancestor_genome(store, "profile-x") is None


# ---------------------------------------------------------------------------
# Store integration tests for new evolution fields
# ---------------------------------------------------------------------------


class TestStoreEvolutionFields:
    def test_upsert_and_get_genome_fields(self, store):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        genome = Genome(strategy_hint="Test", temperature=0.42)
        lineage = Lineage(parent_genome_hash="abc", generation=3, mutations_applied=["temp"])

        manifest = AgentManifest(
            agent_id="evo_agent",
            name="Evo Agent",
            description="Evolutionary test",
            embedding_model="test",
            embedding_vector=[0.5] * 384,
            tags=["evo-test"],
            created_at=now,
            genome_json=json.dumps(genome.to_dict()),
            lineage_json=json.dumps(lineage.to_dict()),
            fitness_score=0.75,
        )
        store.upsert_manifest(manifest)
        got = store.get_manifest("evo_agent")
        assert got is not None
        assert got.fitness_score == 0.75

        genome_back = Genome.from_dict(json.loads(got.genome_json))
        assert genome_back.strategy_hint == "Test"
        assert genome_back.temperature == 0.42

        lineage_back = Lineage.from_dict(json.loads(got.lineage_json))
        assert lineage_back.generation == 3
        assert lineage_back.parent_genome_hash == "abc"

    def test_update_fitness(self, store):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        manifest = AgentManifest(
            agent_id="fit_agent",
            name="Fit Agent",
            description="Fitness test",
            embedding_model="test",
            embedding_vector=[0.3] * 384,
            tags=[],
            created_at=now,
            fitness_score=0.0,
        )
        store.upsert_manifest(manifest)
        store.update_fitness("fit_agent", 0.85)
        got = store.get_manifest("fit_agent")
        assert got is not None
        assert abs(got.fitness_score - 0.85) < 0.001

    def test_find_agents_by_tag(self, store):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        for i in range(3):
            manifest = AgentManifest(
                agent_id=f"tag_agent_{i}",
                name=f"Tag Agent {i}",
                description=f"Tag test {i}",
                embedding_model="test",
                embedding_vector=[0.1 * i] * 384,
                tags=["shared-tag"] if i < 2 else ["other-tag"],
                created_at=now,
            )
            store.upsert_manifest(manifest)

        found = store.find_agents_by_tag("shared-tag")
        assert len(found) == 2
        agent_ids = {a["agent_id"] for a in found}
        assert "tag_agent_0" in agent_ids
        assert "tag_agent_1" in agent_ids


# ---------------------------------------------------------------------------
# Factory integration test — genome injection into skill.md
# ---------------------------------------------------------------------------


class TestFactoryGenomeInjection:
    def test_capsule_with_genome_modifies_skill(self, tmp_path):
        from closed_claw.agents.factory import AgentFactory

        factory = AgentFactory(tmp_path / "agents")
        genome = Genome(
            strategy_hint="Be thorough.",
            persona_traits=["cautious", "precise"],
        )
        lineage = Lineage(generation=0)

        manifest = factory.create_capsule(
            name="Evo Test",
            description="Test agent with genome",
            embedding_model="test",
            embedding_vector=[0.1] * 384,
            tools_allowlist=["file_io"],
            tags=["test"],
            api_capabilities=[],
            requires_approval_for=[],
            skill_content="# Base Skill\nDo the thing.",
            genome=genome,
            lineage_dict=lineage.to_dict(),
        )

        # Verify skill.md was modified with genome hints
        skill_path = tmp_path / "agents" / manifest.agent_id / "skill.md"
        skill_text = skill_path.read_text()
        assert "## Approach" in skill_text
        assert "Be thorough." in skill_text
        assert "cautious, precise" in skill_text
        assert "# Base Skill" in skill_text

        # Verify genome is in manifest
        assert manifest.genome_json != "{}"
        genome_back = Genome.from_dict(json.loads(manifest.genome_json))
        assert genome_back.strategy_hint == "Be thorough."

    def test_capsule_without_genome_unchanged(self, tmp_path):
        from closed_claw.agents.factory import AgentFactory

        factory = AgentFactory(tmp_path / "agents")
        manifest = factory.create_capsule(
            name="Plain Test",
            description="No genome",
            embedding_model="test",
            embedding_vector=[0.1] * 384,
            tools_allowlist=[],
            tags=[],
            api_capabilities=[],
            requires_approval_for=[],
            skill_content="# Base Skill",
        )

        skill_path = tmp_path / "agents" / manifest.agent_id / "skill.md"
        assert skill_path.read_text() == "# Base Skill"
        assert manifest.genome_json == "{}"

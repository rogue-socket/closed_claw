# Purpose: Regression tests for CoordinatorNodes._find_reusable_capability_agent.
#
# The reuse gate has historically been the single hardest-to-test piece of the
# orchestrator: it landed broken once (matching on a non-existent profile_id
# tag), so these tests lock in the current D-style behavior — similarity
# threshold + role-tag overlap + tools superset, weighted by success_rate.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from closed_claw.coordinator.nodes import CoordinatorNodes
from closed_claw.registry.store import SearchCandidate


def _manifest(
    agent_id: str,
    tags: list[str],
    tools: list[str],
    *,
    status: str = "active",
    usage_count: int = 5,
    success_rate: float = 0.8,
) -> SimpleNamespace:
    """Lightweight manifest stand-in matching the attributes the gate reads."""
    return SimpleNamespace(
        agent_id=agent_id,
        tags=tags,
        tools_allowlist=tools,
        status=status,
        usage_count=usage_count,
        success_rate=success_rate,
    )


def _node(
    candidates: list[SearchCandidate],
    manifests_by_id: dict[str, SimpleNamespace],
    *,
    threshold: float = 0.62,
) -> SimpleNamespace:
    """Build a self-like object exposing only what the gate needs."""
    return SimpleNamespace(
        embedder=SimpleNamespace(embed=lambda _desc: [0.0]),
        registry=SimpleNamespace(
            semantic_search=lambda _vec, k=10: candidates,
            get_manifest=lambda aid: manifests_by_id.get(aid),
        ),
        settings=SimpleNamespace(low_confidence_threshold=threshold),
    )


def _call(self_like: SimpleNamespace, profile: dict[str, Any]) -> str | None:
    return CoordinatorNodes._find_reusable_capability_agent(self_like, profile)


# ---------------------------------------------------------------------------
# Behaviors


def test_clean_reuse_when_all_gates_pass():
    """Above threshold + role-tag overlap + tools superset → returns the agent."""
    cand = SearchCandidate(agent_id="a1", score=0.9, description="x")
    node = _node([cand], {"a1": _manifest("a1", tags=["filesystem-navigator"], tools=["terminal", "file_io"])})
    profile = {
        "description": "navigate filesystem",
        "tags": ["filesystem-navigator"],
        "tools_allowlist": ["terminal"],
    }
    assert _call(node, profile) == "a1"


def test_similarity_below_threshold_rejects():
    """A candidate whose score is below low_confidence_threshold is dropped."""
    cand = SearchCandidate(agent_id="a1", score=0.50, description="x")
    node = _node(
        [cand],
        {"a1": _manifest("a1", tags=["filesystem-navigator"], tools=["terminal"])},
        threshold=0.62,
    )
    profile = {
        "description": "navigate filesystem",
        "tags": ["filesystem-navigator"],
        "tools_allowlist": ["terminal"],
    }
    assert _call(node, profile) is None


def test_role_tag_mismatch_rejects():
    """No overlap between profile tags (minus noise) and candidate tags → None."""
    cand = SearchCandidate(agent_id="a1", score=0.9, description="x")
    node = _node([cand], {"a1": _manifest("a1", tags=["sql-runner"], tools=["terminal"])})
    profile = {
        "description": "navigate filesystem",
        "tags": ["filesystem-navigator"],
        "tools_allowlist": ["terminal"],
    }
    assert _call(node, profile) is None


def test_missing_required_tool_rejects():
    """Candidate must have every tool the new role requires."""
    cand = SearchCandidate(agent_id="a1", score=0.9, description="x")
    node = _node(
        [cand],
        {"a1": _manifest("a1", tags=["filesystem-navigator"], tools=["terminal"])},
    )
    profile = {
        "description": "navigate filesystem",
        "tags": ["filesystem-navigator"],
        "tools_allowlist": ["terminal", "python_exec"],  # python_exec missing on a1
    }
    assert _call(node, profile) is None


def test_noise_only_profile_tags_skip_tag_filter():
    """When the profile's tags are only the noise tags (auto/capability),
    the role-overlap gate is skipped — otherwise reuse would never fire for
    freshly auto-created agents. Locks in nodes.py:846."""
    cand = SearchCandidate(agent_id="a1", score=0.9, description="x")
    node = _node(
        [cand],
        {"a1": _manifest("a1", tags=["something-unrelated", "auto"], tools=["terminal"])},
    )
    profile = {
        "description": "any task",
        "tags": ["auto", "capability"],  # all noise — strips to empty
        "tools_allowlist": ["terminal"],
    }
    assert _call(node, profile) == "a1"


def test_picks_highest_effective_score_weighted_by_success_rate():
    """When multiple candidates pass, the one with the highest
    score * (0.3 + 0.7 * success_rate) wins."""
    high_score_failing = SearchCandidate(agent_id="failing", score=0.95, description="x")
    lower_score_winning = SearchCandidate(agent_id="winning", score=0.80, description="x")
    node = _node(
        [high_score_failing, lower_score_winning],
        {
            # failing: usage_count >= 2 → real success_rate used (0.0) →
            #   effective = 0.95 * (0.3 + 0.7*0.0) = 0.285
            "failing": _manifest(
                "failing", tags=["role"], tools=["terminal"],
                usage_count=10, success_rate=0.0,
            ),
            # winning: usage_count >= 2 + sr=1.0 → effective = 0.80 * 1.0 = 0.80
            "winning": _manifest(
                "winning", tags=["role"], tools=["terminal"],
                usage_count=10, success_rate=1.0,
            ),
        },
    )
    profile = {"description": "x", "tags": ["role"], "tools_allowlist": ["terminal"]}
    assert _call(node, profile) == "winning"


def test_non_active_status_rejects():
    """Degraded/inactive capsules aren't reusable even if every other gate passes."""
    cand = SearchCandidate(agent_id="a1", score=0.9, description="x")
    node = _node(
        [cand],
        {"a1": _manifest("a1", tags=["role"], tools=["terminal"], status="degraded")},
    )
    profile = {"description": "x", "tags": ["role"], "tools_allowlist": ["terminal"]}
    assert _call(node, profile) is None


def test_empty_candidate_list_returns_none():
    """No semantic-search hits → no reuse."""
    node = _node([], {})
    profile = {"description": "x", "tags": ["role"], "tools_allowlist": ["terminal"]}
    assert _call(node, profile) is None


def test_low_usage_count_uses_neutral_success_rate():
    """An agent with <2 runs is scored with a neutral 0.5 success rate, so it
    can still win against a high-usage failing agent. Locks in nodes.py:852."""
    high_failing = SearchCandidate(agent_id="failing", score=0.95, description="x")
    new_agent = SearchCandidate(agent_id="new", score=0.80, description="x")
    node = _node(
        [high_failing, new_agent],
        {
            # failing: real sr=0.0 → effective 0.95 * 0.3 = 0.285
            "failing": _manifest(
                "failing", tags=["role"], tools=["terminal"],
                usage_count=10, success_rate=0.0,
            ),
            # new: usage_count < 2 → neutral 0.5 → effective 0.80 * 0.65 = 0.52
            "new": _manifest(
                "new", tags=["role"], tools=["terminal"],
                usage_count=1, success_rate=0.0,
            ),
        },
    )
    profile = {"description": "x", "tags": ["role"], "tools_allowlist": ["terminal"]}
    assert _call(node, profile) == "new"

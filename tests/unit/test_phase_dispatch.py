# Purpose: Tests for the discovery/execution dispatch decision in execute_task_pool.

from __future__ import annotations

from closed_claw.coordinator.nodes import CoordinatorNodes


def test_skip_discovery_when_execution_pool_present_and_no_discovery():
    """The simple-classified state (execution pool set, discovery pool empty)
    should skip discovery — historically this fell through and synthesized a
    discovery plan anyway, doubling LLM calls."""
    state = {
        "execution_subtask_pool": [{"task_id": "t1", "title": "x"}],
        "discovery_subtask_pool": [],
        "subtask_pool": [{"task_id": "t1", "title": "x"}],
    }
    assert CoordinatorNodes._should_skip_discovery(state) is True


def test_skip_discovery_for_legacy_single_pool_state():
    """The pre-two-phase shape (only subtask_pool set) is execution-only."""
    state = {
        "subtask_pool": [{"task_id": "t1", "title": "x"}],
        "discovery_subtask_pool": [],
        "execution_subtask_pool": [],
    }
    assert CoordinatorNodes._should_skip_discovery(state) is True


def test_run_discovery_when_discovery_pool_present():
    """Complex-classified state has a discovery pool already — run it."""
    state = {
        "discovery_subtask_pool": [{"task_id": "d1", "title": "d"}],
        "execution_subtask_pool": [],
        "subtask_pool": [{"task_id": "d1", "title": "d"}],
    }
    assert CoordinatorNodes._should_skip_discovery(state) is False


def test_run_discovery_when_no_pools_present():
    """Empty state means execute_task_pool needs to synthesize a discovery plan."""
    state: dict = {
        "discovery_subtask_pool": [],
        "execution_subtask_pool": [],
        "subtask_pool": [],
    }
    assert CoordinatorNodes._should_skip_discovery(state) is False

# Purpose: Tests for the web list_agents row-shaping helper.
#
# The /api/agents endpoint historically renamed the `*_json` columns to their
# plain names without parsing them, so callers got JSON-encoded strings while
# /api/agents/{id} returned actual lists. The frontend defensively handled
# this in one place but not others (chip rendering called .map() on a string
# → TypeError). The helper extracted here parses the JSON columns so the two
# endpoints ship the same contract.

from __future__ import annotations

from closed_claw.web.serializers import shape_agent_row


def test_tags_json_parsed_to_list():
    row = {"agent_id": "a1", "tags_json": '["alpha", "beta"]'}
    shaped = shape_agent_row(row)
    assert shaped["tags"] == ["alpha", "beta"]
    assert "tags_json" not in shaped


def test_all_four_json_columns_parsed():
    """tools_allowlist, api_capabilities, skill_ids also need parsing."""
    row = {
        "agent_id": "a1",
        "tags_json": "[]",
        "tools_allowlist_json": '["terminal", "file_io"]',
        "api_capabilities_json": '["openai_chat"]',
        "skill_ids_json": '["base", "advanced"]',
    }
    shaped = shape_agent_row(row)
    assert shaped["tools_allowlist"] == ["terminal", "file_io"]
    assert shaped["api_capabilities"] == ["openai_chat"]
    assert shaped["skill_ids"] == ["base", "advanced"]
    for encoded in (
        "tags_json",
        "tools_allowlist_json",
        "api_capabilities_json",
        "skill_ids_json",
    ):
        assert encoded not in shaped


def test_missing_columns_default_to_empty_lists():
    """A row without the _json columns shouldn't crash — default to []."""
    row = {"agent_id": "a1"}
    shaped = shape_agent_row(row)
    assert shaped["tags"] == []
    assert shaped["tools_allowlist"] == []
    assert shaped["api_capabilities"] == []
    assert shaped["skill_ids"] == []


def test_invalid_json_falls_back_to_empty_list():
    """Garbled JSON shouldn't crash the endpoint — fall back to []."""
    row = {"agent_id": "a1", "tags_json": "{ not json"}
    shaped = shape_agent_row(row)
    assert shaped["tags"] == []


def test_non_list_json_value_falls_back_to_empty_list():
    """If a column accidentally holds an object/scalar, return [] not the wrong shape."""
    row = {"agent_id": "a1", "tags_json": '{"oops": true}'}
    shaped = shape_agent_row(row)
    assert shaped["tags"] == []


def test_scalar_columns_passed_through_unchanged():
    """Non-_json fields (agent_id, status, etc.) are preserved as-is."""
    row = {
        "agent_id": "a1",
        "name": "Demo",
        "status": "active",
        "usage_count": 7,
        "tags_json": "[]",
    }
    shaped = shape_agent_row(row)
    assert shaped["agent_id"] == "a1"
    assert shaped["name"] == "Demo"
    assert shaped["status"] == "active"
    assert shaped["usage_count"] == 7

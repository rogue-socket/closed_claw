# Purpose: Pure data-shaping helpers for the web API responses.
#
# Lives outside ``server.py`` so it can be imported (and unit-tested) without
# requiring fastapi.

from __future__ import annotations

import json
from typing import Any


def shape_agent_row(row: dict[str, Any]) -> dict[str, Any]:
    """Parse the ``*_json`` columns in a raw ``agents`` row into actual lists.

    The /api/agents list endpoint queries SQL directly (no Pydantic round-trip),
    so its rows arrive with JSON-encoded strings in ``tags_json``,
    ``tools_allowlist_json``, ``api_capabilities_json``, ``skill_ids_json``.
    Returning those raw strings creates a contract mismatch with
    /api/agents/{id} (which returns actual lists) and crashes the frontend
    when it calls ``.map()`` on what it thinks is an array.
    """
    out = dict(row)
    mapping = {
        "tags": "tags_json",
        "tools_allowlist": "tools_allowlist_json",
        "api_capabilities": "api_capabilities_json",
        "skill_ids": "skill_ids_json",
    }
    for plain, encoded in mapping.items():
        raw = out.pop(encoded, "[]") or "[]"
        try:
            value = json.loads(raw)
            out[plain] = value if isinstance(value, list) else []
        except (TypeError, ValueError):
            out[plain] = []
    return out

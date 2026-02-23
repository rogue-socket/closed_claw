from __future__ import annotations

from datetime import UTC, datetime

from closed_claw.registry.store import AgentManifest


def test_manifest_supports_new_fields():
    manifest = AgentManifest(
        agent_id="a1",
        name="Agent A",
        description="desc",
        embedding_model="model",
        embedding_vector=[0.1, 0.2],
        tools_allowlist=["local_fs"],
        tags=["tag"],
        api_capabilities=["external_paid_api"],
        requires_approval_for=["external_paid_api"],
        created_at=datetime.now(UTC).isoformat(),
    )
    assert manifest.api_capabilities == ["external_paid_api"]
    assert manifest.requires_approval_for == ["external_paid_api"]

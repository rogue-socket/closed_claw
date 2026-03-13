# Purpose: Unit tests for setup wizard.

from __future__ import annotations

from pathlib import Path

from closed_claw.setup_wizard import upsert_env, verify_provider


def test_upsert_env(tmp_path: Path):
    """Test upsert env."""
    env = tmp_path / ".env"
    env.write_text("A=1\nB=2\n", encoding="utf-8")
    upsert_env(env, {"B": "22", "C": "3"})
    content = env.read_text(encoding="utf-8")
    assert "A=1" in content
    assert "B=22" in content
    assert "C=3" in content


def test_verify_provider_missing_key():
    """verify_provider must return False when API key is empty."""
    for provider in ("openai", "gemini", "claude", "siemens"):
        ok, msg = verify_provider(provider, "some-model", "")
        assert ok is False, f"Expected False for {provider} with no key"
        assert "missing API key" in msg

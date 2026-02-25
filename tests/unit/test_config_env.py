# Purpose: Unit tests for config env.

from __future__ import annotations

from pathlib import Path

from closed_claw.config import Settings


def test_settings_reads_dotenv(monkeypatch, tmp_path: Path):
    """Test settings reads dotenv."""
    monkeypatch.chdir(tmp_path)
    # Remove any OS-level env vars so the .env file values are not shadowed.
    for _key in [
        "CLOSED_CLAW_LLM_PROVIDER",
        "CLOSED_CLAW_LLM_MODEL",
        "GEMINI_API_KEY",
    ]:
        monkeypatch.delenv(_key, raising=False)
    (tmp_path / ".env").write_text(
        "CLOSED_CLAW_LLM_PROVIDER=gemini\n"
        "CLOSED_CLAW_LLM_MODEL=gemini-2.5-flash-lite\n"
        "GEMINI_API_KEY=test_key\n",
        encoding="utf-8",
    )
    settings = Settings.from_env()
    assert settings.llm_provider == "gemini"
    assert settings.llm_model == "gemini-2.5-flash-lite"
    assert settings.gemini_api_key == "test_key"

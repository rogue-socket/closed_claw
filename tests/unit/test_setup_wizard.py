from __future__ import annotations

from pathlib import Path

from closed_claw.setup_wizard import upsert_env, verify_provider


def test_upsert_env(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("A=1\nB=2\n", encoding="utf-8")
    upsert_env(env, {"B": "22", "C": "3"})
    content = env.read_text(encoding="utf-8")
    assert "A=1" in content
    assert "B=22" in content
    assert "C=3" in content


def test_verify_heuristic():
    ok, msg = verify_provider("heuristic", "local-heuristic", "")
    assert ok is True
    assert "requires no API key" in msg

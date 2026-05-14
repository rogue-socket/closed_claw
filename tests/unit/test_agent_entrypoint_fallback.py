# Purpose: Unit tests for agent entrypoint fallback.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import closed_claw
from closed_claw.agents.factory import ENTRYPOINT_TEMPLATE


def test_generated_entrypoint_requires_llm_for_tool_reasoning(tmp_path: Path):
    """Test generated entrypoint requires llm for tool reasoning."""
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text(ENTRYPOINT_TEMPLATE, encoding="utf-8")

    request = {
        "session_id": "s1",
        "task": "Inspect a folder and generate a python file.",
        "context": {},
        "config": {
            "tool_registry": [{"name": "file_io"}],
            "llm": {"provider": "heuristic", "api_key": "", "model": "local", "base_url": "", "timeout_s": 5},
        },
    }

    # The shim's walk-up won't find the package from tmp_path; pass the
    # project root explicitly via PYTHONPATH so the shim's import resolves.
    project_root = Path(closed_claw.__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(project_root)}

    proc = subprocess.run(
        [sys.executable, str(entrypoint)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]
    final = payloads[-1]
    assert final["status"] == "error"
    assert final["error_code"] == "agent_llm_not_configured"

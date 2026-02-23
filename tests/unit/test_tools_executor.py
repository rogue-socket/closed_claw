from __future__ import annotations

from pathlib import Path

from closed_claw.tools.executor import SUPPORTED_TOOLS, ToolExecutionError, ToolExecutor


def test_terminal_tool(tmp_path: Path):
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute("terminal", {"cmd": "echo hi"}, allowlist=["terminal"])
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]


def test_tool_blocked(tmp_path: Path):
    executor = ToolExecutor(workspace_root=tmp_path)
    try:
        executor.execute("terminal", {"cmd": "echo hi"}, allowlist=[])
    except ToolExecutionError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("Expected ToolExecutionError")


def test_supported_tools_registry():
    assert "terminal" in SUPPORTED_TOOLS
    assert "organize_by_type" not in SUPPORTED_TOOLS

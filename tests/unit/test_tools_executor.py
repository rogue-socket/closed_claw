# Purpose: Unit tests for tools executor.

from __future__ import annotations

from pathlib import Path

from closed_claw.tools.executor import SUPPORTED_TOOLS, ToolExecutionError, ToolExecutor


def test_terminal_tool(tmp_path: Path):
    """Test terminal tool."""
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute("terminal", {"cmd": "echo hi"}, allowlist=["terminal"])
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]


def test_tool_blocked(tmp_path: Path):
    """Test tool blocked."""
    executor = ToolExecutor(workspace_root=tmp_path)
    try:
        executor.execute("terminal", {"cmd": "echo hi"}, allowlist=[])
    except ToolExecutionError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("Expected ToolExecutionError")


def test_file_io_list_directory(tmp_path: Path):
    """Test file io list directory."""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("print('x')\n", encoding="utf-8")

    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute(
        "file_io",
        {"op": "list", "path": str(tmp_path), "recursive": True, "max_entries": 10},
        allowlist=["file_io"],
    )

    assert out["path"] == str(tmp_path)
    assert isinstance(out["entries"], list)
    paths = {item["path"] for item in out["entries"]}
    assert str(tmp_path / "a.txt") in paths
    assert str(tmp_path / "sub" / "b.py") in paths


def test_supported_tools_registry():
    """Test supported tools registry."""
    assert "terminal" in SUPPORTED_TOOLS
    assert "organize_by_type" not in SUPPORTED_TOOLS

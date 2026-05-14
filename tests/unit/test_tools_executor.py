# Purpose: Unit tests for tools executor.

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from closed_claw.tools.executor import (
    SUPPORTED_TOOLS,
    ToolExecutionError,
    ToolExecutor,
    tool_registry_for_allowlist,
)


def test_terminal_tool(tmp_path: Path):
    """Test terminal tool."""
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute("terminal", {"cmd": "echo hi"}, allowlist=["terminal"])
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]


def test_workspace_root_must_exist(tmp_path: Path):
    """ToolExecutor raises a clear error when workspace_root is missing."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError, match="workspace_root does not exist"):
        ToolExecutor(workspace_root=missing)


def test_workspace_root_must_be_directory(tmp_path: Path):
    """ToolExecutor rejects a file as workspace_root."""
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(NotADirectoryError, match="not a directory"):
        ToolExecutor(workspace_root=a_file)


def test_safe_path_expands_workspace_placeholder(tmp_path: Path):
    """`$WORKSPACE` and `${WORKSPACE}` in path args are substituted with the
    resolved workspace root, so LLMs that pass the literal token still get
    valid path arguments."""
    executor = ToolExecutor(workspace_root=tmp_path)
    (tmp_path / "hi.txt").write_text("hello", encoding="utf-8")

    out = executor.execute(
        "file_io",
        {"op": "read", "path": "$WORKSPACE/hi.txt"},
        allowlist=["file_io"],
    )
    assert out["content"] == "hello"

    out2 = executor.execute(
        "file_io",
        {"op": "read", "path": "${WORKSPACE}/hi.txt"},
        allowlist=["file_io"],
    )
    assert out2["content"] == "hello"


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


def test_terminal_arg_normalization(tmp_path: Path):
    """Test that common LLM aliases like 'command' are normalized to 'cmd'."""
    executor = ToolExecutor(workspace_root=tmp_path)
    # LLMs often send {"command": "..."} instead of {"cmd": "..."}
    out = executor.execute(
        "terminal", {"command": "echo normalized"}, allowlist=["terminal"]
    )
    assert out["returncode"] == 0
    assert "normalized" in out["stdout"]


def test_file_io_arg_normalization(tmp_path: Path):
    """Test that file_io aliases like 'operation'/'file_path' are normalized."""
    (tmp_path / "demo.txt").write_text("content", encoding="utf-8")
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute(
        "file_io",
        {"operation": "read", "file_path": str(tmp_path / "demo.txt")},
        allowlist=["file_io"],
    )
    assert out["content"] == "content"


def test_normalize_does_not_overwrite_canonical(tmp_path: Path):
    """Test that normalization doesn't overwrite an already-present canonical key."""
    executor = ToolExecutor(workspace_root=tmp_path)
    # If both 'cmd' (canonical) and 'command' (alias) are present, 'cmd' wins
    out = executor.execute(
        "terminal",
        {"cmd": "echo canonical", "command": "echo alias"},
        allowlist=["terminal"],
    )
    assert out["returncode"] == 0
    assert "canonical" in out["stdout"]


# ── python_exec stdin guardrails ─────────────────────────────────────────────


def test_python_exec_stdin_closed(tmp_path: Path):
    """python_exec should close stdin so input() gets immediate EOF, not hang."""
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute(
        "python_exec",
        {"code": "x = input('prompt: ')\nprint(x)", "timeout_s": 5},
        allowlist=["python_exec"],
    )
    # Script should fail fast with EOFError, NOT hang until timeout
    assert out["returncode"] != 0
    assert "EOFError" in out["stderr"] or "EOF" in out["stderr"]


def test_python_exec_interactive_warning(tmp_path: Path):
    """python_exec should inject a warning when code contains input()."""
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute(
        "python_exec",
        {"code": "x = input('name: ')", "timeout_s": 5},
        allowlist=["python_exec"],
    )
    assert "WARNING" in out["stderr"]
    assert "input(" in out["stderr"]


def test_python_exec_no_warning_for_normal_code(tmp_path: Path):
    """python_exec should NOT warn when code has no interactive patterns."""
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute(
        "python_exec",
        {"code": "print('hello world')"},
        allowlist=["python_exec"],
    )
    assert out["returncode"] == 0
    assert "WARNING" not in out["stderr"]
    assert "hello world" in out["stdout"]


# ── http_api tests ───────────────────────────────────────────────────────────


def test_http_api_success(tmp_path: Path):
    """http_api returns status_code, headers, and text from a mocked response."""
    executor = ToolExecutor(workspace_root=tmp_path)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.text = '{"ok": true}'
    mock_client = MagicMock()
    mock_client.__enter__ = lambda self: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.request.return_value = mock_resp
    with patch("httpx.Client", return_value=mock_client):
        out = executor.execute("http_api", {"url": "https://example.com/api", "method": "POST"}, allowlist=["http_api"])
    assert out["status_code"] == 200
    assert "content-type" in out["headers"]
    assert "ok" in out["text"]


def test_http_api_missing_url(tmp_path: Path):
    """http_api raises ToolExecutionError when url is missing."""
    executor = ToolExecutor(workspace_root=tmp_path)
    with pytest.raises(ToolExecutionError, match="http_api requires 'url'"):
        executor.execute("http_api", {"method": "GET"}, allowlist=["http_api"])


# ── web_fetch tests ──────────────────────────────────────────────────────────


def test_web_fetch_success(tmp_path: Path):
    """web_fetch returns status_code and text from a mocked response."""
    executor = ToolExecutor(workspace_root=tmp_path)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html>hello</html>"
    mock_client = MagicMock()
    mock_client.__enter__ = lambda self: mock_client
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_resp
    with patch("httpx.Client", return_value=mock_client):
        out = executor.execute("web_fetch", {"url": "https://example.com"}, allowlist=["web_fetch"])
    assert out["status_code"] == 200
    assert "hello" in out["text"]


def test_web_fetch_missing_url(tmp_path: Path):
    """web_fetch raises ToolExecutionError when url is missing."""
    executor = ToolExecutor(workspace_root=tmp_path)
    with pytest.raises(ToolExecutionError, match="web_fetch requires 'url'"):
        executor.execute("web_fetch", {}, allowlist=["web_fetch"])


# ── sql_query tests ──────────────────────────────────────────────────────────


def test_sql_query_select_enforcement(tmp_path: Path):
    """sql_query rejects non-SELECT statements."""
    db_path = tmp_path / "test.db"
    sqlite3.connect(db_path).close()
    executor = ToolExecutor(workspace_root=tmp_path)
    with pytest.raises(ToolExecutionError, match="sql_query only allows SELECT"):
        executor.execute("sql_query", {"db_path": str(db_path), "query": "DELETE FROM x"}, allowlist=["sql_query"])


def test_sql_query_success(tmp_path: Path):
    """sql_query returns rows from a real SQLite database."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO items VALUES (1, 'alpha')")
    conn.execute("INSERT INTO items VALUES (2, 'beta')")
    conn.commit()
    conn.close()
    executor = ToolExecutor(workspace_root=tmp_path)
    out = executor.execute("sql_query", {"db_path": str(db_path), "query": "SELECT * FROM items"}, allowlist=["sql_query"])
    assert len(out["rows"]) == 2
    assert out["rows"][0]["name"] == "alpha"


# ── safe_path tests ──────────────────────────────────────────────────────────


def test_safe_path_traversal_blocked(tmp_path: Path):
    """_safe_path blocks path traversal attempts."""
    executor = ToolExecutor(workspace_root=tmp_path)
    with pytest.raises(ToolExecutionError, match="file path escapes workspace"):
        executor._safe_path("../../etc/passwd")


def test_safe_path_absolute_outside_workspace(tmp_path: Path):
    """_safe_path blocks absolute paths outside workspace."""
    executor = ToolExecutor(workspace_root=tmp_path)
    with pytest.raises(ToolExecutionError, match="file path escapes workspace"):
        executor._safe_path("/etc/passwd")


def test_safe_path_within_workspace(tmp_path: Path):
    """_safe_path resolves relative paths within workspace correctly."""
    executor = ToolExecutor(workspace_root=tmp_path)
    result = executor._safe_path("subdir/file.txt")
    assert result == (tmp_path / "subdir" / "file.txt").resolve()


# ── tool_registry_for_allowlist tests ────────────────────────────────────────


def test_tool_registry_for_allowlist_filters():
    """tool_registry_for_allowlist returns only allowed tools."""
    result = tool_registry_for_allowlist(["terminal", "file_io"])
    names = [t["name"] for t in result]
    assert names == ["terminal", "file_io"]


def test_tool_registry_for_allowlist_unknown():
    """tool_registry_for_allowlist skips unknown tool names."""
    result = tool_registry_for_allowlist(["nonexistent"])
    assert result == []


# ── misc edge cases ──────────────────────────────────────────────────────────


def test_unknown_tool_dispatch(tmp_path: Path):
    """Dispatching an unknown tool raises ToolExecutionError."""
    executor = ToolExecutor(workspace_root=tmp_path)
    # Tool must be in allowlist to reach the dispatch; use a name not in TOOL_REGISTRY
    with pytest.raises(ToolExecutionError, match="unknown tool"):
        executor.execute("nonexistent_tool", {}, allowlist=["nonexistent_tool"])


def test_terminal_empty_cmd(tmp_path: Path):
    """Terminal tool with empty cmd raises ToolExecutionError."""
    executor = ToolExecutor(workspace_root=tmp_path)
    with pytest.raises(ToolExecutionError, match="terminal requires non-empty"):
        executor.execute("terminal", {"cmd": ""}, allowlist=["terminal"])

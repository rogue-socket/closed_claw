# Purpose: Tool execution sandbox and built-in tool implementations.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class ToolExecutionError(RuntimeError):
    pass


SUPPORTED_TOOLS = ["terminal", "http_api", "web_fetch", "file_io", "python_exec", "sql_query"]

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "terminal": {
        "description": "Run a shell command in the workspace.",
        "args_schema": {"cmd": "string", "timeout_s": "int(optional)"},
    },
    "http_api": {
        "description": "Make an HTTP request and return status/body snippet.",
        "args_schema": {
            "method": "string(optional, default=GET)",
            "url": "string",
            "headers": "object(optional)",
            "json": "object(optional)",
            "params": "object(optional)",
            "timeout_s": "float(optional)",
        },
    },
    "web_fetch": {
        "description": "Fetch a webpage by URL and return status/body snippet.",
        "args_schema": {"url": "string", "timeout_s": "float(optional)"},
    },
    "file_io": {
        "description": "List directories and read/write/append text files inside allowed roots.",
        "args_schema": {
            "op": "string(list|read|write|append)",
            "path": "string",
            "content": "string(optional for write/append)",
            "recursive": "bool(optional for list)",
            "max_entries": "int(optional for list)",
        },
    },
    "python_exec": {
        "description": (
            "Execute a short Python snippet. "
            "IMPORTANT: stdin is closed — code must NOT use input(), sys.stdin, "
            "or any interactive prompts. Use hardcoded values or function args instead. "
            "Scripts with input() will receive immediate EOF and fail."
        ),
        "args_schema": {"code": "string", "timeout_s": "int(optional)"},
    },
    "sql_query": {
        "description": "Execute a SELECT query on a SQLite database file.",
        "args_schema": {
            "db_path": "string",
            "query": "string(SELECT only)",
            "params": "array(optional)",
        },
    },
}


def tool_registry_for_allowlist(allowlist: list[str]) -> list[dict[str, Any]]:
    """Run tool registry for allowlist."""
    return [
        {"name": name, **TOOL_REGISTRY[name]}
        for name in allowlist
        if name in TOOL_REGISTRY
    ]


class ToolExecutor:
    # Common LLM arg-name mistakes → canonical arg names per tool.
    # Defence-in-depth: even when the prompt tells the LLM the correct names,
    # some models still use natural-language aliases like "command" for "cmd".
    _ARG_ALIASES: dict[str, dict[str, str]] = {
        "terminal": {"command": "cmd", "shell": "cmd", "run": "cmd", "exec": "cmd",
                      "timeout": "timeout_s"},
        "http_api": {"body": "json", "data": "json", "timeout": "timeout_s"},
        "web_fetch": {"timeout": "timeout_s", "link": "url"},
        "file_io": {"operation": "op", "action": "op", "file": "path", "file_path": "path",
                     "filepath": "path", "data": "content", "text": "content"},
        "python_exec": {"script": "code", "python": "code", "snippet": "code",
                         "timeout": "timeout_s"},
        "sql_query": {"sql": "query", "database": "db_path", "db": "db_path"},
    }

    def __init__(self, workspace_root: Path, allowed_roots: list[Path] | None = None) -> None:
        """Initialize the instance."""
        self.workspace_root = workspace_root.resolve()
        self.allowed_roots = [self.workspace_root]
        if allowed_roots:
            self.allowed_roots.extend(p.resolve() for p in allowed_roots)

    @classmethod
    def _normalize_args(cls, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Remap common LLM arg-name mistakes to canonical names."""
        aliases = cls._ARG_ALIASES.get(tool)
        if not aliases:
            return args
        normalized: dict[str, Any] = {}
        for key, value in args.items():
            canonical = aliases.get(key, key)
            # Don't overwrite an already-present canonical key
            if canonical in normalized:
                continue
            normalized[canonical] = value
        return normalized

    def execute(self, tool: str, args: dict[str, Any], allowlist: list[str]) -> dict[str, Any]:
        """Run execute."""
        if tool not in allowlist:
            raise ToolExecutionError(f"tool '{tool}' is not allowed for this agent")

        # Normalize common arg-name aliases before dispatching
        args = self._normalize_args(tool, args)

        if tool == "terminal":
            return self._terminal(args)
        if tool == "http_api":
            return self._http_api(args)
        if tool == "web_fetch":
            return self._web_fetch(args)
        if tool == "file_io":
            return self._file_io(args)
        if tool == "python_exec":
            return self._python_exec(args)
        if tool == "sql_query":
            return self._sql_query(args)
        raise ToolExecutionError(f"unknown tool: {tool}")

    def _terminal(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run terminal."""
        cmd = str(args.get("cmd", "")).strip()
        if not cmd:
            raise ToolExecutionError("terminal requires non-empty 'cmd'")
        timeout_s = int(args.get("timeout_s", 15))
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=self.workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    def _http_api(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run http api."""
        import httpx

        method = str(args.get("method", "GET")).upper()
        url = str(args.get("url", "")).strip()
        if not url:
            raise ToolExecutionError("http_api requires 'url'")
        timeout = float(args.get("timeout_s", 20))
        headers = args.get("headers") or {}
        payload = args.get("json")
        params = args.get("params")
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, url, headers=headers, json=payload, params=params)
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "text": resp.text[:10000],
        }

    def _web_fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run web fetch."""
        import httpx

        url = str(args.get("url", "")).strip()
        if not url:
            raise ToolExecutionError("web_fetch requires 'url'")
        with httpx.Client(timeout=float(args.get("timeout_s", 20))) as client:
            resp = client.get(url)
        return {"status_code": resp.status_code, "text": resp.text[:10000]}

    def _safe_path(self, path_str: str) -> Path:
        """Run safe path."""
        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = (self.workspace_root / path).resolve()
        else:
            path = path.resolve()
        if not any(root == path or root in path.parents for root in self.allowed_roots):
            raise ToolExecutionError("file path escapes workspace")
        return path

    def _file_io(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run file io."""
        op = str(args.get("op", "read"))
        path = self._safe_path(str(args.get("path", "")))
        if op == "list":
            if not path.exists():
                raise ToolExecutionError(f"path not found: {path}")
            if not path.is_dir():
                raise ToolExecutionError("file_io list requires a directory path")
            recursive = bool(args.get("recursive", False))
            max_entries = int(args.get("max_entries", 200))
            max_entries = max(1, min(max_entries, 2000))

            entries: list[dict[str, Any]] = []
            iterator = path.rglob("*") if recursive else path.iterdir()
            for child in sorted(iterator, key=lambda p: str(p).lower()):
                kind = "dir" if child.is_dir() else "file"
                item: dict[str, Any] = {"path": str(child), "name": child.name, "kind": kind}
                if child.is_file():
                    try:
                        item["size_bytes"] = child.stat().st_size
                    except Exception:
                        item["size_bytes"] = None
                entries.append(item)
                if len(entries) >= max_entries:
                    break
            return {
                "path": str(path),
                "recursive": recursive,
                "truncated": len(entries) >= max_entries,
                "entries": entries,
            }
        if op == "read":
            return {"content": path.read_text(encoding="utf-8")}
        if op == "write":
            content = str(args.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"written": True, "path": str(path)}
        if op == "append":
            content = str(args.get("content", ""))
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
            return {"appended": True, "path": str(path)}
        raise ToolExecutionError("file_io op must be list|read|write|append")

    # Patterns that indicate a script reads from stdin and would block forever.
    _INTERACTIVE_PATTERNS = (
        "input(", "sys.stdin", "raw_input(", "fileinput.input(",
    )

    def _python_exec(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run python exec.

        Stdin is closed (subprocess.DEVNULL) so scripts containing ``input()``
        receive immediate EOF instead of hanging until timeout.  An explicit
        warning is injected into stderr when interactive patterns are detected.
        """
        code = str(args.get("code", "")).strip()
        if not code:
            raise ToolExecutionError("python_exec requires 'code'")
        timeout_s = int(args.get("timeout_s", 15))

        # Detect interactive stdin patterns — warn but still run (stdin is
        # closed via DEVNULL so the script will get immediate EOF / EOFError).
        interactive_warning = ""
        code_lower = code.lower()
        for pattern in self._INTERACTIVE_PATTERNS:
            if pattern in code_lower:
                interactive_warning = (
                    f"WARNING: code contains '{pattern}' which reads from stdin. "
                    "stdin is closed in python_exec — the script will receive "
                    "immediate EOF.  Rewrite the code to use function arguments "
                    "or hardcoded values instead of interactive input().\n"
                )
                break

        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=self.workspace_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        stderr = proc.stderr
        if interactive_warning:
            stderr = interactive_warning + stderr
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": stderr,
        }

    def _sql_query(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run sql query."""
        import sqlite3

        db_path = self._safe_path(str(args.get("db_path", "")))
        query = str(args.get("query", "")).strip()
        if not query:
            raise ToolExecutionError("sql_query requires 'query'")
        if not query.lower().startswith("select"):
            raise ToolExecutionError("sql_query only allows SELECT statements")
        params = args.get("params") or []

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
            return {"rows": [dict(r) for r in rows]}
        finally:
            conn.close()

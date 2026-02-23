from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


class ToolExecutionError(RuntimeError):
    pass


SUPPORTED_TOOLS = ["terminal", "http_api", "web_fetch", "file_io", "python_exec", "sql_query"]


class ToolExecutor:
    def __init__(self, workspace_root: Path, allowed_roots: list[Path] | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.allowed_roots = [self.workspace_root]
        if allowed_roots:
            self.allowed_roots.extend(p.resolve() for p in allowed_roots)

    def execute(self, tool: str, args: dict[str, Any], allowlist: list[str]) -> dict[str, Any]:
        if tool not in allowlist:
            raise ToolExecutionError(f"tool '{tool}' is not allowed for this agent")

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
        import httpx

        url = str(args.get("url", "")).strip()
        if not url:
            raise ToolExecutionError("web_fetch requires 'url'")
        with httpx.Client(timeout=float(args.get("timeout_s", 20))) as client:
            resp = client.get(url)
        return {"status_code": resp.status_code, "text": resp.text[:10000]}

    def _safe_path(self, path_str: str) -> Path:
        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = (self.workspace_root / path).resolve()
        else:
            path = path.resolve()
        if not any(root == path or root in path.parents for root in self.allowed_roots):
            raise ToolExecutionError("file path escapes workspace")
        return path

    def _file_io(self, args: dict[str, Any]) -> dict[str, Any]:
        op = str(args.get("op", "read"))
        path = self._safe_path(str(args.get("path", "")))
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
        raise ToolExecutionError("file_io op must be read|write|append")

    def _python_exec(self, args: dict[str, Any]) -> dict[str, Any]:
        code = str(args.get("code", "")).strip()
        if not code:
            raise ToolExecutionError("python_exec requires 'code'")
        timeout_s = int(args.get("timeout_s", 15))
        proc = subprocess.run(
            [sys.executable, "-c", code],
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

    def _sql_query(self, args: dict[str, Any]) -> dict[str, Any]:
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

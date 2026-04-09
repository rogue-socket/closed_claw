# Purpose: Tool execution sandbox and built-in tool implementations.

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("closed_claw.tools")


class ToolExecutionError(RuntimeError):
    pass


class _HTMLTextExtractor:
    """Stdlib-only HTML-to-text extractor (no BeautifulSoup dependency)."""

    def __init__(self) -> None:
        from html.parser import HTMLParser

        class _Parser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.pieces: list[str] = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "noscript", "svg"):
                    self._skip = True
                elif tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"):
                    self.pieces.append("\n")

            def handle_endtag(self, tag):
                if tag in ("script", "style", "noscript", "svg"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    self.pieces.append(data)

        self._parser = _Parser()

    def extract(self, html: str, max_chars: int = 8000) -> str:
        import re

        self._parser.feed(html)
        raw = " ".join(self._parser.pieces)
        text = re.sub(r"[ \t]+", " ", raw)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()[:max_chars]


SUPPORTED_TOOLS = ["terminal", "http_api", "web_fetch", "file_io", "python_exec", "sql_query"]

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "terminal": {
        "name": "terminal",
        "description": (
            "Run a command-line program in the workspace directory. "
            "Use for: running build tools (npm, pip, make), git operations, "
            "listing processes, checking system info, or any CLI utility. "
            "Commands are executed WITHOUT a shell — no pipes, redirections, or "
            "chaining (&&, ||, ;). Pass a single program with arguments. "
            "Destructive commands (rm -rf, format, dd) are blocked."
        ),
        "args_schema": {"cmd": "string", "timeout_s": "int(optional, default=15)"},
        "risk_level": "high",
        "side_effects": True,
    },
    "http_api": {
        "name": "http_api",
        "description": (
            "Make an HTTP request to a REST API endpoint and return the response. "
            "Supports GET, POST, PUT, PATCH, DELETE methods. Use for: calling "
            "external APIs, webhooks, or services. Returns status code, headers, "
            "and response body (truncated to 10KB)."
        ),
        "args_schema": {
            "method": "string(optional, default=GET)",
            "url": "string",
            "headers": "object(optional)",
            "json": "object(optional)",
            "params": "object(optional)",
            "timeout_s": "float(optional, default=20)",
        },
        "risk_level": "medium",
        "side_effects": True,
    },
    "web_fetch": {
        "name": "web_fetch",
        "description": (
            "Fetch a webpage by URL and return its text content. "
            "HTML is automatically stripped to plain text. "
            "Use for: reading documentation, checking web pages, scraping data. "
            "Returns status code, extracted text, and content type."
        ),
        "args_schema": {"url": "string", "timeout_s": "float(optional, default=20)"},
        "risk_level": "low",
        "side_effects": False,
    },
    "file_io": {
        "name": "file_io",
        "description": (
            "Read, write, append, or list files and directories inside the workspace. "
            "Operations: 'list' (directory listing), 'read' (file content), "
            "'write' (create/overwrite), 'append' (add to end). "
            "Path must be within allowed workspace roots — escapes are blocked. "
            "Use for: reading source code, writing outputs, exploring project structure."
        ),
        "args_schema": {
            "op": "string(list|read|write|append)",
            "path": "string",
            "content": "string(required for write/append)",
            "recursive": "bool(optional for list, default=false)",
            "max_entries": "int(optional for list, default=200, max=2000)",
        },
        "risk_level": "medium",
        "side_effects": True,
    },
    "python_exec": {
        "name": "python_exec",
        "description": (
            "Execute a Python code snippet and return stdout/stderr/returncode. "
            "Use for: data processing, calculations, file transformations, "
            "testing snippets, or any task best expressed in Python. "
            "IMPORTANT: stdin is closed — code must NOT use input(), sys.stdin, "
            "or any interactive prompts. Use hardcoded values or function args instead."
        ),
        "args_schema": {"code": "string", "timeout_s": "int(optional, default=15)"},
        "risk_level": "high",
        "side_effects": True,
    },
    "sql_query": {
        "name": "sql_query",
        "description": (
            "Execute a read-only SELECT query on a SQLite database file. "
            "Use for: inspecting database contents, querying structured data, "
            "checking schema information. Only SELECT statements are allowed — "
            "INSERT/UPDATE/DELETE/DROP are rejected. Database is opened in read-only mode."
        ),
        "args_schema": {
            "db_path": "string",
            "query": "string(SELECT only)",
            "params": "array(optional)",
        },
        "risk_level": "low",
        "side_effects": False,
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
    # Maximum size of stdout/stderr returned from terminal/python_exec
    _MAX_OUTPUT_BYTES = 50_000
    # Maximum Python code size (prevents accidentally dumping huge blobs)
    _MAX_CODE_SIZE = 30_000
    # Blocked non-HTTP URL schemes (SSRF protection)
    _BLOCKED_SCHEMES = ("file://", "ftp://", "gopher://", "data:")
    # Well-known cloud metadata hostnames to block before DNS resolution
    _METADATA_HOSTS = frozenset({"metadata.google.internal", "169.254.169.254"})
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
            # Detect duplicate: two keys mapping to the same canonical name
            if canonical in normalized:
                import logging
                logging.getLogger("closed_claw.tools").warning(
                    "Tool '%s': arg '%s' maps to canonical '%s' which is already set — ignoring duplicate",
                    tool, key, canonical,
                )
                continue
            normalized[canonical] = value
        return normalized

    def _check_url_safety(self, url: str) -> None:
        """Block requests to internal/dangerous URLs (SSRF prevention).

        Resolves the hostname to IP address(es) and rejects any private,
        loopback, link-local, or reserved address.  This defeats encoding
        tricks (octal/decimal/hex IPs) and DNS rebinding of known prefixes.
        """
        from urllib.parse import urlparse

        lower = url.lower()
        for scheme in self._BLOCKED_SCHEMES:
            if lower.startswith(scheme):
                raise ToolExecutionError(
                    f"URL blocked by security policy: disallowed scheme '{scheme}'"
                )

        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise ToolExecutionError("URL blocked by security policy: no hostname")

        if hostname in self._METADATA_HOSTS:
            raise ToolExecutionError(
                f"URL blocked by security policy: metadata endpoint '{hostname}'"
            )

        # Resolve hostname → IP(s) and reject private/reserved addresses
        try:
            addrinfos = socket.getaddrinfo(hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise ToolExecutionError(
                f"URL blocked by security policy: cannot resolve hostname '{hostname}'"
            )

        for _family, _type, _proto, _canonname, sockaddr in addrinfos:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ToolExecutionError(
                    f"URL blocked by security policy: '{hostname}' resolves to "
                    f"non-public address {ip}"
                )

    @staticmethod
    def _truncate_output(text: str, max_bytes: int = 50_000) -> str:
        """Truncate tool output to prevent memory bloat."""
        if len(text) <= max_bytes:
            return text
        return text[:max_bytes] + f"\n... [truncated, {len(text)} total chars]"

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

    # Deny-list of dangerous command patterns — matched against the full
    # command string *before* execution.  Each entry is a compiled regex.
    _COMMAND_DENYLIST: list[re.Pattern[str]] = [
        re.compile(p, re.IGNORECASE)
        for p in [
            r"\brm\s+(-\S+\s+)*-r\s*f\b",       # rm -rf
            r"\brm\s+(-\S+\s+)*-f\s*r\b",       # rm -fr
            r"\brmdir\s+/s\b",                    # Windows rmdir /s
            r"\bformat\s+[a-zA-Z]:",              # format drive
            r"\bmkfs\b",                           # make filesystem
            r"\bdd\s+.*\bof=",                    # dd to device/file
            r":;\s*\)\s*;",                        # fork bomb pattern :(){ :|:& };:
            r">\s*/dev/sd[a-z]",                   # write to raw device
            r"\bcurl\b.*\|\s*(ba)?sh",             # curl pipe to shell
            r"\bwget\b.*\|\s*(ba)?sh",             # wget pipe to shell
            r"\bchmod\s+(-\S+\s+)*777\b",         # world-writable
            r"\bnc\s+-\S*[el]",                    # netcat listen/exec
            r"\bpowershell\b.*-e(nc(oded)?c(ommand)?)?",  # encoded PowerShell
        ]
    ]

    def _terminal(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a command without shell=True to prevent injection.

        The command string is split using shlex.split, preventing shell
        metacharacter attacks (;, &&, |, $(), backticks, etc.).
        A denylist catches known destructive patterns before execution.
        """
        cmd = str(args.get("cmd", "")).strip()
        if not cmd:
            raise ToolExecutionError("terminal requires non-empty 'cmd'")

        # Check denylist before execution
        for pattern in self._COMMAND_DENYLIST:
            if pattern.search(cmd):
                raise ToolExecutionError(
                    f"command blocked by security policy: matches denylist pattern '{pattern.pattern}'"
                )

        timeout_s = int(args.get("timeout_s", 15))

        # Split command into argv list — this prevents shell injection
        # by removing metacharacter interpretation.
        try:
            argv = shlex.split(cmd, posix=(sys.platform != "win32"))
        except ValueError as exc:
            raise ToolExecutionError(f"invalid command syntax: {exc}") from exc

        if not argv:
            raise ToolExecutionError("terminal requires non-empty 'cmd'")

        proc = subprocess.run(
            argv,
            shell=False,
            cwd=self.workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return {
            "returncode": proc.returncode,
            "stdout": self._truncate_output(proc.stdout, self._MAX_OUTPUT_BYTES),
            "stderr": self._truncate_output(proc.stderr, self._MAX_OUTPUT_BYTES),
        }

    def _http_api(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run http api."""
        import httpx

        method = str(args.get("method", "GET")).upper()
        url = str(args.get("url", "")).strip()
        if not url:
            raise ToolExecutionError("http_api requires 'url'")
        self._check_url_safety(url)
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
        self._check_url_safety(url)
        with httpx.Client(timeout=float(args.get("timeout_s", 20))) as client:
            resp = client.get(url)
        content_type = resp.headers.get("content-type", "")
        raw_text = resp.text
        if "html" in content_type.lower():
            text = _HTMLTextExtractor().extract(raw_text)
        else:
            text = raw_text[:10000]
        return {
            "status_code": resp.status_code,
            "text": text,
            "content_type": content_type,
            "raw_length": len(raw_text),
        }

    def _safe_path(self, path_str: str) -> Path:
        """Validate that *path_str* resolves to a location inside allowed roots.

        Defence-in-depth: we check both the logical path (``strict=False``,
        without following symlinks) *and* the fully-resolved path so that a
        symlink placed inside the workspace cannot redirect access outside.
        """
        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path

        # Check the logical path (no symlink resolution) first
        logical = Path(os.path.normpath(path))
        if not any(root == logical or root in logical.parents for root in self.allowed_roots):
            raise ToolExecutionError("file path escapes workspace")

        # Also check fully resolved path (follows symlinks) to block symlink escapes
        resolved = path.resolve()
        if not any(root == resolved or root in resolved.parents for root in self.allowed_roots):
            raise ToolExecutionError("file path escapes workspace (symlink target outside allowed roots)")

        return resolved

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
        if len(code) > self._MAX_CODE_SIZE:
            raise ToolExecutionError(
                f"python_exec code exceeds maximum size ({len(code)} > {self._MAX_CODE_SIZE})"
            )
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
            "stdout": self._truncate_output(proc.stdout, self._MAX_OUTPUT_BYTES),
            "stderr": self._truncate_output(stderr, self._MAX_OUTPUT_BYTES),
        }

    # Patterns forbidden in SQL queries (beyond the SELECT-only check).
    _SQL_DENYLIST: list[re.Pattern[str]] = [
        re.compile(p, re.IGNORECASE)
        for p in [
            r";",                       # multi-statement
            r"\bATTACH\b",              # attach external DB
            r"\bDETACH\b",
            r"\bload_extension\b",      # dynamic extension loading
            r"\bPRAGMA",                # configuration changes (also pragma_* functions)
        ]
    ]

    def _sql_query(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run sql query."""
        import sqlite3

        db_path = self._safe_path(str(args.get("db_path", "")))
        query = str(args.get("query", "")).strip()
        if not query:
            raise ToolExecutionError("sql_query requires 'query'")
        if not query.lower().startswith("select"):
            raise ToolExecutionError("sql_query only allows SELECT statements")
        for pattern in self._SQL_DENYLIST:
            if pattern.search(query):
                raise ToolExecutionError(
                    f"sql_query blocked: query contains forbidden pattern '{pattern.pattern}'"
                )
        params = args.get("params") or []

        # Open in read-only mode to prevent any data mutation
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
            return {"rows": [dict(r) for r in rows]}
        finally:
            conn.close()

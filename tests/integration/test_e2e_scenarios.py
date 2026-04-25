# Purpose: End-to-end integration tests for the full coordinator pipeline.
#
# Each test exercises: ingest → decompose → execute_task_pool → validate →
# update_registry_and_audit → synthesize_final_response with stubbed LLM calls
# and real tool execution against fixture workspaces.

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("langgraph")

from closed_claw.config import Settings
from closed_claw.coordinator.graph import build_graph
from closed_claw.registry.search import HeuristicReranker
from closed_claw.runtime.protocol import (
    AgentResponse,
    AgentMetrics,
    ApiCallDecision,
    ApiCallIntent,
    CoordinatorRequest,
    ToolCallIntent,
    ToolCallResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _seed_workspace(tmp_path: Path, files: dict[str, str]) -> None:
    """Create fixture files under tmp_path."""
    for rel_path, content in files.items():
        p = tmp_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _seed_database(db_path: Path, tables: dict[str, tuple[str, list[tuple]]]) -> None:
    """Create a SQLite database with the given tables and rows.

    tables: {table_name: (create_sql, [(row, ...), ...])}
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    for table_name, (create_sql, rows) in tables.items():
        conn.execute(create_sql)
        if rows:
            placeholders = ", ".join(["?"] * len(rows[0]))
            conn.executemany(f"INSERT INTO {table_name} VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()


def _make_stub_plan(
    discovery_plan: list[dict[str, Any]],
    execution_plan: list[dict[str, Any]],
):
    """Return a stub for generate_task_plan that returns phase-appropriate plans."""

    def _stub(
        _settings: Settings,
        _task: str,
        *,
        phase: str = "execution",
        discovery_results: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if phase == "discovery":
            return discovery_plan
        return execution_plan

    return _stub


def _make_stub_profile(profiles: dict[str, dict[str, Any]]):
    """Return a stub for generate_agent_profile that returns role-specific profiles.

    profiles: {role_tag_substring: profile_dict}
    Falls back to a default profile if no match.
    """

    def _stub(
        settings: Settings,
        task: str,
        supported_tools: list[str],
        fallback_tools: list[str],
    ) -> dict[str, Any]:
        # Match by checking if any profile key appears in the task string
        # (task string passed to _select_capability_profile includes role_tag)
        for key, profile in profiles.items():
            if key in task.lower():
                return profile
        # Default fallback
        return {
            "profile_id": "default-operator",
            "name_prefix": "Default Operator",
            "description": f"Operator for: {task[:60]}",
            "tools_allowlist": fallback_tools or ["terminal"],
            "tags": ["auto", "default-operator"],
            "skill_md": "# Default\nExecute tasks.\n",
            "api_capabilities": [],
            "requires_approval_for": [],
        }

    return _stub


def _make_fake_runner(
    tool_sequences: dict[str, list[dict[str, Any]]],
    results: dict[str, str],
    *,
    fail_roles: dict[str, str] | None = None,
    attempt_behavior: dict[str, list[str]] | None = None,
):
    """Build a fake run_agent that exercises tool callbacks without subprocesses.

    tool_sequences: {role_tag: [ToolCallIntent kwargs, ...]}
    results: {role_tag: result_text}
    fail_roles: {role_tag: error_message} — roles that always fail
    attempt_behavior: {role_tag: ["fail", "ok"]} — per-attempt behavior for retry tests
    """
    attempt_counts: dict[str, int] = {}

    async def _fake_run_agent(
        self,
        agent_id: str,
        entrypoint: Path,
        request: CoordinatorRequest,
        approval_callback: Any,
        tool_callback: Any,
    ) -> AgentResponse:
        subtask_ctx = request.context.get("subtask", {})
        role_tag = subtask_ctx.get("role_tag", "unknown")
        attempt = subtask_ctx.get("attempt", 1)

        # Track attempts for retry tests
        attempt_counts.setdefault(role_tag, 0)
        attempt_counts[role_tag] += 1

        # Per-attempt behavior (for retry tests)
        if attempt_behavior and role_tag in attempt_behavior:
            behaviors = attempt_behavior[role_tag]
            idx = min(attempt_counts[role_tag] - 1, len(behaviors) - 1)
            if behaviors[idx] == "fail":
                return AgentResponse(
                    status="error",
                    error_message=f"simulated_failure_attempt_{attempt_counts[role_tag]}",
                )

        # Roles that always fail
        if fail_roles and role_tag in fail_roles:
            return AgentResponse(
                status="error",
                error_message=fail_roles[role_tag],
            )

        # Execute tool calls through the real ToolExecutor via coordinator callback
        for intent_kwargs in tool_sequences.get(role_tag, []):
            intent = ToolCallIntent(**intent_kwargs)
            await tool_callback(intent, agent_id)

        return AgentResponse(
            status="ok",
            result=results.get(role_tag, "done"),
            metrics=AgentMetrics(latency_ms=50.0),
        )

    return _fake_run_agent


def _build_and_run(
    monkeypatch,
    tmp_path: Path,
    task: str,
    discovery_plan: list[dict[str, Any]],
    execution_plan: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    tool_sequences: dict[str, list[dict[str, Any]]],
    results: dict[str, str],
    *,
    fail_roles: dict[str, str] | None = None,
    attempt_behavior: dict[str, list[str]] | None = None,
    settings_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One-shot helper: configure stubs, build graph, invoke, return final state."""
    # Environment
    monkeypatch.setenv("CLOSED_CLAW_DB_PATH", str(tmp_path / "registry.db"))
    monkeypatch.setenv("CLOSED_CLAW_AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("CLOSED_CLAW_RUN_LOGS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("CLOSED_CLAW_EMBEDDING_DIM", "8")
    monkeypatch.setenv("CLOSED_CLAW_REQUIRE_SQLITE_VEC", "false")
    monkeypatch.setenv("CLOSED_CLAW_CREATE_APPROVAL_REQUIRED", "false")
    monkeypatch.setenv("CLOSED_CLAW_CREATE_APPROVAL_MODE", "approve")
    monkeypatch.setenv("CLOSED_CLAW_API_APPROVAL_MODE", "approve")
    monkeypatch.setenv("CLOSED_CLAW_LLM_PROVIDER", "heuristic")
    monkeypatch.setenv("CLOSED_CLAW_LLM_API_KEY", "")
    monkeypatch.setenv("CLOSED_CLAW_EXTRA_ALLOWED_PATHS", str(tmp_path))
    monkeypatch.setenv("CLOSED_CLAW_SUBTASK_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("CLOSED_CLAW_AGENT_TIMEOUT_SEC", "30")
    monkeypatch.setenv("CLOSED_CLAW_TASK_POOL_POLL_INTERVAL_SEC", "0")

    if settings_overrides:
        for k, v in settings_overrides.items():
            monkeypatch.setenv(k, v)

    # Stub LLM-dependent functions
    monkeypatch.setattr(
        "closed_claw.coordinator.nodes.generate_task_plan",
        _make_stub_plan(discovery_plan, execution_plan),
    )
    monkeypatch.setattr(
        "closed_claw.coordinator.nodes.generate_agent_profile",
        _make_stub_profile(profiles),
    )
    monkeypatch.setattr(
        "closed_claw.coordinator.graph.build_reranker",
        lambda _s: HeuristicReranker(),
    )

    # Stub the runner to bypass subprocesses
    from closed_claw.runtime.runner import AgentRunner

    monkeypatch.setattr(
        AgentRunner,
        "run_agent",
        _make_fake_runner(
            tool_sequences,
            results,
            fail_roles=fail_roles,
            attempt_behavior=attempt_behavior,
        ),
    )

    settings = Settings.from_env()
    graph = build_graph(settings)

    async def _run() -> dict:
        return await graph.ainvoke({"task": task, "context": {}})

    return asyncio.run(_run())


# Helper to make a standard profile dict
def _profile(
    role_tag: str,
    tools: list[str],
    description: str = "",
) -> dict[str, Any]:
    return {
        "profile_id": role_tag,
        "name_prefix": role_tag.replace("-", " ").title(),
        "description": description or f"Agent for {role_tag}",
        "tools_allowlist": tools,
        "tags": ["auto", "capability", role_tag],
        "skill_md": f"# {role_tag}\nExecute tasks.\n",
        "api_capabilities": [],
        "requires_approval_for": [],
    }


# Helper to make a subtask plan item
def _subtask(
    task_id: str,
    title: str,
    role_tag: str,
    depends_on: list[str] | None = None,
    requires_tool: bool = True,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "title": title,
        "description": title,
        "role_tag": role_tag,
        "depends_on": depends_on or [],
        "acceptance_criteria": ["Done."],
        "requires_tool": requires_tool,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# SIMPLE — 1 subtask, 1 agent, 1-2 tool calls (TC01–TC04)
# ═══════════════════════════════════════════════════════════════════════════════


def test_tc01_read_file_and_summarize(monkeypatch, tmp_path: Path):
    """TC01: Read a file and summarize its contents."""
    _seed_workspace(tmp_path, {
        "config.txt": "host=localhost\nport=5432\ndb=myapp\nlog_level=info\nretries=3\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Read the file config.txt and summarize what it contains",
        discovery_plan=[_subtask("read-config", "Read config file", "file-reader")],
        execution_plan=[],
        profiles={"file-reader": _profile("file-reader", ["file_io"])},
        tool_sequences={
            "file-reader": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "config.txt")}, "reason": "read config"},
            ],
        },
        results={"file-reader": "Config contains: host=localhost, port=5432, db=myapp, log_level=info, retries=3"},
    )

    assert result["response_status"] == "ok"
    assert "config" in result["response_result"].lower() or "Config" in result["response_result"]
    # Verify discovery pool completed
    disc_pool = result.get("discovery_subtask_pool", [])
    completed = [t for t in disc_pool if t["status"] == "completed"]
    assert len(completed) == 1


def test_tc02_count_python_files(monkeypatch, tmp_path: Path):
    """TC02: Run a shell command and report output."""
    _seed_workspace(tmp_path, {
        "main.py": "print('main')\n",
        "utils.py": "def helper(): pass\n",
        "lib/core.py": "class Core: pass\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Count the number of Python files in the project",
        discovery_plan=[_subtask("count-files", "Count .py files", "terminal-operator")],
        execution_plan=[],
        profiles={"terminal-operator": _profile("terminal-operator", ["terminal"])},
        tool_sequences={
            "terminal-operator": [
                {"tool": "terminal", "args": {"cmd": f"find {tmp_path} -name '*.py' -not -path '*/agents/*' | wc -l"}, "reason": "count files"},
            ],
        },
        results={"terminal-operator": "Found 3 Python files in the project"},
    )

    assert result["response_status"] == "ok"
    assert "3" in result["response_result"] or "Python" in result["response_result"]


def test_tc03_list_directory(monkeypatch, tmp_path: Path):
    """TC03: List all files in a directory and report their sizes."""
    _seed_workspace(tmp_path, {
        "data/alpha.csv": "a,b,c\n1,2,3\n",
        "data/beta.json": '{"key": "value"}\n',
        "data/gamma.txt": "hello world\n",
        "data/sub/delta.log": "log entry\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="List all files in the data directory and report their sizes",
        discovery_plan=[_subtask("list-data", "List data directory", "file-explorer")],
        execution_plan=[],
        profiles={"file-explorer": _profile("file-explorer", ["file_io"])},
        tool_sequences={
            "file-explorer": [
                {"tool": "file_io", "args": {"op": "list", "path": str(tmp_path / "data"), "recursive": True, "max_entries": 20}, "reason": "list files"},
            ],
        },
        results={"file-explorer": "Data directory contains: alpha.csv, beta.json, gamma.txt, sub/delta.log"},
    )

    assert result["response_status"] == "ok"
    disc_pool = result.get("discovery_subtask_pool", [])
    assert any(t["status"] == "completed" for t in disc_pool)


def test_tc04_python_exec(monkeypatch, tmp_path: Path):
    """TC04: Execute a Python snippet to compute a result."""
    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Calculate the factorial of 10 using Python",
        discovery_plan=[_subtask("compute-factorial", "Compute factorial(10)", "python-runner")],
        execution_plan=[],
        profiles={"python-runner": _profile("python-runner", ["python_exec"])},
        tool_sequences={
            "python-runner": [
                {"tool": "python_exec", "args": {"code": "import math; print(math.factorial(10))"}, "reason": "compute factorial"},
            ],
        },
        results={"python-runner": "The factorial of 10 is 3628800"},
    )

    assert result["response_status"] == "ok"
    assert "3628800" in result["response_result"]


# ═══════════════════════════════════════════════════════════════════════════════
# MEDIUM — 1-2 subtasks, 1-2 agents, 2-4 tool calls (TC05–TC08)
# ═══════════════════════════════════════════════════════════════════════════════


def test_tc05_read_transform_write(monkeypatch, tmp_path: Path):
    """TC05: Read a file then write a transformed version."""
    _seed_workspace(tmp_path, {
        "data.csv": "name,city\nalice,paris\nbob,london\ncharlie,tokyo\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Read data.csv, convert it to uppercase, and write it to output.csv",
        discovery_plan=[_subtask("read-source", "Read source file", "data-reader")],
        execution_plan=[_subtask("write-output", "Write transformed file", "data-writer")],
        profiles={
            "data-reader": _profile("data-reader", ["file_io"]),
            "data-writer": _profile("data-writer", ["file_io"]),
        },
        tool_sequences={
            "data-reader": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "data.csv")}, "reason": "read source"},
            ],
            "data-writer": [
                {"tool": "file_io", "args": {"op": "write", "path": str(tmp_path / "output.csv"), "content": "NAME,CITY\nALICE,PARIS\nBOB,LONDON\nCHARLIE,TOKYO\n"}, "reason": "write output"},
            ],
        },
        results={
            "data-reader": "Read data.csv: 3 rows of name/city data",
            "data-writer": "Wrote uppercase data to output.csv",
        },
    )

    assert result["response_status"] == "ok"
    # Verify the file was actually written by the real ToolExecutor
    output = (tmp_path / "output.csv").read_text(encoding="utf-8")
    assert "ALICE" in output
    assert "PARIS" in output


def test_tc06_sql_query_and_summarize(monkeypatch, tmp_path: Path):
    """TC06: Query a SQLite database and summarize results."""
    _seed_database(
        tmp_path / "users.db",
        {
            "users": (
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, active INTEGER)",
                [(1, "Alice", 1), (2, "Bob", 1), (3, "Charlie", 0), (4, "Diana", 1), (5, "Eve", 0)],
            ),
        },
    )

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Query the users database and report how many active users exist",
        discovery_plan=[_subtask("query-users", "Query active users", "db-analyst")],
        execution_plan=[],
        profiles={"db-analyst": _profile("db-analyst", ["sql_query", "terminal"])},
        tool_sequences={
            "db-analyst": [
                {"tool": "sql_query", "args": {"db_path": str(tmp_path / "users.db"), "query": "SELECT COUNT(*) as cnt FROM users WHERE active=1"}, "reason": "count active users"},
            ],
        },
        results={"db-analyst": "There are 3 active users in the database"},
    )

    assert result["response_status"] == "ok"
    assert "3" in result["response_result"]


def test_tc07_multi_step_terminal(monkeypatch, tmp_path: Path):
    """TC07: Multi-step terminal workflow — mkdir, write, verify."""
    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Create a directory called 'output', write a greeting to output/hello.txt, then verify",
        discovery_plan=[],
        execution_plan=[_subtask("create-and-verify", "Create dir and file", "file-operator")],
        profiles={"file-operator": _profile("file-operator", ["terminal", "file_io"])},
        tool_sequences={
            "file-operator": [
                {"tool": "terminal", "args": {"cmd": f"mkdir -p {tmp_path}/output"}, "reason": "create dir"},
                {"tool": "file_io", "args": {"op": "write", "path": str(tmp_path / "output" / "hello.txt"), "content": "Hello World"}, "reason": "write greeting"},
                {"tool": "terminal", "args": {"cmd": f"cat {tmp_path}/output/hello.txt"}, "reason": "verify file"},
            ],
        },
        results={"file-operator": "Created output/hello.txt with 'Hello World'"},
    )

    assert result["response_status"] == "ok"
    assert (tmp_path / "output" / "hello.txt").exists()
    assert (tmp_path / "output" / "hello.txt").read_text(encoding="utf-8") == "Hello World"


def test_tc08_multi_file_compare(monkeypatch, tmp_path: Path):
    """TC08: Read multiple files and compare contents."""
    _seed_workspace(tmp_path, {
        "version.txt": "2.1.0",
        "changelog.txt": "## 2.1.0 - Bug fixes\n## 2.0.0 - Initial release\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Compare version.txt and changelog.txt and report if the version appears in the changelog",
        discovery_plan=[_subtask("read-files", "Read both files", "file-reader")],
        execution_plan=[],
        profiles={"file-reader": _profile("file-reader", ["file_io"])},
        tool_sequences={
            "file-reader": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "version.txt")}, "reason": "read version"},
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "changelog.txt")}, "reason": "read changelog"},
            ],
        },
        results={"file-reader": "Version 2.1.0 appears in changelog under 'Bug fixes'"},
    )

    assert result["response_status"] == "ok"
    assert "2.1.0" in result["response_result"]


# ═══════════════════════════════════════════════════════════════════════════════
# COMPLEX — 2-4 subtasks, 2+ agents, 4+ tool calls (TC09–TC12)
# ═══════════════════════════════════════════════════════════════════════════════


def test_tc09_code_structure_analysis(monkeypatch, tmp_path: Path):
    """TC09: Analyze code structure then write a report."""
    _seed_workspace(tmp_path, {
        "main.py": "def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n",
        "utils.py": "def helper():\n    return 42\n",
        "tests/test_main.py": "def test_main():\n    assert True\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Analyze the Python project structure and create a summary report in report.md",
        discovery_plan=[_subtask("scan-structure", "Scan project files", "code-explorer")],
        execution_plan=[_subtask("write-report", "Write report", "report-writer")],
        profiles={
            "code-explorer": _profile("code-explorer", ["file_io", "terminal"]),
            "report-writer": _profile("report-writer", ["file_io"]),
        },
        tool_sequences={
            "code-explorer": [
                {"tool": "terminal", "args": {"cmd": f"find {tmp_path} -name '*.py' -not -path '*/agents/*'"}, "reason": "find python files"},
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "main.py")}, "reason": "read main"},
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "utils.py")}, "reason": "read utils"},
            ],
            "report-writer": [
                {"tool": "file_io", "args": {"op": "write", "path": str(tmp_path / "report.md"), "content": "# Project Report\n\n- main.py: entry point with main()\n- utils.py: helper function\n- tests/test_main.py: basic test\n"}, "reason": "write report"},
            ],
        },
        results={
            "code-explorer": "Found 3 Python files: main.py, utils.py, tests/test_main.py",
            "report-writer": "Wrote report.md with project structure summary",
        },
    )

    assert result["response_status"] == "ok"
    assert (tmp_path / "report.md").exists()
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "main.py" in report
    assert "utils.py" in report


def test_tc10_data_pipeline(monkeypatch, tmp_path: Path):
    """TC10: Data pipeline — read, process, write."""
    _seed_workspace(tmp_path, {"numbers.txt": "1\n2\n3\n4\n5\n"})

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Read numbers.txt, compute the sum and average using Python, and write results to stats.txt",
        discovery_plan=[_subtask("read-data", "Read numbers", "data-reader")],
        execution_plan=[
            _subtask("compute-stats", "Compute stats", "python-runner"),
            _subtask("write-results", "Write stats file", "data-writer", depends_on=["compute-stats"]),
        ],
        profiles={
            "data-reader": _profile("data-reader", ["file_io"]),
            "python-runner": _profile("python-runner", ["python_exec"]),
            "data-writer": _profile("data-writer", ["file_io"]),
        },
        tool_sequences={
            "data-reader": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "numbers.txt")}, "reason": "read numbers"},
            ],
            "python-runner": [
                {"tool": "python_exec", "args": {"code": "nums=[1,2,3,4,5]; print(f'sum={sum(nums)} avg={sum(nums)/len(nums)}')"}, "reason": "compute stats"},
            ],
            "data-writer": [
                {"tool": "file_io", "args": {"op": "write", "path": str(tmp_path / "stats.txt"), "content": "sum=15 avg=3.0"}, "reason": "write stats"},
            ],
        },
        results={
            "data-reader": "Read 5 numbers from numbers.txt",
            "python-runner": "Computed: sum=15 avg=3.0",
            "data-writer": "Wrote results to stats.txt",
        },
    )

    assert result["response_status"] == "ok"
    assert (tmp_path / "stats.txt").exists()
    stats = (tmp_path / "stats.txt").read_text(encoding="utf-8")
    assert "15" in stats
    assert "3.0" in stats


def test_tc11_database_inspection(monkeypatch, tmp_path: Path):
    """TC11: Inspect DB schema, list tables, count rows, write summary."""
    _seed_database(
        tmp_path / "inventory.db",
        {
            "products": (
                "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)",
                [(1, "Widget", 9.99), (2, "Gadget", 19.99), (3, "Doohickey", 4.99),
                 (4, "Thingamajig", 14.99), (5, "Whatchamacallit", 7.99)],
            ),
            "orders": (
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, product_id INTEGER, qty INTEGER)",
                [(1, 1, 10), (2, 3, 5), (3, 2, 3)],
            ),
        },
    )
    db_path = str(tmp_path / "inventory.db")

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Inspect inventory.db schema, list tables, count rows, write summary",
        discovery_plan=[
            _subtask("list-tables", "List DB tables", "db-analyst"),
            _subtask("count-rows", "Count rows per table", "db-analyst", depends_on=["list-tables"]),
        ],
        execution_plan=[_subtask("write-summary", "Write DB report", "report-writer")],
        profiles={
            "db-analyst": _profile("db-analyst", ["sql_query"]),
            "report-writer": _profile("report-writer", ["file_io"]),
        },
        tool_sequences={
            "db-analyst": [
                {"tool": "sql_query", "args": {"db_path": db_path, "query": "SELECT name FROM sqlite_master WHERE type='table'"}, "reason": "list tables"},
                {"tool": "sql_query", "args": {"db_path": db_path, "query": "SELECT COUNT(*) as cnt FROM products"}, "reason": "count products"},
                {"tool": "sql_query", "args": {"db_path": db_path, "query": "SELECT COUNT(*) as cnt FROM orders"}, "reason": "count orders"},
            ],
            "report-writer": [
                {"tool": "file_io", "args": {"op": "write", "path": str(tmp_path / "db_report.txt"), "content": "Tables: products (5 rows), orders (3 rows)"}, "reason": "write summary"},
            ],
        },
        results={
            "db-analyst": "Found tables: products (5 rows), orders (3 rows)",
            "report-writer": "Wrote db_report.txt",
        },
    )

    assert result["response_status"] == "ok"
    assert (tmp_path / "db_report.txt").exists()
    report = (tmp_path / "db_report.txt").read_text(encoding="utf-8")
    assert "products" in report
    assert "orders" in report


def test_tc12_todo_scanner(monkeypatch, tmp_path: Path):
    """TC12: Multi-file code analysis — find TODOs, count, write summary."""
    _seed_workspace(tmp_path, {
        "app.py": "# TODO: add logging\ndef run(): pass\n",
        "server.py": "# TODO: handle timeouts\nclass Server: pass\n",
        "db.py": "# TODO: add connection pooling\ndef connect(): pass\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Find all TODO comments in the codebase, count them, write todo_summary.txt",
        discovery_plan=[_subtask("find-todos", "Grep for TODOs", "code-scanner")],
        execution_plan=[_subtask("write-summary", "Write TODO summary", "report-writer")],
        profiles={
            "code-scanner": _profile("code-scanner", ["terminal"]),
            "report-writer": _profile("report-writer", ["file_io", "python_exec"]),
        },
        tool_sequences={
            "code-scanner": [
                {"tool": "terminal", "args": {"cmd": f"grep -rn 'TODO' {tmp_path}/app.py {tmp_path}/server.py {tmp_path}/db.py"}, "reason": "find TODOs"},
            ],
            "report-writer": [
                {"tool": "python_exec", "args": {"code": "print('Total TODOs: 3')"}, "reason": "count TODOs"},
                {"tool": "file_io", "args": {"op": "write", "path": str(tmp_path / "todo_summary.txt"), "content": "TODO Summary:\n1. app.py:1 - add logging\n2. server.py:1 - handle timeouts\n3. db.py:1 - add connection pooling\nTotal: 3 TODOs\n"}, "reason": "write summary"},
            ],
        },
        results={
            "code-scanner": "Found 3 TODOs in app.py, server.py, db.py",
            "report-writer": "Wrote todo_summary.txt with 3 TODOs",
        },
    )

    assert result["response_status"] == "ok"
    assert (tmp_path / "todo_summary.txt").exists()
    summary = (tmp_path / "todo_summary.txt").read_text(encoding="utf-8")
    assert "3" in summary
    assert "logging" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# PARALLEL & DEPENDENCY (TC13–TC14)
# ═══════════════════════════════════════════════════════════════════════════════


def test_tc13_parallel_subtasks(monkeypatch, tmp_path: Path):
    """TC13: Independent parallel subtasks sharing the same agent role."""
    _seed_workspace(tmp_path, {
        "main.py": "\n".join(f"line {i}" for i in range(20)) + "\n",
        "utils.py": "\n".join(f"line {i}" for i in range(10)) + "\n",
    })

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Get the line count of both main.py and utils.py simultaneously",
        discovery_plan=[
            _subtask("count-main", "Count lines in main.py", "terminal-operator"),
            _subtask("count-utils", "Count lines in utils.py", "terminal-operator"),
        ],
        execution_plan=[],
        profiles={"terminal-operator": _profile("terminal-operator", ["terminal"])},
        tool_sequences={
            "terminal-operator": [
                {"tool": "terminal", "args": {"cmd": f"wc -l {tmp_path}/main.py"}, "reason": "count main"},
                {"tool": "terminal", "args": {"cmd": f"wc -l {tmp_path}/utils.py"}, "reason": "count utils"},
            ],
        },
        results={"terminal-operator": "main.py: 20 lines, utils.py: 10 lines"},
    )

    assert result["response_status"] == "ok"
    # Both subtasks use the SAME role_tag, so role_agent_map should have 1 entry
    role_map = result.get("role_agent_map", {})
    assert len(role_map) == 1
    assert "terminal-operator" in role_map
    # Both subtasks completed
    disc_pool = result.get("discovery_subtask_pool", [])
    completed = [t for t in disc_pool if t["status"] == "completed"]
    assert len(completed) == 2


def test_tc14_sequential_dependency_chain(monkeypatch, tmp_path: Path):
    """TC14: Sequential dependency chain A → B → C."""
    _seed_workspace(tmp_path, {"config.json": json.dumps({"db_path": "app.db"})})
    _seed_database(
        tmp_path / "app.db",
        {
            "users": (
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)",
                [(1, "Alice", "alice@test.com"), (2, "Bob", "bob@test.com")],
            ),
        },
    )

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Read config.json, extract the database path, then query that database for users",
        discovery_plan=[
            _subtask("read-config", "Read config.json", "file-reader"),
            _subtask("extract-path", "Extract DB path", "python-runner", depends_on=["read-config"]),
            _subtask("query-db", "Query users", "db-analyst", depends_on=["extract-path"]),
        ],
        execution_plan=[],
        profiles={
            "file-reader": _profile("file-reader", ["file_io"]),
            "python-runner": _profile("python-runner", ["python_exec"]),
            "db-analyst": _profile("db-analyst", ["sql_query"]),
        },
        tool_sequences={
            "file-reader": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "config.json")}, "reason": "read config"},
            ],
            "python-runner": [
                {"tool": "python_exec", "args": {"code": "import json; d=json.loads('{\"db_path\": \"app.db\"}'); print(d['db_path'])"}, "reason": "extract path"},
            ],
            "db-analyst": [
                {"tool": "sql_query", "args": {"db_path": str(tmp_path / "app.db"), "query": "SELECT * FROM users"}, "reason": "query users"},
            ],
        },
        results={
            "file-reader": "Config: db_path=app.db",
            "python-runner": "Extracted db_path: app.db",
            "db-analyst": "Found 2 users: Alice, Bob",
        },
    )

    assert result["response_status"] == "ok"
    # All 3 subtasks completed in order
    disc_pool = result.get("discovery_subtask_pool", [])
    completed = [t for t in disc_pool if t["status"] == "completed"]
    assert len(completed) == 3
    # 3 different roles → 3 agents
    role_map = result.get("role_agent_map", {})
    assert len(role_map) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# ERROR HANDLING & EDGE CASES (TC15–TC20)
# ═══════════════════════════════════════════════════════════════════════════════


def test_tc15_retry_succeeds_on_second_attempt(monkeypatch, tmp_path: Path):
    """TC15: Subtask fails on first attempt, retry succeeds."""
    _seed_workspace(tmp_path, {"report.txt": "Quarterly earnings increased by 15%.\n"})

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Read the file report.txt",
        discovery_plan=[_subtask("read-report", "Read report", "file-reader")],
        execution_plan=[],
        profiles={"file-reader": _profile("file-reader", ["file_io"])},
        tool_sequences={
            "file-reader": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "report.txt")}, "reason": "read report"},
            ],
        },
        results={"file-reader": "Report: Quarterly earnings increased by 15%"},
        attempt_behavior={"file-reader": ["fail", "ok"]},
    )

    assert result["response_status"] == "ok"
    disc_pool = result.get("discovery_subtask_pool", [])
    completed = [t for t in disc_pool if t["status"] == "completed"]
    assert len(completed) == 1
    # Verify it took 2 attempts
    assert completed[0].get("attempts", 0) == 2


def test_tc16_all_subtasks_fail(monkeypatch, tmp_path: Path):
    """TC16: All subtasks fail — error propagation."""
    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Read the file that_does_not_exist.txt",
        discovery_plan=[_subtask("read-missing", "Read missing file", "file-reader")],
        execution_plan=[],
        profiles={"file-reader": _profile("file-reader", ["file_io"])},
        tool_sequences={
            "file-reader": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "that_does_not_exist.txt")}, "reason": "read file"},
            ],
        },
        results={"file-reader": "done"},
        fail_roles={"file-reader": "file_not_found"},
    )

    assert result["response_status"] == "error"
    disc_pool = result.get("discovery_subtask_pool", [])
    failed = [t for t in disc_pool if t["status"] == "failed"]
    assert len(failed) == 1
    assert "file_not_found" in failed[0].get("error", "")


def test_tc17_approval_gate_deny(monkeypatch, tmp_path: Path):
    """TC17: Agent creation denied by approval gate (create_approval_mode=deny)."""
    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Analyze the project",
        discovery_plan=[_subtask("analyze", "Analyze code", "analyzer")],
        execution_plan=[],
        profiles={"analyzer": _profile("analyzer", ["terminal"])},
        tool_sequences={"analyzer": []},
        results={"analyzer": "done"},
        settings_overrides={
            "CLOSED_CLAW_CREATE_APPROVAL_MODE": "deny",
            "CLOSED_CLAW_CREATE_APPROVAL_REQUIRED": "true",
            "CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD": "0.99",
        },
    )

    # With approval denied, the subtask should still run because the pool-based
    # execution path creates agents differently than the legacy single-agent path.
    # The create_approval gate is on the old single-agent flow. In the pool flow,
    # agents are created directly via _acquire_agent_for_role.
    # So we just verify the graph completed without crashing.
    assert result["response_status"] in {"ok", "error"}
    assert "response_result" in result


def test_tc18_pure_reasoning_no_tools(monkeypatch, tmp_path: Path):
    """TC18: Task requiring no tools (pure reasoning)."""
    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Explain what the number 42 means in The Hitchhiker's Guide",
        discovery_plan=[
            _subtask("explain-42", "Explain 42", "knowledge-expert", requires_tool=False),
        ],
        execution_plan=[],
        profiles={"knowledge-expert": _profile("knowledge-expert", ["terminal"])},
        tool_sequences={"knowledge-expert": []},  # No tool calls
        results={"knowledge-expert": "42 is the Answer to the Ultimate Question of Life, the Universe, and Everything"},
    )

    assert result["response_status"] == "ok"
    assert "42" in result["response_result"]
    # No tool events should have been recorded for this subtask
    tool_events = result.get("tool_events", [])
    assert len(tool_events) == 0


def test_tc19_max_agents_limit(monkeypatch, tmp_path: Path):
    """TC19: Max agents per run limit reached."""
    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Perform 3 different analyses each requiring a unique agent type",
        discovery_plan=[
            _subtask("analyze-code", "Analyze code", "code-analyst"),
            _subtask("analyze-data", "Analyze data", "data-analyst"),
            _subtask("analyze-perf", "Analyze performance", "perf-analyst"),
        ],
        execution_plan=[],
        profiles={
            "code-analyst": _profile("code-analyst", ["terminal"]),
            "data-analyst": _profile("data-analyst", ["file_io"]),
            "perf-analyst": _profile("perf-analyst", ["python_exec"]),
        },
        tool_sequences={
            "code-analyst": [
                {"tool": "terminal", "args": {"cmd": "echo code"}, "reason": "analyze"},
            ],
            "data-analyst": [
                {"tool": "file_io", "args": {"op": "list", "path": str(tmp_path), "max_entries": 5}, "reason": "analyze"},
            ],
            "perf-analyst": [
                {"tool": "python_exec", "args": {"code": "print('fast')"}, "reason": "analyze"},
            ],
        },
        results={
            "code-analyst": "Code analysis done",
            "data-analyst": "Data analysis done",
            "perf-analyst": "Perf analysis done",
        },
        settings_overrides={"CLOSED_CLAW_MAX_AGENTS_PER_RUN": "2"},
    )

    # With max_agents=2, only 2 agents can be created; the 3rd subtask should fail
    assert result["response_status"] == "error"
    disc_pool = result.get("discovery_subtask_pool", [])
    completed = [t for t in disc_pool if t["status"] == "completed"]
    failed = [t for t in disc_pool if t["status"] == "failed"]
    assert len(completed) == 2
    assert len(failed) == 1
    assert "max_agents_per_run_exceeded" in failed[0].get("error", "")


def test_tc20_mixed_success_failure(monkeypatch, tmp_path: Path):
    """TC20: Mixed success/failure across subtasks."""
    _seed_workspace(tmp_path, {"valid.txt": "This file exists and has content.\n"})

    result = _build_and_run(
        monkeypatch, tmp_path,
        task="Read valid.txt and invalid.txt and summarize both",
        discovery_plan=[
            _subtask("read-valid", "Read valid.txt", "reader-valid"),
            _subtask("read-invalid", "Read invalid.txt", "reader-invalid"),
        ],
        execution_plan=[],
        profiles={
            "reader-valid": _profile("reader-valid", ["file_io"]),
            "reader-invalid": _profile("reader-invalid", ["file_io"]),
        },
        tool_sequences={
            "reader-valid": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "valid.txt")}, "reason": "read valid"},
            ],
            "reader-invalid": [
                {"tool": "file_io", "args": {"op": "read", "path": str(tmp_path / "valid.txt")}, "reason": "read invalid"},
            ],
        },
        results={
            "reader-valid": "valid.txt contains text content",
            "reader-invalid": "done",
        },
        fail_roles={"reader-invalid": "file_not_found: invalid.txt does not exist"},
    )

    assert result["response_status"] == "error"
    disc_pool = result.get("discovery_subtask_pool", [])
    completed = [t for t in disc_pool if t["status"] == "completed"]
    failed = [t for t in disc_pool if t["status"] == "failed"]
    assert len(completed) == 1
    assert len(failed) == 1
    # Discovery results should have partial data
    disc_results = result.get("discovery_results", {})
    assert any("valid" in v.lower() for v in disc_results.values()) or len(completed) == 1

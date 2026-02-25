# Coding Conventions

All conventions are enforced by the existing codebase. Follow them exactly when adding or modifying code.

---

## Python Version and Imports

- **Python 3.11** is required.
- Every module must start with `from __future__ import annotations`.
- Every module must have a `# Purpose: <one-line description>.` comment at the very top (after the future import).

```python
# Purpose: What this module does in one line.

from __future__ import annotations
```

---

## Pydantic Models

- **Never** import `BaseModel` or `Field` directly from `pydantic`.
- Always import from the compat shim:

```python
from closed_claw.compat import BaseModel, Field
```

This ensures Pydantic v1/v2 compatibility across installs.

---

## Configuration

- `Settings.from_env()` is the **only** way to load config.
- Call it at the CLI boundary (in `cmd_*` functions), never at module import time.
- Never hardcode file paths. All paths come from `Settings` fields (`db_path`, `agents_dir`, `run_logs_dir`, etc.).

```python
# Correct
def cmd_run(args):
    settings = Settings.from_env()
    db = settings.db_path

# Wrong
DB_PATH = Path(".closed_claw/registry.db")  # don't do this
```

---

## Async vs Sync

- Coordinator graph **nodes** are `async def`.
- `asyncio.run()` is used at the CLI boundary when invoking the graph.
- `AgentRunner` is synchronous internally (subprocess I/O loop).
- Async nodes call `AgentRunner` via `asyncio.get_event_loop().run_in_executor` or directly if already on a thread.

---

## Error Handling

- Raise specific exceptions rather than bare `Exception`.
- `AgentRuntimeError` — agent protocol violations or subprocess errors.
- `ToolExecutionError` — tool-level failures.
- `ValueError` — bad input/state in coordinator nodes.
- Always propagate errors up to the coordinator node level; do not swallow them silently.

---

## Protocol Messages

When adding new message types:
1. Add a Pydantic model to `closed_claw/runtime/protocol.py`.
2. Update `parse_agent_line()` to attempt parsing the new type.
3. Add the corresponding handler in `CoordinatorNodes.execute_task_pool`.

Message types must have a `type: Literal["<type_name>"]` discriminator field.

---

## Adding a New Tool

1. Add the tool name string to `SUPPORTED_TOOLS` list in `executor.py`.
2. Add an entry to `TOOL_REGISTRY` dict with `description` and `args_schema`.
3. Implement `_run_<tool_name>(self, **args)` in `ToolExecutor`.
4. Dispatch via the `if tool == "<tool_name>":` block in `ToolExecutor.execute()`.
5. Write a unit test in `tests/unit/test_tools_executor.py`.

---

## Adding a New CLI Command

1. Write `cmd_<name>(args: argparse.Namespace) -> int` in `cli.py`.
2. Register a subparser in `_build_parser()`:
   ```python
   p = sub.add_parser("name", help="...")
   p.set_defaults(func=cmd_<name>)
   ```
3. Return `0` on success, non-zero on failure.

---

## Adding a New Coordinator Node

1. Add `async def <node_name>(self, state: dict[str, Any]) -> dict[str, Any]` to `CoordinatorNodes` in `nodes.py`.
2. Always use `self._merge(state, **updates)` to return updated state — never mutate the input dict.
3. Use `self._emit_runlog(state, "event_name", {...})` for observability.
4. Register the node in `graph.py`:
   ```python
   graph.add_node("node_name", nodes.node_name)
   graph.add_edge("prev_node", "node_name")
   graph.add_edge("node_name", "next_node")
   ```

---

## Adding a New Base Skill Module

Base skill modules live in `agents/skills/` and form Layer 1 of every agent's system prompt. They describe how to use a specific tool domain at an expert level.

1. Create `agents/skills/<name>.md`. The file should cover:
   - When to use this capability
   - Concrete patterns / example tool call arguments
   - Error handling and edge cases
   - Output format expectations
2. Add the module name string to `_BASE_SKILL_IDS` in `closed_claw/registry/search.py` — this makes the LLM aware of the module when generating new agent profiles.
3. The LLM will automatically assign the module to relevant agents via `generate_agent_profile`. You can also manually add the name to an existing `manifest.json` `skill_ids` list.

**Rules:**
- Module names must be lowercase `snake_case` and match the filename without `.md`.
- Never include agent-specific content in a base skill module — that belongs in `agents/<agent_id>/skill.md`.
- `skill_ids` in `AgentManifest` are always validated against `_BASE_SKILL_IDS` in `_normalize_profile_payload`; unknown IDs are silently dropped.

---

## Testing

- One test file per module: `tests/unit/test_<module_name>.py`.
- Use `pytest` fixtures; no unittest classes unless unavoidable.
- Mock external calls (HTTP, subprocess, sqlite-vec) in unit tests.
- Integration tests in `tests/integration/` may use real SQLite but should not hit external APIs.
- Run all tests: `pytest -q`.

---

## Database Schema Changes

1. Edit `closed_claw/registry/schema.sql` (DDL-only, `CREATE TABLE IF NOT EXISTS`).
2. Update `RegistryStore` methods in `store.py` to use new columns/tables.
3. If adding a column to `agents`, update `AgentManifest` in `store.py`.
4. The schema is applied fresh on each `RegistryStore.__init__` via `executescript`.

---

## Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| Modules | `snake_case` | `registry/store.py` |
| Classes | `PascalCase` | `RegistryStore` |
| Functions/methods | `snake_case` | `cmd_delete_agent` |
| Constants | `UPPER_SNAKE` | `SUPPORTED_TOOLS` |
| Private methods | `_snake_case` | `_run_terminal` |
| Env vars | `CLOSED_CLAW_<NAME>` | `CLOSED_CLAW_DB_PATH` |
| Agent IDs | `uuid4().hex` (32 chars) | `a1b2c3d4...` |
| Run IDs | `uuid4().hex` (32 chars) | `e5f6g7h8...` |

---

## Logging and Observability

- **Never** use `print()` inside library code (coordinator, registry, tools, etc.).
- Use `RunLogger.emit(event, payload)` for structured run events.
- Use `AuditStore.record(...)` for compliance/audit events.
- Rich `Console.print()` is allowed in `cli.py`, `interactive.py`, `setup_wizard.py`, and `approval.py`.

---

## File I/O Safety

- Agent tool calls go through `ToolExecutor` which enforces `allowed_roots` (from `Settings.extra_allowed_paths`).
- Never allow agents to write outside their capsule dir or explicitly allowed paths.
- `sql_query` tool always validates the query starts with `SELECT` before execution.

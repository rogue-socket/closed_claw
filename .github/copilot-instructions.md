# Closed Claw — Copilot Instructions

## What This Repository Is

**Closed Claw** is a **local, capsule-based multi-agent orchestrator** written in Python 3.11. It lets a coordinator LLM discover, create, and run specialized "agent capsules", enforce human approvals before paid API usage, execute sandboxed tools on behalf of agents, and persist full run/audit observability — all locally via SQLite and a JSON-line subprocess protocol.

Current LLM provider support: `heuristic` (default, no API), `openai`, `gemini`, `claude`.

---

## Repository Layout (every file explained)

```
closed_claw/                   # main package
  __init__.py
  cli.py                       # argparse CLI entrypoints (init/run/agents/audit/doctor/setup/…)
  compat.py                    # thin pydantic v1/v2 shim (BaseModel, Field)
  config.py                    # Settings dataclass loaded from env/.env
  interactive.py               # Rich-powered interactive menu (python -m closed_claw.cli)
  setup_wizard.py              # guided provider/key setup + live verification

  coordinator/
    __init__.py
    graph.py                   # LangGraph StateGraph wiring — node edges, build_graph()
    nodes.py                   # CoordinatorNodes — all graph node implementations (~1200 lines)
    state.py                   # CoordinatorState TypedDict + Candidate TypedDict

  embeddings/
    __init__.py
    provider.py                # EmbeddingProvider — sentence-transformers or zero-vector fallback

  observability/
    __init__.py
    runlog.py                  # RunLogger — JSONL event emitter to .closed_claw/runs/<run_id>.jsonl

  policy/
    __init__.py
    approval.py                # ApprovalGate — human-in-the-loop prompt for paid API calls
    audit.py                   # AuditStore — writes audit_events rows to SQLite

  registry/
    __init__.py
    schema.sql                 # DDL for agents, runs, agent_vectors, audit_events, circuit_breakers
    search.py                  # RerankerProtocol, heuristic/LLM reranker, generate_task_plan/agent_profile
    store.py                   # RegistryStore — SQLite CRUD + semantic vector search (sqlite-vec)

  runtime/
    __init__.py
    protocol.py                # Pydantic models: CoordinatorRequest, ApiCallIntent/Decision, ToolCallIntent/Result, AgentResponse
    runner.py                  # AgentRunner — subprocess launch, JSON-line I/O loop, retries, circuit breaker

  tools/
    __init__.py
    executor.py                # ToolExecutor + SUPPORTED_TOOLS — sandboxed terminal/http/file/python/sql

  web/
    __init__.py
    server.py                  # FastAPI/Starlette web server (optional UI layer)
    ui.html                    # Single-page UI served by server.py

agents/                        # runtime-generated agent capsule directory
  registry.json                # flat list of all registered agent manifests
  <agent_id>/
    manifest.json              # AgentManifest (id, name, tools_allowlist, tags, metrics, embedding)
    skill.md                   # role definition injected into agent system prompt
    memory.db                  # per-agent SQLite episodic memory
    entrypoint.py              # agent subprocess entrypoint (speaks JSON-line protocol)
    logs/                      # per-run agent output artifacts

.closed_claw/                  # runtime data (gitignored)
  registry.db                  # SQLite: agents, runs, audit_events, circuit_breakers, agent_vectors
  runs/
    <run_id>.jsonl             # per-run event stream

docs/
  ARCHITECTURE.md              # deep architecture reference
  CODEBASE_MAP.md              # exhaustive per-file + per-class map (READ THIS FIRST for onboarding)
  CONVENTIONS.md               # coding conventions and patterns
  QUICKSTART.md                # fastest path to running the system
  todo.md                      # near-term roadmap (incomplete features)
  analysis.txt                 # scratch analysis notes

tests/
  integration/test_flow.py     # end-to-end coordinator flow test
  unit/                        # unit tests per module (see CODEBASE_MAP.md for coverage map)

.env.example                   # all env vars with defaults/comments
requirements.txt               # Python dependencies
pytest.ini                    # pytest config
```

---

## Core Concepts (mental model for any agent)

### 1. Capsule Agents
Every agent is a **capsule**: a directory under `agents/<agent_id>/` containing a manifest, a skill description, a memory database, and a subprocess entrypoint. Agents are not classes — they are **isolated subprocesses** that communicate via newline-delimited JSON on stdin/stdout.

### 2. Coordinator Graph
The coordinator is a **LangGraph `StateGraph`** with these nodes in order:
```
ingest_task → decompose_task → execute_task_pool → validate_outputs
  → update_registry_and_audit → synthesize_final_response → END
```
All node logic lives in `closed_claw/coordinator/nodes.py` (`CoordinatorNodes`). State is a plain `dict` (typed via `CoordinatorState`).

### 3. JSON-Line Protocol
Coordinator ↔ Agent communication is via `\n`-delimited JSON messages:
- **Coordinator → Agent**: `CoordinatorRequest` (task, context, config)
- **Agent → Coordinator (intents)**: `ApiCallIntent` | `ToolCallIntent`
- **Coordinator → Agent (responses)**: `ApiCallDecision` | `ToolCallResult`
- **Agent → Coordinator (final)**: `AgentResponse` (status, result, memory_updates, artifacts)

Protocol models are in `closed_claw/runtime/protocol.py`.

### 4. Registry + Semantic Search
Agents are stored in `registry.db`. On each task, the coordinator:
1. Embeds the task text (`EmbeddingProvider`)
2. Searches `agent_vectors` via sqlite-vec cosine similarity
3. Reranks candidates (heuristic or LLM)
4. Decides reuse vs. create (with optional human gate if confidence is low)

### 5. Policy Gates
Two approval gates exist in `closed_claw/policy/approval.py`:
- **Create/reuse gate**: triggered when best match is below `CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD`
- **API approval gate**: triggered when agent emits `api_call_intent` for a paid provider

Mode per gate: `interactive` (human prompt) | `approve` (auto-yes) | `deny` (auto-no).

### 6. Tool Execution
Agents never run tools directly. They emit `tool_call_intent` → coordinator validates against the agent's `tools_allowlist` → `ToolExecutor` runs the tool → `tool_call_result` returned.

Available tools: `terminal`, `http_api`, `web_fetch`, `file_io`, `python_exec`, `sql_query` (SELECT-only).

### 7. Configuration
Everything is controlled by env vars (or `.env` file). Key var prefix: `CLOSED_CLAW_`. See `closed_claw/config.py` → `Settings` dataclass for the full list. `Settings.from_env()` is the single entrypoint.

---

## How to Navigate for Common Tasks

| Goal | Where to look |
|------|--------------|
| Add a new CLI command | `closed_claw/cli.py` — add `cmd_<name>` function + register in `_build_parser()` |
| Add a new tool | `closed_claw/tools/executor.py` — add to `TOOL_REGISTRY` dict + implement in `ToolExecutor.execute()` |
| Add a new coordinator node | `closed_claw/coordinator/nodes.py` + wire in `graph.py` |
| Change approval logic | `closed_claw/policy/approval.py` |
| Change routing/reranking | `closed_claw/registry/search.py` |
| Add a new LLM provider | `closed_claw/registry/search.py` + `closed_claw/coordinator/nodes.py` (api_call_intent handler) |
| Change agent capsule template | `closed_claw/agents/factory.py` → `ENTRYPOINT_TEMPLATE` |
| Add a new config variable | `closed_claw/config.py` → `Settings` dataclass + `from_env()` |
| Change DB schema | `closed_claw/registry/schema.sql` (DDL) + `closed_claw/registry/store.py` |
| Change protocol messages | `closed_claw/runtime/protocol.py` |

---

## Key Conventions

- **Python 3.11**, `from __future__ import annotations` in every module.
- **Pydantic models** go through `closed_claw/compat.py` (`BaseModel`, `Field`) to abstract v1/v2 differences.
- **`Settings.from_env()`** is called fresh at CLI entry; never cache settings at module level.
- **All file paths** come from `Settings` (never hardcode paths).
- **Async** coordinator nodes use `async def` + `asyncio.run()` at the CLI boundary. `AgentRunner` is sync internally but called from async context.
- **`# Purpose:`** comment at top of every module explains what it does.
- Tests live in `tests/unit/` (one file per module) and `tests/integration/`.

---

## Running the System

```bash
# Setup
pip install -r requirements.txt
cp .env.example .env
python -m closed_claw.cli init
python -m closed_claw.cli doctor

# Run a task
python -m closed_claw.cli run "your task here"

# Interactive menu
python -m closed_claw.cli

# Non-interactive (CI/scripts)
python -m closed_claw.cli run "task" --create-approval-mode approve --api-approval-mode approve

# Tests
pytest -q
```

---

## What Is NOT Yet Implemented

(See `docs/todo.md` for full list)
- Real provider API clients (openai/gemini/claude integration stubs exist but are heuristic)
- Persistent background workers (task pool is in-run only, not durable)
- Production deployment (auth, service API, multi-tenant)
- UI (web/server.py skeleton exists)
- Playwright / browser automation
- Heartbeat / cron scheduling
- Hard safety guardrails

---

## Agent Authoring Notes

When writing a new agent entrypoint (`entrypoint.py`), the contract is:
1. Read one JSON line from `stdin` → parse as `CoordinatorRequest`
2. Optionally emit `tool_call_intent` / `api_call_intent` JSON lines to `stdout`, read the `tool_call_result` / `api_call_decision` response lines back
3. Emit exactly one final `AgentResponse` JSON line to `stdout`
4. Exit with code 0

See `closed_claw/runtime/protocol.py` for all message schemas.

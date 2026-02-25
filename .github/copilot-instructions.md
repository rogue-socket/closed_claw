# Closed Claw — Copilot Instructions

## What This Repository Is

**Closed Claw** is a **local, capsule-based multi-agent orchestrator** written in Python 3.11. It lets a coordinator LLM discover, create, and run specialized "agent capsules", enforce human approvals before paid API usage, execute sandboxed tools on behalf of agents, and persist full run/audit observability — all locally via SQLite and a JSON-line subprocess protocol.

Current LLM provider support: `siemens` (default, uses `SIEMENS_API_KEY`), `openai`, `gemini`, `claude`. The `heuristic` fallback is used when no API key is available.

---

## Repository Layout (every file explained)

```
closed_claw/                   # main package AND Python 3.11 venv root
                               # Activate venv: .\closed_claw\Scripts\Activate.ps1
  __init__.py
  cli.py                       # argparse CLI entrypoints (init/run/agents/audit/doctor/setup/web/…)
  compat.py                    # thin pydantic v1/v2 shim (BaseModel, Field)
  config.py                    # Settings dataclass loaded from env/.env
  interactive.py               # Rich-powered interactive menu (python -m closed_claw.cli)
  setup_wizard.py              # guided provider/key setup + live verification

  agents/                      # SOURCE package — agent capsule creation logic
    __init__.py
    factory.py                 # AgentFactory + ENTRYPOINT_TEMPLATE (v12, multi-step loop)
                               # v12: plan prompt includes full tool args_schema so LLM uses
                               #      correct arg names; fix prompt includes failing tool schema;
                               #      real LLM HTTP calls via stdlib urllib, system_prompt injection
                               # Template auto-migrated on `init`/`run`

  coordinator/
    __init__.py
    graph.py                   # LangGraph StateGraph wiring — node edges, build_graph()
    nodes.py                   # CoordinatorNodes — all graph node implementations
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
    executor.py                # ToolExecutor + SUPPORTED_TOOLS + TOOL_REGISTRY (with LLM-readable descriptions)
                               # _normalize_args() maps common LLM alias mistakes (e.g. command→cmd) before validation

  web/
    __init__.py
    server.py                  # FastAPI/Starlette dashboard — `cli web` → http://127.0.0.1:7860
    ui.html                    # Single-page UI served by server.py

agents/                        # runtime-generated agent capsule directory (gitignored contents)
  registry.json                # flat list of all registered agent manifests
  seed_terminal_master.py      # seed script to pre-register the Terminal Master specialist agent
  skills/                      # shared modular skill library (Layer 1 of system prompt composition)
    terminal.md                # Shell & command execution patterns
    python_scripting.md        # Python code / data-processing patterns
    git.md                     # Git workflow patterns
    file_system.md             # file_io tool patterns
    web_http.md                # http_api / web_fetch patterns
    sql_databases.md           # sql_query patterns (SQLite SELECT)
    data_analysis.md           # data ingestion → transform → report patterns
  <agent_id>/
    manifest.json              # AgentManifest (id, name, tools_allowlist, tags, skill_ids, metrics, embedding)
    skill.md                   # agent role overlay — identity, decision rules, output format (Layer 2)
    memory.db                  # per-agent SQLite episodic memory
    entrypoint.py              # agent subprocess entrypoint (JSON-line protocol, v12)
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

### 7. Skill System — Two-Layer System Prompt Composition
Every agent's LLM identity is built by `CoordinatorNodes._compose_system_prompt()` from two layers:

```
Layer 1 — Base skill modules (agents/skills/<skill_id>.md)
          Modular, reusable capability knowledge (terminal, git, python_scripting, …)
          Composed from the agent manifest's skill_ids list.

Layer 2 — Role overlay (agents/<agent_id>/skill.md)
          Agent-specific identity, decision rules, and output format.
```

The composed text is passed as `config["system_prompt"]` to the agent subprocess and injected as the system/role message in every LLM call. Adding more `skill_ids` broadens the agent's competence without touching `skill.md`. The skill library lives in `agents/skills/` and is shared across all agents.

### 8. Configuration
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
| Add a new LLM provider | `closed_claw/registry/search.py` + `coordinator/nodes.py` (`_execute_llm_http` in entrypoint template) + `config.py` |
| Change agent capsule template | `closed_claw/agents/factory.py` → `ENTRYPOINT_TEMPLATE` (bump version constant) |
| Add a new config variable | `closed_claw/config.py` → `Settings` dataclass + `from_env()` + `.env.example` |
| Change DB schema | `closed_claw/registry/schema.sql` (DDL) + `closed_claw/registry/store.py` |
| Change protocol messages | `closed_claw/runtime/protocol.py` |
| **Add a base skill module** | **Create `agents/skills/<name>.md` + add name to `_BASE_SKILL_IDS` in `registry/search.py`** |

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

```powershell
# Activate the venv (Windows)
.\closed_claw\Scripts\Activate.ps1

# First-time setup (venv already created as 'closed_claw')
pip install -r requirements.txt
copy .env.example .env           # then edit SIEMENS_API_KEY in .env
python -m closed_claw.cli init
python -m closed_claw.cli doctor

# Run a task
python -m closed_claw.cli run "your task here"

# Interactive menu
python -m closed_claw.cli

# Non-interactive (CI/scripts)
python -m closed_claw.cli run "task" --create-approval-mode approve --api-approval-mode approve

# With explicit provider override
python -m closed_claw.cli run "task" --llm-provider siemens --llm-model qwen3-30b-a3b-instruct-2507

# Launch web dashboard
python -m closed_claw.cli web          # http://127.0.0.1:7860

# Agent management
python -m closed_claw.cli agents
python -m closed_claw.cli delete-agent <agent_id>
python -m closed_claw.cli delete-all-agents

# Inspect a run
python -m closed_claw.cli runlog <run_id>
python -m closed_claw.cli cancel-run <run_id>

# Tests
pytest -q
```

---

## What Is NOT Yet Implemented

(See `docs/todo.md` for full list)
- Persistent background workers (task pool is in-run only, not durable — durable job system with heartbeat/lease is planned)
- Production deployment (auth, service API, multi-tenant)
- Playwright / browser automation
- Heartbeat / cron scheduling
- Hard safety guardrails
- Agent-to-agent communication + task queues
- soul.md personality layer for the coordinator

## What IS Now Working
- `siemens` provider (default) with Qwen3-30b — requires `SIEMENS_API_KEY` in `.env`
- `openai`, `gemini`, `claude` providers (real API clients wired)
- Real agent capsules created and persisted in `agents/`
- Web dashboard at `http://127.0.0.1:7860` (`cli web`)
- Auto-migration of legacy agent entrypoints on `init` / `run`
- Tool descriptions in `TOOL_REGISTRY` so the coordinator LLM picks tools intelligently
- **Skill system**: agents get a composed system prompt (base skill library + role overlay); LLM calls in the agent entrypoint now execute real HTTP requests (stdlib `urllib`, no new deps)
- **`agents/skills/`** shared library: 7 base skill modules (`terminal`, `python_scripting`, `git`, `file_system`, `web_http`, `sql_databases`, `data_analysis`)

---

## Agent Authoring Notes

When writing a new agent entrypoint (`entrypoint.py`), the contract is:
1. Read one JSON line from `stdin` → parse as `CoordinatorRequest`
2. Read `config["system_prompt"]` — the composed skill identity (base skill modules + role overlay)
3. Optionally emit `tool_call_intent` / `api_call_intent` JSON lines to `stdout`, read the `tool_call_result` / `api_call_decision` response lines back
   - For `api_call_intent` with `call_type="llm_completion"`: after approval, the entrypoint makes the real LLM HTTP call itself via `_execute_llm_http()` (stdlib `urllib`)
4. Emit exactly one final `AgentResponse` JSON line to `stdout`
5. Exit with code 0

`CoordinatorRequest.config` keys passed by v12:
- `llm` — `{provider, model, api_key, base_url, timeout_s}`
- `system_prompt` — composed two-layer identity string
- `tool_registry` — list of `{name, description, args_schema}` for tools in the allowlist

See `closed_claw/runtime/protocol.py` for all message schemas.

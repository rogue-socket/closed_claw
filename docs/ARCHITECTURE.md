# Architecture

> Deep reference. For a quick file-by-file map, see `CODEBASE_MAP.md`. For conventions, see `CONVENTIONS.md`.

---

## Overview

Closed Claw is a **local capsule-based multi-agent orchestrator**. A coordinator LLM routes incoming tasks to specialized agent subprocesses ("capsules"), enforces approval gates, executes tools on behalf of agents, and records full audit/observability traces — all on a single machine, no external services required by default.

**Primary components:**

| Component | Location | Role |
|-----------|----------|------|
| Coordinator graph | `coordinator/graph.py`, `coordinator/nodes.py` | Central LangGraph state machine that drives every run |
| Agent capsules | `agents/<agent_id>/` | Isolated subprocesses; implement the JSON-line protocol |
| Registry + vectors | `registry/store.py`, `registry/schema.sql` | SQLite store for agents, runs, audit events; sqlite-vec for semantic search |
| Runtime protocol | `runtime/protocol.py`, `runtime/runner.py` | JSON-line message framing + subprocess lifecycle |
| Policy engine | `policy/approval.py`, `policy/audit.py` | Human-in-the-loop gates; structured audit trail |
| Tool execution | `tools/executor.py` | Sandboxed terminal, HTTP, file I/O, Python, SQL tools |
| Embeddings | `embeddings/provider.py` | sentence-transformers or zero-vector fallback |
| Run logs | `observability/runlog.py` | Per-run JSONL event stream |
| CLI | `cli.py` | All user-facing commands |

---

## Coordinator Flow

Every `run` command follows this LangGraph node sequence:

```
ingest_task
    │  Validate task string; assign run_id + session_id; initialize state
    ▼
decompose_task
    │  LLM/heuristic: split task into atomic subtasks with role tags + dependency graph
    ▼
execute_task_pool
    │  For each subtask (dependency-aware):
    │    1. Embed role description → semantic search → rerank → reuse or create agent
    │    2. Launch agent subprocess
    │    3. Drive JSON-line I/O loop:
    │       - api_call_intent  → ApprovalGate + circuit breaker → ApiCallDecision
    │       - tool_call_intent → allowlist check → ToolExecutor → ToolCallResult
    │    4. Receive AgentResponse; record result + metrics
    ▼
validate_outputs
    │  Assert all subtasks completed OK; surface failures
    ▼
update_registry_and_audit
    │  Update agent usage_count / success_rate / avg_latency; write audit rows
    ▼
synthesize_final_response
    │  Merge subtask results into a single response string
    ▼
    END
```

State flows as an immutable-merge dict (`CoordinatorState`) through every node. Each node returns `_merge(state, **updates)` — it never mutates the input.

---

## Agent Capsule Model

### Directory Structure

```
agents/<agent_id>/
  manifest.json   # AgentManifest — identity, metrics, tools_allowlist, tags, embedding
  skill.md        # Role definition — injected as system prompt into the agent
  memory.db       # SQLite episodic memory (key/value; agent-writable)
  entrypoint.py   # Subprocess entry; speaks JSON-line protocol
  logs/           # Per-run output artifacts
```

### Lifecycle

1. **Creation:** `CoordinatorNodes._create_agent` calls `AgentFactory.create`, which writes the capsule directory. `generate_agent_profile` (via LLM or heuristic) produces `skill.md` and `tools_allowlist`.
2. **Reuse:** If a registered agent's embedding is above `low_confidence_threshold` for the current task, it is reused directly.
3. **Execution:** `AgentRunner.run(agent_dir, request, on_intent)` launches `entrypoint.py` as a subprocess and drives the I/O loop.
4. **Metrics:** After each run, `RegistryStore.update_agent_metrics` updates `usage_count`, `success_rate`, `avg_latency_ms`.

---

## JSON-Line Protocol

Transport: `\n`-delimited JSON over **stdin** (coordinator → agent) and **stdout** (agent → coordinator).

### Message Flow

```
Coordinator                         Agent
────────────                        ─────
                CoordinatorRequest
               ─────────────────────►
                                    (optional, repeat):
                ToolCallIntent
               ◄─────────────────────
                ToolCallResult
               ─────────────────────►
                ApiCallIntent
               ◄─────────────────────
                ApiCallDecision
               ─────────────────────►
                AgentResponse (final)
               ◄─────────────────────
```

### Message Types (all in `runtime/protocol.py`)

| Type | Direction | Key fields |
|------|-----------|-----------|
| `CoordinatorRequest` | → Agent | `session_id`, `task`, `context`, `artifacts`, `config` |
| `ToolCallIntent` | ← Agent | `tool`, `args`, `reason` |
| `ToolCallResult` | → Agent | `ok`, `result`, `error` |
| `ApiCallIntent` | ← Agent | `provider`, `endpoint`, `estimated_cost_usd`, `reason` |
| `ApiCallDecision` | → Agent | `approved`, `note` |
| `AgentResponse` | ← Agent | `status`, `result`, `memory_updates`, `artifacts`, `metrics` |

An agent **must** emit exactly one `AgentResponse` as its final line.

---

## Registry and Semantic Search

### Storage

All persistent state lives in `.closed_claw/registry.db` (SQLite):

| Table | Purpose |
|-------|---------|
| `agents` | Agent manifest (JSON blob) + indexed scalar fields |
| `agent_vectors` | sqlite-vec virtual table for cosine similarity search |
| `runs` | Run history with status, latency, agent reference |
| `agent_compositions` | Multi-agent composition records |
| `provider_circuit_breakers` | Per-provider failure counters + reset timestamps |
| `audit_events` | Structured audit trail |

A human-readable mirror lives at `agents/registry.json`.

### Routing Pipeline

```
task text
    │
    ▼
EmbeddingProvider.embed(task)       # 384-dim vector
    │
    ▼
RegistryStore.search_agents(vector) # cosine similarity via sqlite-vec
    │
    ▼
Reranker.rerank(task, candidates)   # HeuristicReranker or LLMReranker
    │
    ▼
score < low_confidence_threshold?
    │ yes → ApprovalGate (create/reuse) → create new capsule
    │ no  → reuse existing agent
```

`build_reranker(settings)` in `registry/search.py` selects `HeuristicReranker` (default) or `LLMReranker` based on `settings.llm_provider`.

---

## Policy and Safety

### Approval Gates

Two gates, each independently configurable:

| Gate | Trigger | Config var |
|------|---------|-----------|
| Create/reuse gate | Reranker score below threshold | `CLOSED_CLAW_CREATE_APPROVAL_MODE` |
| API approval gate | Agent emits `api_call_intent` for a paid provider | `CLOSED_CLAW_API_APPROVAL_MODE` |

Modes for each gate:
- `interactive` — prompts operator on stdin with `api_approval_timeout_sec` deadline
- `approve` — auto-approves (use in CI scripts: `--api-approval-mode approve`)
- `deny` — auto-denies

### Circuit Breaker

Tracks per-provider failure/denial counts in `provider_circuit_breakers` table. After `CLOSED_CLAW_CIRCUIT_BREAKER_FAILURES` consecutive failures, the provider is blocked until `CLOSED_CLAW_CIRCUIT_BREAKER_RESET_SEC` elapses. This prevents runaway retry loops against paid APIs.

### Tool Sandbox

- Each agent's `manifest.json` has a `tools_allowlist` — only listed tool names are executable by that agent.
- `ToolExecutor` enforces `allowed_roots` filesystem roots (from `Settings.extra_allowed_paths`).
- `sql_query` validates the query begins with `SELECT` before execution.

---

## Tool Execution Layer

Tools are invoked by the coordinator on behalf of agents (agents never execute tools directly).

| Tool | Description |
|------|-------------|
| `terminal` | Shell command in workspace |
| `http_api` | HTTP request (GET/POST/etc.) |
| `web_fetch` | Fetch + extract text from a URL |
| `file_io` | list/read/write/append text files within allowed roots |
| `python_exec` | Execute a Python code snippet in a subprocess |
| `sql_query` | SELECT-only SQLite query |

Flow: `tool_call_intent` → allowlist check → `ToolExecutor.execute(tool, args)` → `ToolCallResult`.

---

## Configuration System

All configuration is via environment variables (read from `os.environ` first, then `.env` file).

Key variable groups:

| Group | Prefix/Example |
|-------|---------------|
| Storage paths | `CLOSED_CLAW_DB_PATH`, `CLOSED_CLAW_AGENTS_DIR`, `CLOSED_CLAW_RUN_LOGS_DIR` |
| Approval modes | `CLOSED_CLAW_CREATE_APPROVAL_MODE`, `CLOSED_CLAW_API_APPROVAL_MODE` |
| LLM provider | `CLOSED_CLAW_LLM_PROVIDER`, `CLOSED_CLAW_LLM_MODEL` |
| API keys | `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` |
| Thresholds | `CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD`, `CLOSED_CLAW_AGENT_TIMEOUT_SEC` |
| Circuit breaker | `CLOSED_CLAW_CIRCUIT_BREAKER_FAILURES`, `CLOSED_CLAW_CIRCUIT_BREAKER_RESET_SEC` |
| Sandbox | `CLOSED_CLAW_EXTRA_ALLOWED_PATHS` (comma-separated absolute paths) |

See `.env.example` for the complete list with defaults.

---

## Task Pool Execution Model

### Current Behavior (in-run only)

The supervisor owns the task pool loop for the duration of a single `run` invocation:

```
waiting → pending → in_progress → completed / failed
```

- Dependencies are enforced: a subtask stays `waiting` until all its prerequisite subtasks are `completed`.
- Role tags determine which agent handles each subtask.
- If a subtask reaches `CLOSED_CLAW_SUBTASK_MAX_ATTEMPTS` (default 2) failures, it is marked `failed`.
- Status updates are emitted as `task_pool_update` events to the run JSONL log.

### Known Limitation

The task pool is **not durable** — if the CLI process exits mid-run, the task pool is lost. There is no persistent background worker. See `docs/todo.md` for the target architecture.

---

## Extensibility Points

| What to extend | Where |
|----------------|-------|
| Add a real LLM provider client | `registry/search.py` (reranker) + `coordinator/nodes.py` (`_handle_api_call_intent`) |
| Add a new tool | `tools/executor.py` — `SUPPORTED_TOOLS`, `TOOL_REGISTRY`, `ToolExecutor` |
| Change agent capsule template | `closed_claw/agents/factory.py` — `ENTRYPOINT_TEMPLATE` |
| Add a coordinator node | `coordinator/nodes.py` + `coordinator/graph.py` |
| Add a CLI command | `cli.py` — `cmd_<name>` + `_build_parser` |
| Change approval logic | `policy/approval.py` |
| Change routing algorithm | `registry/search.py` — implement `RerankerProtocol` |
| Add DB columns | `registry/schema.sql` + `registry/store.py` + `registry/store.AgentManifest` |
| Add a web API endpoint | `web/server.py` |

---

## Interactive UX

When invoked with no subcommand (`python -m closed_claw.cli`), the system opens a Rich-powered interactive menu:

- ASCII art shown once at startup
- Numbered options: setup / init / doctor / run / list agents / inspect agent / delete agent
- All menu paths delegate to the same `cmd_*` functions used by the CLI

The `setup` wizard prompts for provider → model → API key → runs a live verification request → writes `.env`. No config is saved until the user confirms.

---

## Data Layout

```
.closed_claw/             # runtime data (gitignored)
  registry.db             # SQLite main database
  runs/
    <run_id>.jsonl        # per-run event stream (one line per event)

agents/                   # agent capsule store
  registry.json           # human-readable agent list
  <agent_id>/
    manifest.json
    skill.md
    memory.db
    entrypoint.py
    logs/
```



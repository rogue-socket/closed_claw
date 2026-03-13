<div align="center">

# 🦞 Closed Claw

**A local, capsule-based multi-agent orchestrator for Python 3.11**

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)
[![License](https://img.shields.io/badge/license-proprietary-lightgrey.svg)]()

*Discover, create, and run specialized agent capsules — with human approval gates, sandboxed tools, and full observability — all locally via SQLite and a JSON-line subprocess protocol.*

</div>

---

## Overview

Closed Claw lets a coordinator LLM automatically **discover or create specialized agents**, run them as isolated subprocesses, enforce **human approvals** before paid API usage, execute **sandboxed tools** on behalf of agents, and persist full **run and audit observability**. Everything runs locally — no external orchestration services needed.

### Key Capabilities

- **Capsule-based agents** — each agent is an isolated subprocess with its own manifest, skills, memory, and entrypoint
- **Two-phase task execution** — discovery phase gathers information, execution phase takes action
- **ReAct-style reasoning** (v13) — agents observe → think → act each step, with the LLM deciding the next action dynamically
- **Semantic agent routing** — embed tasks, search agent vectors via sqlite-vec, rerank with LLM
- **Human approval gates** — configurable per create/reuse decisions and paid API calls (interactive, auto-approve, auto-deny, or web UI)
- **Sandboxed tool execution** — per-agent allowlists enforce which tools each agent can use
- **Web dashboard** — full CRUD, SSE streaming, run creation, settings editor, and web-mode approvals
- **Skill composition system** — modular skill library (Layer 1) + agent-specific role overlay (Layer 2)
- **Full observability** — JSONL run logs, SQLite audit events, per-agent episodic memory

### Supported LLM Providers

| Provider | Default Model | API Key Env Var |
|----------|--------------|-----------------|
| `openai` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `gemini` | `gemini-2.5-flash` | `GEMINI_API_KEY` |
| `claude` | `claude-3-5-haiku-latest` | `ANTHROPIC_API_KEY` |

---

## Quick Start

### 1. Activate the venv

```powershell
# Windows (PowerShell)
.\closed_claw\Scripts\Activate.ps1
```
```bash
# macOS / Linux
source closed_claw/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

```powershell
copy .env.example .env      # Windows
# cp .env.example .env      # macOS/Linux
```

Edit `.env` and set your API key for your chosen provider, or use the interactive wizard:

```bash
python -m closed_claw.cli setup
```

The wizard walks through provider selection → model → API key → live verification → save.

### 4. Initialize & verify

```bash
python -m closed_claw.cli init
python -m closed_claw.cli doctor
```

### 5. Run a task

```bash
# Interactive mode (recommended)
python -m closed_claw.cli

# Direct run
python -m closed_claw.cli run "analyze the project structure and summarize key files"

# Non-interactive (CI / scripts)
python -m closed_claw.cli run "task" --create-approval-mode approve --api-approval-mode approve

# With provider override
python -m closed_claw.cli run "task" --llm-provider openai --llm-model gpt-4o-mini
```

### 6. Launch web dashboard

```bash
python -m closed_claw.cli web          # → http://127.0.0.1:7860
```

---

## Architecture

```
User Task
   │
   ▼
┌─────────────────────────────────────────────────────┐
│              Coordinator (LangGraph StateGraph)      │
│                                                      │
│  ingest_task → decompose_task → execute_task_pool    │
│       → validate_outputs → update_registry_and_audit │
│       → synthesize_final_response → END              │
└───────────────────┬─────────────────────────────────┘
                    │
          ┌─────────┼─────────┐
          ▼         ▼         ▼
     ┌─────────┐ ┌─────────┐ ┌─────────┐
     │ Agent 1 │ │ Agent 2 │ │ Agent N │   ← isolated subprocesses
     │ (v13    │ │ (v13    │ │ (v13    │
     │ ReAct)  │ │ ReAct)  │ │ ReAct)  │
     └────┬────┘ └────┬────┘ └────┬────┘
          │           │           │
          ▼           ▼           ▼
    JSON-line protocol (stdin/stdout)
          │           │           │
          ▼           ▼           ▼
  ┌────────────────────────────────────┐
  │   Coordinator Tool Executor        │
  │   (allowlist-enforced per agent)   │
  └────────────────────────────────────┘
```

### How It Works

1. **Ingest** — user task is received and embedded
2. **Decompose** — LLM generates a task plan (two-phase: discovery → execution)
3. **Execute** — agents are matched (semantic search + LLM reranking) or created on the fly; each agent runs in a subprocess using the v13 ReAct loop (observe → think → act, up to 15 steps)
4. **Validate** — subtask outputs are checked against acceptance criteria
5. **Audit** — run metrics, agent usage, approvals, and tool events are persisted
6. **Synthesize** — LLM combines all subtask results into a coherent final response

### Agent Capsules

Every agent lives in `agents/<agent_id>/` as a self-contained capsule:

```
agents/<agent_id>/
  manifest.json      # id, name, tools_allowlist, tags, skill_ids, metrics, embedding
  skill.md           # agent-specific role, decision rules, output format (Layer 2)
  memory.db          # per-agent SQLite episodic memory
  entrypoint.py      # subprocess entrypoint (v13 ReAct, JSON-line protocol)
  logs/              # per-run output artifacts
```

### Skill Composition (Two-Layer System Prompt)

```
Layer 1 — Base skill modules (agents/skills/*.md)
          Shared, reusable knowledge: terminal, git, python_scripting,
          file_system, web_http, sql_databases, data_analysis

Layer 2 — Role overlay (agents/<agent_id>/skill.md)
          Agent-specific identity, decision rules, output format
```

Both layers are composed into a single system prompt passed to the agent subprocess.

---

## CLI Reference

| Command | Description |
|---------|-------------|
| *(no command)* | Launch interactive menu (15 options) |
| `setup` | Interactive provider/model/API key wizard with live verification |
| `init` | Initialize local DB, schema, and agent infrastructure |
| `doctor` | Validate environment: provider, keys, sqlite-vec, dependencies |
| `run <task>` | Execute a task through the full coordinator pipeline |
| `agents` | List registered agents with tools and skill summaries |
| `agent <id>` | Show full agent detail (manifest, tools, skill.md, memory stats) |
| `runs` | List historical runs |
| `audit` | List audit events |
| `runlog <run_id>` | Show JSONL event stream for a run |
| `cancel-run <run_id>` | Gracefully stop an active run |
| `tools` | List supported tools (optionally filtered by agent) |
| `delete-agent <id>` | Delete one agent capsule and registry records |
| `delete-all-agents` | Delete all agents and capsule directories |
| `web` | Launch web dashboard (`--host`, `--port` options) |

### `run` Options

```
python -m closed_claw.cli run <task>
    [--session-id <id>]
    [--context-json '<json>' | /path/to/context.json]
    [--create-approval-mode interactive|approve|deny]
    [--api-approval-mode interactive|approve|deny]
    [--llm-provider openai|gemini|claude]
    [--llm-model <model>]
    [--organize-path /absolute/path]
    [--organize-dry-run]
    [--organize-recursive]
```

---

## Tools

Agents never execute tools directly — they emit `tool_call_intent` messages, the coordinator validates against the agent's `tools_allowlist`, and `ToolExecutor` runs the tool in a sandboxed manner.

| Tool | Description | Key Args |
|------|-------------|----------|
| `terminal` | Run a shell command in the workspace | `cmd`, `timeout_s` |
| `http_api` | Make an HTTP request (any method) | `url`, `method`, `headers`, `json`, `params` |
| `web_fetch` | Fetch a webpage and return content | `url`, `timeout_s` |
| `file_io` | List, read, write, or append files in allowed paths | `op` (list\|read\|write\|append), `path`, `content` |
| `python_exec` | Execute a Python snippet (no stdin) | `code`, `timeout_s` |
| `sql_query` | Execute a SELECT query on a SQLite DB | `db_path`, `query`, `params` |

Common LLM alias mistakes (e.g. `command` → `cmd`, `body` → `json`) are auto-normalized before validation.

---

## Web Dashboard

Launch with `python -m closed_claw.cli web` → **http://127.0.0.1:7860**

The single-page web UI provides:
- **Agent management** — browse, inspect, and delete agents
- **Run management** — create runs, view status, cancel, and analyze results
- **Live streaming** — SSE-powered real-time run log and event streaming
- **Settings editor** — update configuration and API keys
- **Approval queue** — resolve pending approvals in web mode
- **Skill browser** — view base skill modules
- **Tool tester** — test tools directly from the UI
- **System stats** — agent count, run history, system health

28 REST API endpoints available under `/api/`.

---

## Configuration

All configuration is via environment variables (or `.env` file). Prefix: `CLOSED_CLAW_`.

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOSED_CLAW_DB_PATH` | `.closed_claw/registry.db` | SQLite registry database path |
| `CLOSED_CLAW_AGENTS_DIR` | `agents` | Agent capsule directory |
| `CLOSED_CLAW_RUN_LOGS_DIR` | `.closed_claw/runs` | JSONL run log directory |
| `CLOSED_CLAW_EXTRA_ALLOWED_PATHS` | *(empty)* | Comma-separated paths for tool operations |

### LLM Provider

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOSED_CLAW_LLM_PROVIDER` | `openai` | Active provider: `openai` \| `gemini` \| `claude` |
| `CLOSED_CLAW_LLM_MODEL` | *(per provider)* | Model identifier |
| `CLOSED_CLAW_LLM_TIMEOUT_SEC` | `45` | LLM request timeout |
| `CLOSED_CLAW_LLM_API_KEY` | *(empty)* | Generic fallback key |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | | OpenAI credentials |
| `GEMINI_API_KEY` / `GEMINI_BASE_URL` | | Google Gemini credentials |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | | Anthropic Claude credentials |

### Approval & Policy

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOSED_CLAW_CREATE_APPROVAL_REQUIRED` | `true` | Require approval for agent create/reuse decisions |
| `CLOSED_CLAW_CREATE_APPROVAL_MODE` | `interactive` | Mode: `interactive` \| `approve` \| `deny` \| `web` |
| `CLOSED_CLAW_API_APPROVAL_MODE` | `interactive` | Approval mode for paid API calls |
| `CLOSED_CLAW_PAID_API_PROVIDERS` | *(empty)* | Comma-separated providers requiring approval |
| `CLOSED_CLAW_API_APPROVAL_TIMEOUT_SEC` | `30` | Timeout for approval prompts |
| `CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD` | `0.62` | Triggers human gate when match score is below this |

### Guardrails

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOSED_CLAW_SUBTASK_MAX_ATTEMPTS` | `2` | Retry count for failed subtasks |
| `CLOSED_CLAW_MAX_TOOL_CALLS_PER_AGENT` | `50` | Max tool intents per agent run |
| `CLOSED_CLAW_MAX_AGENTS_PER_RUN` | `10` | Max agents created/used per run |
| `CLOSED_CLAW_MAX_SUBTASKS_PER_PHASE` | `4` | Max subtasks per discovery/execution phase |
| `CLOSED_CLAW_AGENT_TIMEOUT_SEC` | `120` | Agent subprocess timeout |
| `CLOSED_CLAW_AGENT_RETRIES` | `2` | Agent subprocess retry count |
| `CLOSED_CLAW_CIRCUIT_BREAKER_FAILURES` | `3` | Failures before circuit breaker opens |
| `CLOSED_CLAW_CIRCUIT_BREAKER_RESET_SEC` | `120` | Circuit breaker reset window |

### Embeddings

| Variable | Default | Description |
|----------|---------|-------------|
| `CLOSED_CLAW_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `CLOSED_CLAW_EMBEDDING_DIM` | `384` | Embedding vector dimension |
| `CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS` | `false` | Use real embeddings (vs SHA-256 hash fallback) |
| `CLOSED_CLAW_REQUIRE_SQLITE_VEC` | `true` | Require sqlite-vec extension |

---

## Project Structure

```
closed_claw/                    # Main package + Python 3.11 venv
  cli.py                        # CLI entrypoints (14 commands)
  config.py                     # Settings dataclass (34 fields)
  interactive.py                # Rich-powered interactive menu (15 options)
  setup_wizard.py               # Guided provider/key setup + live verification
  compat.py                     # Pydantic v1/v2 compatibility shim
  agents/
    factory.py                  # AgentFactory + v13 entrypoint template
  coordinator/
    graph.py                    # LangGraph StateGraph (6 nodes)
    nodes.py                    # CoordinatorNodes (~1650 lines)
    state.py                    # CoordinatorState TypedDict (28 fields)
  embeddings/
    provider.py                 # sentence-transformers or SHA-256 fallback
  observability/
    runlog.py                   # JSONL event emitter
  policy/
    approval.py                 # ApprovalGate (interactive|approve|deny|web)
    audit.py                    # AuditStore (SQLite audit_events)
  registry/
    schema.sql                  # DDL (agents, runs, agent_vectors, etc.)
    search.py                   # LLMReranker, task plan generation, agent profiles
    store.py                    # RegistryStore (SQLite CRUD + vector search)
  runtime/
    protocol.py                 # JSON-line protocol models
    runner.py                   # AgentRunner (subprocess launch, I/O loop)
  tools/
    executor.py                 # ToolExecutor + TOOL_REGISTRY (6 tools)
  web/
    server.py                   # FastAPI dashboard + REST API (28 endpoints)
    ui.html                     # Single-page web UI

agents/                         # Runtime-generated agent capsules
  registry.json                 # Flat agent manifest index
  seed_terminal_master.py       # Seed script for Terminal Master agent
  skills/                       # Shared skill library (7 modules)
    terminal.md, python_scripting.md, git.md, file_system.md,
    web_http.md, sql_databases.md, data_analysis.md

.closed_claw/                   # Runtime data (gitignored)
  registry.db                   # SQLite registry
  runs/<run_id>.jsonl           # Per-run event streams

docs/                           # Documentation
  ARCHITECTURE.md               # Deep architecture reference
  CODEBASE_MAP.md               # Exhaustive per-file + per-class map
  CONVENTIONS.md                # Coding conventions and patterns
  QUICKSTART.md                 # Fastest setup path
  todo.md                       # Near-term roadmap
  BUG_REPORT.md                 # Known bugs and fixes

tests/
  unit/                         # Unit tests (15 files, one per module)
  integration/test_flow.py      # End-to-end coordinator flow test
```

---

## Dependencies

```
langchain        >=0.3,<0.4         # LLM chain framework
langgraph        >=0.2,<0.3         # StateGraph orchestration
sentence-transformers >=3.0,<4.0    # Embedding models
sqlite-vec       >=0.1,<0.2         # Vector similarity in SQLite
pydantic         >=2.8,<3.0         # Data validation
typer            >=0.12,<0.13       # CLI framework
httpx            >=0.27,<0.28       # HTTP client
tenacity         >=8.3,<9.0         # Retry logic
rich             >=13.7,<14.0       # Terminal formatting
fastapi          >=0.115,<0.116     # Web framework
uvicorn          >=0.30,<0.32       # ASGI server
python-multipart >=0.0.9,<0.1       # Form data handling
pytest           >=8.2,<9.0         # Test framework
pytest-asyncio   >=0.23,<0.24       # Async test support
```

---

## Testing

```bash
pytest -q
```

Tests live in `tests/unit/` (one file per module) and `tests/integration/`.

---

## Troubleshooting

### sqlite-vec load failure

Ensure the package is installed, then set the explicit extension path:

```powershell
# PowerShell
$env:SQLITE_VEC_PATH = python -c "import sqlite_vec; print(sqlite_vec.loadable_path())"
python -m closed_claw.cli doctor
```

```bash
# Bash
export SQLITE_VEC_PATH="$(python -c 'import sqlite_vec; print(sqlite_vec.loadable_path())')"
python -m closed_claw.cli doctor
```

### Slow startup from model download

Disable sentence-transformers to use the SHA-256 hash fallback:

```
CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS=false
```

### Interactive prompts block in automation

Use policy override flags:

```bash
python -m closed_claw.cli run "task" \
  --create-approval-mode approve \
  --api-approval-mode approve
```

### Doctor shows provider/key issues

Re-run the setup wizard to reconfigure:

```bash
python -m closed_claw.cli setup
```

---

## Safety & Security

- **Allowlist-enforced tools** — each agent can only use tools listed in its `manifest.json.tools_allowlist`
- **SQL injection prevention** — `sql_query` is restricted to `SELECT` statements only
- **Paid API approval gates** — human approval required before external API calls (configurable per mode)
- **Circuit breaker** — automatically blocks failing providers after repeated failures until a reset window passes
- **Guardrail limits** — hard caps on tool calls, agents per run, subtasks per phase, and retry attempts
- **Subprocess isolation** — agents run as isolated subprocesses, not in-process code execution

---

## Further Reading

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — fastest path to running the system
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — deep architecture reference
- [docs/CODEBASE_MAP.md](docs/CODEBASE_MAP.md) — exhaustive per-file and per-class map
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — coding conventions and patterns

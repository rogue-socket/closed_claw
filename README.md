# Closed Claw

Closed Claw is a local capsule-based multi-agent orchestrator.

It lets a coordinator:
- discover or create specialized agents,
- run them through a structured JSON protocol,
- enforce human approvals before paid API usage,
- execute allowed tools on behalf of agents,
- and persist runs, audit logs, and agent memory.

Additional docs:
- `docs/QUICKSTART.md`
- `docs/ARCHITECTURE.md`

LLM provider support:
- `heuristic` (default, no external API)
- `openai`
- `gemini`
- `claude`

## What You Get

- **Agent capsules** in `agents/<agent_id>/` with:
  - `manifest.json`
  - `skill.md`
  - `memory.db`
  - `entrypoint.py`
  - `logs/`
- **Registry + metrics** in SQLite (`.closed_claw/registry.db`)
- **Semantic routing** (sqlite-vec if enabled)
- **Approval gates**
  - low-confidence create/reuse
  - external paid API calls
- **Tool execution layer** with per-agent allowlists
- **Run/audit observability**
  - run logs: `.closed_claw/runs/<run_id>.jsonl`
  - audit events: `audit_events` table

## Current Status

Implemented and working:
- coordinator graph flow,
- capsule creation/reuse,
- JSON protocol runtime,
- approval policy modes,
- tool-call intents and execution,
- audit + run logs,
- CLI operations,
- tests.

Not implemented yet:
- provider-specific production API integrations,
- production deployment stack (auth, service API, multi-tenant runtime).

## Architecture (High Level)

1. User runs task via CLI.
2. Coordinator embeds task and searches registry.
3. Candidates are reranked (heuristic currently).
3. Candidates are reranked (heuristic or configured LLM provider).
4. Coordinator decides reuse vs create (with optional human gate).
5. Agent runs in subprocess via JSON line protocol.
6. Agent may request:
   - `api_call_intent` (approval required for paid providers)
   - `tool_call_intent` (enforced against allowlist)
7. Coordinator records run metrics, approvals, tool events, and final response.

## Requirements

- Conda
- Python 3.11
- macOS/Linux shell
- Dependencies from `requirements.txt`

## Quick Start

### 1) Create environment

```bash
conda create -n closed_claw python=3.11 -y
conda activate closed_claw
```

### 2) Install dependencies

```bash
cd /Users/yashagrawal/Documents/closed_claw/closed_claw
pip install -r requirements.txt
```

### 3) Configure env

```bash
cp .env.example .env
```

You can also export variables directly from `.env.example` values.

### 4) Initialize

```bash
python -m closed_claw.cli init
```

### 5) Run a task

Interactive approval mode:

```bash
python -m closed_claw.cli run "please use paid_api for analysis"
```

Or launch interactive mode (recommended):

```bash
python -m closed_claw.cli
```

This opens a greet screen with launch ASCII art (shown once at startup), then interactive options for setup/init/doctor/run/inspection.

## Interactive Setup Wizard

Run setup directly:

```bash
python -m closed_claw.cli setup
```

The wizard will:
1. Ask provider (`heuristic`, `openai`, `gemini`, `claude`)
2. Ask model
3. Ask API key (if needed)
4. Run a live provider verification request
5. Save config to `.env` on confirmation

Non-interactive policy mode:

```bash
python -m closed_claw.cli run "please use paid_api for analysis" \
  --create-approval-mode approve \
  --api-approval-mode approve \
  --llm-provider openai \
  --llm-model gpt-4o-mini
```

## CLI Reference

### `init`
Initialize local DB and graph wiring.

```bash
python -m closed_claw.cli init
```

### `doctor`
Validate local environment readiness.

```bash
python -m closed_claw.cli doctor
```
Doctor now reads fresh `.env` values each run and shows:
- configured provider/model,
- expected key env variable,
- whether key is set (with masked length preview),
- sqlite/langgraph health.

### `run`
Run one task.

```bash
python -m closed_claw.cli run "task text" \
  [--session-id <id>] \
  [--context-json '<json>'|/path/to/context.json] \
  [--create-approval-mode interactive|approve|deny] \
  [--api-approval-mode interactive|approve|deny] \
  [--organize-path /absolute/path] \
  [--organize-dry-run] \
  [--organize-recursive]
```

If a new agent is created, output includes `created_agent_description` (LLM-generated when provider is configured).
It also includes a `created_agent` block with generated `tools_allowlist` and `skill_md`.
During execution, user-friendly coordinator updates are printed (create/reuse, approvals, completion status).

### `agents`
List registered agents.

```bash
python -m closed_claw.cli agents --limit 20
```

Agent listing now includes:
- `tools_allowlist`
- generated `skill_md` content

### `agent`
Show one agent in full detail (manifest + tools + `skill.md` + memory count).

```bash
python -m closed_claw.cli agent <agent_id>
python -m closed_claw.cli agent <agent_id> --include-embedding
```

### `runs`
List run history.

```bash
python -m closed_claw.cli runs --limit 20
```

### `audit`
List audit events.

```bash
python -m closed_claw.cli audit --limit 20
```

### `runlog`
Show JSONL events for one run.

```bash
python -m closed_claw.cli runlog <run_id> --tail 200
```

### `cancel-run`
Gracefully stop an active run loop (stops scheduling new subtasks and exits with partial results).

```bash
python -m closed_claw.cli cancel-run <run_id>
```

### `tools`
List supported tools and optionally show one agent's tool allowlist.

```bash
python -m closed_claw.cli tools
python -m closed_claw.cli tools --agent-id <agent_id>
```

### `delete-agent`
Delete agent from registry and remove its capsule directory.

```bash
python -m closed_claw.cli delete-agent <agent_id>
python -m closed_claw.cli delete-agent <agent_id> --yes
```

### `delete-all-agents`
Delete all agents and all capsule directories.

```bash
python -m closed_claw.cli delete-all-agents
python -m closed_claw.cli delete-all-agents --yes
```

### `setup`
Interactive provider/model/key setup + verification.

```bash
python -m closed_claw.cli setup
```

### `menu`
Open interactive greet screen.

```bash
python -m closed_claw.cli menu
```

## Tooling Model

Supported tool names:
- `terminal`
- `http_api`
- `web_fetch`
- `file_io`
- `python_exec`
- `sql_query` (SELECT only)

How it works:
- agent emits `tool_call_intent`,
- coordinator validates against `manifest.json.tools_allowlist`,
- coordinator executes tool and returns `tool_call_result`.

List tools:

```bash
python -m closed_claw.cli tools
python -m closed_claw.cli tools --agent-id <agent_id>
```

## Configuration Guide

Key env vars:

- `CLOSED_CLAW_DB_PATH` (default: `.closed_claw/registry.db`)
- `CLOSED_CLAW_AGENTS_DIR` (default: `agents`)
- `CLOSED_CLAW_RUN_LOGS_DIR` (default: `.closed_claw/runs`)
- `CLOSED_CLAW_EXTRA_ALLOWED_PATHS` (comma-separated absolute paths allowed for tool operations)
- `CLOSED_CLAW_REQUIRE_SQLITE_VEC` (`true|false`)
- `CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS` (`true|false`)
- `CLOSED_CLAW_CREATE_APPROVAL_REQUIRED` (`true|false`)
- `CLOSED_CLAW_CREATE_APPROVAL_MODE` (`interactive|approve|deny`)
- `CLOSED_CLAW_API_APPROVAL_MODE` (`interactive|approve|deny`)
- `CLOSED_CLAW_PAID_API_PROVIDERS` (comma-separated list)
- `CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD` (float)
- `CLOSED_CLAW_AGENT_TIMEOUT_SEC` (int)
- `CLOSED_CLAW_AGENT_RETRIES` (int)
- `CLOSED_CLAW_CIRCUIT_BREAKER_FAILURES` (int)
- `CLOSED_CLAW_CIRCUIT_BREAKER_RESET_SEC` (int)
- `CLOSED_CLAW_LLM_PROVIDER` (`heuristic|openai|gemini|claude`)
- `CLOSED_CLAW_LLM_MODEL` (provider-specific model id)
- `CLOSED_CLAW_LLM_TIMEOUT_SEC` (int)
- `CLOSED_CLAW_LLM_API_KEY` (generic fallback key)
- `OPENAI_API_KEY`
- `GEMINI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_BASE_URL`
- `GEMINI_BASE_URL`
- `ANTHROPIC_BASE_URL`

## Data Layout

```text
.closed_claw/
  registry.db
  runs/
    <run_id>.jsonl

agents/
  registry.json
  <agent_id>/
    manifest.json
    skill.md
    memory.db
    entrypoint.py
    logs/
```

## Testing

Run all tests:

```bash
pytest -q
```

## Troubleshooting

### sqlite-vec load failure
If `doctor` shows sqlite-vec failure:
- ensure package installed in env,
- set explicit extension path if needed:

```bash
export SQLITE_VEC_PATH="$(python -c 'import sqlite_vec; print(sqlite_vec.loadable_path())')"
python -m closed_claw.cli doctor
```

### Slow startup due model download
Set local deterministic embeddings mode:

```bash
export CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS=false
```

### Interactive prompt blocks in automation
Use policy override flags:

```bash
--create-approval-mode approve --api-approval-mode approve
```

### Organizing files in a folder
Example task:

```bash
python -m closed_claw.cli run "access folder /absolute/path/to/test_folder organize files by type" \
  --create-approval-mode approve \
  --api-approval-mode approve
```
This behavior is executed by the **agent itself** (not a supervisor tool).

## Safety Notes

- Tool execution is allowlist-based per agent.
- `sql_query` is restricted to `SELECT`.
- Paid API calls require approval unless policy auto-approves.
- Circuit breaker blocks repeated failing providers until reset window passes.

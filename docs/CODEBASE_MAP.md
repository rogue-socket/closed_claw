# Codebase Map

> Read this first. Every file, class, and key function — one reference.

---

## Package: `closed_claw/`

### `__init__.py`
Empty package marker.

---

### `cli.py`
**Purpose:** argparse CLI entrypoints for every user-facing command.

| Symbol | Type | What it does |
|--------|------|-------------|
| `cmd_init` | function | Creates DB, initializes graph wiring |
| `cmd_doctor` | function | Validates env: provider config, API key presence, sqlite/langgraph health |
| `cmd_run` | function | Runs a single task through the full coordinator graph |
| `cmd_agents` | function | Lists registered agents with manifest details |
| `cmd_agent` | function | Shows one agent in full detail (manifest + skill.md + memory count) |
| `cmd_runs` | function | Lists run history from DB |
| `cmd_audit` | function | Lists audit events from DB |
| `cmd_runlog` | function | Streams JSONL events for a specific run |
| `cmd_tools` | function | Lists supported tools; optionally shows one agent's allowlist |
| `cmd_delete_agent` | function | Removes a single agent from registry + capsule dir |
| `cmd_delete_all_agents` | function | Removes all agents from registry + capsule dirs |
| `cmd_setup` | function | Delegates to interactive `run_setup_wizard` |
| `cmd_cancel_run` | function | Sets a cancellation flag in the run log to stop a running task |
| `_build_parser` | function | Constructs the argparse tree; register new commands here |
| `main` | function | CLI entry (`python -m closed_claw.cli`) — opens menu if no subcommand |

**How to add a CLI command:** add `cmd_<name>(args: Namespace) -> int` then register a subparser in `_build_parser()`.

---

### `compat.py`
**Purpose:** Thin Pydantic v1/v2 compatibility shim.

| Symbol | What it does |
|--------|-------------|
| `BaseModel` | Re-export of `pydantic.BaseModel` |
| `Field` | Re-export of `pydantic.Field` |

Always import `BaseModel`/`Field` from here, not directly from `pydantic`.

---

### `config.py`
**Purpose:** Environment-backed runtime `Settings` loading.

| Symbol | Type | What it does |
|--------|------|-------------|
| `Settings` | dataclass | All runtime config as typed fields; source of truth for all paths |
| `Settings.from_env()` | classmethod | Reads `.env` + `os.environ`; call this at CLI entry, never at module init |
| `_load_dotenv` | function | Parses `.env` file to dict (no dependencies) |
| `_getenv` | function | Reads from `os.environ` first, then dotenv, then falls back to default |

**Key fields in `Settings`:**
- `db_path` — path to `registry.db`
- `agents_dir` — root of all agent capsule dirs
- `run_logs_dir` — where JSONL run logs go
- `llm_provider` — `siemens | openai | gemini | claude` (default: `siemens`)
- `llm_model` — provider-specific model string (default: `qwen3-30b-a3b-instruct-2507` for siemens)
- `llm_timeout_sec` — LLM call timeout in seconds
- `create_approval_mode` / `api_approval_mode` — `interactive | approve | deny`
- `low_confidence_threshold` — float; below this triggers create/reuse gate
- `siemens_api_key` / `openai_api_key` / `gemini_api_key` / `anthropic_api_key` — provider keys
- `siemens_base_url` / `openai_base_url` / `gemini_base_url` / `anthropic_base_url` — provider base URLs
- `extra_allowed_paths` — filesystem sandbox roots for tool execution
- `subtask_max_attempts` — retry count for failed subtasks

---

### `interactive.py`
**Purpose:** Rich-powered terminal interactive menu.

| Symbol | Type | What it does |
|--------|------|-------------|
| `run_main_menu` | function | Infinite loop rendering numbered options (setup/init/doctor/run/inspect/delete) |

Called by `main()` in `cli.py` when no subcommand is given.

---

### `setup_wizard.py`
**Purpose:** Guided provider/key setup with live verification before writing `.env`.

| Symbol | Type | What it does |
|--------|------|-------------|
| `run_setup_wizard` | function | Prompts for provider → model → API key → verifies → writes `.env` |

---

## Package: `closed_claw/agents/`

### `factory.py`
**Purpose:** Creates agent capsule directories and manages the `registry.json` flat index.

| Symbol | Type | What it does |
|--------|------|-------------|
| `ENTRYPOINT_TEMPLATE` | str constant | Template for `entrypoint.py` (currently **v12**). Contains the full multi-step agent loop (MAX_STEPS=12, MAX_RETRIES_PER_STEP=3). v12 adds full tool `args_schema` in plan/fix prompts (v11 added `_execute_llm_http()` (stdlib urllib) and `system_prompt` injection). Auto-migrated on `cli init` / `cli run`. |
| `AgentFactory` | class | Creates capsule dirs: `manifest.json`, `skill.md`, `memory.db`, `entrypoint.py`, `logs/` |
| `AgentFactory.create_capsule(name, description, ..., skill_content, skill_ids)` | method | Writes capsule files to `agents_dir/<agent_id>/`; updates `registry.json`; stores `skill_ids` in manifest |
| `AgentFactory.save_registry_index(path, manifests)` | static method | Overwrites `registry.json` with a fresh list |

**Version detection:** The entrypoint template version tag (`CLOSED_CLAW_ENTRYPOINT_VERSION=13`) is read by `_migrate_legacy_agents` in `cli.py` to auto-upgrade older capsules. v13 replaced the frozen upfront plan with a ReAct-style observe→think→act loop.

---

## Package: `closed_claw/coordinator/`

### `state.py`
**Purpose:** Shared coordinator state shapes.

| Symbol | What it is |
|--------|-----------|
| `CoordinatorState` | `TypedDict` — all possible keys in graph state dict |
| `Candidate` | `TypedDict` — `{agent_id, score, reason}` entry in candidate lists |

---

### `graph.py`
**Purpose:** Builds and wires the LangGraph `StateGraph`.

| Symbol | What it does |
|--------|-------------|
| `build_graph(settings)` | Instantiates all sub-components, creates `StateGraph(dict)`, adds all nodes + edges, compiles and returns the runnable graph |

**Node order (edges):**
```
ingest_task → decompose_task → execute_task_pool → validate_outputs
  → update_registry_and_audit → synthesize_final_response → END
```

**To add a node:** add it to `CoordinatorNodes`, then call `graph.add_node(...)` and `graph.add_edge(...)` in `build_graph`.

---

### `nodes.py`
**Purpose:** All coordinator graph node implementations. ~1200 lines — the heart of the system.

| Symbol | Type | What it does |
|--------|------|-------------|
| `CoordinatorNodes` | class | Holds all node methods plus shared sub-components |
| `__init__` | method | Wires settings, registry, reranker, embedder, runner, factory, approval_gate, audit, tool_executor |
| `ingest_task` | async node | Validates task string, sets run_id/session_id, initializes state |
| `decompose_task` | async node | Uses LLM/heuristic to split task into atomic subtasks with role tags and dependencies |
| `execute_task_pool` | async node | Dependency-aware loop: resolves agents per role tag, runs them via `AgentRunner`, handles api_call_intent / tool_call_intent mid-flight |
| `validate_outputs` | async node | Checks all subtasks completed OK; marks failures |
| `update_registry_and_audit` | async node | Updates agent metrics (usage, success rate, latency); writes audit rows |
| `synthesize_final_response` | async node | Merges all subtask results into a final response string |
| `_handle_api_call_intent` | method | Routes api_call_intent through `ApprovalGate` and circuit breaker |
| `_handle_tool_call_intent` | method | Validates allowlist + executes via `ToolExecutor` |
| `_acquire_agent_for_role` | method | Embeds role description → semantic search → rerank → reuse or create |
| `_create_agent` | method | Calls `AgentFactory.create_capsule`, generates skill.md + tools_allowlist + skill_ids via LLM/heuristic |
| `_compose_system_prompt` | method | Loads base skill modules (Layer 1) + role overlay (Layer 2) into a single system prompt string |
| `_request_config_for_agent` | method | Builds `config` dict for `CoordinatorRequest` — includes `llm`, `system_prompt` (composed), and `tool_registry` |
| `_merge` | static method | Shallow-merges two state dicts |
| `_emit_runlog` | method | Writes a named event to the run's JSONL log |

---

## Package: `closed_claw/embeddings/`

### `provider.py`
**Purpose:** Text-to-vector embedding with graceful fallback.

| Symbol | What it does |
|--------|-------------|
| `EmbeddingProvider` | Wraps sentence-transformers if available; returns zero-vector of correct dim otherwise |
| `EmbeddingProvider.embed(text)` | Returns `list[float]` of length `embedding_dim` |

---

## Package: `closed_claw/observability/`

### `runlog.py`
**Purpose:** Per-run JSONL event logging.

| Symbol | What it does |
|--------|-------------|
| `RunLogger` | Writes `{"event": ..., "payload": ..., "ts": ...}` lines to `.closed_claw/runs/<run_id>.jsonl` |
| `RunLogger.emit(event, payload)` | Appends one event line |

---

## Package: `closed_claw/policy/`

### `approval.py`
**Purpose:** Human-in-the-loop approval gate for paid API calls / low-confidence agent creation.

| Symbol | What it does |
|--------|-------------|
| `ApprovalRequest` | Pydantic model — call type, provider, endpoint, cost, reason, session_id |
| `ApprovalDecision` | Pydantic model — approved bool, operator, timestamp, note |
| `ApprovalGate` | Prompts user with timeout; returns `ApprovalDecision` |
| `ApprovalGate.prompt(req, operator)` | Blocks on stdin with `timeout_sec` deadline |

**Gate modes** (set via CLI flags or env vars):
- `interactive` — shows prompt and waits
- `approve` — auto-approves (CI / scripts)
- `deny` — auto-denies

---

### `audit.py`
**Purpose:** Structured audit event persistence.

| Symbol | What it does |
|--------|-------------|
| `AuditStore` | Writes rows to `audit_events` table in `registry.db` |
| `AuditStore.record(event_type, agent_id, session_id, payload)` | Inserts one audit row |

---

## Package: `closed_claw/registry/`

### `schema.sql`
**Purpose:** SQLite DDL. Tables:
- `agents` — agent manifests (JSON blob + indexed fields)
- `agent_vectors` — sqlite-vec virtual table for cosine search
- `runs` — run history
- `agent_compositions` — multi-agent composition records
- `provider_circuit_breakers` — failure tracking per provider
- `audit_events` — structured audit log

---

### `store.py`
**Purpose:** SQLite-backed CRUD and semantic vector search for agents and runs.

| Symbol | What it does |
|--------|-------------|
| `AgentManifest` | Pydantic model — full agent identity, metrics, tools, embedding. Includes `skill_ids: list[str]` for base skill module selection. |
| `SearchCandidate` | Dataclass — `{agent_id, score, description}` from vector search |
| `RegistryStore` | All DB operations |
| `RegistryStore.upsert_agent(manifest)` | Insert or update agent + vector |
| `RegistryStore.search_agents(vector, limit)` | Cosine similarity search via sqlite-vec |
| `RegistryStore.get_agent(agent_id)` | Fetch one `AgentManifest` |
| `RegistryStore.list_agents(limit)` | Fetch N agents ordered by usage |
| `RegistryStore.delete_agent(agent_id)` | Remove agent + vector row |
| `RegistryStore.record_run(...)` | Insert a run record |
| `RegistryStore.update_agent_metrics(...)` | Update usage_count, success_rate, avg_latency |

---

### `search.py`
**Purpose:** Reranking, task plan generation, and agent profile generation.

| Symbol | What it does |
|--------|-------------|
| `_BASE_SKILL_IDS` | `list[str]` constant — canonical list of available base skill module IDs; must match files in `agents/skills/` |
| `RerankerProtocol` | Protocol interface — `.rerank(task, candidates) -> list[Candidate]` |
| `HeuristicReranker` | Default: keyword overlap + recency scoring |
| `LLMReranker` | Uses configured LLM provider to rerank candidates |
| `build_reranker(settings)` | Returns the right reranker based on `settings.llm_provider` |
| `generate_task_plan(settings, task, *, phase, discovery_results)` | Decomposes task text into subtask list via LLM/heuristic |
| `generate_agent_profile(settings, task, supported_tools, fallback_tools)` | Generates `skill_md`, `tools_allowlist`, and `skill_ids` for a new agent |
| `_normalize_profile_payload(payload)` | Validates and normalises LLM-returned profile dict; filters `skill_ids` to valid `_BASE_SKILL_IDS` members |

---

## Package: `closed_claw/runtime/`

### `protocol.py`
**Purpose:** All JSON-line protocol message models.

| Symbol | Direction | What it is |
|--------|-----------|-----------|
| `CoordinatorRequest` | Coordinator → Agent | Task, context, artifacts, config |
| `ApiCallIntent` | Agent → Coordinator | Request to call a paid external API |
| `ApiCallDecision` | Coordinator → Agent | Approval/denial for `ApiCallIntent` |
| `ToolCallIntent` | Agent → Coordinator | Request to run a tool |
| `ToolCallResult` | Coordinator → Agent | Tool execution result/error |
| `AgentResponse` | Agent → Coordinator | Final response: status, result, memory_updates, artifacts, metrics |
| `AgentMetrics` | (nested in AgentResponse) | latency_ms |
| `parse_agent_line(line)` | utility | Parses a raw stdout line into `ApiCallIntent | ToolCallIntent | AgentResponse` |

---

### `runner.py`
**Purpose:** Subprocess lifecycle management for agent capsules.

| Symbol | What it does |
|--------|-------------|
| `AgentRuntimeError` | Exception raised on agent protocol violations or subprocess errors |
| `AgentRunner` | Launches `entrypoint.py`, drives JSON-line I/O loop, enforces retries |
| `AgentRunner.run(agent_dir, request, on_intent)` | Sends `CoordinatorRequest`, loops on intents until `AgentResponse`, returns it |

`on_intent` is a callback `(intent) -> str` that the coordinator uses to handle `ApiCallIntent`/`ToolCallIntent` and return the JSON-encoded response line.

---

## Package: `closed_claw/tools/`

### `executor.py`
**Purpose:** Sandboxed tool execution.

| Symbol | What it does |
|--------|-------------|
| `SUPPORTED_TOOLS` | `list[str]` — canonical tool names |
| `TOOL_REGISTRY` | `dict` — name → description + args_schema (used for CLI display + LLM prompt) |
| `ToolExecutionError` | Exception for tool-level failures |
| `ToolExecutor` | Executes tools within allowed filesystem roots |
| `ToolExecutor.execute(tool, args)` | Dispatches to the right tool impl; returns `dict` result |
| `tool_registry_for_allowlist(allowlist)` | Returns filtered `TOOL_REGISTRY` subset for an agent |

**Tool implementations (all inside `ToolExecutor`):**
- `_run_terminal(cmd, timeout_s)` — subprocess shell command
- `_run_http_api(method, url, ...)` — HTTP request via `requests`
- `_run_web_fetch(url, ...)` — webpage fetch + text extraction
- `_run_file_io(op, path, ...)` — list/read/write/append within allowed roots
- `_run_python_exec(code, timeout_s)` — executes Python snippet in subprocess
- `_run_sql_query(db_path, query, params)` — SELECT-only SQLite query

---

## Package: `closed_claw/web/`

### `server.py`
**Purpose:** FastAPI/Starlette web dashboard. Launched via `python -m closed_claw.cli web` → `http://127.0.0.1:7860`.

Exposes REST endpoints for agent listing, run history, and audit events. Serves `ui.html`.

### `ui.html`
Single-page HTML UI served by `server.py`.

---

## Generated Directories (runtime, not in source control)

### `agents/skills/`
Shared base skill library. Each file is a Markdown document describing tool usage patterns for one capability domain. These are **not agent-specific** — they are composed into any agent's system prompt based on `manifest.skill_ids`.

| File | Domain |
|------|-------|
| `terminal.md` | Shell execution, process management, packages |
| `python_scripting.md` | Python code via `python_exec` tool |
| `git.md` | Git workflow via `terminal` tool |
| `file_system.md` | `file_io` tool patterns |
| `web_http.md` | `http_api` and `web_fetch` tool patterns |
| `sql_databases.md` | `sql_query` tool, SELECT-only SQLite |
| `data_analysis.md` | Cross-tool data pipeline patterns |

To add a module: create the `.md` file here and add its name to `_BASE_SKILL_IDS` in `registry/search.py`.

### `agents/<agent_id>/`
| File | Purpose |
|------|---------|
| `manifest.json` | `AgentManifest` — identity, tools, metrics, tags, skill_ids, embedding |
| `skill.md` | Role overlay (Layer 2) — agent-specific identity, decision rules, output format |
| `memory.db` | Per-agent SQLite episodic memory (key/value store) |
| `entrypoint.py` | Agent subprocess (v12); speaks JSON-line protocol; reads `config["system_prompt"]` |
| `logs/` | Per-run output artifacts |

### `agents/registry.json`
Flat JSON array of all registered agent manifests (human-readable mirror of DB).

### `.closed_claw/registry.db`
Main SQLite database. Tables: `agents`, `agent_vectors`, `runs`, `agent_compositions`, `provider_circuit_breakers`, `audit_events`.

### `.closed_claw/runs/<run_id>.jsonl`
One file per run. Each line is a JSON event: `{event, payload, ts}`.

---

## Test Coverage Map

| Test file | Covers |
|-----------|--------|
| `tests/integration/test_flow.py` | Full coordinator graph end-to-end |
| `tests/unit/test_protocol.py` | Protocol model parse/serialize |
| `tests/unit/test_manifest.py` | AgentManifest validation |
| `tests/unit/test_approval.py` | ApprovalGate modes |
| `tests/unit/test_config_env.py` | Settings.from_env() loading |
| `tests/unit/test_registry_audit.py` | RegistryStore + AuditStore operations |
| `tests/unit/test_tools_executor.py` | ToolExecutor sandboxing |
| `tests/unit/test_reranker_config.py` | Reranker selection logic |
| `tests/unit/test_agent_profile_generation.py` | generate_agent_profile |
| `tests/unit/test_agent_entrypoint_fallback.py` | Agent entrypoint fallback behavior |
| `tests/unit/test_coordinator_retry_loop.py` | Coordinator retry handling |
| `tests/unit/test_coordinator_two_phase_pool.py` | Two-phase task pool execution |
| `tests/unit/test_delete_all_agents.py` | delete-all-agents CLI command |
| `tests/unit/test_setup_wizard.py` | Setup wizard flow |

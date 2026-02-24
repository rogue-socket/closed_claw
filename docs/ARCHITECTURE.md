# Architecture

## Overview

Closed Claw is a local multi-agent orchestration system with capsule agents, a registry-backed router, policy gates, and structured runtime contracts.

Primary components:
- Coordinator graph (`LangGraph`)
- Agent capsules (`agents/<agent_id>/`)
- Registry + audit store (`SQLite`)
- Runtime protocol (`JSON lines over stdin/stdout`)
- Policy engine (human-in-the-loop approvals)
- Tool execution layer (allowlist-enforced)
- Run logs (`JSONL`)

## Coordinator Flow

1. `ingest_task`
2. `decompose_task` (LLM/heuristic plan generation into atomic subtasks)
3. `execute_task_pool` (role-tag agent acquisition, dependency-aware execution)
9. `validate_outputs`
10. `approval_gate_for_api_calls` (runtime handshake handles decisions)
11. `continue_or_deny_api_path`
12. `update_registry_and_audit`
13. `synthesize_final_response`
14. `failure_recovery`

## Task Pool Execution Model

Current implementation:
- The supervisor owns the task pool loop during a run.
- Subtasks move through `waiting -> pending -> in_progress -> completed/failed`.
- Dependencies are enforced before execution.
- Role tags determine agent reuse/create and assignment.
- Status updates are emitted as `task_pool_update` events to run logs and surfaced in CLI.

Known limitation:
- This is in-run orchestration, not persistent background workers.
- Agents do not run as standalone daemons that independently poll shared task storage.

TODO (target architecture):
- Promote task pool to a durable queue/state store.
- Run persistent background workers per capability role tag.
- Workers poll/claim tasks every `CLOSED_CLAW_TASK_POOL_POLL_INTERVAL_SEC` (default 30s).
- Add lease/heartbeat + retry semantics to avoid duplicate claims and stuck tasks.
- Add CLI `runs watch` for live checklist from queue state (not only runlog tailing).

Acceptance criteria for this TODO:
- A long task continues after CLI process exits.
- Multiple workers can execute independent subtasks concurrently.
- Dependency-blocked tasks remain `waiting` until prerequisites complete.
- User can reconnect and see accurate live status + final synthesized output.

## Capsule Model

Each agent capsule contains:
- `manifest.json`: identity, tools, metrics, tags, embedding metadata
- `skill.md`: role definition
- `memory.db`: local episodic memory
- `entrypoint.py`: agent runtime
- `logs/`: local execution artifacts

Coordinator can create new capsules when match confidence is low.
On creation, agent description is generated from LLM provider when configured (fallback: heuristic summary).

## Runtime Protocol

Transport: newline-delimited JSON on stdin/stdout.

Coordinator request:
- `session_id`, `task`, `context`, `artifacts`, `config`

Agent-to-coordinator intents:
- `api_call_intent` -> coordinator returns `api_call_decision`
- `tool_call_intent` -> coordinator returns `tool_call_result`

Final response:
- `status`, `result`, `memory_updates`, `artifacts`, `metrics`, optional error fields

## Registry and Persistence

DB: `.closed_claw/registry.db`

Key tables:
- `agents`
- `agent_vectors` (sqlite-vec virtual table)
- `runs`
- `agent_compositions`
- `provider_circuit_breakers`
- `audit_events`

Human-readable index:
- `agents/registry.json`

Run logs:
- `.closed_claw/runs/<run_id>.jsonl`

## Routing and Selection

Current behavior:
- semantic retrieval from registry vectors
- heuristic reranking by default
- optional LLM reranking via `openai`, `gemini`, or `claude`
- low-confidence threshold check
- optional human create/reuse gate

Provider configuration:
- `CLOSED_CLAW_LLM_PROVIDER=heuristic|openai|gemini|claude`
- `CLOSED_CLAW_LLM_MODEL=<model-id>`
- key via provider-specific env var (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) or `CLOSED_CLAW_LLM_API_KEY`

## Policy and Safety

### Approval gates
- Create/reuse (low confidence)
- External paid API calls

Modes:
- `interactive`
- `approve`
- `deny`

### Circuit breaker
- Tracks repeated provider failures/denials
- Blocks provider until reset interval expires

### Tool safety
- Per-agent allowlist in `manifest.json`
- Unsupported or disallowed tools are denied
- `sql_query` restricted to `SELECT`

## Tooling Layer

Available tools:
- `terminal`
- `http_api`
- `web_fetch`
- `file_io`
- `python_exec`
- `sql_query`

Execution model:
- Agent requests tool usage via protocol
- Coordinator executes centrally
- Results returned via protocol
- Every tool call is audited

Note: folder organization is handled directly inside the agent runtime logic, not as a coordinator tool.

## Configuration

Configuration is environment-variable driven via `closed_claw/config.py` and `.env.example`.

Most important knobs:
- storage paths (`DB`, `agents`, `run logs`)
- approval modes
- paid-provider classification
- retry/timeouts
- circuit-breaker thresholds
- sqlite-vec strictness

## Extensibility Points

- Replace reranker in `closed_claw/registry/search.py`
- Add real provider clients behind `api_call_intent`
- Expand tool implementations in `closed_claw/tools/executor.py`
- Add richer agent templates in `closed_claw/agents/factory.py`
- Introduce service API/UI around CLI

## Operational Commands

- `python -m closed_claw.cli doctor`
- `python -m closed_claw.cli run ...`
- `python -m closed_claw.cli agent <agent_id>`
- `python -m closed_claw.cli runs`
- `python -m closed_claw.cli audit`
- `python -m closed_claw.cli runlog <run_id>`
- `python -m closed_claw.cli setup`
- `python -m closed_claw.cli` (interactive menu)
- `python -m closed_claw.cli delete-agent <agent_id>`
- `python -m closed_claw.cli delete-all-agents`

## Interactive UX Layer

Closed Claw now includes an interactive menu layer:
- default invocation (`python -m closed_claw.cli`) opens greet/menu
- launch-only ASCII art is shown once when menu starts
- guided options for setup, init, doctor, run, and inspections
- setup wizard verifies provider configuration before persisting `.env`

This UX is additive; non-interactive subcommands still exist for automation/CI.

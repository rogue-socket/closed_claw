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
2. `embed_task`
3. `semantic_search`
4. `llm_rerank` (currently heuristic reranker)
5. `human_gate_if_low_confidence`
6. `decide_reuse_or_create`
7. `create_agent_if_needed`
8. `dispatch_agents_async`
9. `validate_outputs`
10. `approval_gate_for_api_calls` (runtime handshake handles decisions)
11. `continue_or_deny_api_path`
12. `update_registry_and_audit`
13. `synthesize_final_response`
14. `failure_recovery`

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

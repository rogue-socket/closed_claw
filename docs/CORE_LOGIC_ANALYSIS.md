# Core Logic Analysis — Closed Claw

> Analysis date: February 24, 2026  
> Scope: Core coordinator logic, agent lifecycle, runtime protocol, inter-agent communication, state management, policy layer — grounded in the actual implementation.

---

## Method

The application was broken down into seven bite-sized components. Each component was analyzed for internal problems first, then inter-component communication problems, and finally system-level scope problems were identified. Every finding is grounded in the real code, not aspirational documentation.

---

## Component 1 — Task Ingestion & Routing

**What it does:**
Takes a raw task string → embeds it → semantic search in the registry → LLM reranker → decide: reuse an existing agent or create a new one.

### Internal Problems

**P1 — Task is a single undifferentiated string.**

The system receives something like `"write an essay on climate change"` and treats it as one atomic, indivisible task. There is no decomposition step. The coordinator never asks:
- How many agents does this task actually need?
- What are the sub-goals?
- What order should they run in?
- What does success look like?

Every task, regardless of complexity, routes to exactly one agent. This is the single largest architectural gap in the application.

**P2 — Capability profile matching is keyword matching on the raw task string.**

`_select_capability_profile` works by checking if words like `"file"`, `"api"`, `"web"` appear in the task string:

```python
if any(word in t for word in ["file", "folder", "directory", "organize"]):
    return { "profile_id": "filesystem_terminal_expert", ... }
```

This is brittle in several ways:
- `"write a web story about files"` routes to `filesystem_terminal_expert`.
- `"analyze API logs on disk"` could match either `api_integration_expert` or `filesystem_terminal_expert` depending on word order.
- Any task phrased using synonyms the keywords don't cover is silently misrouted.
- There is no fallback signal that the match was uncertain.

**P3 — One embedding, one search, one winner.**

Semantic search returns up to 5 candidates. Reranking narrows it. Then exactly one agent is dispatched. The `candidates` list is used only as a sequential fallback if the top agent errors. It is not a composition list. There is no concept of "this task needs agents A AND B, run in sequence."

**P4 — The `low_confidence` gate is about routing confidence, not task understanding.**

The human gate fires when the top semantic search score is below `low_confidence_threshold`. This means: "we are not sure whether to reuse an existing agent." But there is no gate that asks: "do we understand this task well enough to plan it at all?" A poorly specified task passes through with full confidence if there happens to be a high-scoring agent in the registry.

---

## Component 2 — Agent Creation

**What it does:**
On a "create" decision, picks a capability profile from a hardcoded enum, finds an existing agent matching that profile tag, or creates a new capsule on disk and registers it.

### Internal Problems

**P5 — Profiles are a closed hardcoded enum of five types.**

```python
# nodes.py — _select_capability_profile
if any(word in t for word in ["file", "folder", ...]): return filesystem_terminal_expert
if any(word in t for word in ["api", "endpoint", ...]): return api_integration_expert
if any(word in t for word in ["web", "website", ...]): return web_research_expert
if any(word in t for word in ["sql", "database", ...]): return data_sql_expert
return general_terminal_operator  # catch-all
```

The system can never create a `researcher`, a `writer`, an `evaluator`, a `planner`, a `critic`, a `summarizer`, or any domain-specific agent type. The entire concept of specialized collaborating agents is blocked at this level. No amount of clever routing fixes this because the agent vocabulary itself is locked.

**P6 — Agent creation does not consult the configured LLM.**

The `skill.md`, `description`, and `tools_allowlist` for new agents are hardcoded strings inside the profile dict. When an LLM provider is configured (OpenAI, Gemini, Claude), none of its reasoning ability is used to design the new agent. The LLM integration exists in the config and approval layers but is never invoked for what would be its most valuable use: dynamically generating a well-suited agent for an unfamiliar task.

**P7 — Agent manifests have no output contract or role description.**

An `AgentManifest` has: `agent_id`, `name`, `description`, `tags`, `tools_allowlist`, `api_capabilities`, `requires_approval_for`, `embedding_vector`. It does not have:
- What structured input format the agent expects.
- What structured output it produces.
- Its role in a multi-agent pipeline (researcher, executor, evaluator, etc.).
- What preconditions it requires before running.
- What downstream agents should receive from it.

Without output contracts, the coordinator has no way to wire agents together correctly. Agent A's output might be completely incompatible with what Agent B expects, and the system has no way to detect or handle this.

**P8 — No agent versioning or retirement.**

Once created, agents accumulate. There is no mechanism to flag: "this agent's capability profile has been superseded," "this agent consistently performs poorly," or "this agent's `entrypoint.py` is stale relative to a new profile version." Quality in, permanent fixture. The registry grows indefinitely with no fitness signal driving pruning.

---

## Component 3 — Agent Dispatch & Execution

**What it does:**
Sends a `CoordinatorRequest` (session_id, task, context, config) via stdin/stdout JSON line protocol to an `entrypoint.py` subprocess. Handles `ApiCallIntent` and `ToolCallIntent` mid-stream. Waits for one final `AgentResponse`.

### Internal Problems

**P9 — The protocol is strict request-response: one task in, one result out.**

```python
# protocol.py
class CoordinatorRequest(BaseModel):
    session_id: str
    task: str
    context: dict[str, Any]
    artifacts: list[dict[str, Any]]
    config: dict[str, Any]

class AgentResponse(BaseModel):
    status: Literal["ok", "error"]
    result: str  # a single string
    memory_updates: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    ...
```

Agents cannot say: "I have completed step 1, here is an intermediate result, please pass it to the next agent and come back with their output." There is no streaming of partial work, no structured intermediate handoff, no mid-execution delegation. The protocol enforces a closed box model.

**P10 — Agents are completely isolated from each other.**

Agent A has no way to know Agent B exists, ran, or produced anything. The only inter-agent communication channel would have to be the `context` dict and `artifacts` list on the `CoordinatorRequest`. But these are set once by the coordinator before dispatch. Two agents dispatched in the same session cannot communicate. There is no shared workspace, no message bus, no chalkboard pattern.

**P11 — `dispatch_agents_async` is not actually async.**

Despite the name, the dispatch loop is a plain sequential `for`:

```python
async def dispatch_agents_async(self, state: dict[str, Any]) -> dict[str, Any]:
    for agent_id in candidate_ids:
        ...
        response = await self.runner.run_agent(...)  # awaits one at a time
        return merged  # breaks on first success
```

There is no `asyncio.gather`, no fan-out, no parallelism. The "async" keyword enables awaiting, but only one agent runs at a time. Any scenario where multiple agents could usefully run in parallel (e.g., a researcher and a data-fetcher gathering independent inputs) is completely unexploited.

**P12 — The fallback cascade is semantically random.**

When the top agent errors, the second candidate from the semantic search list is tried. That second candidate was ranked for *similarity to the task*, not for "can it succeed where the first agent failed." If Agent A failed because it lacks a needed tool, Agent B (a lookalike of A) likely lacks the same tool. The fallback logic has no model of *why* the failure occurred and no intelligence about what a recovery attempt should look like.

---

## Component 4 — Inter-Agent Communication (The Missing Component)

**What it does:** Currently, functionally does not exist.

### Problems

**P13 — No shared workspace or artifact bus.**

`state["artifacts"]` is a list populated by the agent's response. But it is never passed forward to subsequent agents. Looking at `dispatch_agents_async`:

```python
req = CoordinatorRequest(
    session_id=state["session_id"],
    task=state["task"],
    context=state.get("context", {}),
    config={"timeout_s": self.settings.agent_timeout_sec},
    # artifacts= is NOT populated from state["artifacts"]
)
```

An agent's `artifacts` are recorded in state and logged but never handed to the next agent. The `CoordinatorRequest` model has an `artifacts` field specifically for this purpose, but it is never used.

**P14 — No pipeline or DAG execution model.**

A research → write → evaluate flow requires:
1. The output of step N to become a typed input to step N+1.
2. An evaluation step that can produce a judgment and route back to the write step, or forward to done.
3. A coordinator that knows it is at step 2 of 3, not at the beginning of a fresh single-agent run.

The current system has none of this. The LangGraph DAG is fixed at 13 nodes and handles one task → one agent → one result. There is no dynamic subgraph, no runtime-planned pipeline.

**P15 — Agent memory is per-capsule and cannot be shared.**

Each agent capsule has its own `memory.db`. If a researcher agent stores discovered facts there, a writer agent has no path to read that database. The `memory_updates` field in `AgentResponse` exists and flows through the state, but `update_registry_and_audit` in `CoordinatorNodes` never writes those updates back to anything:

```python
async def update_registry_and_audit(self, state: dict[str, Any]) -> dict[str, Any]:
    self.registry.record_run(...)
    self.audit.record_event("run_summary", {...})
    # memory_updates from state["memory_updates"] are never processed
    return state
```

Memory is write-only noise from the coordinator's perspective.

---

## Component 5 — The Coordinator Graph (LangGraph)

**What it does:**
A static compiled DAG: ingest → embed → search → rerank → human_gate → decide → (create?) → dispatch → validate → (failure_recovery | approval_gate) → update → synthesize.

### Internal Problems

**P16 — The graph is completely static.**

Every task, simple or complex, traverses the same 13-node sequence. There is no:
- Fast path for trivial tasks (skip embedding/search if task is a known command).
- Planning path for complex tasks (decompose → plan → iterate).
- Adaptive topology based on task analysis.

A `"what time is it"` task and a `"write, research, and evaluate a competitive analysis report"` task follow the exact same execution path.

**P17 — `synthesize_final_response` is a passthrough.**

```python
async def synthesize_final_response(self, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("response_status") == "ok":
        return self._merge(state, response_result=state.get("response_result", ""))
    return self._merge(state, response_result="Unable to complete task...")
```

This does nothing. If three agents had run and produced three partial results, this node would return only the last one. There is no summarization, no aggregation, no cross-agent synthesis, no reconciliation of conflicting outputs. The name implies behavior the code doesn't implement.

**P18 — `failure_recovery` is a no-op stub.**

```python
async def failure_recovery(self, state: dict[str, Any]) -> dict[str, Any]:
    return state
```

When all candidate agents fail, this is called. It returns the state unchanged and passes to `update_registry_and_audit`, which records the failure. There is no:
- Retry with a different strategy.
- Escalation path (ask the human what to do).
- Decomposition attempt (maybe the task is too big for one agent).
- Alert or notification.
- Logged diagnosis of why recovery was skipped.

**P19 — Two approval graph nodes are dead scaffolding.**

`approval_gate_for_api_calls` and `continue_or_deny_api_path` both just `return state`. The actual API approval logic lives inside `_approval_callback`, which runs during the `dispatch_agents_async` node via the `approval_callback` lambda passed to `AgentRunner`. The post-execution approval path in the graph is a structural skeleton with no behavior.

---

## Component 6 — State Management

**What it does:**
A plain `dict` (nominally typed as `CoordinatorState` TypedDict) flows through the graph nodes. Nodes read and write keys on this dict.

### Internal Problems

**P20 — State has no representation of multi-step execution.**

`CoordinatorState` declares:
```python
class CoordinatorState(TypedDict, total=False):
    run_id: str
    session_id: str
    task: str              # one task
    decision: str          # one decision
    selected_agent_id: str # one agent
    response_result: str   # one result
    ...
```

There are no fields for: a subtask list, an execution plan, a current step pointer, a step-local result accumulator, a pipeline dependency graph, or accumulated cross-agent outputs. The schema enforces single-agent single-task semantics at the data model level.

**P21 — No state checkpointing; no resumability.**

LangGraph supports checkpointers (e.g., `MemorySaver`, `SqliteSaver`) that allow a graph run to pause and resume. The current `build_graph` doesn't configure one:

```python
return graph.compile()  # no checkpointer
```

A timeout at any node mid-run, or a crash, means restarting from scratch. For long-running multi-agent tasks (the primary goal), this is a significant reliability gap.

**P22 — `runtime_policies` is an undeclared implicit dependency.**

Multiple nodes read `state.get("runtime_policies", {})` to override approval modes:
```python
mode = (state.get("runtime_policies", {}) or {}).get(
    "create_approval_mode", self.settings.create_approval_mode
)
```

But `runtime_policies` is not in the `CoordinatorState` TypedDict. It is passed in from the CLI as an ad-hoc extra key. This is a hidden coupling: type checkers cannot see it, and any caller that forgets to pass it silently falls back to defaults with no warning.

---

## Component 7 — Approval & Policy Layer

**What it does:**
Two approval gates: one for agent creation (low confidence routing), one for API calls (paid providers). Circuit breaker per-provider.

### Internal Problems

**P23 — Approval granularity stops at agent boundaries, not task boundaries.**

There are approval types for:
- Should we create a new agent? (create gate)
- Should this agent call a paid API? (api gate)

But there are no approvals for:
- Should this multi-agent plan be executed at all?
- Should Agent A's output be passed to Agent B?
- Should a tool result be acted upon (vs. just being logged)?
- Should the synthesized result be returned to the user?

For higher-stakes workflows, approval at the task decomposition level (before work begins) is more valuable than approval at the individual API call level (after most work has been done).

**P24 — Circuit breaker tracks providers, not failure modes.**

```python
self.registry.open_circuit_if_needed(provider=intent.provider, threshold=...)
```

If OpenAI keeps failing, the circuit opens for OpenAI globally across all agents, all tasks, all sessions. But the failure might be:
- One specific agent passing a malformed prompt.
- One specific task generating an invalid request.
- A transient rate limit, not a systemic failure.

Tracking at provider granularity is too coarse. A circuit on `(provider, agent_type)` or `(provider, error_class)` would be more accurate and less disruptive.

---

## System-Level Scope Problems

These are problems visible only when looking at the entire application as a whole.

**P25 — The coordinator is a router, not a planner.**

The coordinator's entire decision space is: reuse agent X vs. create new agent Y. That is routing. A planner would:
1. Decompose the task into typed subtasks.
2. Assign agent roles to each subtask.
3. Determine execution order and dependencies.
4. Handle conditional branching (if evaluation fails, retry write step).
5. Synthesize a final result from all subtask outputs.

None of this exists. The coordinator is a smart single-agent dispatcher wearing the name of a multi-agent coordinator.

**P26 — No task decomposition model anywhere in the system.**

For the essay example, the system needs to answer: "is this a single-agent task or a multi-agent task? If multi, what are the subtasks and in what order?" This reasoning is entirely absent. The LLM provider, when configured, could answer this question trivially — but there is no node or prompt in the system that asks it to.

The closest thing to decomposition is the heuristic keyword profile selection, which is a shallow approximation and not decomposition at all.

**P27 — Agent `memory_updates` are produce-only, never consumed.**

Agents emit `memory_updates: list[dict]` in their `AgentResponse`. These populate `state["memory_updates"]`. Then:

```python
# update_registry_and_audit — the only consumer
self.registry.record_run(...)   # doesn't use memory_updates
self.audit.record_event(...)    # doesn't include memory_updates
return state                    # passed to synthesize, which also ignores it
```

Memory updates are completely discarded. They are part of the protocol spec but have no effect. Any agent that stores learning is wasting its effort.

**P28 — `entrypoint.py` is a static file written once at creation time.**

When an agent capsule is created, an `entrypoint.py` is written. The coordinator executes it as-is, every time. The file:
- Cannot be updated by the coordinator based on new context.
- Cannot be specialized for a particular task at runtime.
- Is not generated by the LLM (it is a template from `AgentFactory`).
- Cannot evolve based on agent performance.

The agent's executable behavior is frozen at creation. For the system to support genuinely capable agents, `entrypoint.py` generation and runtime injection of task-specific prompts/instructions is necessary.

**P29 — Sessions have no continuity across runs.**

```python
session_id=state.get("session_id", uuid.uuid4().hex[:12])
```

`session_id` is either passed in or randomly generated. The system never loads prior session state when a session_id is reused. There is no "continue where we left off" behavior, no cross-run context accumulation within a session, no session-level memory. A session is purely a label.

**P30 — No evaluation loop.**

A quality-aware workflow requires:
1. An agent produces output.
2. An evaluator agent reads the output and scores/critiques it.
3. Based on the score, the coordinator decides: accept, refine (loop back), or escalate.

The system has no evaluation signal. `response_status` is only `"ok"` or `"error"` — it carries no quality information. There is no way for the coordinator to know that a response is technically successful but substantively poor.

---

## Dependency Map Between Components

```
Task Ingestion
    └─ P1 (no decomposition) ──────────────────────────────► blocks everything multi-agent
    └─ P2 (keyword routing) ──────►  Component 2 (P5, P6)
    └─ P4 (confidence gate wrong type)

Agent Creation
    └─ P5 (closed profile enum) ──► blocks novel agent types
    └─ P6 (no LLM involvement) ───► blocks dynamic capability design
    └─ P7 (no output contract) ───► blocks inter-agent wiring (P13, P14)
    └─ P8 (no versioning) ────────► registry quality degradation over time

Dispatch
    └─ P9 (closed protocol) ──────► blocks handoff (P13)
    └─ P10 (agent isolation) ─────► is the core of missing inter-agent comms
    └─ P11 (false async) ─────────► no parallel execution benefit
    └─ P12 (dumb fallback) ────────► no intelligent recovery

Inter-Agent Comms (missing)
    └─ P13 (no artifact bus) ─────┐
    └─ P14 (no pipeline model) ───┤ these three together mean multi-agent is impossible
    └─ P15 (no shared memory) ────┘

Graph
    └─ P16 (static topology) ────► can't represent adaptive plans
    └─ P17 (synthesis stub) ─────► multi-agent outputs never combined
    └─ P18 (recovery stub) ──────► all failures are terminal
    └─ P19 (dead nodes) ─────────► misleading code, future confusion

State
    └─ P20 (no plan fields) ─────► schema enforces single-agent semantics
    └─ P21 (no checkpoint) ──────► no resumability for long tasks
    └─ P22 (hidden runtime_policies) ─► silent misconfiguration risk
```

---

## Prioritized Fix Matrix

| Priority | ID | Problem | What It Unblocks |
|---|---|---|---|
| Critical | P1 | No task decomposition | All multi-agent flows |
| Critical | P5 | Hardcoded profile enum | Dynamic agent creation for novel tasks |
| Critical | P13 | No artifact handoff between agents | Sequential pipelines |
| Critical | P14 | No pipeline/DAG execution model | Any structured multi-agent flow |
| Critical | P25 | Coordinator is a router, not a planner | The core multi-agent orchestration concept |
| High | P6 | LLM not used for agent design | Dynamic capability generation |
| High | P7 | No output contract on manifests | Agent wiring compatibility |
| High | P10 | Agent isolation | Collaboration and context sharing |
| High | P17 | `synthesize_final_response` is a stub | Aggregated results |
| High | P18 | `failure_recovery` is a stub | Any recovery strategy |
| High | P27 | Memory updates discarded | Agent learning and continuity |
| High | P30 | No evaluation loop | Quality-aware iteration |
| Medium | P9 | Single-response closed protocol | Mid-execution delegation |
| Medium | P16 | Static graph topology | Adaptive workflow planning |
| Medium | P20 | State schema has no plan fields | Tracking multi-step progress |
| Medium | P21 | No checkpointing | Resumability for long runs |
| Medium | P28 | Static entrypoint.py | Runtime agent specialization |
| Medium | P29 | Sessions have no continuity | Multi-turn contextual work |
| Low | P2 | Keyword-based profile matching | Routing accuracy |
| Low | P11 | Dispatch not actually concurrent | Parallel agent speedup |
| Low | P12 | Dumb fallback cascade | Intelligent error recovery |
| Low | P19 | Dead graph nodes | Code clarity |
| Low | P22 | `runtime_policies` not in TypedDict | Type safety |
| Low | P24 | Circuit breaker too coarse | Precise failure isolation |

---

## The Core Inflection Point

The system as-built is a **single-agent dispatcher with a sophisticated routing front-end**. It does that well. The name and ambition are multi-agent, but the execution model bottlenecks at exactly one agent per task.

The two architectural additions that unlock everything on the priority list above are:

1. **A planning node** — uses the configured LLM to take an incoming task and produce a typed execution plan: a list of steps, each with a role (`researcher`, `writer`, `evaluator`), a description of what it needs as input, and what it will produce as output. This replaces keyword profile matching and makes the profile vocabulary open-ended.

2. **An execution loop** — iterates through the plan, dispatching one agent per step, routing the previous step's artifacts and result as context into the next step's `CoordinatorRequest`, and collecting results. After the loop, synthesis is not a passthrough but an actual aggregation over structured step outputs. The evaluation step is just a step in a plan that can route backwards.

Everything else — shared memory, output contracts, session continuity, quality evaluation — follows naturally once the coordinator knows it is executing a plan rather than dispatching a single task.

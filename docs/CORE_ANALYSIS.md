# Core Logic Analysis — closed_claw

> Analysis date: 2026-02-24  
> Scope: coordinator graph, runtime protocol, agent capsule model, registry, policy gates, tool execution, and the cross-cutting issues that span all of them.  
> This is a conceptual and architectural review, not a line-by-line code audit.

---

## How to Read This Document

The analysis is structured in three layers:

1. **Component-level** — what each subsystem does, what is broken or missing inside it.
2. **Seam-level** — what breaks at the boundary between two components.
3. **System-level** — problems that only appear when you step back and look at the whole.

Each problem entry has:
- **What exists** — honest description of current state.
- **The problem** — the conceptual or structural flaw.
- **The consequence** — what fails or can never work because of it.
- **A direction** — a conceptual solution (not pseudocode, but a clear design intent).

---

## Part 1 — Component-Level Analysis

---

### 1.1 Task Ingestion (`ingest_task` + `embed_task`)

#### What exists
A task string enters the system. It is assigned a `run_id` and `session_id`. It is then embedded into a vector using the configured embedding model. The result is a flat vector that will be used for registry search.

#### Problem A — The task is never understood, only encoded
The system converts the task string from text into a vector and immediately moves on. It never asks: *what kind of thing is this task?* Is it atomic or composite? Does it require sequential sub-tasks? Does it have explicit or implicit acceptance criteria? What would a good answer actually look like?

The embedding captures semantic similarity to other tasks/agents, but it captures nothing about *task shape* — the structure of work required to complete it.

**Consequence:** Every task, regardless of complexity, enters the same single-agent pipeline. A task like "refactor this module" and "research and write a 2000-word report on topic X" are treated identically at ingestion time. The difference in required work is invisible to the system.

**Direction:** A decomposition step should sit between ingestion and routing. For simple tasks, it produces a plan with one node. For complex tasks, it produces an ordered graph of sub-tasks, each with a role type, input dependencies (what prior outputs it needs), and acceptance criteria. This plan — not just the raw task string — becomes the basis for routing.

#### Problem B — Context is inert
`context` is accepted as a freeform `dict[str, Any]` and passed unchanged to agents. The coordinator never reads it, never uses it to influence routing, never uses it to set constraints. It exists structurally but does nothing.

**Consequence:** A caller cannot tell the system "prefer fast over thorough" or "this is a follow-up to run X; re-use those findings" in any way that actually changes behavior.

**Direction:** Define a typed `TaskContext` schema. Distinguish at minimum: execution preferences (speed vs. quality), prior run linkages (for continuation), user-declared constraints, and domain hints. The routing and decomposition layers should read this explicitly.

#### Problem C — A single vector for a multi-dimensional task
Embedding "write a detailed analysis of X with citations" produces one vector. That vector's nearest neighbors in the registry will be writing/documentation agents. The research dimension and the evaluation dimension of the task produce no signal in routing. The task's full intent is projected onto a one-dimensional axis that the embedding model happens to represent well.

**Consequence:** Multi-role tasks will always route to a single-role agent — whichever role's vocabulary dominates the task description.

**Direction:** Decomposition (see Problem A) solves this structurally. Each sub-task gets its own embedding, its own routing pass, and its own agent assignment. You don't need multi-vector task representation if you decompose first.

---

### 1.2 Routing (`semantic_search` → `llm_rerank` → `decide_reuse_or_create`)

#### What exists
The task's embedding is used to find the K nearest agents in the registry by cosine similarity. Candidates are reranked (heuristically by default, optionally via LLM). If confidence is low and approval is granted, a new agent is created from a hardcoded profile. Otherwise, the top candidate is selected.

#### Problem A — Binary and single: reuse OR create, one agent, full stop
The decision node produces one `selected_agent_id`. A single agent will run. There is no third option in this decision: *compose a pipeline of agents*. The routing architecture makes multi-agent execution structurally unreachable before dispatch even begins.

**Consequence:** No matter what the task is, the answer to "who will do this?" is always "one agent." The system cannot route a task to a sequence like [researcher → writer → evaluator] because the concept of a sequence doesn't exist in the routing output.

**Direction:** The routing output should be a **task execution plan** — an ordered list (or DAG) of `{sub_task, role_type, agent_id_or_create_spec, input_from, output_id}` entries. For simple tasks this degenerates to a list of one. For complex tasks it becomes a directed flow. Everything downstream becomes iteration over that plan rather than dispatch of one agent.

#### Problem B — `_select_capability_profile()` is a hardcoded keyword switch
Agent creation is driven by a `if/elif` block that matches ~20 lowercase keywords to 5 fixed profiles (filesystem, API integration, web research, SQL/data, general fallback). This is brittle. New domains require code edits. Combinations of domains ("write a Python script that queries an API and stores results") hit at most one profile — whichever keyword appears first in the string.

**Consequence:** The system's ability to create *appropriate* agents is bounded by its 5-profile vocabulary. Everything outside those 5 patterns silently falls back to "General Terminal Operator" — a profile that has no specific skills for the actual task.

**Direction:** Use the LLM (already configured in the system) to generate role descriptions rather than keyword-matching to hardcoded templates. The prompt: "Given this task, write a concise professional role description for the agent that should execute it, including key skills and tool requirements." Feed the generated description into the registry search and agent creation pipeline. The 5 profiles become seed templates for bootstrapping, not the ceiling.

#### Problem C — Agent reuse is profile-tag matching, not performance-aware
`_find_reusable_capability_agent` looks for agents whose tags contain the `profile_id` string. It uses semantic search as a pre-filter but the actual reuse decision is binary tag membership. Past performance (success rate, latency, quality scores from audit history) plays no role in the reuse decision.

**Consequence:** A frequently-failing agent with the right tag will be reused indefinitely. A newly created high-quality agent with a slightly different tag won't be found. The registry accumulates agents with no feedback pressure toward quality.

**Direction:** The run history already exists in the `runs` table. Weight candidate scores by historical success rate for similar task embeddings. An agent with 80% success on similar tasks should rank above one with 20% success even if the raw vector similarity is identical.

---

### 1.3 Dispatch (`dispatch_agents_async`)

This is the most important node in the graph to analyze carefully.

#### What exists
The node takes `selected_agent_id`, builds a `candidate_ids` list (selected first, other candidates as fallbacks), and iterates through them. For each candidate, it checks that a manifest and entrypoint exist, builds a `CoordinatorRequest`, and calls `AgentRunner.run_agent()`. On first success, it returns. On failure, it appends to `failed_agents` and tries the next candidate.

#### Problem A — The name is wrong and the behavior is misleading
`dispatch_agents_async` is **not** asynchronous in any practical parallel sense. It runs agents *sequentially*, one at a time, and returns immediately on the first success. The "async" is Python coroutine syntax, not concurrent execution of multiple agents.

**Consequence:** No parallelism. No true multi-agent dispatch. The name creates a false impression in the architecture docs and in the minds of anyone reading the graph definition. This also means the "candidates as fallbacks" behavior only works because it's sequential — which further cements the 1-at-a-time assumption.

**Direction:** For simple/atomic tasks, sequential-with-fallback is fine and logical — keep it. For complex tasks with a decomposed plan, this node becomes a **plan executor** that dispatches sub-tasks in dependency order, passing each agent's output as the next agent's input artifacts.

#### Problem B — Agent outputs are never composed, only replaced
When fallback occurs, the second agent gets the same original `CoordinatorRequest` with no information about why the first agent failed or what partial work it may have done. Each attempt is a clean re-start. On success, the first agent's `response_result` string is the final answer with no further processing.

**Consequence:** Even if the system had two agents dispatched intentionally (e.g., researcher then writer), there is no mechanism to feed the researcher's output into the writer's input. The `CoordinatorRequest.artifacts` field exists but is never populated by the coordinator before dispatch.

**Direction:** For pipeline flows, the coordinator should populate `artifacts` and `context` for each subsequent agent with the structured outputs (not just the raw result string) from prior agents. This requires structured output contracts between agent roles — something the protocol supports in principle (`artifacts` is a list of dicts) but never uses in practice.

#### Problem C — No progress visibility, no mid-run course correction
The coordinator dispatches an agent and blocks on `asyncio.wait_for()` until the agent finishes or times out. Streaming partial results, checking quality mid-run, redirecting execution when the agent is going the wrong direction — none of this is possible.

**Consequence:** For long-running tasks, you either wait for the full timeout or get the answer. No ability to say "stop, you're going the wrong direction" or "this partial result is enough, synthesize from here."

**Direction:** Add a `status_update` message type to the agent protocol. Agents emit periodic progress signals. The coordinator can read them without blocking and use them to update observability, set partial artifacts, or (eventually) interrupt and redirect.

---

### 1.4 Runtime Protocol (`protocol.py` + `runner.py`)

#### What exists
Agents are subprocesses. Communication is JSON-lines over stdin/stdout. An agent can emit `api_call_intent`, `tool_call_intent`, or a final `AgentResponse`. The coordinator responds inline to intents. The subprocess exits after emitting the final response.

#### Problem A — `agent_call_intent` does not exist
An agent can request a tool. An agent can request an API call. An agent **cannot** request that another agent be spawned to help it complete its task. There is no delegation within the protocol.

**Consequence:** An agent that needs internet research to complete a writing task must either have `web_fetch` in its allowlist and do the research itself, or fail. It cannot say "I need a researcher for this sub-problem." Every agent must be self-sufficient for everything it might encounter — which forces either over-provisioned tool allowlists or capability gaps.

**Direction:** Add `agent_call_intent` as a first-class protocol message. It contains a `role_type`, a `subtask` description, and `input_artifacts`. The coordinator's `_run_once` loop intercepts it the same way it intercepts `tool_call_intent` today: it routes and runs the sub-agent, collects its `AgentResponse`, and sends the result back as an `agent_call_result` message. This makes every agent a potential sub-coordinator without changing the process model.

#### Problem B — Protocol message type discrimination is fragile
`parse_agent_line()` attempts to validate a message against three schemas in sequence — `ApiCallIntent`, then `ToolCallIntent`, then `AgentResponse` — using try/except to discriminate. The type of a message is inferred by which schema doesn't throw.

**Consequence:** If a new message type is added (e.g., `agent_call_intent`, `status_update`), you must insert it into exactly the right position in this try/except chain. A type field exists on `ApiCallIntent` and `ToolCallIntent` (`Literal["api_call_intent"]`, etc.) but it isn't used for dispatch. Adding `AgentResponse.type` and switching to type-field dispatch would make new message types safe to add without ordering concerns.

**Direction:** Add a `type` field to `AgentResponse`. Use a single JSON decode + `data["type"]` switch to dispatch to the right schema. This is O(1) lookup instead of O(n) sequential try/except, and it fails fast with a clear error when an unknown type arrives.

#### Problem C — `CoordinatorRequest.artifacts` is structurally present but functionally empty
The field is defined and serialized. No code path in the coordinator ever populates it before dispatch. The protocol supports inter-agent artifact passing as a concept, but the implementation doesn't exercise it.

**Consequence:** Agents always start with an empty artifact context. Prior work in the same session is invisible to subsequent agents.

**Direction:** In a plan-execution model, the coordinator tracks output artifacts by sub-task ID. Before dispatching each agent, it populates `artifacts` with the outputs of all completed dependency sub-tasks. This requires no protocol change — just coordinator-side state management.

---

### 1.5 Agent Capsules

#### What exists
Each capsule: `manifest.json` (identity, tools, tags, embedding), `skill.md` (role definition), `memory.db` (episodic memory), `entrypoint.py` (agent runtime), `logs/`. The `skill.md` is written at creation time from the capability profile template.

#### Problem A — `skill.md` is static and profile-generic
`skill.md` content is the profile's template string, written once at creation. It describes the agent's general role. It does not contain anything about the specific task being run. When the coordinator dispatches the agent, the agent only gets the task string via `CoordinatorRequest.task`.

**Consequence:** The agent's "brief" for any given task is a generic role description plus a raw task string. There is no task-specific framing, no context about why this agent was selected, no hints about what the coordinator expects in the output, no acceptance criteria. The agent must infer everything from the task string alone.

**Direction:** Inject a **run brief** into `CoordinatorRequest.context` at dispatch time. The run brief contains: why this agent was selected, what role it plays in the larger plan (if multi-step), what inputs are provided and what specific output format is expected, and what the acceptance criteria are. The agent reads this from context, not from a hardcoded skill file.

#### Problem B — `memory.db` is local and invisible to the coordinator
Agent episodic memory is stored in the agent's capsule directory. The coordinator never reads it. The registry knows an agent exists and its static description, but not what the agent has actually learned or done.

**Consequence:** The coordinator's routing decisions are based entirely on static embedding vectors and tags set at creation time. An agent that has successfully handled 50 similar tasks — building up rich episodic memory along the way — looks identical to a brand new agent with the same profile from the coordinator's perspective.

**Direction:** Two options. One: surface a memory summary from `memory.db` and store it in the registry manifest (updated after each successful run) so the coordinator can factor it into routing. Two: at dispatch time, retrieve relevant episodic memories from the agent's db based on task embedding and inject them into the `CoordinatorRequest.context`. Both require the coordinator and agent memory to be loosely coupled through a defined interface.

#### Problem C — Agents are execution terminals, not reasoning nodes
Every agent in the system is a leaf: it receives work, does work, returns result, exits. There is no agent type that can break a task into sub-tasks, delegate, monitor, and synthesize. The capsule model has no concept of a **sub-coordinator** agent.

**Consequence:** Hierarchical, recursive task decomposition is impossible. The only orchestrator is the top-level coordinator graph. This means every non-trivial decomposition decision has to be built into the coordinator's planning layer — there is no ability to delegate planning itself.

**Direction:** Define an optional `orchestrator` capability in the manifest. An orchestrator-capable agent follows a richer protocol: it can emit `agent_call_intent` messages, receive `agent_call_result` responses, and manage a local sub-task plan. The coordinator's runner handles orchestrator agents differently — keeping the connection alive across multiple sub-agent invocations rather than expecting a single response.

---

### 1.6 Synthesis & Evaluation (`synthesize_final_response`)

#### What exists
If `response_status == "ok"`, return `response_result` as-is. If error, return a canned error string. No LLM call, no aggregation, no quality check.

#### Problem A — This is not synthesis, it is a conditional identity function
The node does nothing on the success path. The word "synthesize" in the node name implies aggregation, formatting, or quality improvement. None of that happens.

**Consequence:** The quality of the final answer depends entirely on the agent. There is no coordinator-level quality gate. A confident but wrong answer, a partial answer, an answer in the wrong format — all pass through without challenge.

**Direction:** For multi-agent plans, this node should genuinely synthesize: aggregate partial outputs from each sub-task into a coherent final response, structured according to the original task's output requirements. For single-agent tasks, it should at minimum verify that the response satisfies the decomposition step's acceptance criteria before returning to the caller.

#### Problem B — There is no evaluator agent
There is no EvaluatorAgent, no quality-score node, no mechanism that reads the output and asks "does this actually answer the task?"  Nothing in the graph architecture accommodates a feedback loop from evaluation back to re-execution.

**Consequence:** The system cannot self-correct. Quality is not a property the system measures, only something the user judges after the fact.

**Direction:** After the primary agent(s) complete, dispatch an evaluator agent (or use the coordinator's LLM directly) with: the original task, the acceptance criteria from the plan, and the current result. The evaluator returns a score or a pass/fail with critique. If pass, synthesize. If fail and within retry budget, re-dispatch the relevant agent with the critique injected as context. If fail and retry budget exhausted, return the best result along with the critique so the user can understand the deficiency.

---

### 1.7 Failure Recovery (`failure_recovery`)

#### What exists
```python
async def failure_recovery(self, state: dict[str, Any]) -> dict[str, Any]:
    return state
```

#### The problem
This is a no-op stub. When all candidate agents fail, the system routes to this node and then falls through to `update_registry_and_audit`, which records the error, and then to `synthesize_final_response`, which returns "Unable to complete task via current agents."

**Consequence:** There is functionally no failure recovery. The system fails, acknowledges the failure, and stops. No adaptive behavior, no escalation, no re-decomposition, no falling back to a simpler strategy, no asking the user for clarification.

**Direction:** Failure recovery should be a genuine decision node. It should distinguish between:
- Agent execution failure (process crash, timeout) → retry with different candidate or create new agent
- Output quality failure (evaluator rejected result) → re-dispatch with critique
- Complete capability gap (no agent can plausibly do this) → decompose differently or escalate to human
- Quota/permission failure (circuit breaker, approval denied) → pause and request user decision

Each failure type warrants a different recovery strategy.

---

## Part 2 — Seam-Level Analysis

---

### 2.1 Coordinator → Agent: The 1:1 Dispatch Contract

**The problem:** Every edge in the coordinator graph assumes one agent per task. The data model (`selected_agent_id: str` — singular), the dispatch logic (iterate candidates, return on first success), and the protocol (one subprocess, one task, one response) all encode this assumption at different layers.

**Why this is a load-bearing flaw:** You cannot fix multi-agent collaboration by changing just one of these layers. If you make dispatch parallel but the state still has one `selected_agent_id`, you lose the first agent's result. If you add `agent_call_intent` to the protocol but dispatch still returns on first response, the sub-agent infrastructure is never reached. These layers must be changed together as a coherent design.

**Direction:** The refactoring unit is the transition from "task → one agent → result" to "task → plan → plan executor → aggregated result." The plan executor is what replaces `dispatch_agents_async`. It owns the ordered execution of sub-tasks, artifact passing, and result aggregation. The state model expands to represent a plan, not a single selected agent.

---

### 2.2 Agent → Agent: Non-Existent

**The problem:** There is no mechanism by which one agent's output directly influences another agent's execution within the same run. The only shared space is the coordinator state, and agents don't write to it — they write to their `AgentResponse`, which the coordinator reads after the agent exits.

**Why this matters:** In any realistic multi-step task, later agents need more than the original task string. A writer needs the researcher's findings as structured data, not guessed from the task description. An evaluator needs the writer's draft, not a re-inference of what the draft might contain. The hand-off between agents must be explicit and structured.

**Direction:** Define **artifact contracts** between agent roles. A researcher role produces `{sources: [...], findings: {...}, confidence: float}`. A writer role consumes it. The coordinator is responsible for filling `CoordinatorRequest.artifacts` from the plan's dependency graph before each dispatch. Agents declare what artifacts they produce and what they require, and the coordinator enforces this at plan-execution time.

---

### 2.3 Registry ↔ Routing: Static Vectors, No Learning

**The problem:** Agent embeddings are computed at creation time from the agent's description. They never change. The registry `runs` table records success/failure/latency but nothing in the routing path reads this table during routing decisions.

**Why this matters:** The routing layer makes decisions as if every agent were new. An agent's track record is invisible to the system that decides whether to use it. The registry is a write-only audit log from the routing layer's perspective.

**Direction:** After each run, recompute a weighted capability score for the agent that combines static similarity with dynamic performance. Store this in the manifest or as a separate scoring table. The `llm_rerank` step (or a dedicated scoring step) reads these dynamic scores and uses them to adjust the final candidate ordering. This creates a feedback loop: good agents get used more, weak ones get created less often.

---

### 2.4 Memory ↔ Routing: Complete Isolation

**The problem:** Agent `memory.db` is opaque to the coordinator. It exists inside the capsule and is readable only by the agent's own `entrypoint.py` code. The coordinator has no interface to query it.

**Why this matters:** Episodic memory is the highest-quality signal for routing. "This agent successfully handled five tasks with semantic similarity > 0.9 to the current task" is better evidence than any static embedding. But that signal is locked inside the agent's capsule.

**Direction:** Define a lightweight **memory summary** interface. After each successful run, the agent (or the coordinator on the agent's behalf) updates a structured `memory_summary` field in the manifest — a condensed description of categories of tasks the agent has successfully handled, written in embedding-friendly language. The manifest summary is re-embedded and stored in the registry. Routing now benefits from accumulated experience without the coordinator needing to open every agent's SQLite file.

---

### 2.5 Approval Gate ↔ Dispatch: Temporal Mismatch

**The problem:** The approval gate for API calls is a runtime handshake — the agent emits `api_call_intent`, the coordinator responds inline. The graph also has a post-dispatch `approval_gate_for_api_calls` node that is currently a pass-through stub. There are two different approval concepts that don't clearly separate concerns.

**Why this matters:** The runtime approval (inline during execution) is the right model for fine-grained per-call decisions. A post-execution node for approvals makes no sense — by the time you reach it, the agent has already run. This confusion suggests the original intended design was different from what was implemented.

**Direction:** Remove or repurpose `approval_gate_for_api_calls` and `continue_or_deny_api_path`. These nodes conflate pre-execution authorization (which providers is this agent allowed to call at all?) with runtime per-call approval (approve this specific call to this endpoint now?). Pre-execution authorization should be a manifest check before dispatch. Per-call approval is already handled correctly in the runtime callback. The graph nodes are misleading overhead.

---

## Part 3 — System-Level Analysis

---

### 3.1 The System Has No Planning Layer

**What is missing:** Between "understand the task" and "dispatch an agent," there is no step that determines the structure of work required. The question "does this task need one agent or many, and in what order?" is never asked.

**The consequence:** Every task is treated as if it were atomic and executable by a single general-purpose agent. The system cannot decompose. It cannot delegate. Complex tasks either get handled by an over-generalized agent that muddles through, or they fail.

**What a planning layer does:**
- Takes the task + context
- Determines whether the task is atomic (→ single agent route as today) or composite (→ DAG of sub-tasks)
- For composite tasks: produces an ordered list of sub-tasks, each with: role type, input dependencies, expected output format, and acceptance criteria
- The result is a **run plan** that becomes the primary state object driving execution

**How it fits in:** Insert a `decompose_task` node between `embed_task` and `semantic_search`. For composite tasks, `semantic_search` runs once per sub-task in the plan, not once for the whole task. `decide_reuse_or_create` runs once per sub-task. The plan drives the loop.

---

### 3.2 The Coordinator Has No Reasoning Identity

**What is missing:** The coordinator is a mechanical pipeline. It has no system prompt, no personality, no reasoning style, no goals. The todo file mentions adding a `soul.md` — this points at the right problem but frames it as personality. The deeper issue is that the coordinator doesn't *reason* at any step.

**The consequence:** Every decision in the pipeline (route, create, recover) is heuristic or hardcoded. When the heuristics are wrong — which they frequently will be for anything non-trivial — there is no reasoning to fall back on.

**What a coordinator identity enables:**
- The decomposition step uses the coordinator's LLM with a system prompt that defines its reasoning philosophy (prefer delegation to specialists, always specify acceptance criteria, prefer fewer agents over more, etc.)
- The routing and evaluation steps benefit from consistent reasoning rather than ad-hoc heuristics
- The coordinator's behavior can be tuned by editing one authoritative definition, not by changing code
- The `soul.md` concept is valid — it should define the coordinator's reasoning style, values, and decision-making principles, and it should be loaded as the system prompt for every LLM call the coordinator makes

---

### 3.3 No Quality Loop Exists at Any Level

**What is missing:** The system produces an answer and trusts it. There is no evaluation step, no self-critique, no iterative improvement. The graph is a strict one-way DAG from task to answer.

**The consequence:** Output quality is entirely dependent on the agent's first attempt. For tasks with clear correctness criteria this matters greatly; for tasks with subjective output quality it means the system can't improve without human feedback on every run.

**What a quality loop requires:**
- Acceptance criteria extracted at decomposition time (Problem 1.1-A)
- An evaluator role that can assess any artifact against criteria
- A feedback path in the state (critique + score) that can route back to re-dispatch
- A retry budget per sub-task to prevent infinite loops
- A "best effort" fallback when budget is exhausted: return the best result produced so far with the critique attached

**Where this fits in the graph:** After `validate_outputs`, if quality check fails and retry budget remains, route back to `dispatch_agents_async` (for the relevant sub-task) with the critique added to `CoordinatorRequest.context`. `failure_recovery` gets real logic as the retry manager.

---

### 3.4 State Is a Flat, Untyped Bag

**What exists:** `CoordinatorState` is a TypedDict with ~15 top-level keys. All coordination information — routing, execution, results, errors, approvals — lives at the same level.

**The consequence at scale:** As multi-agent plans get implemented, the state will accumulate keys like `subtask_1_agent_id`, `subtask_1_result`, `subtask_2_agent_id`, `failed_subtasks`, etc. This flat bag will become unnavigable. It already conflates distinct concerns: routing state (candidates, scores), execution state (selected agent, response), policy state (approvals), and observability state (run_id, artifacts).

**Direction:** Structure state into named namespaces:
- `routing`: query vector, candidates, rerank scores, confidence, decision
- `plan`: the decomposed sub-task list, dependency graph, acceptance criteria per sub-task
- `execution`: current/completed sub-task states, each with assigned agent, status, input/output artifacts
- `policy`: approval decisions, circuit breaker states
- `meta`: run_id, session_id, task, context

Each node reads and writes only its namespace. This makes the graph's data flow explicit and makes adding new nodes safe — they can't accidentally overwrite unrelated state.

---

### 3.5 The Agent Population Has No Lifecycle Management

**What exists:** Agents are created. They are marked `active`. There is a circuit breaker for API providers. There is no mechanism to retire, demote, merge, or prune agents.

**The consequence:** Over time, `agents/` fills with capsules. Many will be near-duplicates (multiple "General Terminal Operator" agents with slightly different hex suffixes). The registry search will find all of them; the reranker will have to sift through noise. The reuse detection (`_find_reusable_capability_agent`) only deduplicates by profile tag, not by semantic similarity to existing agents.

**Direction:**
- Before creating a new agent, check not just for tag match but for semantic similarity above a **consolidation threshold**. If an existing agent is semantically similar enough, update its description and re-embed rather than creating a duplicate.
- Define agent lifecycle states: `active`, `deprecated`, `retired`. An agent moves to `deprecated` after N consecutive failures; to `retired` after M days without a successful run. Retired agents are excluded from routing but preserved in the registry for audit.
- Periodic registry maintenance: merge agents with cosine similarity above a high threshold by keeping the one with better metrics and updating its description to be the union.

---

### 3.6 Observability Feeds Nothing

**What exists:** `RunLogger` emits JSONL events per run. `AuditStore` records events to SQLite. Rich structured data about every run is being captured. 

**The problem:** None of this data feeds back into system behavior. It is write-only from the system's perspective. You can read it as a human, but nothing in the coordinator reads the run logs during routing, planning, evaluation, or recovery.

**Direction:** The run log and audit data are the system's long-term memory. Three specific feedback loops worth building:
1. **Routing quality**: After N runs, recompute agent embedding weights based on which agents actually succeeded vs. failed for which task types.
2. **Profile improvement**: After an agent created from a generic profile succeeds repeatedly at a specific task type, update its description and re-embed to be more specific — improving future routing precision for those tasks.
3. **Decomposition learning**: When a human-composed task (e.g., "research + write + evaluate") succeeds, record the plan structure as a **plan template** for similar future tasks. This bootstraps the planning layer with real-world validated patterns.

---

## Summary Table

| Component | Core Problem | Severity | Blocks |
|---|---|---|---|
| Task ingestion | No decomposition or acceptance criteria extraction | Critical | Multi-agent flows, quality loops |
| Capability profiles | Hardcoded 5-profile keyword switch | High | New domains, nuanced role creation |
| Routing model | 1-agent output, no plan concept | Critical | All multi-agent collaboration |
| `dispatch_agents_async` | Sequential 1-agent execution, no artifact passing | Critical | Multi-agent, pipeline flows |
| Protocol | No `agent_call_intent`, fragile type dispatch | High | Agent delegation, recursive orchestration |
| Synthesizer | Pass-through, no evaluation or aggregation | High | Quality, multi-agent result merging |
| Failure recovery | No-op stub | High | Any resilient behavior |
| State model | Flat untyped dict | Medium | Maintainability at scale |
| Agent memory | Invisible to coordinator | Medium | Experience-driven routing |
| Agent lifecycle | No retirement, consolidation, or pruning | Medium | Registry quality over time |
| Observability | Write-only, no feedback loops | Medium | System self-improvement |
| Coordinator identity | No reasoning, no system prompt | High | Decomposition quality, routing judgment |

---

## Proposed Architecture Direction (Conceptual)

The design direction that resolves most of the above problems is:

```
ingest_task
  ↓
decompose_task              ← NEW: LLM-driven, produces a Plan (DAG of sub-tasks with roles, deps, criteria)
  ↓
[for each sub-task in plan]:
  route_sub_task            ← semantic search + rerank per sub-task
  create_or_select_agent    ← experience-weighted selection, LLM-driven creation
  dispatch_agent            ← populates artifacts from deps, injects run brief
    ↓ (loop: agent emits intents, receives results, until AgentResponse)
  evaluate_output           ← evaluator agent or LLM check against acceptance criteria
    ↓ if fail and retry budget > 0: re-dispatch with critique appended
    ↓ if fail and exhausted: record best-effort + critique
  record_sub_task_result    ← update plan execution state, store artifacts
[end loop]
  ↓
synthesize_final            ← aggregate all sub-task outputs per plan's synthesis spec
  ↓
update_registry_and_audit   ← includes performance-weighted embedding updates
  ↓
END
```

The key structural changes from today:

1. **Plan replaces single selected agent** as the primary execution state object.
2. **Sub-task loop** replaces the single `dispatch_agents_async` call.
3. **Artifact passing** is explicit, coordinator-managed, driven by plan dependency graph.
4. **Evaluator** is a first-class step in the sub-task loop.
5. **Retry with critique** is the primary recovery mechanism, not try-next-candidate.
6. **Coordinator has a reasoning identity** (system prompt / soul.md) that drives decomposition, evaluation, and synthesis quality.
7. **Protocol gains `agent_call_intent`** for recursive delegation from capable orchestrator agents.

None of these changes require discarding the current codebase. The existing registry, approval gate, tool executor, audit store, and run logger all remain valid. The changes are additive at the graph and protocol layer, and corrective at the capability profile and state model layer.

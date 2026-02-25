# Closed Claw — Concrete Problems & Novel Improvements

> Every item here is grounded in specific code. File + line references included.

---

## 1. `skill.md` is written to disk but agents never see it — they run blind

**What the code actually does:**

`AgentFactory.create_capsule()` writes a customised `skill.md` to `agents/<id>/skill.md`.
`_request_config_for_agent()` (nodes.py ~L1418) builds the config dict sent to the agent subprocess. It passes `tool_registry` and `llm` config — **`skill.md` is never loaded or included**.

In the entrypoint template (factory.py ~L175), the LLM prompt is:

```python
plan_prompt = (
    "You are a specialist agent inside the Closed Claw orchestrator.\n"
    "Available tools: " + json.dumps(available_tools) + "\n"
    "Context: " + json.dumps(context)[:2000] + "\n\n"
    "Task:\n" + task + "\n\n"
    ...
)
```

There is no system prompt. There is no reference to the agent's identity or role. A "Terminal Master" agent and a "Legal Document Analyst" agent send **the exact same prompt text** to the LLM — only the `task` string differs.

**The fix:**  
In `_request_config_for_agent`, read `(agents_dir / agent_id / "skill.md").read_text()` and pass it as `config["system_prompt"]`.  
In the entrypoint template, use it as the `system` message:

```python
messages = [
    {"role": "system", "content": config.get("system_prompt", "You are a specialist agent.")},
    {"role": "user", "content": plan_prompt},
]
```

This is the **single highest ROI change** in the codebase. Every agent would immediately behave according to its specialty.

---

## 2. `memory_updates` are logged then silently discarded — agents are permanently amnesiac

**What the code actually does:**

`AgentResponse` has a `memory_updates` field. Agents can return `[{"key": "last_result", "value": "..."}]`.

In `_execute_phase_pool` (nodes.py ~L815), after a successful run:

```python
item["result"] = response.result
subtask_results[item["task_id"]] = response.result
# memory_updates logged to runlog... and that's it.
```

`memory.db` is initialised with a `memory` table (factory.py ~L386) but **no code path ever reads from it or writes to it after creation**. Every agent starts with zero memory of every previous run, forever.

**The fix:**  
1. After a successful agent run, persist `memory_updates` to `agents/<id>/memory.db`:
   ```python
   _flush_memory_updates(agents_dir / agent_id / "memory.db", response.memory_updates)
   ```
2. Before constructing `CoordinatorRequest`, load the last N memory rows and inject them into `context["prior_memory"]`.

This enables agents to accumulate facts across runs — e.g. a "Code Analyst" remembers which modules it already scanned, a "Terminal Master" remembers which commands previously failed.

---

## 3. The agent plan is frozen before execution starts — no reaction to tool results

**What the code actually does (entrypoint template, factory.py ~L155):**

```
Phase 1: Ask LLM to produce all steps upfront → {"steps": [...]}
Phase 2: Execute each step in order
Phase 3 (on failure): Ask LLM to fix args for the SAME step only
```

If step 1 runs `file_io list /some/dir` and returns `["file_a.py", "file_b.py", "file_c.py"]`, step 2's args were already committed before step 1 ran. The agent cannot decide to process each file individually based on what step 1 discovered.

The "corrective action" in Phase 3 only updates args for the failing step, not the whole plan forward. The agent is flying blind after step 0.

**The fix — ReAct-style per-step planning:**

Replace the frozen plan with a loop:

```
observe current state →
ask LLM: "what is the next single step?" →
execute it →
observe result →
ask LLM: "what is the next single step given this result?" →
... repeat until LLM says {"done": true, "result": "..."}
```

LLM prompt on each iteration includes the **accumulated step history** so it can reason about what it knows now. This is architecturally a small change to the entrypoint template but makes agents vastly more capable on non-trivial tasks.

---

## 4. Subtask results are untyped text blobs — downstream agents must re-parse natural language

**What the code actually does:**

```python
dep_context = {
    dep: by_id[dep].get("result", "")
    for dep in item.get("depends_on", [])
    if dep in by_id
}
```

`result` is a plain string — whatever the LLM summary said. If a discovery subtask returns `"Found 3 Python files: utils.py, main.py, config.py"`, the downstream execution subtask receives this as a natural language sentence injected into its context.

The execution agent then has to ask **its** LLM to re-parse "3 Python files: utils.py, main.py, config.py" back into a list before it can act on them. This is a lossy, error-prone round trip.

**The fix:**  
Add a `structured_output: dict` field to `AgentResponse`. Discovery agents return:  
```json
{"files": ["utils.py", "main.py", "config.py"], "count": 3}
```  
Execution agents receive this as `context["subtask"]["depends_on_structured"][dep_id]`, bypassing the natural language re-parse entirely.

`skill.md` declares the output schema. `CoordinatorRequest` config includes the expected schema for agents that depend on a structured output.

---

## 5. The entire task pool executes serially — the async architecture is fake

**What the code actually does (nodes.py ~L558):**

```python
ready = [t for t in pool if t["status"] == "pending"]
for item in ready:   # ← serial loop
    item["status"] = "in_progress"
    ...
    response = await self.runner.run_agent(...)  # ← blocks until this agent finishes
```

Even if ten tasks are all in `ready` state with no dependencies on each other, they execute one by one. The `await` here is sequential, not concurrent.

**The fix:**

```python
ready = [t for t in pool if t["status"] == "pending"]
if ready:
    await asyncio.gather(*[self._run_subtask(item, ...) for item in ready])
```

Agent subprocesses are fully independent — their stdin/stdout are isolated. There is no shared mutable state between concurrent runners beyond the `tool_events` and `approvals` lists (which need a lock). This single change could make multi-agent runs 3-5x faster for tasks with independent subtasks.

---

## 6. Circular dependency graphs are silently converted to "unresolved_dependencies" failures

**What the code actually does (nodes.py ~L622):**

```python
waiting_cycles += 1
if waiting_cycles >= 2:
    for item in pool:
        if item["status"] in {"waiting", "pending"}:
            item["status"] = "failed"
            item["error"] = "unresolved_dependencies"
    break
```

If the LLM planner emits `A depends_on [B]` and `B depends_on [A]`, both tasks sit in `"waiting"` forever. After 2 polling cycles they silently fail with "unresolved_dependencies" and the user has no idea why.

**The fix — topological sort in `_prepare_phase_pool`:**

```python
# Kahn's algorithm
in_degree = {t["task_id"]: len(t["depends_on"]) for t in pool}
queue = [t for t in pool if in_degree[t["task_id"]] == 0]
order = []
while queue:
    node = queue.pop()
    order.append(node)
    for t in pool:
        if node["task_id"] in t["depends_on"]:
            in_degree[t["task_id"]] -= 1
            if in_degree[t["task_id"]] == 0:
                queue.append(t)
if len(order) != len(pool):
    raise ValueError(f"Circular dependency detected in task plan: {[t['task_id'] for t in pool if t not in order]}")
```

This runs instantly (topological sort is O(V+E)) and produces a clear error at plan time, not at execution time.

---

## 7. Role tags from LLM output are strings — semantically identical roles spawn duplicate agents

**What the code actually does:**

The LLM planner freely chooses `role_tag` strings. In one run it might output `"file-manager"`, in another `"filesystem-operator"`, in another `"file-handler"`. These are three different keys in `role_agent_map`, which means three different agent creation calls, three different registry entries, and three agents that all do identical things.

Within a single run, `role_agent_map` caches perfectly. But across runs, the semantic search (`_find_reusable_capability_agent`) uses vector similarity on the agent's `description` — not on the `role_tag`. The role_tag is just logged and stored as a manifest tag, never used for lookup.

**The fix — canonical role registry:**  
Add a `role_tag_index` table to `registry.db`: `(canonical_role_tag TEXT, agent_id TEXT)`.  
Before creating an agent for a role, embed the role_tag and check cosine similarity against all canonical tags in the index. If similarity > 0.9, reuse the existing mapping. Otherwise insert a new canonical entry.

This is different from the capability profile reuse — it's about recognising that "file-manager" and "filesystem-operator" are the same role at the naming level, not just the capability level.

---

## 8. All LLM calls are sync `httpx.Client` blocking the async event loop

**What the code actually does:**

`generate_task_plan`, `generate_agent_profile`, `_call_openai`, `_call_gemini`, `_call_claude` in `search.py` and `nodes.py` all use:

```python
with httpx.Client(timeout=timeout_sec) as client:
    resp = client.post(...)
```

These are called from `async def decompose_task`, `async def execute_task_pool`, etc. — inside the event loop.

Synchronous I/O in an async context blocks the event loop thread entirely during the HTTP request. If LLM calls take 5–30 seconds (typical), no other async activity can proceed.

**The fix:**

```python
async with httpx.AsyncClient(timeout=timeout_sec) as client:
    resp = await client.post(...)
```

Every `_generate_text_with_provider` call in `search.py` should be `async def`. This unlocks true concurrency between LLM calls and future parallel subtask execution (point 5 above).

---

## 9. The discovery phase fires unconditionally — simple tasks waste an entire LLM planning round trip

**What the code actually does:**

`execute_task_pool` always calls `generate_task_plan(..., phase="discovery")` then `generate_task_plan(..., phase="execution")`. That's two full LLM planning calls for every run, regardless of the task.

"List files in this directory." — fires a discovery plan, executes it, fires an execution plan, executes that.  
"What day is today?" — same.

**The fix — task complexity classifier:**  
Add a single cheap LLM call before decomposition:

```
Classify this task as SIMPLE or COMPLEX.
SIMPLE: can be answered or executed in one step with no prior exploration needed.
COMPLEX: requires information gathering before the execution plan can be formed.
Task: {task}
Return JSON: {"complexity": "simple"|"complex", "reason": str}
```

If `simple`: skip discovery phase, generate one execution plan.  
A `simple` classification saves one full LLM planning call and one full phase of agent execution for probably half of all real-world tasks.

---

## 10. The `_verify_subtask_tool_execution` gate can fail correct agents

**What the code actually does (nodes.py ~L1452):**

```python
if not bool(item.get("requires_tool", False)):
    return True, ""
if not new_tool_events:
    return False, "required_tool_call_not_observed"
```

`requires_tool` is set by the **LLM planner** when it generates the task plan. If the planner says `"requires_tool": true` for a subtask, but the agent resolves it entirely through LLM reasoning without using a tool, it gets marked as failed — even if the `result` is correct.

Concretely: a "summarize text" subtask with `requires_tool: true` (planner thought it would read a file) but the text was already in context means the agent will be marked failed for doing a good job.

**The fix:**  
Remove `requires_tool` as a binary verification gate. Replace it with `expected_tool_calls: list[str]` — specific tool names the planner expects to be used. Verification checks that at least one tool from this list was called successfully, but does **not** fail an agent that returned `status: ok` with a non-empty result. The verification should augment retry decisions, not override a clearly successful response.

---

## 11. Two coordinator state pools (`discovery_subtask_pool`, `execution_subtask_pool`) merge but discovery results are string-keyed by task_id — same task_id names across phases collide

**What the code actually does:**

`_prepare_phase_pool` prefixes task IDs with `"discover-"` or `"execute-"`. But `subtask_results` stores them as:

```python
subtask_results[item["task_id"]] = response.result
subtask_results[f"{phase}.{item['task_id']}"] = response.result
```

So a task `"discover-file-scan"` is stored under both key `"discover-file-scan"` AND `"discovery.discover-file-scan"`. The downstream `dep_context` lookup uses `item.get("depends_on", [])` — the raw `depends_on` entries from the plan, which reference `discovery` phase task_ids as plain strings.

If the planner outputs a dependency like `"depends_on": ["file-scan"]` (without the phase prefix), the lookup `by_id["file-scan"]` will miss because the actual key is `"discover-file-scan"` or `"execute-file-scan"`. The `dep_context` for that dependency silently returns `""` instead of failing loudly. Downstream agents get empty context for their dependencies with no error signal.

**The fix:**  
Centralise result lookup into a single `_resolve_dep_result(task_id, subtask_results)` function that tries the exact key, then `"discovery.{key}"`, then `"execution.{key}"`, then fuzzy-prefix matches. Log a warning when a dependency resolves to empty string so the dev can detect silent context loss.

---

## 12. New architecture idea: Agents should declare output contracts, not just tool allowlists

**Current model:**  
Agent = tools_allowlist + skill.md (unread) + an entrypoint that's identical for every agent.

**Proposed model:**  
Each agent has an `output_contract` in its manifest describing what it promises to produce:

```json
{
  "output_contract": {
    "type": "file_list",
    "schema": {"files": ["string"], "base_dir": "string"}
  }
}
```

The coordinator uses this contract to:
1. **Validate agent output** against the declared schema before marking a subtask complete.
2. **Route dependencies** — only assign a downstream subtask to an agent whose expected input matches the upstream agent's output contract.
3. **Generate concrete acceptance criteria** that can be machine-checked, not just "task output is complete."

This turns the agent registry from a fuzzy semantic search into a typed capability graph — more like function composition than "find something vaguely like this."

---

## 13. The circuit breaker tracks provider failures (API call denials) but not agent logic failures

**What the code actually does:**

`registry.py` has `provider_circuit_breakers` — it opens when an API provider gets denied too many times.

But there's no equivalent for **agent-level failures**. If agent `abc123` fails 5 times in a row on every run (maybe its entrypoint has a bug, or its LLM prompts are poorly tuned for a class of tasks), it keeps getting selected and retried indefinitely.

`AgentManifest` has `success_rate` and `usage_count` fields that are updated in `update_registry_and_audit` — but these metrics are **never consulted during agent selection**. `_find_reusable_capability_agent` only checks status == "active" and semantic similarity score. A 0% success rate agent is just as likely to be selected as a 100% success rate agent.

**The fix:**  
Weight reranking scores by agent success_rate:

```python
adjusted_score = semantic_score * (0.3 + 0.7 * manifest.success_rate)
```

And add a soft circuit breaker: if `success_rate < 0.2 AND usage_count > 5`, mark agent status as `"degraded"` and skip it during selection. Surface this in `cli agents` as a warning.

---

## 14. `synthesize_final_response` makes a fresh LLM call but doesn't know which subtask results are contradictory

**What the code actually does:**

The synthesis prompt dumps all subtask text results and asks the LLM to "write a clear summary." This is additive — it assumes all results are consistent and complementary.

Consider this scenario: a file-write subtask (execution phase) says "Created report.md". A file-read subtask that runs AFTER it (also execution phase, different agent) says "report.md not found." The synthesis LLM sees both strings and might say "Created report.md (not found)." — which is meaningless.

**The fix — contradiction detection pass:**  
Before synthesis, run a lightweight scan across results looking for known contradictory patterns:
- Any subtask with `status: completed` + any other subtask saying it failed to find the artifact
- Any two subtasks operating on the same file path with conflicting outcomes

Flag these as `conflicting_results` and pass them to the synthesis prompt explicitly:  
`"Warning: the following results may conflict: [...]. Reconcile these before summarising."`

---

## 15. The `organize_options` context path is hardcoded in `cmd_run` — it's one feature's context getting special CLI flags

**What the code actually does (cli.py ~L85):**

```python
if organize_path:
    context_obj["organize_options"] = {
        "path": organize_path,
        "dry_run": bool(getattr(args, "organize_dry_run", False)),
        "recursive": bool(getattr(args, "organize_recursive", False)),
    }
```

`--organize-path`, `--organize-dry-run`, `--organize-recursive` are special-cased CLI flags for one specific type of task. Every other task type that needs structured parameters has to jam things into `--context-json` as a raw JSON string.

**The fix:**  
Replace with a generic `--param KEY=VALUE` flag (multi-value):

```
python -m closed_claw.cli run "organize files" --param path=/some/folder --param dry_run=true
```

Which builds `context_obj["params"] = {"path": "/some/folder", "dry_run": "true"}`. The coordinator passes this to agents without the CLI needing to know anything about specific task types.

Remove the three organize-specific flags, and document the `--param` pattern in `skill.md` for parameter-driven agents.

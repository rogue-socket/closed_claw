# Closed Claw — Concrete Problems & Novel Improvements

> Every item here is grounded in specific code and real run-log evidence.
> File + line references included. Run-log evidence tagged with `[LOG]`.

### Implementation Status (last verified 2026-02-26)

| # | Title | Status | Priority |
|---|-------|--------|----------|
| 1 | `skill.md` never read by agents | **DONE** | — |
| 2 | `memory_updates` discarded | open | medium |
| 3 | Frozen plan / no reaction to tool results | **DONE** | — |
| 4 | Subtask results are untyped text | open | medium |
| 5 | Task pool executes serially | **DONE** — `asyncio.gather` in `_execute_phase_pool` | — |
| 6 | Circular deps silently fail | open | high |
| 7 | Duplicate role tags across runs | open | medium |
| 8 | Sync httpx blocks async loop | open | medium |
| 9 | Discovery fires unconditionally | open | **critical** |
| 10 | `_verify_subtask_tool_execution` gate too strict | open | medium |
| 11 | Phase task_id collisions | open | low |
| 12 | Output contracts (architecture idea) | open | low |
| 13 | Agent-level circuit breaker missing | open | high |
| 14 | Contradiction detection in synthesis | open | medium |
| 15 | `organize_options` hardcoded in CLI | open | low |
| 16 | Duplicate tool calls not blocked | open | **critical** |
| 17 | Generic "Task Operator" fallback is useless | open | **critical** |
| 18 | `web_fetch` returns raw HTML — agents re-fetch | open | high |
| 19 | LLM errors disguised as successful results | open | high |
| 20 | Discovery results relevance not validated | open | high |
| 21 | Entrypoint template null-byte corruption | open | high |
| 22 | Agent gets ALL tools on profile fallback | open | high |
| 23 | httpx.Client created per call — no connection reuse | open | medium |
| 24 | Tool result truncation is invisible | open | medium |
| 25 | Agent subprocess stderr is silently discarded | open | medium |
| 26 | `_find_reusable_capability_agent` ignores success_rate | open | high |
| 27 | Run-level cost/token tracking missing | open | medium |
| 28 | No `--dry-run` mode for the coordinator | open | medium |
| 29 | Stale agent cleanup / garbage collection | open | medium |
| 30 | Sandbox directory is implicit — no config control | open | medium |

---

## 1. ~~`skill.md` is written to disk but agents never see it — they run blind~~ ✅ IMPLEMENTED

> **Resolved in v12/v13.** `_compose_system_prompt()` (nodes.py ~L1458) now reads base
> skill modules from `agents/skills/<skill_id>.md` (Layer 1) and the agent role overlay
> from `agents/<agent_id>/skill.md` (Layer 2), composes them, and passes the result as
> `config["system_prompt"]`. The v13 entrypoint template injects it as the system message
> in every `_execute_llm_http()` call.

<details><summary>Original analysis (preserved for context)</summary>

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

</details>

---

## 2. `memory_updates` are logged then silently discarded — agents are permanently amnesiac

**What the code actually does:**

`AgentResponse` has a `memory_updates` field. Agents can return `[{"key": "last_result", "value": "..."}]`.

In `_execute_phase_pool` (nodes.py ~L745), after a successful run:

```python
item["result"] = response.result
subtask_results[item["task_id"]] = response.result
subtask_results[f"{phase}.{item['task_id']}"] = response.result
# memory_updates logged to runlog (L759)... and that's it.
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

## 3. ~~The agent plan is frozen before execution starts — no reaction to tool results~~ ✅ IMPLEMENTED

> **Resolved in v13.** The entrypoint template (factory.py) was rewritten to use a
> ReAct-style iterative loop: observe → think (LLM) → act (tool) → loop. Each
> iteration builds a prompt with the **full accumulated step history** so the LLM
> can adapt to tool output. The frozen up-front plan is gone. The agent can now
> iterate over files discovered in step 1, retry with different args on failure,
> or finish early via `{"done": true, "result": "..."}`. Hard ceiling of
> `MAX_STEPS=15` and `MAX_CONSECUTIVE_ERRORS=4`.

<details><summary>Original analysis (preserved for context)</summary>

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

</details>

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
Add a `structured_output: dict` field to `AgentResponse` (which now includes `type: Literal["agent_response"]` as of the recent bug-fix round). Discovery agents return:  
```json
{"files": ["utils.py", "main.py", "config.py"], "count": 3}
```  
Execution agents receive this as `context["subtask"]["depends_on_structured"][dep_id]`, bypassing the natural language re-parse entirely.

`skill.md` declares the output schema. `CoordinatorRequest` config includes the expected schema for agents that depend on a structured output.

---

## 5. ~~The entire task pool executes serially — the async architecture is fake~~ ✅ IMPLEMENTED

> **Resolved.** `_execute_phase_pool` (nodes.py ~L838) now uses `asyncio.gather` to run
> all `pending` subtasks concurrently within each pool iteration. Per-attempt local
> `attempt_tool_events` / `attempt_approvals` lists prevent cross-contamination between
> concurrent subtasks. The `_run_single_subtask` coroutine (~L532) is designed for
> concurrent execution via `asyncio.gather` inside the ready-batch.

<details><summary>Original analysis (preserved for context)</summary>

**What the code actually does:**

The pool loop in `_execute_phase_pool` discovers all `pending` tasks and gathers them concurrently:

```python
ready = [t for t in pool if t["status"] == "pending"]
if ready:
    await asyncio.gather(*(self._run_single_subtask(...) for item in ready))
```

Agent subprocesses are fully independent — their stdin/stdout are isolated. Per-attempt local event lists prevent cross-contamination.

</details>

---

## 6. Circular dependency graphs are silently converted to "unresolved_dependencies" failures

**What the code actually does (nodes.py ~L795):**

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

**What the code actually does (nodes.py ~L1543):**

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

`_prepare_phase_pool` (nodes.py ~L448) prefixes task IDs with `"discover-"` or `"execute-"`. But `subtask_results` stores them as (nodes.py ~L746):

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
Agent = tools_allowlist + skill.md (now read via `_compose_system_prompt`) + an entrypoint that's identical for every agent.

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

`AgentManifest` has `success_rate` and `usage_count` fields that are updated in `update_registry_and_audit` (nodes.py ~L1094) — but these metrics are **never consulted during agent selection**. `_find_reusable_capability_agent` (nodes.py ~L836) only checks `status == "active"` and semantic similarity score. A 0% success rate agent is just as likely to be selected as a 100% success rate agent.

**The fix:**  
Weight reranking scores by agent success_rate:

```python
adjusted_score = semantic_score * (0.3 + 0.7 * manifest.success_rate)
```

And add a soft circuit breaker: if `success_rate < 0.2 AND usage_count > 5`, mark agent status as `"degraded"` and skip it during selection. Surface this in `cli agents` as a warning.

---

## 14. `synthesize_final_response` makes a fresh LLM call but doesn't know which subtask results are contradictory

**What the code actually does:**

The synthesis prompt (nodes.py ~L1178) dumps all subtask text results and asks the LLM to "write a clear, concise summary of what was accomplished." This is additive — it assumes all results are consistent and complementary.

Consider this scenario: a file-write subtask (execution phase) says "Created report.md". A file-read subtask that runs AFTER it (also execution phase, different agent) says "report.md not found." The synthesis LLM sees both strings and might say "Created report.md (not found)." — which is meaningless.

**The fix — contradiction detection pass:**  
Before synthesis, run a lightweight scan across results looking for known contradictory patterns:
- Any subtask with `status: completed` + any other subtask saying it failed to find the artifact
- Any two subtasks operating on the same file path with conflicting outcomes

Flag these as `conflicting_results` and pass them to the synthesis prompt explicitly:  
`"Warning: the following results may conflict: [...]. Reconcile these before summarising."`

---

## 15. The `organize_options` context path is hardcoded in `cmd_run` — it's one feature's context getting special CLI flags

**What the code actually does (cli.py ~L81, parser flags at ~L574):**

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

---

## 16. CRITICAL: Duplicate tool calls not blocked — agents loop on identical calls, wasting tokens and time

**[LOG] Evidence:** Run `61e636c6` — the discovery agent called `web_fetch` with the **exact same URL** (`docs.python.org/3/library/math.html`) **9 consecutive times**, receiving identical 10KB HTML responses each time. Each call's reason was a minor rephrase of "fetch once more to confirm." Run `5c5ab19f` shows the same pattern. The file also read `instructions.txt` twice with identical results. **10 of 12 tool calls (83%) were pure waste.** This bloated the JSONL log to 112KB and burned 9 unnecessary LLM turns.

**What the code actually does:**

The v13 entrypoint template (factory.py ~L323) includes a prompt instruction:
```
"NEVER call the same tool with identical arguments more than once."
```
But this is a **soft prompt instruction only** — there is no programmatic enforcement. The LLM simply ignores it under certain conditions (especially with weaker models like qwen3-30b).

The `AgentRunner` (runner.py ~L67) forwards every `tool_call_intent` to the tool executor without any deduplication check. The `_tool_callback` (nodes.py ~L1440) also doesn't check for duplicates.

**The fix — enforce at two levels:**

1. **In AgentRunner** — maintain a `set()` of `(tool, json.dumps(args, sort_keys=True))` tuples seen in the current run. If an identical call is received, return a synthetic `ToolCallResult(ok=True, result=previous_result)` immediately without executing the tool, and log a `"tool_call_deduplicated"` event.

2. **In the entrypoint template** — maintain a `_tool_cache: dict[str, dict]` that maps `f"{tool}:{json.dumps(args, sort_keys=True)}"` to the previous result. Before emitting a `tool_call_intent`, check the cache and reuse the result locally. This avoids even the coordinator round-trip.

3. **Configurable limit** — add `CLOSED_CLAW_MAX_IDENTICAL_TOOL_CALLS` (default: 1) to Settings. After N identical calls, return the cached result. Setting to 0 disables dedup (for tools with side effects like `terminal`).

**Impact:** Would have reduced run `61e636c6` from 12 tool calls to 3, and from 112KB JSONL to ~15KB. Estimated 4x throughput improvement for runs with looping agents.

---

## 17. CRITICAL: Generic "Task Operator" fallback produces useless agents that can't accomplish anything

**[LOG] Evidence:** Runs `e46fa5d4`, `096f8464`, `61e636c6` all produced agents named "Task Operator XXXX" with `profile_id: "task-operator"`, `tools_allowlist: ["terminal", "http_api", "web_fetch", "file_io", "python_exec", "sql_query"]` (all 6 tools), and a trivial skill.md: `"# Task Operator\n\nExecute assigned tasks safely.\n"`. In contrast, run `7e06cbf6` (which had a concrete task) produced specialized agents: "Instruction Reader 317e" with `tools: ["file_io"]` and "Python Code Generator e7f6" with `tools: ["file_io", "python_exec"]`.

**What the code actually does:**

`generate_agent_profile` in search.py (~L213) calls the LLM to generate a profile. When the LLM call **fails** or returns unparseable JSON, `_normalize_profile_payload` (~L668) applies the fallback:
```python
tools = [t for t in fallback_tools if t in supported_tools]  # ALL tools
```
And the skill.md fallback is:
```python
skill_md = f"# {name_prefix}\n\n...Execute requests safely...\n"
```

But the problem isn't just in the fallback — the LLM itself produces a generic profile when the **input task is vague**. The profile generator receives the raw subtask description (e.g., `"Role tag: context-discoverer. Subtask title: Collect Required Context"`) which gives the LLM no specifics to specialize on.

**The fix — three changes:**

1. **Enrich profile generation input:** Pass the **original user task** along with the subtask to `generate_agent_profile`, so the LLM has real context:
   ```python
   profile = generate_agent_profile(settings, task=f"User task: {state['task']}\nSubtask: {role_prompt}", ...)
   ```

2. **Hard minimum quality gate:** After `_normalize_profile_payload`, check `profile["profile_id"] == "task-operator"` AND `len(profile["tools_allowlist"]) > 4`. If both true, this is the useless generic fallback. Log a warning and **narrow tools to the 2 most relevant** based on keyword heuristics (e.g., task mentions "file" → `["file_io"]`, mentions "web" → `["web_fetch", "http_api"]`).

3. **Mandatory skill_md length check:** If `len(skill_md) < 50`, reject the profile and retry once with a more explicit prompt that includes the full user task.

---

## 18. `web_fetch` returns raw HTML — agents can't extract useful information and re-fetch endlessly

**[LOG] Evidence:** Run `61e636c6` — agent fetched `docs.python.org/3/library/math.html` 9 times. Each response was ~10KB of raw HTML (`<!DOCTYPE html>...`) with navigation menus, CSS links, and JavaScript. The agent's history shows it couldn't extract the actual function documentation from the HTML and kept refetching "to confirm completeness."

**What the code actually does:**

`ToolExecutor.execute` for `web_fetch` (executor.py ~L200) returns:
```python
{"status_code": 200, "text": response.text}
```
This is the **raw HTML source**. For an LLM trying to extract structured information, raw HTML is noise-heavy — meta tags, nav bars, scripts, stylesheets all consume context window but carry zero useful content.

**The fix:**

1. **Add HTML-to-text extraction** in `web_fetch`:
   ```python
   from html.parser import HTMLParser
   class _HTMLStripper(HTMLParser):
       # Strip tags, scripts, styles → return clean text
   ```
   Return `{"status_code": 200, "text": stripped_text, "raw_html_bytes": len(response.text)}`. This is stdlib-only (no beautifulsoup dependency).

2. **Truncate text to a configurable max** (e.g., `CLOSED_CLAW_WEB_FETCH_MAX_TEXT_CHARS=8000`). The agent rarely needs more than the first few KB of text content.

3. **Add optional `selector` arg** to `web_fetch` so agents can request specific content portions (e.g., `{"url": "...", "selector": "article"}` returns only `<article>` tag content).

**Impact:** Would have reduced each web_fetch result from ~10KB of HTML noise to ~2KB of useful text, making the LLM's context window dramatically more useful and likely preventing the re-fetch loop entirely.

---

## 19. LLM errors are disguised as successful agent results — coordinator can't distinguish success from failure

**[LOG] Evidence:** Run `7e06cbf6` — the discovery agent returned `status: "ok"` with `result: "[llm_error: HTTP 400: TextEncodeInput must be...]"`. The coordinator treated this as a successful discovery, passing the error text as `discovery_results` to the execution phase. The execution agent then received an LLM error string as its "dependency context."

**What the code actually does:**

The v13 entrypoint (factory.py ~L293) has a check for this:
```python
if response.result and response.result.strip().startswith("[llm_error:"):
    failure_reason = f"agent_llm_failure: {response.result[:200]}"
```
This is in `_run_single_subtask` (nodes.py ~L709). **But this check is brittle** — it only matches the exact prefix `"[llm_error:"`. The entrypoint can return LLM errors in many formats:
- `"[llm_error: ...]"` (detected)
- `"Error: HTTP 400..."` (NOT detected)
- `"I was unable to complete the task because the API returned..."` (NOT detected)
- An empty result `""` with `status: "ok"` (NOT detected)

**The fix — multi-layer detection:**

1. **Expand prefix patterns** in `_run_single_subtask`:
   ```python
   _ERROR_PREFIXES = ("[llm_error:", "Error:", "RuntimeError:", "HTTP 4", "HTTP 5",
                       "I was unable to", "I cannot complete", "No response")
   if any(result.strip().startswith(p) for p in _ERROR_PREFIXES):
       failure_reason = f"agent_result_looks_like_error: {result[:200]}"
   ```

2. **Empty result gate:** If `result.strip() == ""` and `status == "ok"`, treat as failure.

3. **LLM-based result validation** (optional, for high-value runs): Before accepting a subtask result, run a cheap classification prompt: `"Does this text report successful task completion or an error? Reply 'success' or 'error'."` This catches cases where the agent wrote a full paragraph explaining why it failed.

---

## 20. Discovery results relevance is never validated — execution uses irrelevant context

**[LOG] Evidence:** Run `61e636c6` — task was `"please use paid_api for analysis"`. Discovery agent found and read `instructions.txt` from a leftover sandbox file (about building a Python calculator) and returned this as "context." The execution phase received calculator instructions as "discovery results" and correctly concluded the task couldn't be done — but only after wasting 12 tool calls gathering irrelevant context.

**What the code actually does:**

In `execute_task_pool` (nodes.py ~L400), discovery results are passed directly to `generate_task_plan(..., phase="execution", discovery_results=discovery_results)`. There is no validation that the discovery results are **relevant to the original task**. The execution planner receives whatever the discovery phase found, and must infer relevance on its own.

**The fix:**

After discovery completes, add a **relevance check**:
```python
relevance_prompt = (
    f"Original task: {state['task']}\n"
    f"Discovery results: {json.dumps(discovery_results)[:2000]}\n\n"
    "Are these discovery results relevant to the original task? "
    "Return JSON: {\"relevant\": true|false, \"reason\": str}"
)
```
If `relevant == false`, skip the execution phase entirely and return a helpful message: `"Discovery found no relevant context for this task. The task may need to be rephrased or the working directory may not contain the expected files."`

This is cheaper than running a full execution phase on irrelevant data. One LLM call (~0.001 USD) to save 2-10 tool calls + agent creation.

---

## 21. Entrypoint template may write files with null bytes — agent subprocess can't parse them

**[LOG] Evidence:** Run `e46fa5d4` — agent failed with `SyntaxError: source code cannot contain null bytes`. The entrypoint.py file was corrupted. Both retry attempts (1/2 and 2/2) hit the same error because the corrupted file persists on disk.

**What the code actually does:**

`AgentFactory.create_capsule` (factory.py ~L515) writes:
```python
(capsule_dir / "entrypoint.py").write_text(ENTRYPOINT_TEMPLATE, encoding="utf-8")
```
The `ENTRYPOINT_TEMPLATE` is a triple-quoted string (~470 lines) defined at module level. If any edit introduces a `\x00` byte (e.g., copy-paste from a binary editor, or string interpolation with a null), the written file becomes unparseable by Python.

Additionally, this specific failure was in a **pytest temp directory**, suggesting the test framework or file system operation may have introduced corruption.

**The fix:**

1. **Sanitize on write** — strip null bytes from the template before writing:
   ```python
   safe_template = ENTRYPOINT_TEMPLATE.replace("\x00", "")
   (capsule_dir / "entrypoint.py").write_text(safe_template, encoding="utf-8")
   ```

2. **Validate after write** — immediately compile the written file to ensure it's valid Python:
   ```python
   import py_compile
   try:
       py_compile.compile(str(capsule_dir / "entrypoint.py"), doraise=True)
   except py_compile.PyCompileError as exc:
       raise RuntimeError(f"Generated entrypoint.py is not valid Python: {exc}") from exc
   ```

3. **Add a test** — `test_factory.py` should have a test that writes an entrypoint and verifies `compile(source, '<test>', 'exec')` succeeds.

---

## 22. When profile generation falls back, agent gets ALL 6 tools — violating principle of least privilege

**[LOG] Evidence:** Runs `e46fa5d4`, `096f8464`, `61e636c6` — all fallback agents got `tools_allowlist: ["terminal", "http_api", "web_fetch", "file_io", "python_exec", "sql_query"]`. A "context discoverer" agent should never need `terminal`, `python_exec`, or `sql_query`. In contrast, run `7e06cbf6`'s specialized "Instruction Reader" agent correctly got only `["file_io"]`.

**What the code actually does:**

`_normalize_profile_payload` (search.py ~L680):
```python
tools = [t for t in requested_tools if isinstance(t, str) and t in supported_tools]
if not tools:
    tools = [t for t in fallback_tools if t in supported_tools]
```
`fallback_tools` is always the full `SUPPORTED_TOOLS` list. So any profile with zero valid tool matches gets everything.

**The fix:**

1. **Role-based tool defaults** — define a mapping from common role tags to minimal tool sets:
   ```python
   _ROLE_DEFAULT_TOOLS = {
       "context-discoverer": ["file_io", "web_fetch"],
       "file-reader": ["file_io"],
       "code-writer": ["file_io", "python_exec"],
       "web-researcher": ["web_fetch", "http_api"],
       "data-analyst": ["file_io", "python_exec", "sql_query"],
   }
   ```

2. **Before falling back to ALL tools**, check if the role_tag fuzzy-matches a known role.

3. **Hard cap: max 3 tools in fallback mode.** If the LLM couldn't produce a profile, the agent shouldn't get more than 3 tools. Pick the 3 most commonly used: `["file_io", "terminal", "web_fetch"]`.

---

## 23. `httpx.Client` is created fresh for every single LLM call — no connection pooling

**What the code actually does:**

Every call in `search.py` uses:
```python
with httpx.Client(timeout=timeout_sec) as client:
    resp = client.post(...)
```

In a single run, `search.py` may be called 4-6 times: `generate_task_plan` (discovery), `generate_agent_profile` (per role), `generate_task_plan` (execution), `LLMReranker.rerank`, `synthesize_final_response`. Each creates a new TCP connection, performs TLS handshake, sends the request, and closes. For the Siemens API proxy, this is 4-6 TLS handshakes per run.

**The fix:**

Create a module-level or Settings-scoped `httpx.Client` instance with connection pooling:
```python
_CLIENT: httpx.Client | None = None
def _get_client(timeout_sec: int) -> httpx.Client:
    global _CLIENT
    if _CLIENT is None or _CLIENT.is_closed:
        _CLIENT = httpx.Client(timeout=timeout_sec, limits=httpx.Limits(max_connections=5))
    return _CLIENT
```

This reuses TCP connections across calls, reducing latency by ~200-500ms per call on typical corporate proxies. For 5 calls per run, that's 1-2.5s saved.

---

## 24. Tool results are truncated at the entrypoint level but the agent doesn't know it was truncated

**What the code actually does:**

The `_format_history` function in the entrypoint template (factory.py ~L283):
```python
if len(result_text) > 800:
    result_text = result_text[:800] + "… (truncated)"
```

And the overall history:
```python
if len(text) > max_chars:
    text = text[:max_chars] + "\n… (history truncated)"
```

The LLM sees `"… (truncated)"` in its history but has no idea **how much** was truncated or whether the truncated portion contains critical information. It might make decisions based on incomplete data without realizing it.

**The fix:**

1. **Include truncation metadata** in the history:
   ```python
   if len(result_text) > 800:
       result_text = f"{result_text[:800]}… [TRUNCATED: {len(result_text)} chars total, showing first 800]"
   ```

2. **Add a "recall" mechanism** — if the agent's LLM requests the full result for a specific step, the coordinator can re-send it. Add a special tool call:
   ```python
   {"tool": "_recall_step_result", "args": {"step": 3}}
   ```
   This returns the full, untruncated result for that step. This doesn't require a new tool in `TOOL_REGISTRY` — it can be handled directly in the entrypoint template's ReAct loop.

---

## 25. Agent subprocess stderr is silently discarded — Python tracebacks are invisible

**What the code actually does:**

`AgentRunner._run_once` (runner.py ~L55) creates the subprocess:
```python
proc = await asyncio.create_subprocess_exec(
    sys.executable, str(entrypoint), ...
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,  # captured but never read except on failure
)
```

If the subprocess writes to stderr (Python warnings, traceback from unhandled exceptions in helper functions, import errors), this output is only captured in the `AgentRuntimeError` message when the process exits with non-zero. During normal execution, stderr content is discarded.

**The fix:**

1. **Always capture and log stderr** after `proc.communicate()`:
   ```python
   _, stderr_data = await proc.communicate()
   if stderr_data:
       self._emit_runlog(state, "agent_stderr", {"agent_id": agent_id, "stderr": stderr_data.decode()[:2000]})
   ```

2. **Check stderr even on success** — if the agent returns `status: "ok"` but stderr contains `"Traceback"` or `"Warning"`, flag it in the runlog as `"agent_stderr_warning"`.

---

## 26. `_find_reusable_capability_agent` ignores success_rate — failed agents keep getting selected

**What the code actually does (nodes.py ~L903):**

```python
def _find_reusable_capability_agent(self, profile: dict) -> str | None:
    query_vector = self.embedder.embed(profile["description"])
    candidates = self.registry.semantic_search(query_vector, k=10)
    for cand in candidates:
        manifest = self.registry.get_manifest(cand.agent_id)
        if manifest is None or manifest.status != "active":
            continue
        tags = set(manifest.tags)
        if profile_id in tags:
            return manifest.agent_id
    return None
```

The selection checks only `status == "active"` and tag matching. `AgentManifest` has `success_rate` and `usage_count` fields, but they are **never used** in this selection path. An agent with `success_rate: 0.0` and `usage_count: 20` (failed 100% of the time across 20 runs) is just as likely to be reused as one with `success_rate: 1.0`.

Similarly, `update_registry_and_audit` (nodes.py ~L1162) records the run but **doesn't update the manifest's success_rate** in the SQLite registry.

**The fix:**

1. **Update success_rate after every run** in `update_registry_and_audit`:
   ```python
   for agent_id in role_agent_map.values():
       manifest = self.registry.get_manifest(agent_id)
       if manifest:
           # Increment usage, update running success rate
           new_count = manifest.usage_count + 1
           agent_succeeded = agent_id not in [item.get("assigned_agent_id") 
                                                for item in failed_subtasks]
           new_rate = ((manifest.success_rate * manifest.usage_count) + (1 if agent_succeeded else 0)) / new_count
           self.registry.update_agent_metrics(agent_id, usage_count=new_count, success_rate=new_rate)
   ```

2. **Filter by success_rate in `_find_reusable_capability_agent`:**
   ```python
   if manifest.success_rate < 0.2 and manifest.usage_count >= 5:
       continue  # Skip degraded agents
   ```

3. **Weight semantic search results by success_rate during reranking.**

---

## 27. No run-level cost or token tracking — impossible to measure efficiency

**What the code actually does:**

The `AgentResponse.metrics` includes `latency_ms`, `steps`, `ok`, `failed` — but no token counts, no API call costs. The `approval_callback` (nodes.py ~L1320) logs `estimated_cost_usd` from the intent but doesn't aggregate it. The JSONL runlog has individual events but no summary of total cost.

**The fix:**

1. **Track tokens in the entrypoint** — extract `usage.total_tokens` from LLM API responses and include in `AgentResponse.metrics`:
   ```python
   "metrics": {"latency_ms": ..., "steps": ..., "total_tokens": accumulated_tokens, "llm_calls": call_count}
   ```

2. **Aggregate in `update_registry_and_audit`** — sum up total tokens, total LLM calls, total tool calls across all agents in the run, and emit a `"run_cost_summary"` event.

3. **Add `cli run --budget-tokens N`** — abort the run if total tokens exceed the budget. This prevents runaway loops (like item 16) from burning through API quota.

---

## 28. No `--dry-run` mode — can't preview what the coordinator would do without executing

**What the code actually does:**

`cli run` immediately calls `build_graph()` and invokes the full graph. There's no way to see what the coordinator would plan (how many agents, what tools, what phases) without actually running everything — creating agent capsules, making LLM calls, and executing tools.

**The fix:**

Add `--dry-run` flag to `cmd_run` (cli.py):
```python
if args.dry_run:
    # Run only: ingest_task → decompose_task → (print plan) → exit
    graph = build_graph(settings, dry_run=True)
    # decompose_task generates the plan but doesn't execute
    # Print: discovery plan, estimated agent count, expected tool usage
```

Output something like:
```
Dry run for: "read instructions and create a py file"
Discovery plan: 1 subtask (role: file-reader, tools: [file_io])
Execution plan: TBD (depends on discovery results)
Estimated agents: 1-2
Estimated LLM calls: 4-6
```

---

## 29. No stale agent cleanup — `agents/` directory grows forever with capsules that are never reused

**What the code actually does:**

Every run that creates new agents adds directories under `agents/`. There is no garbage collection, no expiry, no cleanup mechanism. After 53 runs, there are 25+ agent capsules with `memory.db`, `entrypoint.py`, `skill.md`, and `manifest.json` each. Many of these are generic "Task Operator" agents that will never be selected again because `_find_reusable_capability_agent` matches by profile_id tag, and multiple agents share the same `"task-operator"` tag.

**The fix:**

1. **Add `cli gc` command** — removes agents with `usage_count == 0` older than N days (default: 7).

2. **Add `cli gc --aggressive`** — also removes agents with `success_rate < 0.2 AND usage_count > 5`.

3. **Add auto-gc in `cmd_run`** — before each run, if `len(agents/) > CLOSED_CLAW_MAX_AGENTS` (default: 50), run a soft cleanup removing the oldest unused agents.

4. **Report agent count** in `cli doctor`:
   ```
   Agents registered: 25 (12 active, 8 unused, 5 degraded)
   ```

---

## 30. Sandbox/working directory is implicit — no config control over where agents operate

**What the code actually does:**

Tool execution happens in whatever the current working directory is. `ToolExecutor._safe_path` (executor.py) resolves paths relative to `Settings.sandbox_dir`, but the sandbox itself defaults to `./sandbox` relative to the CWD at invocation time.

When the user runs `cli run` from their project root, agents operate in that project root's sandbox. But if they run from a different directory, agents see completely different files — leading to the discrepancy in run `61e636c6` where the agent found a leftover `instructions.txt` from a previous task.

**The fix:**

1. **Make `CLOSED_CLAW_SANDBOX_DIR` explicit** — require it in `.env` or set a sensible default relative to the `.closed_claw/` runtime directory (not CWD).

2. **Pass sandbox info in CoordinatorRequest** — so agents know exactly where they're operating:
   ```python
   context["sandbox_dir"] = str(settings.sandbox_dir.resolve())
   context["sandbox_contents"] = [f.name for f in settings.sandbox_dir.iterdir()][:20]
   ```

3. **Clear sandbox between runs** (opt-in) — `CLOSED_CLAW_CLEAR_SANDBOX_BETWEEN_RUNS=true` removes leftover files from previous runs to prevent context pollution.

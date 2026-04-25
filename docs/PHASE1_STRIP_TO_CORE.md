# Phase 1 — Strip to Core

## Context

Product review identified that Closed Claw is slower, harder to use, and higher friction than alternatives. The framework-to-value ratio is inverted: 7,000 lines of orchestration infrastructure for what amounts to multi-step LLM+tool execution. A simple task like "read a file" requires 6-7 LLM calls, takes 20-30s, and demands manual setup (34+ env vars, 3 setup commands, inaccessible default provider).

**Goal:** Transform the product from "impressive framework looking for users" to "tool that solves a problem in 5 seconds." Four concrete objectives:

1. **Zero-config first run** — `pip install closed-claw && claw "read main.py"` works
2. **Fast path** — Simple tasks complete in 1-2 LLM calls, not 7
3. **Human-readable output** — Clean text by default, JSON behind `--json`
4. **Simplified config** — 3 presets (`--safe`, `--balanced`, `--fast`) replace 34 env vars

## Shipping Phases

### Phase A: Zero-Config First Run

**A1. Create `pyproject.toml` for pip install**
- Create: `closed_claw/pyproject.toml`
- PEP 621 metadata, `[project.scripts] claw = "closed_claw.cli:main"`, dependencies from `requirements.txt`
- ~40 lines

**A2. Create `__main__.py`**
- Create: `closed_claw/closed_claw/__main__.py`
- `from closed_claw.cli import main; raise SystemExit(main())`
- 3 lines

**A3. Auto-detect LLM provider from environment**
- Modify: `closed_claw/config.py` — `Settings.from_env()` line 74
- Replace `provider = _getenv("CLOSED_CLAW_LLM_PROVIDER", "siemens", dotenv)` with `_auto_detect_provider(dotenv)`:
  1. If `CLOSED_CLAW_LLM_PROVIDER` is explicitly set → use it
  2. Else probe: `OPENAI_API_KEY` → "openai", `ANTHROPIC_API_KEY` → "claude", `GEMINI_API_KEY` → "gemini"
  3. Fallback: "openai" (most common, will fail with clear error in A4)
- ~20 lines changed

**A4. Graceful API key error with instructions**
- Modify: `closed_claw/cli.py` — `cmd_run()` line 69
- Wrap body in `try/except ValueError` that catches API key errors and prints:
  ```
  Error: No API key found.
  Set one of: OPENAI_API_KEY, ANTHROPIC_API_KEY, or GEMINI_API_KEY
  Example: export OPENAI_API_KEY=sk-...
  ```
- Keep `ValueError` in library functions (existing tests expect it), catch at CLI level only
- ~15 lines

**A5. Auto-init on first run**
- Modify: `closed_claw/cli.py` — `cmd_run()` line 71
- Add `settings.ensure_dirs()` before `_migrate_legacy_agents(settings)` (line 78)
- Currently `_migrate_legacy_agents` constructs `RegistryStore` which needs the DB directory to exist. `build_graph()` line 26 already calls `ensure_dirs()` but that's after `_migrate_legacy_agents` on line 78.
- 1 line added

**A6. Make `claw "task"` work without `run` subcommand**
- Modify: `closed_claw/cli.py` — `main()` at line 632
- If first arg isn't a known subcommand and doesn't start with `-`, treat entire argv as task → invoke `cmd_run` directly
- ~20 lines
- Depends on: A4

### Phase B: Fast Path for Simple Tasks

Current simple task = 7+ LLM calls: `generate_task_plan(discovery)` → `generate_agent_profile` → agent loop → `generate_task_plan(execution)` → `generate_agent_profile` → agent loop → `synthesize_final_response`

Target: 1 LLM call (agent loop only)

**B1. Add simple task classifier**
- Create: `closed_claw/coordinator/classifier.py`
- `is_simple_task(task: str) -> bool` — heuristic, no LLM call
  - True for single-action verbs ("read", "list", "count", "find", "check", "explain", "summarize")
  - False for multi-step indicators ("then", "first...then", "step 1...step 2", "and create...and modify")
  - False for long tasks (>100 words)
- ~40 lines

**B2. Add builtin capability profiles**
- Modify: `closed_claw/registry/search.py` — add `_BUILTIN_PROFILES` dict near line 200
- Pre-built profiles for common roles: `task-operator`, `file-reader`, `code-writer`, `web-researcher`, `db-analyst`
- Add `get_builtin_profile(role_tag: str) -> dict | None` lookup function
- These profiles skip the `generate_agent_profile()` LLM call entirely
- ~60 lines

**B3. Add fast-path execution node**
- Modify: `closed_claw/coordinator/nodes.py` — add `execute_single_task()` method to `CoordinatorNodes`
  - Uses `get_builtin_profile("task-operator")` from B2 (no `generate_agent_profile` call)
  - Creates single subtask from user's task directly (no `generate_task_plan` call)
  - Runs agent (1 ReAct loop = 1 LLM call)
  - Sets `response_result` directly from agent response (no `synthesize_final_response` call)
  - Eliminates 6 of 7 LLM calls
- Modify: `closed_claw/coordinator/graph.py` — add `build_fast_graph(settings)`:
  - Graph: `ingest_task → execute_single_task → update_registry_and_audit → END`
- ~80 lines in nodes.py, ~30 lines in graph.py
- Depends on: B1, B2

**B4. Wire fast path into CLI**
- Modify: `closed_claw/cli.py` — `cmd_run()` line 89
  ```python
  if is_simple_task(args.task):
      app_graph = build_fast_graph(settings)
  else:
      app_graph = build_graph(settings)
  ```
- Add `--no-fast` flag to `run` subparser as escape hatch
- ~10 lines
- Depends on: B3

### Phase C: Human-Readable Output

**C1. Add `--json` and `--verbose` flags to `run` command**
- Modify: `closed_claw/cli.py` — `build_parser()` line 577
  ```python
  p_run.add_argument("--json", action="store_true")
  p_run.add_argument("--verbose", "-v", action="store_true")
  ```
- ~5 lines

**C2. Restructure `cmd_run` output**
- Modify: `closed_claw/cli.py` — `cmd_run()` lines 163-249
- **Default mode** (no flags): Clean summary using Rich:
  ```
  Done (3.2s)

  Status: completed
  Result: [the actual answer text]

  Agents: 1 used (task-operator-a3f2)
  Tools:  3 calls (file_io: 2, terminal: 1)
  ```
- **`--json` mode**: Current JSON dump (unchanged, backward compatible)
- **`--verbose` mode**: Current detailed operational output (agent creation, tool calls, task pool)
- ~80 lines rewritten

**C3. Add Rich progress display during execution**
- Modify: `closed_claw/cli.py` — `monitor()` coroutine lines 110-152
- Replace plain print with `rich.live.Live` + spinner:
  - Show current phase: "Planning...", "Executing...", "Synthesizing..."
  - Brief inline tool call status
  - Only in default mode (not `--json`/`--verbose`)
- ~60 lines
- Depends on: C1, C2

### Phase D: Simplified Configuration

**D1. Add `--safe`, `--balanced`, `--fast` presets**
- Modify: `closed_claw/config.py` — add `Settings.with_preset(preset: str) -> Settings`
  - `--safe`: `create_approval_mode="interactive"`, `api_approval_mode="interactive"`, force full pipeline
  - `--balanced` (default): `create_approval_mode="approve"`, `api_approval_mode="approve"`, fast path enabled
  - `--fast`: same as balanced + reduced retries (0), reduced timeout
- Modify: `closed_claw/cli.py` — add mutually exclusive group `--safe`/`--balanced`/`--fast`
- ~30 lines total
- Depends on: B4

**D2. Hide advanced flags from `--help`**
- Modify: `closed_claw/cli.py` — lines 570-571
- Add `help=argparse.SUPPRESS` to `--create-approval-mode` and `--api-approval-mode`
- Flags still work, just hidden from default help (presets are the user-facing API)
- 2 lines

## Dependency Graph

```
A1, A2, A3, A5 ─── independent, ship together
A4 ─── depends on A3
A6 ─── depends on A4

B1, B2 ─── independent, ship together
B3 ─── depends on B1 + B2
B4 ─── depends on B3

C1 ─── independent
C2 ─── depends on C1
C3 ─── depends on C2

D1 ─── depends on B4
D2 ─── depends on D1
```

## Critical Files

| File | Changes |
|------|---------|
| `closed_claw/cli.py` | A4, A5, A6, B4, C1, C2, C3, D1, D2 |
| `closed_claw/config.py` | A3, D1 |
| `closed_claw/coordinator/nodes.py` | B3 |
| `closed_claw/coordinator/graph.py` | B3 |
| `closed_claw/coordinator/classifier.py` | B1 (new file) |
| `closed_claw/registry/search.py` | B2 |
| `closed_claw/__main__.py` | A2 (new file) |
| `pyproject.toml` | A1 (new file) |

## Test Strategy

All 113 existing tests must pass after each work item. New tests per item:

- **A3**: 3 unit tests — auto-detect from OPENAI_API_KEY, ANTHROPIC_API_KEY, fallback to openai
- **A4**: 1 CLI test — verify clean error message on missing API key
- **A6**: 1 test — `claw "task"` routes to cmd_run correctly
- **B1**: ~15 unit tests — classifier accuracy on simple vs complex tasks
- **B2**: 2 unit tests — builtin profile lookup, unknown role returns None
- **B3**: 3 tests — fast graph builds, executes single subtask, no planning LLM calls
- **B4**: 2 tests — simple task routes to fast graph, `--no-fast` forces full graph
- **C1-C2**: 3 tests — default output has no JSON, `--json` has valid JSON, `--verbose` has detail
- **D1**: 3 tests — each preset applies correct settings

## Verification

```bash
# After each phase:
conda run -n closed_claw python -m pytest -q

# End-to-end smoke test (after all phases):
conda run -n closed_claw pip install -e .
export OPENAI_API_KEY=sk-test
claw "list files in current directory"     # should use fast path
claw run "analyze project structure" --safe # should use full pipeline
claw "read main.py" --json                 # should output JSON
```

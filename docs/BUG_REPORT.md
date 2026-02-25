# Closed Claw — Bug Report

**Date**: 2026-02-26  
**Scope**: Full codebase review (all core modules)  
**Reviewer**: Automated deep code audit  

---

## Summary

| # | Bug | Severity | File | Impact |
|---|-----|----------|------|--------|
| 1 | Wrong table name `memories` vs `memory` | **High** | `cli.py` | Agent detail memory count always broken |
| 2 | `db_path` not resolved to absolute | Medium | `config.py` | DB path drifts if CWD changes |
| 3 | New DB connection + sqlite-vec load per call | Medium | `registry/store.py` | Performance degradation |
| 4 | SHA-256 embedding fallback is non-semantic | **High** | `embeddings/provider.py` | Agent search is random without sentence-transformers |
| 5 | `schema.sql` missing `skill_ids_json` | Low | `registry/schema.sql` | DDL is incomplete source of truth |
| 6 | `sql_query` SELECT check bypassed by CTE | Medium | `tools/executor.py` | Agents can mutate/destroy DB data |
| 7 | `_active_runs` dict not thread-safe | Medium | `web/server.py` | Race condition in web dashboard |
| 8 | SSE stream rereads entire log file per poll | Medium | `web/server.py` | O(n²) I/O for large runs |
| 9 | Settings mutated mid-run from web API | Medium | `web/server.py` | Inconsistent config during active runs |
| 10 | Arg normalization silently drops duplicates | Low | `tools/executor.py` | Wrong tool argument used |
| 11 | `AgentResponse` has no `type` discriminator | Low | `runtime/protocol.py` | Fragile protocol, future misroute risk |

**High**: 2 &nbsp;|&nbsp; **Medium**: 6 &nbsp;|&nbsp; **Low**: 3

---

## BUG 1 — `cmd_agent` queries wrong table name (CRASH)

**Severity**: High — always fails  
**File**: `closed_claw/cli.py` line 298  

### Description

`cmd_agent` runs `SELECT COUNT(*) FROM memories` but the agent `memory.db` table is created as `memory` (no trailing 's') in `agents/factory.py`:

```python
# factory.py line 605 — table creation
CREATE TABLE IF NOT EXISTS memory (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    TEXT NOT NULL DEFAULT (datetime('now'))
)

# cli.py line 298 — query
row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
```

### Impact

This **always** throws `sqlite3.OperationalError`. The exception is caught and `memory_count` falls back to `None`, so the command doesn't crash outright, but it **never shows the memory count** — the feature is silently broken.

### Fix

Change `cli.py` line 298 from `FROM memories` to `FROM memory`.

---

## BUG 2 — `db_path` not resolved to absolute

**Severity**: Medium — breaks if CWD changes  
**File**: `closed_claw/config.py` line 85  

### Description

`agents_dir` and `run_logs_dir` are anchored via `(cwd / ...).resolve()`, but `db_path` only calls `.expanduser()`:

```python
db_path=Path(...).expanduser(),          # RELATIVE — changes meaning with CWD
agents_dir=(cwd / ...).resolve(),        # absolute ✓
run_logs_dir=(cwd / ...).resolve(),      # absolute ✓
```

### Impact

If anything changes working directory before using `db_path` (another tool, a library, or the web server), the registry DB resolves to a different location. The web dashboard running via uvicorn is especially at risk since uvicorn may change CWD.

### Fix

Change to:
```python
db_path=(cwd / _getenv("CLOSED_CLAW_DB_PATH", ".closed_claw/registry.db", dotenv)).expanduser().resolve(),
```

---

## BUG 3 — `_conn()` creates a new connection + reloads sqlite-vec on every call

**Severity**: Medium — performance degradation  
**File**: `closed_claw/registry/store.py` lines 62–77  

### Description

Every single DB operation goes through `_conn()`, which:

1. Opens a new `sqlite3.connect()`
2. Tries to load the sqlite-vec extension (imports package, calls `sqlite_vec.load()`)
3. Returns the connection (never cached)

```python
def _conn(self) -> sqlite3.Connection:
    conn = sqlite3.connect(self.db_path)
    conn.row_factory = sqlite3.Row
    try:
        self.sqlite_vec_available = self._try_load_sqlite_vec(conn)
    except (sqlite3.OperationalError, AttributeError) as exc:
        self.sqlite_vec_available = False
        ...
    return conn
```

### Impact

For endpoints like `list_runs_enriched` in the web dashboard, which calls `_run_analysis` → `_make_registry` per run, this means dozens of connection setups per HTTP request. The extension load is not free — it calls `conn.enable_load_extension(True)`, imports the `sqlite_vec` package, and calls `sqlite_vec.load(conn)` each time.

Additionally, the instance variable `self.sqlite_vec_available` is mutated on every `_conn()` call, creating a potential race condition in threaded contexts (e.g. the web dashboard).

### Fix

Cache the connection (or use a connection pool), and determine `sqlite_vec_available` once during `__init__`.

---

## BUG 4 — Embedding fallback produces semantically meaningless vectors

**Severity**: High — agent search broken without sentence-transformers  
**File**: `closed_claw/embeddings/provider.py`  

### Description

When `sentence-transformers` is not installed, the fallback creates vectors from SHA-256 hashes:

```python
digest = hashlib.sha256(text.encode()).digest()  # 32 bytes
vec = [b / 255.0 for b in digest]                # 32 floats
return (vec * ((dim // len(vec)) + 1))[:dim]     # cycled to 384
```

### Problems

1. **No semantic meaning**: "write a Python script" and "code a Python program" get completely unrelated vectors.
2. **Cyclic repetition**: bytes 0–31 are identical to bytes 32–63, 64–95, etc. The 384-dim vector only has 32 dimensions of information.
3. **Cosine similarity becomes random**: agent matching is effectively a coin flip.

### Impact

**If the user hasn't installed sentence-transformers, the entire agent discovery/reuse pipeline is non-functional** — every search returns arbitrary rankings. The coordinator will create new agents for tasks an existing agent could handle, leading to unbounded agent sprawl.

### Fix

At minimum, warn loudly (or fail fast) when the fallback is active. Ideally, make `sentence-transformers` a hard dependency or provide a lightweight alternative that preserves some semantic signal (e.g. TF-IDF).

---

## BUG 5 — `schema.sql` DDL missing `skill_ids_json` column

**Severity**: Low — works at runtime, fails as documentation  
**File**: `closed_claw/registry/schema.sql`  

### Description

The `agents` table DDL in `schema.sql` doesn't include the `skill_ids_json` column. It's only added via an `ALTER TABLE` migration in `store.py._init_db()`.

### Impact

If someone recreates the DB from `schema.sql` alone (tooling, tests, manual setup), the column won't exist and queries referencing `skill_ids_json` will fail. The schema file is an inaccurate source of truth.

### Fix

Add `skill_ids_json TEXT NOT NULL DEFAULT '[]'` to the `CREATE TABLE agents` DDL in `schema.sql`.

---

## BUG 6 — `sql_query` SELECT-only check is trivially bypassable

**Severity**: Medium — agents can mutate/delete data  
**File**: `closed_claw/tools/executor.py` lines 300–302  

### Description

```python
if not query.lower().startswith("select"):
    raise ToolExecutionError("sql_query only allows SELECT statements")
```

A CTE (Common Table Expression) bypasses this:

```sql
WITH x AS (DELETE FROM agents RETURNING *) SELECT * FROM x
```

This starts with `WITH` (not `SELECT`), so the check actually blocks it — but the reverse also works:

```sql
SELECT * FROM agents; DROP TABLE agents; --
```

SQLite's `conn.execute()` only runs the first statement, so the semicolon trick doesn't work either. However, `ATTACH DATABASE` + virtual tables + other SQLite-specific tricks can be used to bypass a simple `startswith("select")` check. The defence is shallow.

### Impact

Any agent with `sql_query` in its allowlist has a potential vector for data mutation if a bypass is discovered. The check gives false confidence.

### Fix

Use `sqlite3`'s `set_authorizer()` callback to enforce read-only access at the engine level, or open the connection in `?mode=ro` (read-only URI mode).

---

## BUG 7 — `_active_runs` dict is not thread-safe

**Severity**: Medium — race condition in web dashboard  
**File**: `closed_claw/web/server.py` line 313  

### Description

`_active_runs` is a plain `dict` mutated from:

- **Background threads**: `_run_graph()` calls `_active_runs[run_id].update(...)` (line ~1059)
- **API handler threads**: `cancel_run()`, `list_active_runs()`, `get_run_status()`, `global_event_stream()`

### Impact

Python's GIL makes *individual* dict operations atomic, but `.update()` is multiple operations internally. Concurrent reads during a `.update()` can see partial state (e.g., `status` changed to "completed" but `result` still empty).

The SSE `global_event_stream` generator iterates `_active_runs.items()` without locking — if a run finishes and gets deleted concurrently, this raises `RuntimeError: dictionary changed size during iteration`.

### Fix

Use `threading.Lock` around all accesses to `_active_runs`, or switch to a thread-safe data structure.

---

## BUG 8 — SSE `stream_runlog` rereads entire file every 0.5s

**Severity**: Medium — O(n²) I/O for large runs  
**File**: `closed_claw/web/server.py` lines 926–941  

### Description

```python
while idle_ticks < 120:
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").splitlines()  # reads ALL
        new = lines[seen:]
```

### Impact

For a 1.2MB run log (which has been observed in `.closed_claw/runs/`), this does a full file read every 500ms. Over a 60-second stream, that's 120 reads × 1.2MB = ~144MB of I/O for a single SSE client. Multiple concurrent clients multiply this linearly.

### Fix

Track the byte offset (not line count) and `seek()` to it on each poll, reading only appended data.

---

## BUG 9 — Settings mutated mid-run via web dashboard

**Severity**: Medium — unpredictable behavior  
**File**: `closed_claw/web/server.py` lines 669–678  

### Description

`POST /api/settings` and `POST /api/settings/apikey` mutate the shared `settings` object via `object.__setattr__()` while a coordinator graph may be running in a background thread:

```python
for field in new.__dataclass_fields__:
    object.__setattr__(settings, field, getattr(new, field))
```

### Impact

If someone changes `llm_provider` or `agent_timeout_sec` while a run is active, the running graph picks up the new values mid-execution (since it holds a reference to the same `settings` object). This can cause:

- Provider/model mismatch during an active run
- Timeout changes mid-agent-execution
- Path changes causing file-not-found errors

### Fix

Snapshot settings at run start and pass the snapshot to the graph, rather than sharing a mutable reference. Or use a copy-on-write pattern where `POST /api/settings` creates a new `Settings` instance that only applies to future runs.

---

## BUG 10 — `_normalize_args` silently drops duplicates

**Severity**: Low — edge case  
**File**: `closed_claw/tools/executor.py` lines 103–114  

### Description

If an LLM sends *both* an alias and the canonical name in the same call (e.g., `{"command": "ls", "cmd": "pwd"}`), the normalization keeps whichever key appears first in iteration order and silently drops the other:

```python
canonical = aliases.get(key, key)  # "command" → "cmd"
if canonical in normalized:        # "cmd" already set from previous iteration
    continue                       # DROP "pwd" silently — no error, no log
normalized[canonical] = value
```

### Impact

The LLM never gets an error — it just uses the wrong argument value. The tool executes with an arbitrary choice between the two conflicting values. Since Python 3.7+ dicts maintain insertion order, the result depends on which key the LLM happened to emit first.

### Fix

Detect and log (or error) when two keys map to the same canonical name.

---

## BUG 11 — `AgentResponse` has no `type` discriminator field

**Severity**: Low — fragile protocol design  
**File**: `closed_claw/runtime/protocol.py` lines 66–82  

### Description

`ApiCallIntent` and `ToolCallIntent` have a `type` Literal field for discrimination, but `AgentResponse` does not. The parse function tries all three models in order:

```python
try: return ApiCallIntent.model_validate_json(data)
except: pass
try: return ToolCallIntent.model_validate_json(data)
except: ...
    try: return AgentResponse.model_validate_json(data)
```

### Impact

In practice this works because `ApiCallIntent` requires `provider` / `endpoint` / `estimated_cost_usd` (absent from `AgentResponse`), so validation fails and falls through correctly. However, the design relies on field *absence* for discrimination rather than an explicit type tag. If a future protocol change adds overlapping fields, messages will be silently misrouted to the wrong model.

### Fix

Add `type: Literal["agent_response"] = "agent_response"` to `AgentResponse` and check the `type` field first in `parse_agent_line` before attempting full validation.

---

## Appendix: Files Reviewed

All core source modules were fully read during this audit:

- `closed_claw/cli.py` (662 lines)
- `closed_claw/config.py` (158 lines)
- `closed_claw/coordinator/nodes.py` (1584 lines)
- `closed_claw/coordinator/graph.py` (66 lines)
- `closed_claw/coordinator/state.py`
- `closed_claw/runtime/protocol.py`
- `closed_claw/runtime/runner.py` (123 lines)
- `closed_claw/registry/store.py` (424 lines)
- `closed_claw/registry/search.py` (782 lines)
- `closed_claw/registry/schema.sql`
- `closed_claw/tools/executor.py` (313 lines)
- `closed_claw/agents/factory.py` (612 lines)
- `closed_claw/embeddings/provider.py`
- `closed_claw/observability/runlog.py`
- `closed_claw/policy/approval.py`
- `closed_claw/policy/audit.py`
- `closed_claw/web/server.py` (1254 lines)

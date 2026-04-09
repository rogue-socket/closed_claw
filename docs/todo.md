# Closed Claw — Engineering TODO

> Generated from full codebase audit (34 findings). Ordered by priority.
> Items marked ✅ were completed in prior sessions.

---

## ✅ DONE

- [x] Add a soul.md personality layer to the coordinator node
- [x] Add enriched tool descriptions for LLM-based tool selection
- [x] Implement hard guardrails (SSRF blocklist, output truncation, code size limit, command denylist)
- [x] Fix `shell=True` command injection in `_terminal`
- [x] Split circuit breaker from policy denial
- [x] Fix missing `agents/` package
- [x] Fix duplicate tool call dedup in `AgentRunner`
- [x] **S-1** SSRF bypass — replaced string-prefix check with `socket.getaddrinfo` + `ipaddress.is_private` resolution
- [x] **S-2** sql_query hardened — added denylist for `;`, `ATTACH`, `DETACH`, `load_extension`, `PRAGMA`
- [x] **S-5** Symlink traversal fixed — `_safe_path` now checks both logical and resolved paths
- [x] **S-7** `enable_load_extension(False)` called after sqlite-vec loading in store.py and server.py
- [x] **S-8** `_extract_json` greedy regex replaced with `json.JSONDecoder.raw_decode` iteration
- [x] **P-1** Bare `except: pass` replaced with `logger.exception(...)` in nodes.py, search.py, server.py
- [x] **P-2** (partial) Added `logging.getLogger` to nodes.py, search.py, server.py, audit.py, runlog.py
- [x] **P-4** AuditStore now caches DB connection like RegistryStore
- [x] **P-5** `RunLogger.emit` made thread-safe with `threading.Lock`
- [x] **D-1** `CoordinatorState` expanded to full schema with docstring (serves as state documentation)
- [x] **R-4** Created `tests/conftest.py` with shared `make_test_settings` factory

---

## P0 — CRITICAL: Fix before any deployment

- [x] ~~**S-1  SSRF bypass via IP encoding**~~ ✅ Fixed
- [ ] **S-3  API keys leaked to agent subprocesses** — `_llm_runtime_config()` sends raw API key over stdin. Any buggy/malicious entrypoint can exfiltrate it. **Fix:** coordinator proxies LLM calls; never pass keys to capsules. *(nodes.py, runner.py, protocol.py)*
- [ ] **S-4  `python_exec` is unsandboxed** — Runs arbitrary Python with host-process privileges (network, filesystem, env vars). **Fix:** use `resource.setrlimit` (Linux) or restricted subprocess with network disabled. *(executor.py)*

## P1 — HIGH: Fix before production use

- [x] ~~**S-2  `sql_query` injection surface**~~ ✅ Fixed
- [x] ~~**S-7  `enable_load_extension(True)` left open**~~ ✅ Fixed
- [x] ~~**P-1  Bare `except Exception: pass` in critical paths**~~ ✅ Fixed
- [x] ~~**R-1  Provider key/base resolution duplicated 5× **~~ ✅ Extracted `closed_claw/llm_client.py` with `provider_key_and_base()` + `generate_text()`
- [x] ~~**R-2  LLM HTTP call logic duplicated per-provider**~~ ✅ All callers funneled through `generate_text()`: LLMReranker, generate_agent_description, setup_wizard verify functions
- [ ] **A-1  `nodes.py` is an 1800-line god module** — 15+ responsibilities. **Fix:** extract `AgentAcquisition`, `SubtaskExecutor`, `PromptComposer`, `ResultSynthesizer`. *(nodes.py)*
- [ ] **T-1  Zero tests for `web/server.py`** — ~900 lines, ~27 endpoints, untested. **Fix:** `TestClient` tests. *(tests/)*
- [x] ~~**P-2  No Python logging in most modules**~~ ✅ Partially fixed (nodes, search, server, audit, runlog)

## P2 — MEDIUM: Engineering quality

- [x] ~~**S-5  Symlink traversal in `_safe_path`**~~ ✅ Fixed
- [ ] **S-6  Web dashboard has zero authentication** — All endpoints including `/api/settings/apikey` and `/api/agents/delete-bulk`. **Fix:** bearer token or HTTP basic auth. *(server.py)*
- [x] ~~**S-8  Greedy regex in `_extract_json`**~~ ✅ Fixed
- [x] ~~**P-4  `AuditStore._conn()` creates new DB connection per call**~~ ✅ Fixed
- [x] ~~**P-5  `RunLogger.emit` not atomic**~~ ✅ Fixed
- [x] ~~**P-6  `compat.py` fallback doesn't handle `default_factory`**~~ ✅ Fixed — added `_resolve_class_default()` that detects `dataclasses.Field` descriptors
- [x] ~~**A-2  Private function imports across modules**~~ ✅ Public `closed_claw.llm_client` module; nodes.py imports from it directly
- [x] ~~**A-3  Graph skips most defined nodes**~~ ✅ Resolved — dead methods removed, all 6 public async methods now wired
- [x] ~~**A-4  `RegistryStore._cached_conn` not thread-safe**~~ ✅ Fixed — replaced with `threading.local()` per-thread connection caching
- [x] ~~**R-3  `_sync_registry_index` duplicated**~~ ✅ Fixed — centralized as `AgentFactory.sync_registry_index()`
- [x] ~~**D-1  `CoordinatorState` TypedDict unused**~~ ✅ Updated to full schema with docstring
- [x] ~~**D-2  `dispatch_agents_async` dead code**~~ ✅ Removed
- [x] ~~**D-3  `approval_gate_for_api_calls` + `continue_or_deny_api_path` unused**~~ ✅ Removed
- [x] ~~**D-4  `failure_recovery` node unused**~~ ✅ Removed
- [ ] **T-2  Zero tests for `interactive.py`** — Mock `input()` and test navigation. *(tests/)*
- [ ] **T-3  `cli.py` barely tested** — Only `cmd_delete_all_agents` covered. *(tests/)*
- [x] ~~**T-4  No SSRF edge-case tests**~~ ✅ Added 8 edge-case tests (172.16, IPv6 ::1, data: scheme, credentials, DNS rebind, multi-IP)
- [x] ~~**T-5  No `file_io` path-traversal tests**~~ ✅ Added 5 path-traversal tests (absolute outside, dotdot-in-middle, subdirectory, tilde expansion, dot-current)

## P3 — LOW: Nice to have

- [x] ~~**D-5  10+ dead node methods**~~ ✅ Removed `embed_task`, `semantic_search`, `llm_rerank`, `human_gate_if_low_confidence`, `decide_reuse_or_create`, `create_agent_if_needed`
- [ ] **D-6  `docs/temp/brainstorm.md` shouldn't be in main tree** *(docs/temp/)*
- [x] ~~**P-3  lowercase `callable` as type hint**~~ ✅ Fixed — replaced with `Callable[..., object]` from `collections.abc`
- [x] ~~**R-4  `_settings()` test helper duplicated in 5+ test files**~~ ✅ Created `tests/conftest.py`
- [ ] **T-6  No tests for `EmbeddingProvider` SHA-256 fallback** *(tests/)*
- [ ] **T-7  No test for `RunLogger` concurrent write safety** *(tests/)*

---

## FEATURE BACKLOG

- [ ] Give the supervisor node a way to create subagents and subflows
- [ ] Deploy on laptop and get agents to work end-to-end
- [ ] Extend functionality to use external agents and other APIs
- [ ] Extend functionality with launching servers
- [ ] Add playwright capabilities
- [ ] Add a heartbeat / cron jobs
- [ ] Create a way to add tools and customise them
- [ ] Replace in-run supervisor task-pool loop with a proper background job system (durable task pool + persistent role-tag workers, lease/heartbeat/retry, dependency resolver, CLI `runs watch` live checklist that survives CLI exit)
- [ ] Add way for the agents to communicate + launch queues/task pools for other agents to pick up

# Closed Claw: Product Completeness + QoL Direction Report

Date: 2026-02-24

## Assumptions

- This is a **CLI-first product** (no separate frontend in this repository).
- The target from docs is a **local orchestrator with optional external model providers**, not a multi-tenant SaaS service.
- Recommendations prioritize **product completeness and developer/operator quality-of-life** over infra scale.
- Where code and docs conflict, this report defaults to docs intent and explicitly calls out doc-change candidates.

---

## A) What this product is (from docs)

Closed Claw is a local multi-agent coordinator that turns user tasks into routed execution via reusable “agent capsules.” A coordinator graph embeds and reranks task-to-agent fit, decides create-vs-reuse, runs an agent through a line-oriented JSON runtime protocol, applies approval policy for paid API actions, executes allowlisted tools, and records run/audit telemetry.

The product’s intended value is: fast local orchestration with policy controls, safe tool execution, and reusable capability capsules. The docs suggest a progression from local MVP toward richer supervisor/subflow behaviors, external integrations, and stronger acceptance criteria.

### Core personas + jobs-to-be-done

1. **Power user / operator**
   - Wants to run tasks quickly with confidence that risky calls are approval-gated.
2. **Agent author / workflow builder**
   - Wants reusable specialist agents/capsules and composable execution patterns.
3. **Maintainer / team engineer**
   - Wants observability, reproducibility, and predictable local setup/debug loops.

### Top 5 user journeys (step-by-step)

#### Journey 1: First-time setup and health check
1. Install dependencies and initialize environment.
2. Run setup wizard and verify provider credentials.
3. Run init and doctor checks.
4. Confirm db, vector extension, and provider readiness.

#### Journey 2: Run a task and route to best agent
1. User runs a task via CLI/menu.
2. Coordinator embeds task and retrieves candidates.
3. Coordinator reranks candidates and checks confidence.
4. Coordinator reuses or creates an agent.
5. Agent runs and returns final response.

#### Journey 3: Human-in-the-loop safety flow
1. Agent requests external paid API call.
2. Coordinator evaluates policy and circuit state.
3. Human (or policy mode) approves/denies.
4. Decision is returned to agent and audited.

#### Journey 4: Tool execution flow
1. Agent emits tool intent (terminal/http/file/sql/python).
2. Coordinator validates tool allowlist.
3. Tool executes centrally and returns structured result.
4. Coordinator audits tool event and continues run.

#### Journey 5: Inspect and manage artifacts
1. User lists agents/runs/audit events.
2. User inspects detailed agent manifest/skills/memory count.
3. User tails run log for event-by-event diagnosis.
4. User deletes one or all agents when needed.

### Non-goals / out-of-scope (current docs)

- Production multi-tenant service stack (auth, service API, deployment platform).
- Provider-specific production-grade integrations (currently partial / fallback-heavy).
- Rich UI app beyond CLI + interactive menu.

### Key entities and lifecycle/state models

#### Entities
- **Run**: one task execution instance (`run_id`, status, result, artifacts, approvals, tool events).
- **Agent capsule**: persisted specialist (`manifest.json`, `skill.md`, `memory.db`, `entrypoint.py`, logs).
- **Manifest**: identity, embeddings, tags, tool allowlist, usage metrics.
- **Approval decision**: allow/deny records for paid API calls/create decisions.
- **Tool event**: execution of allowlisted coordinator tools.

#### Run lifecycle (intended)
`ingest -> embed -> search -> rerank -> (create/reuse gate) -> dispatch -> validate -> (policy/failure handling) -> persist/audit -> synthesize`

#### Agent lifecycle (intended)
`candidate selection -> reused OR created -> run attempts/retries -> metrics update -> remains active/deleted`

### Definition of done for v1 (feature completeness checklist)

- [ ] End-to-end run works from clean checkout with no missing modules.
- [ ] All documented graph nodes are behaviorally implemented (not pass-through placeholders).
- [ ] Capsule create/reuse is deterministic, inspectable, and fully tested.
- [ ] Approval policy semantics are explicit (separate policy deny vs provider failure).
- [ ] Tool execution has clear safety envelopes and user-facing explanations.
- [ ] Run/audit/agent inspection yields actionable, correlated diagnostics.
- [ ] Setup/doctor/quickstart are cross-platform accurate and tested.
- [ ] Golden-path and edge-path tests cover failures, timeouts, malformed protocol, and denied actions.

---

## B) Gap analysis: docs vs current project

| Capability / Workflow | Doc reference | Current status | Evidence (files/modules) | What’s missing | Recommended approach |
|---|---|---|---|---|---|
| Capsule factory and index management | [docs/ARCHITECTURE.md](./ARCHITECTURE.md), [README.md](../README.md) | Partial | Imports in [closed_claw/cli.py](../closed_claw/cli.py#L13), [closed_claw/coordinator/graph.py](../closed_claw/coordinator/graph.py#L6), [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py#L7); package absent | `closed_claw/agents/factory.py` missing in tree | Restore/add agents package with explicit APIs + tests + import smoke checks |
| Coordinator full flow nodes | [docs/ARCHITECTURE.md](./ARCHITECTURE.md#coordinator-flow) | Partial | Node declarations in [closed_claw/coordinator/graph.py](../closed_claw/coordinator/graph.py#L42-L55) | `approval_gate_for_api_calls`, `continue_or_deny_api_path`, `failure_recovery` are pass-through in [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py#L434-L482) | Implement explicit state transitions and branch-specific outputs |
| Runtime JSON protocol | [docs/ARCHITECTURE.md](./ARCHITECTURE.md#runtime-protocol) | Y | Contracts in [closed_claw/runtime/protocol.py](../closed_claw/runtime/protocol.py#L8-L59), subprocess loop in [closed_claw/runtime/runner.py](../closed_claw/runtime/runner.py#L25-L92) | Better error taxonomy, protocol versioning, schema evolution strategy | Add protocol discriminator/version and stricter parse/validation paths |
| Approval gates (create + paid API) | [docs/ARCHITECTURE.md](./ARCHITECTURE.md#approval-gates), [README.md](../README.md) | Partial | Create gate in [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py#L125-L153); API decisions via callback [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py#L484-L536) | Graph-level policy nodes are no-op; denial and provider-failure semantics blended | Separate policy decisions from health breaker logic; implement node behavior |
| Circuit breaker behavior | [docs/ARCHITECTURE.md](./ARCHITECTURE.md#circuit-breaker) | Partial | Store methods in [closed_claw/registry/store.py](../closed_claw/registry/store.py#L293-L337) | User denials increment breaker failures (behavior mismatch for “provider health”) | Track technical failures separately from policy denies |
| Tool execution layer | [docs/ARCHITECTURE.md](./ARCHITECTURE.md#tooling-layer), [README.md](../README.md#tooling-model) | Partial | Implemented in [closed_claw/tools/executor.py](../closed_claw/tools/executor.py#L16-L158), callback in [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py#L538-L592) | Terminal uses `shell=True`; no per-tool policy classes/approval levels | Replace with structured command model + risk tiers |
| Interactive UX layer | [docs/ARCHITECTURE.md](./ARCHITECTURE.md#interactive-ux-layer), [README.md](../README.md) | Y | Menu workflow in [closed_claw/interactive.py](../closed_claw/interactive.py#L7-L222), handlers in [closed_claw/cli.py](../closed_claw/cli.py#L455-L489) | UX lacks stateful guidance, presets, and recoverable run history shortcuts | Add guided “run profiles,” recent runs, one-key inspect/retry |
| Setup wizard provider verification | [README.md](../README.md#interactive-setup-wizard) | Y | Wizard + verify in [closed_claw/setup_wizard.py](../closed_claw/setup_wizard.py#L14-L172) | Secrets persisted plaintext `.env`; no profile handling | Move secrets to keyring/secret provider and separate config profiles |
| Data persistence and search | [docs/ARCHITECTURE.md](./ARCHITECTURE.md#registry-and-persistence) | Y | Schema [closed_claw/registry/schema.sql](../closed_claw/registry/schema.sql), store methods [closed_claw/registry/store.py](../closed_claw/registry/store.py#L43-L379) | No migration framework/versioning, minimal relational constraints | Introduce migration tooling + schema versioning + constraint hardening |
| Docs quickstart cross-platform reliability | [docs/QUICKSTART.md](./QUICKSTART.md), [README.md](../README.md#requirements) | Partial | Shell commands are mostly POSIX examples | Windows path/shell differences not fully represented | Add OS-specific quickstart blocks + CI docs validation |
| Supervisor/subflow evolution | [docs/todo.md](./todo.md#L1-L6) | N | No supervisor/subflow module in code | Missing orchestrator abstraction for nested runs | Add subflow orchestration module and explicit acceptance criteria |
| External agents/APIs/playwright roadmap | [docs/todo.md](./todo.md#L1-L6) | N | Not present in modules | No integration boundary definitions yet | Define integration interfaces first, then ship minimal adapters |

---

## C) Direction-setting recommendations (product completeness + QoL)

### Theme 1: Product experience improvements

#### 1) Introduce “Run Profiles” (Safe / Balanced / Power)
- **Why it matters:** Reduces cognitive load in setup and run flags.
- **What to build/change:** Profile presets for approval modes, timeout/retries, and tool strictness.
- **Where it fits:** [closed_claw/cli.py](../closed_claw/cli.py), [closed_claw/interactive.py](../closed_claw/interactive.py), [closed_claw/config.py](../closed_claw/config.py)
- **Effort:** M
- **Risks/trade-offs:** More config surface; must avoid hidden behavior.
- **Success metric:** % runs launched without manual flag overrides increases.

#### 2) Add “explain routing” output mode
- **Why it matters:** Trust and debuggability for agent reuse/create decisions.
- **What to build/change:** Emit structured rationale: candidate scores, threshold, gate decision.
- **Where it fits:** [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py), [closed_claw/cli.py](../closed_claw/cli.py)
- **Effort:** S
- **Risks/trade-offs:** Slightly noisier output.
- **Success metric:** Reduced “why this agent?” support questions.

#### 3) Add one-command “setup + doctor + sample run” wizard
- **Why it matters:** Converts first-run friction into guided activation.
- **What to build/change:** `bootstrap` command that runs setup checks and a dry-run sample task.
- **Where it fits:** [closed_claw/cli.py](../closed_claw/cli.py), [closed_claw/setup_wizard.py](../closed_claw/setup_wizard.py)
- **Effort:** S
- **Risks/trade-offs:** Could mask partial failures if not designed clearly.
- **Success metric:** Time-to-first-successful-run drops.

### Theme 2: Domain modeling & correctness

#### 4) Formalize run state machine
- **Why it matters:** Prevents ambiguous transitions and partial success confusion.
- **What to build/change:** Introduce explicit `RunState` enum and transition validator.
- **Where it fits:** new module under `closed_claw/domain/run_state.py`, used in coordinator nodes.
- **Effort:** M
- **Risks/trade-offs:** Refactor touches many node updates.
- **Success metric:** No invalid transition events in run logs.

#### 5) Split provider health failure from policy denial
- **Why it matters:** Circuit breaker should model provider reliability, not human policy choices.
- **What to build/change:** Separate counters/tables for `provider_failures` and `policy_denials`.
- **Where it fits:** [closed_claw/registry/store.py](../closed_claw/registry/store.py), [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py)
- **Effort:** S
- **Risks/trade-offs:** Migration required for existing DBs.
- **Success metric:** Circuit-open events correlate to technical failures only.

#### 6) Implement missing graph node behavior completely
- **Why it matters:** Architecture currently over-promises vs runtime behavior.
- **What to build/change:** Implement logic in `approval_gate_for_api_calls`, `continue_or_deny_api_path`, `failure_recovery`.
- **Where it fits:** [closed_claw/coordinator/nodes.py](../closed_claw/coordinator/nodes.py#L434-L482)
- **Effort:** M
- **Risks/trade-offs:** Could alter outputs and existing tests.
- **Success metric:** Branch coverage + explicit failure-mode outcomes.

### Theme 3: Workflow automation

#### 7) Add “task templates” for common workflows
- **Why it matters:** Speeds repeat usage and improves consistency.
- **What to build/change:** Save/load named templates with context and policy profile.
- **Where it fits:** new `closed_claw/templates` package + CLI commands.
- **Effort:** M
- **Risks/trade-offs:** Needs clear schema/versioning.
- **Success metric:** Repeat-task launch time and flag count decrease.

#### 8) Add background run queue mode (local async worker)
- **Why it matters:** Enables non-blocking long tasks while preserving local-first model.
- **What to build/change:** Queue file/db table + worker loop + `runs watch` command.
- **Where it fits:** new `closed_claw/runtime/queue.py`, CLI additions.
- **Effort:** L
- **Risks/trade-offs:** Must avoid race/duplication.
- **Success metric:** Long-run completion without interactive blocking.

### Theme 4: UX consistency & ergonomics

#### 9) Standardize error classes and user-facing remediation text
- **Why it matters:** Better operator experience than raw exception strings.
- **What to build/change:** `errors.py` with typed errors and remediation hints in CLI output.
- **Where it fits:** cross-cutting (runtime, tools, setup, registry).
- **Effort:** M
- **Risks/trade-offs:** Short-term refactor overhead.
- **Success metric:** Faster issue triage and fewer ambiguous failures.

#### 10) Add undo safety for destructive agent operations
- **Why it matters:** Reduces fear and accidental data loss in delete flows.
- **What to build/change:** Soft-delete tombstone + restore command before hard purge.
- **Where it fits:** [closed_claw/cli.py](../closed_claw/cli.py), [closed_claw/registry/store.py](../closed_claw/registry/store.py)
- **Effort:** M
- **Risks/trade-offs:** Additional state complexity.
- **Success metric:** Zero unrecoverable accidental deletions.

### Theme 5: Integrations for completeness

#### 11) Add pluggable approval backends
- **Why it matters:** Real teams need non-terminal approvals (chat/webhook/queue).
- **What to build/change:** Approval backend interface + CLI backend + webhook backend.
- **Where it fits:** [closed_claw/policy/approval.py](../closed_claw/policy/approval.py) -> split into strategy modules.
- **Effort:** M
- **Risks/trade-offs:** More moving parts in local mode.
- **Success metric:** Approval completion latency and adoption in team workflows.

#### 12) Add provider capability registry
- **Why it matters:** Clarifies what each LLM provider supports and normalizes behavior.
- **What to build/change:** Provider metadata (limits/features/cost estimates) and adapter contracts.
- **Where it fits:** new `closed_claw/providers` package; consumed by reranker/setup.
- **Effort:** M
- **Risks/trade-offs:** Need ongoing maintenance.
- **Success metric:** Fewer provider-specific errors and cleaner fallback behavior.

### Theme 6: Permissions and audit/history

#### 13) Introduce execution policy tiers per tool
- **Why it matters:** Current allowlist is binary and too coarse for real-world safety.
- **What to build/change:** Tiered policies (`read_only`, `write`, `network`, `shell`) with escalation rules.
- **Where it fits:** [closed_claw/tools/executor.py](../closed_claw/tools/executor.py), policy package.
- **Effort:** M
- **Risks/trade-offs:** More policy configuration burden.
- **Success metric:** Reduced risky tool usage without approval.

#### 14) Add immutable “run summary” snapshots
- **Why it matters:** Durable forensic records improve trust and debugging.
- **What to build/change:** Persist normalized run summary object with key phases/timings/decisions.
- **Where it fits:** [closed_claw/registry/store.py](../closed_claw/registry/store.py), [closed_claw/observability/runlog.py](../closed_claw/observability/runlog.py)
- **Effort:** S
- **Risks/trade-offs:** Slight storage growth.
- **Success metric:** Mean-time-to-explain-failure decreases.

### Theme 7: Developer flow and architecture clarity

#### 15) Replace missing/implicit “agents factory” with explicit domain module
- **Why it matters:** Unblocks runtime and prevents architectural drift.
- **What to build/change:** Formal `agents` package with typed capsule API and tests.
- **Where it fits:** new `closed_claw/agents/*`; consumers in CLI/graph/nodes/tests.
- **Effort:** M
- **Risks/trade-offs:** Requires alignment on capsule schema ownership.
- **Success metric:** Imports/tests stable; no missing module regressions.

#### 16) Add architecture tests for dependency direction
- **Why it matters:** Prevents coordinator/runtime/policy tangling as product expands.
- **What to build/change:** Lightweight tests asserting allowed module dependencies.
- **Where it fits:** `tests/architecture/`.
- **Effort:** S
- **Risks/trade-offs:** Initial setup effort.
- **Success metric:** Fewer cyclic/accidental cross-layer dependencies.

---

## D) Module surgery: remove / refactor / add

### Remove / retire

1. **`compat.py` pydantic fallback branch**
   - Rationale: Hidden alternate runtime model increases ambiguity; enforce explicit dependency instead.
2. **No-op graph behaviors** as “finalized” architecture claims
   - Rationale: Placeholder behavior should not masquerade as complete flow.
3. **Ad-hoc inline error fallbacks that swallow exceptions silently**
   - Rationale: Replace with explicit degraded-mode events and typed errors.

### Refactor / re-scope

1. **`closed_claw/coordinator/nodes.py`**
   - Re-scope into smaller modules (`routing`, `execution`, `policy_bridge`, `finalization`).
2. **`closed_claw/tools/executor.py`**
   - Move from direct executor to policy-driven execution engine with adapters.
3. **`closed_claw/setup_wizard.py`**
   - Separate provider probing from secret persistence and CLI interaction.
4. **`closed_claw/registry/store.py`**
   - Split repository concerns: agent repository, run repository, policy/circuit repository.

### Add (missing modules that unlock completeness)

#### 1) `closed_claw/agents/` (capsule domain)
- **Responsibility + boundaries:** Create/load/validate capsules, registry index generation, template management.
- **Public API:**
  - `create_capsule(spec: CapsuleSpec) -> AgentManifest`
  - `load_capsule(agent_id: str) -> CapsuleHandle`
  - `save_registry_index(manifests: list[AgentManifest]) -> None`
- **Data model changes:** add `capsule_version`, `profile_id`, `last_validation_at`.
- **Example structure:**
  - `closed_claw/agents/__init__.py`
  - `closed_claw/agents/factory.py`
  - `closed_claw/agents/models.py`
  - `closed_claw/agents/templates.py`

#### 2) `closed_claw/domain/` (state + invariants)
- **Responsibility + boundaries:** central domain enums/states/rules independent of CLI or storage.
- **Public API:**
  - `validate_run_transition(from_state, to_state) -> bool`
  - `class RunState(Enum)`
  - `class DecisionReason(Enum)`
- **Data model changes:** normalized run state field and transition events.
- **Example structure:**
  - `closed_claw/domain/run_state.py`
  - `closed_claw/domain/policy.py`

#### 3) `closed_claw/policy/backends/` (approval strategies)
- **Responsibility + boundaries:** backend-specific approval transport (terminal, webhook, queue).
- **Public API:**
  - `ApprovalBackend.request(req: ApprovalRequest) -> ApprovalDecision`
- **Data model changes:** approval backend metadata in audit events.
- **Example structure:**
  - `closed_claw/policy/backends/cli_backend.py`
  - `closed_claw/policy/backends/webhook_backend.py`

#### 4) `closed_claw/commands/` (CLI use-cases)
- **Responsibility + boundaries:** command handlers decoupled from parser wiring.
- **Public API:**
  - `run_task(opts)`, `inspect_run(run_id)`, `bootstrap()`
- **Data model changes:** none mandatory.
- **Example structure:**
  - `closed_claw/commands/run.py`
  - `closed_claw/commands/inspect.py`
  - `closed_claw/commands/bootstrap.py`

#### 5) `closed_claw/diagnostics/` (developer/operator tooling)
- **Responsibility + boundaries:** env checks, dependency checks, protocol smoke checks.
- **Public API:**
  - `doctor() -> DoctorReport`
  - `smoke_run() -> SmokeResult`
- **Data model changes:** optional diagnostics table/event stream.
- **Example structure:**
  - `closed_claw/diagnostics/doctor.py`
  - `closed_claw/diagnostics/smoke.py`

---

## E) Quality-of-life improvements (developer + operator)

Prioritized by impact vs effort.

| Priority | QoL Improvement | Impact | Effort | Why it helps |
|---|---|---:|---:|---|
| 1 | One-command bootstrap (`setup + init + doctor + smoke run`) | High | S | Removes onboarding friction and catches env drift early. |
| 2 | Golden-path smoke test in CI (`import + init + dry run`) | High | S | Prevents silent breakage like missing modules. |
| 3 | Seed/demo fixtures for local runs | High | S | Makes demos and bug repro deterministic. |
| 4 | Typed config validation with clear startup errors | High | M | Avoids subtle misconfiguration failures. |
| 5 | Rich debug bundle command (`collect logs/run/audit/env summary`) | High | M | Faster issue triage across team members. |
| 6 | Better CLI error messaging with remediation hints | Medium | S | Reduces support burden and trial/error. |
| 7 | Contract tests for runtime protocol messages | Medium | S | Protects protocol evolution from regressions. |
| 8 | “How to add a feature” guide + architecture map | Medium | S | Reduces contributor ramp-up time. |
| 9 | Fixture-based integration harness for tool callbacks | Medium | M | Speeds safe iteration on coordinator logic. |
| 10 | CLI helper commands (`seed`, `reset`, `diag`, `replay-run`) | Medium | M | Improves operator productivity and repeatability. |

---

## F) Opinionated product roadmap (feature completeness)

### Now (Milestone 1: Make core coherent)
- Restore capsule domain (`agents` package) and unblock imports.
- Implement missing coordinator node behaviors.
- Introduce run state machine + explicit transition logging.
- Harden tool execution policy (replace shell string execution model).
- Add bootstrap and smoke diagnostics command.

**Dependencies:** `agents` package must land before reliable run/create flows.

**Prototype vs proper:**
- Prototype: run profiles (simple static presets)
- Proper now: state machine + node completeness + tool policy foundation

### Next (Milestone 2: Complete operator experience)
- Add run templates and recent-run ergonomics.
- Add pluggable approval backends (CLI + webhook).
- Add immutable run summaries and richer inspection output.
- Expand edge-case integration tests (deny/failure/retry/tool rejection).

**Dependencies:** state machine and policy backend interfaces should be stable first.

**Prototype vs proper:**
- Prototype: webhook approval adapter
- Proper now: run summary schema and template schema

### Later (Milestone 3: Extension-ready product)
- Supervisor/subflow orchestration model (from docs TODO direction).
- External agent/API adapter framework.
- Playwright and server-launch capabilities with explicit risk tiers.

**Dependencies:** strong policy model and execution sandboxing first.

**What not to build yet (avoid scope creep)**
- Full web UI or multi-tenant service APIs.
- Complex distributed queue system.
- Heavy infra platforming beyond local/dev productivity.

---

## G) If this were my codebase…

### Top 5 PRs I would open this week

1. **Reintroduce capsule domain module and unblock core imports**
   - Add `closed_claw/agents/factory.py`, tests, and import-smoke check.
2. **Implement coordinator “missing middle” nodes with explicit run-state transitions**
   - Fill `approval_gate_for_api_calls`, `continue_or_deny_api_path`, `failure_recovery`.
3. **Replace shell-string terminal executor with structured command policy engine**
   - Remove `shell=True`, add command schema + risk tier approvals.
4. **Add bootstrap + smoke diagnostics CLI flow**
   - One-command local bring-up and confidence verification.
5. **Introduce run templates + profiles for repeatable usage**
   - Reduce run friction and improve consistency.

### Top 5 design decisions to make explicitly

1. **Execution model boundary**
   - Options: pure local subprocess only vs local + remote adapters.
2. **Approval architecture**
   - Options: terminal-only vs pluggable backends (webhook/queue/chat).
3. **Capsule ownership and versioning**
   - Options: static manifest schema vs versioned schema with migration contracts.
4. **Tool safety posture**
   - Options: coarse allowlist vs tiered policy with escalation + sandboxing.
5. **Protocol evolution strategy**
   - Options: implicit best-effort JSON vs versioned contracts with compatibility policy.

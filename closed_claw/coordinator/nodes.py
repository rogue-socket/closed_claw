# Purpose: Coordinator node implementations for planning, execution, and auditing.

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger("closed_claw.coordinator")

from closed_claw.agents.factory import AgentFactory
from closed_claw.config import Settings
from closed_claw.embeddings.provider import EmbeddingProvider
from closed_claw.evolution.fitness import evaluate_fitness
from closed_claw.evolution.genome import Genome
from closed_claw.evolution.mutation import mutate_genome
from closed_claw.evolution.selection import select_ancestor_genome
from closed_claw.observability.runlog import RunLogger
from closed_claw.policy.approval import ApprovalGate, ApprovalRequest
from closed_claw.policy.audit import AuditStore
from closed_claw.registry.search import (
    RerankerProtocol,
    classify_task_complexity,
    generate_agent_profile,
    generate_task_plan,
)
from closed_claw.registry.store import AgentManifest, RegistryStore, SearchCandidate
from closed_claw.runtime.protocol import (
    ApiCallDecision,
    ApiCallIntent,
    CoordinatorRequest,
    ToolCallIntent,
    ToolCallResult,
)
from closed_claw.runtime.runner import AgentRunner, AgentRuntimeError
from closed_claw.tools.executor import (
    SUPPORTED_TOOLS,
    ToolExecutionError,
    ToolExecutor,
    tool_registry_for_allowlist,
)


class CoordinatorNodes:
    def __init__(
        self,
        settings: Settings,
        registry: RegistryStore,
        reranker: RerankerProtocol,
        embedder: EmbeddingProvider,
        runner: AgentRunner,
        factory: AgentFactory,
        approval_gate: ApprovalGate,
        audit: AuditStore,
    ) -> None:
        """Initialize the instance."""
        self.settings = settings
        self.registry = registry
        self.reranker = reranker
        self.embedder = embedder
        self.runner = runner
        self.factory = factory
        self.approval_gate = approval_gate
        self.audit = audit

        # If explicit allowed paths are configured, treat them as the authoritative
        # filesystem sandbox roots for file_io/sql_query tools.
        if self.settings.extra_allowed_paths:
            workspace_root = self.settings.extra_allowed_paths[0]
            dynamic_roots = self.settings.extra_allowed_paths
        else:
            workspace_root = self.settings.agents_dir.parent
            dynamic_roots = [self.settings.agents_dir.parent.parent]
        self.tool_executor = ToolExecutor(
            workspace_root=workspace_root,
            allowed_roots=dynamic_roots,
        )

    @staticmethod
    def _merge(state: dict[str, Any], **updates: Any) -> dict[str, Any]:
        """Run merge."""
        merged = dict(state)
        merged.update(updates)
        return merged

    def _emit_runlog(self, state: dict[str, Any], event: str, payload: dict[str, Any]) -> None:
        """Run emit runlog."""
        run_id = state.get("run_id")
        if not run_id:
            return
        RunLogger(self.settings.run_logs_dir, run_id=run_id).emit(event, payload)

    async def ingest_task(self, state: dict[str, Any]) -> dict[str, Any]:
        """Asynchronously run ingest task."""
        task = state.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Missing required field: task")
        merged = self._merge(
            state,
            run_id=state.get("run_id", uuid.uuid4().hex),
            session_id=state.get("session_id", uuid.uuid4().hex[:12]),
            task=task,
            context=state.get("context", {}),
            artifacts=[],
            approvals=[],
            tool_events=[],
            failed_agents=[],
            subtask_pool=[],
            discovery_subtask_pool=[],
            execution_subtask_pool=[],
            role_agent_map={},
            discovery_results={},
            execution_results={},
            subtask_results={},
        )
        self._emit_runlog(merged, "task_ingested", {"task": task, "session_id": merged["session_id"]})
        return merged

    async def decompose_task(self, state: dict[str, Any]) -> dict[str, Any]:
        """Asynchronously run decompose task."""
        task = state["task"]

        # Classify task complexity to decide whether discovery is needed
        try:
            complexity = classify_task_complexity(self.settings, task)
        except (ValueError, RuntimeError):
            complexity = "complex"  # safe default

        self._emit_runlog(state, "task_complexity_classified", {"complexity": complexity})

        if complexity == "simple":
            # Skip discovery — generate execution plan directly
            plan = generate_task_plan(
                self.settings,
                task,
                phase="execution",
            )
            execution_pool = self._prepare_phase_pool(plan, phase="execution")
            merged = self._merge(
                state,
                task_complexity=complexity,
                discovery_subtask_pool=[],
                execution_subtask_pool=execution_pool,
                discovery_results={},
                execution_results={},
                subtask_pool=execution_pool,
            )
            self._emit_runlog(
                merged,
                "task_plan_created",
                {
                    "phase": "execution",
                    "complexity": "simple",
                    "subtask_count": len(execution_pool),
                    "subtasks": self._task_pool_snapshot(execution_pool),
                },
            )
            self._emit_task_pool(merged, execution_pool, phase="execution")
            return merged

        # Complex task — generate discovery plan as before
        plan = generate_task_plan(
            self.settings,
            task,
            phase="discovery",
        )
        discovery_pool = self._prepare_phase_pool(plan, phase="discovery")
        merged = self._merge(
            state,
            task_complexity=complexity,
            discovery_subtask_pool=discovery_pool,
            execution_subtask_pool=[],
            discovery_results={},
            execution_results={},
            subtask_pool=discovery_pool,
        )
        self._emit_runlog(
            merged,
            "task_plan_created",
            {
                "phase": "discovery",
                "complexity": "complex",
                "subtask_count": len(discovery_pool),
                "subtasks": self._task_pool_snapshot(discovery_pool),
            },
        )
        self._emit_task_pool(merged, discovery_pool, phase="discovery")
        return merged

    async def execute_task_pool(self, state: dict[str, Any]) -> dict[str, Any]:
        """Asynchronously run execute task pool."""
        approvals = list(state.get("approvals", []))
        tool_events = list(state.get("tool_events", []))
        api_mode = (state.get("runtime_policies", {}) or {}).get(
            "api_approval_mode", self.settings.api_approval_mode
        )
        role_agent_map: dict[str, str] = dict(state.get("role_agent_map", {}))
        created_agents: list[dict[str, Any]] = list(state.get("created_agents", []))
        subtask_results: dict[str, str] = dict(state.get("subtask_results", {}))
        legacy_pool = [dict(item) for item in state.get("subtask_pool", [])]

        if self._should_skip_discovery(state):
            # Use the explicit execution pool when present (simple-classified
            # path); fall back to the legacy single-pool shape.
            execution_only_pool = [
                dict(item) for item in state.get("execution_subtask_pool", [])
            ] or legacy_pool
            for item in execution_only_pool:
                item.setdefault("phase", "execution")
            execution_phase = await self._execute_phase_pool(
                state=state,
                phase="execution",
                pool=execution_only_pool,
                discovery_results=dict(state.get("discovery_results", {})),
                approvals=approvals,
                tool_events=tool_events,
                role_agent_map=role_agent_map,
                created_agents=created_agents,
                subtask_results=subtask_results,
                api_mode=api_mode,
            )
            return self._merge(
                state,
                response_status=execution_phase["status"],
                response_result=self._format_subtask_result_summary([execution_phase["pool"]]),
                response_error=execution_phase["error"],
                selected_agent_id=next(iter(role_agent_map.values()), state.get("selected_agent_id", "unknown")),
                role_agent_map=role_agent_map,
                created_agents=created_agents,
                subtask_pool=execution_phase["pool"],
                discovery_subtask_pool=[],
                execution_subtask_pool=execution_phase["pool"],
                discovery_results=dict(state.get("discovery_results", {})),
                execution_results=execution_phase["phase_results"],
                subtask_results=subtask_results,
                approvals=approvals,
                tool_events=tool_events,
            )

        discovery_pool = [dict(item) for item in state.get("discovery_subtask_pool", [])]
        if not discovery_pool:
            discovery_plan = generate_task_plan(
                self.settings,
                state["task"],
                phase="discovery",
            )
            discovery_pool = self._prepare_phase_pool(discovery_plan, phase="discovery")
            self._emit_runlog(
                state,
                "task_plan_created",
                {
                    "phase": "discovery",
                    "subtask_count": len(discovery_pool),
                    "subtasks": self._task_pool_snapshot(discovery_pool),
                },
            )
            self._emit_task_pool(state, discovery_pool, phase="discovery")

        discovery_phase = await self._execute_phase_pool(
            state=state,
            phase="discovery",
            pool=discovery_pool,
            discovery_results={},
            approvals=approvals,
            tool_events=tool_events,
            role_agent_map=role_agent_map,
            created_agents=created_agents,
            subtask_results=subtask_results,
            api_mode=api_mode,
        )
        discovery_results = discovery_phase["phase_results"]

        if discovery_phase["status"] != "ok":
            return self._merge(
                state,
                response_status="error",
                response_result=self._format_subtask_result_summary([discovery_phase["pool"]]),
                response_error=discovery_phase["error"],
                selected_agent_id=next(iter(role_agent_map.values()), state.get("selected_agent_id", "unknown")),
                role_agent_map=role_agent_map,
                created_agents=created_agents,
                subtask_pool=discovery_phase["pool"],
                discovery_subtask_pool=discovery_phase["pool"],
                execution_subtask_pool=[],
                discovery_results=discovery_results,
                execution_results={},
                subtask_results=subtask_results,
                approvals=approvals,
                tool_events=tool_events,
            )

        execution_plan = generate_task_plan(
            self.settings,
            state["task"],
            phase="execution",
            discovery_results=discovery_results,
        )
        execution_pool = self._prepare_phase_pool(execution_plan, phase="execution")
        self._emit_runlog(
            state,
            "task_plan_created",
            {
                "phase": "execution",
                "subtask_count": len(execution_pool),
                "subtasks": self._task_pool_snapshot(execution_pool),
            },
        )
        self._emit_task_pool(state, execution_pool, phase="execution")
        execution_phase = await self._execute_phase_pool(
            state=state,
            phase="execution",
            pool=execution_pool,
            discovery_results=discovery_results,
            approvals=approvals,
            tool_events=tool_events,
            role_agent_map=role_agent_map,
            created_agents=created_agents,
            subtask_results=subtask_results,
            api_mode=api_mode,
        )

        combined_pool = [*discovery_phase["pool"], *execution_phase["pool"]]
        return self._merge(
            state,
            response_status=execution_phase["status"],
            response_result=self._format_subtask_result_summary(
                [discovery_phase["pool"], execution_phase["pool"]]
            ),
            response_error=execution_phase["error"],
            selected_agent_id=next(iter(role_agent_map.values()), state.get("selected_agent_id", "unknown")),
            role_agent_map=role_agent_map,
            created_agents=created_agents,
            subtask_pool=combined_pool,
            discovery_subtask_pool=discovery_phase["pool"],
            execution_subtask_pool=execution_phase["pool"],
            discovery_results=discovery_results,
            execution_results=execution_phase["phase_results"],
            subtask_results=subtask_results,
            approvals=approvals,
            tool_events=tool_events,
        )

    def _prepare_phase_pool(self, plan: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
        """Run prepare phase pool."""
        # Cap number of subtasks to prevent unbounded growth
        max_subtasks = max(1, self.settings.max_subtasks_per_phase)
        capped_plan = plan[:max_subtasks]

        prefix = "discover" if phase == "discovery" else "execute"
        id_map: dict[str, str] = {}
        used_ids: set[str] = set()
        for index, item in enumerate(capped_plan, start=1):
            original = str(item.get("task_id", f"task-{index}")).strip() or f"task-{index}"
            base = original if original.startswith(f"{prefix}-") else f"{prefix}-{original}"
            task_id = base
            suffix = 2
            while task_id in used_ids:
                task_id = f"{base}-{suffix}"
                suffix += 1
            used_ids.add(task_id)
            id_map[original] = task_id

        pool: list[dict[str, Any]] = []
        for index, item in enumerate(capped_plan, start=1):
            original = str(item.get("task_id", f"task-{index}")).strip() or f"task-{index}"
            task_id = id_map[original]
            deps = []
            for dep in item.get("depends_on", []) or []:
                if dep in id_map:
                    deps.append(id_map[dep])
            pool.append(
                {
                    **item,
                    "task_id": task_id,
                    "depends_on": deps,
                    "phase": phase,
                    "status": "waiting" if deps else "pending",
                    "assigned_agent_id": None,
                    "result": "",
                    "error": "",
                }
            )
        return pool

    @staticmethod
    def _format_subtask_result_summary(pools: list[list[dict[str, Any]]]) -> str:
        """Run format subtask result summary."""
        completed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for pool in pools:
            completed.extend([item for item in pool if item.get("status") == "completed"])
            failed.extend([item for item in pool if item.get("status") in {"failed", "cancelled"}])

        parts: list[str] = []

        if completed:
            parts.append("Completed sub-tasks:")
            parts.extend(
                f"- [{item.get('phase','task')}/{item.get('task_id','')}] "
                f"({item.get('role_tag','')}) {item.get('title','')}: {item.get('result','')}"
                for item in completed
            )

        if failed:
            parts.append("\nFailed/Cancelled sub-tasks:" if completed else "Failed/Cancelled sub-tasks:")
            parts.extend(
                f"- [{item.get('phase','task')}/{item.get('task_id','')}] "
                f"({item.get('role_tag','')}) {item.get('title','')}: error={item.get('error','unknown')}"
                for item in failed
            )

        if not parts:
            total = sum(len(p) for p in pools)
            return f"No sub-tasks completed out of {total} total." if total else "No sub-tasks were generated."

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Single-subtask coroutine — designed to run concurrently via
    # asyncio.gather inside _execute_phase_pool's ready-batch.
    # ------------------------------------------------------------------

    async def _run_single_subtask(
        self,
        *,
        item: dict[str, Any],
        state: dict[str, Any],
        phase: str,
        pool: list[dict[str, Any]],
        by_id: dict[str, dict[str, Any]],
        discovery_results: dict[str, str],
        approvals: list[dict[str, Any]],
        tool_events: list[dict[str, Any]],
        role_agent_map: dict[str, str],
        created_agents: list[dict[str, Any]],
        subtask_results: dict[str, str],
        api_mode: str,
        max_agents: int,
        agents_created_counter: list[int],
    ) -> None:
        """Execute a single subtask (with retries), to be gathered concurrently.

        Uses per-attempt local lists for tool_events and approvals to avoid
        cross-contamination when multiple subtasks run in parallel.  Results
        are merged into the shared lists after each attempt completes.
        """
        role_tag = str(item.get("role_tag", "task-operator")) or "task-operator"
        item["status"] = "in_progress"
        self._emit_task_pool(state, pool, phase=phase)

        dep_context = {
            dep: by_id[dep].get("result", "")
            for dep in item.get("depends_on", [])
            if dep in by_id
        }
        criteria = item.get("acceptance_criteria", [])
        workspace = str(self.tool_executor.workspace_root)
        task_payload = (
            f"Workspace root: {workspace}\n"
            f"`$WORKSPACE` and `${{WORKSPACE}}` resolve to this path. "
            f"When you need a file path, substitute the actual path above "
            f"rather than passing the literal token `$WORKSPACE`.\n\n"
            f"{item.get('description', '')}\n\n"
            "Acceptance criteria:\n"
            + "\n".join(f"- {c}" for c in criteria)
        ).strip()
        max_attempts = max(1, int(self.settings.subtask_max_attempts))
        attempt_failures: list[str] = []
        item["attempts"] = 0
        agent_id: str | None = None

        for attempt_idx in range(1, max_attempts + 1):
            item["attempts"] = attempt_idx
            if self._is_cancel_requested(state["run_id"]):
                item["status"] = "cancelled"
                item["error"] = "cancelled_by_user"
                break

            # Agent acquisition — sync code, effectively atomic in asyncio's
            # single-thread model (no await between check and mutation).
            if role_tag not in role_agent_map:
                if agents_created_counter[0] >= max_agents:
                    item["status"] = "failed"
                    item["error"] = f"max_agents_per_run_exceeded ({max_agents})"
                    self._emit_runlog(
                        state,
                        "agent_creation_limit_reached",
                        {"phase": phase, "max_agents": max_agents, "task_id": item.get("task_id")},
                    )
                    break
                agent_id, created = self._acquire_agent_for_role(role_tag, item, parent_task=state.get("task", ""))
                role_agent_map[role_tag] = agent_id
                if created:
                    agents_created_counter[0] += 1
                    created_agents.append(created)
                    self._emit_runlog(
                        state,
                        "agent_created_for_role",
                        {
                            "agent_id": created["agent_id"],
                            "name": created["name"],
                            "description": created["description"],
                            "role_tag": role_tag,
                            "profile_id": created.get("profile_id", ""),
                            "tools_allowlist": created.get("tools_allowlist", []),
                            "skill_md_preview": (created.get("skill_md", "") or "")[:500],
                            "genome_hash": created.get("genome_hash"),
                            "generation": created.get("generation", 0),
                        },
                    )
                else:
                    self._emit_runlog(
                        state,
                        "agent_reused_for_role",
                        {
                            "agent_id": agent_id,
                            "role_tag": role_tag,
                            "task_id": item.get("task_id"),
                        },
                    )
            agent_id = role_agent_map[role_tag]
            item["assigned_agent_id"] = agent_id

            attempt_task_payload = (
                f"Subtask attempt {attempt_idx}/{max_attempts}.\n"
                f"{task_payload}"
            )
            req = CoordinatorRequest(
                session_id=state["session_id"],
                task=attempt_task_payload,
                context={
                    **(state.get("context", {}) or {}),
                    "parent_task": state["task"],
                    "task_phase": phase,
                    "discovery_results": discovery_results,
                    "subtask": {
                        "task_id": item["task_id"],
                        "title": item.get("title", ""),
                        "phase": phase,
                        "role_tag": role_tag,
                        "depends_on_results": dep_context,
                        "attempt": attempt_idx,
                        "max_attempts": max_attempts,
                        "prior_failures": list(attempt_failures),
                    },
                },
                config=self._request_config_for_agent(agent_id),
            )
            composed_skill_ids = req.config.get("skill_ids", [])
            entrypoint = self.settings.agents_dir / agent_id / "entrypoint.py"

            # Per-attempt local event lists — avoids cross-contamination when
            # multiple subtasks execute concurrently.
            attempt_tool_events: list[dict[str, Any]] = []
            attempt_approvals: list[dict[str, Any]] = []

            if composed_skill_ids:
                self._emit_runlog(
                    state,
                    "skills_composed",
                    {
                        "agent_id": agent_id,
                        "skill_ids": composed_skill_ids,
                        "task_id": item.get("task_id"),
                    },
                )
            self._emit_runlog(
                state,
                "subtask_attempt_started",
                {
                    "phase": phase,
                    "task_id": item.get("task_id"),
                    "title": item.get("title", ""),
                    "role_tag": role_tag,
                    "attempt": attempt_idx,
                    "max_attempts": max_attempts,
                    "agent_id": agent_id,
                    "task_payload": task_payload[:1000],
                    "depends_on": item.get("depends_on", []),
                    "dependency_context": {k: v[:300] for k, v in dep_context.items()} if dep_context else {},
                },
            )
            failure_reason = ""
            try:
                response = await self.runner.run_agent(
                    agent_id=agent_id,
                    entrypoint=entrypoint,
                    request=req,
                    approval_callback=lambda intent, a_id, _app=attempt_approvals: self._approval_callback(
                        intent=intent,
                        agent_id=a_id,
                        run_id=state["run_id"],
                        approvals=_app,
                        mode=api_mode,
                    ),
                    tool_callback=lambda intent, a_id, _te=attempt_tool_events: self._tool_callback(
                        intent=intent,
                        agent_id=a_id,
                        run_id=state["run_id"],
                        tool_events=_te,
                    ),
                )
                if response.status == "ok":
                    # Detect agent LLM errors disguised as successful results
                    if response.result and response.result.strip().startswith("[llm_error:"):
                        failure_reason = f"agent_llm_failure: {response.result[:200]}"
                    else:
                        new_events = attempt_tool_events
                        verification_ok, verification_msg = self._verify_subtask_tool_execution(
                            item=item,
                            new_tool_events=new_events,
                        )
                        content_ok, content_reason = (True, "")
                        if verification_ok:
                            content_ok, content_reason = self._verify_acceptance_criteria(
                                item=item,
                                response_result=response.result or "",
                            )
                        if verification_ok and content_ok:
                            item["status"] = "completed"
                            item["error"] = ""
                            item["result"] = response.result
                            subtask_results[item["task_id"]] = response.result
                            subtask_results[f"{phase}.{item['task_id']}"] = response.result
                            self._emit_runlog(
                                state,
                                "subtask_attempt_succeeded",
                                {
                                    "phase": phase,
                                    "task_id": item.get("task_id"),
                                    "title": item.get("title", ""),
                                    "attempt": attempt_idx,
                                    "agent_id": agent_id,
                                    "result": (response.result or "")[:2000],
                                    "tool_calls_in_attempt": len(new_events),
                                    "memory_updates": response.memory_updates[:5] if response.memory_updates else [],
                                },
                            )
                            # Merge per-attempt events into shared lists
                            tool_events.extend(attempt_tool_events)
                            approvals.extend(attempt_approvals)
                            break
                        if not verification_ok:
                            failure_reason = verification_msg or "filesystem_verification_failed"
                        else:
                            failure_reason = content_reason or "acceptance_criteria_not_met"
                else:
                    failure_reason = response.error_message or "subtask_failed"
            except AgentRuntimeError as exc:
                failure_reason = str(exc)

            # Merge per-attempt events into shared lists
            tool_events.extend(attempt_tool_events)
            approvals.extend(attempt_approvals)

            attempt_failures.append(failure_reason)
            self._emit_runlog(
                state,
                "subtask_attempt_failed",
                {
                    "phase": phase,
                    "task_id": item.get("task_id"),
                    "attempt": attempt_idx,
                    "max_attempts": max_attempts,
                    "agent_id": agent_id,
                    "error": failure_reason,
                },
            )
            if attempt_idx >= max_attempts:
                item["status"] = "failed"
                item["error"] = failure_reason

        if item["status"] not in {"completed", "failed", "cancelled"}:
            item["status"] = "failed"
            item["error"] = attempt_failures[-1] if attempt_failures else "subtask_failed"

        # --- Evolution: evaluate fitness for the agent that ran this subtask ---
        if self.settings.evolution_enabled and agent_id:
            try:
                agent_tool_events = [
                    e for e in tool_events if e.get("agent_id") == agent_id
                ]
                fitness = evaluate_fitness(
                    task_succeeded=(item["status"] == "completed"),
                    tool_events=agent_tool_events,
                    verification_passed=(item["status"] == "completed"),
                    latency_ms=None,  # per-subtask latency not tracked; use aggregate later
                )
                score = fitness.aggregate()
                # Exponential moving average: new_fitness = 0.6 * old + 0.4 * new
                manifest = self.registry.get_manifest(agent_id)
                if manifest is not None:
                    old = manifest.fitness_score
                    blended = 0.6 * old + 0.4 * score if old > 0 else score
                    self.registry.update_fitness(agent_id, round(blended, 4))
            except Exception:
                logger.exception("fitness evaluation failed for agent %s", agent_id)

        self._emit_task_pool(state, pool, phase=phase)

    async def _execute_phase_pool(
        self,
        *,
        state: dict[str, Any],
        phase: str,
        pool: list[dict[str, Any]],
        discovery_results: dict[str, str],
        approvals: list[dict[str, Any]],
        tool_events: list[dict[str, Any]],
        role_agent_map: dict[str, str],
        created_agents: list[dict[str, Any]],
        subtask_results: dict[str, str],
        api_mode: str,
    ) -> dict[str, Any]:
        """Asynchronously run execute phase pool."""
        by_id = {item["task_id"]: item for item in pool}
        waiting_cycles = 0
        # Guardrails: limit total iterations and agent creation to prevent infinite loops
        max_pool_iterations = len(pool) * max(1, int(self.settings.subtask_max_attempts)) + 5
        max_agents = max(1, self.settings.max_agents_per_run)
        pool_iteration = 0
        agents_created_counter: list[int] = [0]   # mutable container for concurrent access
        while True:
            pool_iteration += 1
            if pool_iteration > max_pool_iterations:
                for item in pool:
                    if item["status"] in {"waiting", "pending", "in_progress"}:
                        item["status"] = "failed"
                        item["error"] = "max_pool_iterations_exceeded"
                self._emit_runlog(
                    state,
                    "pool_iteration_limit_reached",
                    {"phase": phase, "max_iterations": max_pool_iterations},
                )
                break
            if self._is_cancel_requested(state["run_id"]):
                for item in pool:
                    if item["status"] in {"waiting", "pending"}:
                        item["status"] = "cancelled"
                        item["error"] = "cancelled_by_user"
                self._emit_task_pool(state, pool, phase=phase)
                self._emit_runlog(
                    state,
                    "run_cancelled",
                    {"phase": phase, "reason": "cancel_file_detected"},
                )
                break

            pending_left = [t for t in pool if t["status"] in {"pending", "waiting", "in_progress"}]
            if not pending_left:
                break

            progressed = False
            completed_ids = {t["task_id"] for t in pool if t["status"] == "completed"}
            failed_ids = {t["task_id"] for t in pool if t["status"] == "failed"}
            for item in pool:
                if item["status"] not in {"waiting", "pending"}:
                    continue
                deps = item.get("depends_on", []) or []
                if any(dep in failed_ids for dep in deps):
                    item["status"] = "failed"
                    item["error"] = "dependency_failed"
                    progressed = True
                    continue
                item["status"] = "pending" if all(dep in completed_ids for dep in deps) else "waiting"

            ready = [t for t in pool if t["status"] == "pending"]
            if ready:
                progressed = True
                await asyncio.gather(*(
                    self._run_single_subtask(
                        item=item,
                        state=state,
                        phase=phase,
                        pool=pool,
                        by_id=by_id,
                        discovery_results=discovery_results,
                        approvals=approvals,
                        tool_events=tool_events,
                        role_agent_map=role_agent_map,
                        created_agents=created_agents,
                        subtask_results=subtask_results,
                        api_mode=api_mode,
                        max_agents=max_agents,
                        agents_created_counter=agents_created_counter,
                    )
                    for item in ready
                ))

            if progressed:
                waiting_cycles = 0
                continue

            waiting_cycles += 1
            self._emit_task_pool(state, pool, phase=phase)
            if waiting_cycles >= 2:
                for item in pool:
                    if item["status"] in {"waiting", "pending"}:
                        item["status"] = "failed"
                        item["error"] = "unresolved_dependencies"
                break
            await asyncio.sleep(max(1, int(self.settings.task_pool_poll_interval_sec)))

        completed = [t for t in pool if t["status"] == "completed"]
        failed = [t for t in pool if t["status"] in {"failed", "cancelled"}]
        cancelled = [t for t in pool if t["status"] == "cancelled"]
        status = "ok" if not failed else "error"
        if status == "ok":
            error = ""
        elif cancelled:
            error = "cancelled_by_user"
        else:
            error = f"{phase}_phase_failed"
        return {
            "pool": pool,
            "phase_results": {item["task_id"]: item.get("result", "") for item in completed},
            "status": status,
            "error": error,
        }

    def _select_capability_profile(self, task: str) -> dict[str, Any]:
        """Run select capability profile."""
        return generate_agent_profile(
            settings=self.settings,
            task=task,
            supported_tools=SUPPORTED_TOOLS,
            fallback_tools=SUPPORTED_TOOLS,
        )

    def _find_reusable_capability_agent(self, profile: dict[str, Any]) -> str | None:
        # Reuse by capability: semantic similarity is primary, role/tool overlap
        # is a sanity filter. Weight by success rate so failing agents fall back.
        """Run find reusable capability agent."""
        query_vector = self.embedder.embed(profile["description"])
        candidates = self.registry.semantic_search(query_vector, k=10)
        threshold = self.settings.low_confidence_threshold
        # Tags every auto-created capsule shares; not useful for matching.
        noise_tags = {"auto", "capability"}
        profile_tags = set(profile.get("tags", [])) - noise_tags
        required_tools = set(profile.get("tools_allowlist", []))

        scored: list[tuple[float, str]] = []
        for cand in candidates:
            if cand.score < threshold:
                continue
            manifest = self.registry.get_manifest(cand.agent_id)
            if manifest is None or manifest.status not in ("active",):
                continue
            cand_tags = set(manifest.tags) - noise_tags
            # Role-overlap filter: skipped only if the new profile has no
            # meaningful tags to match on (otherwise we'd never reuse).
            if profile_tags and not (profile_tags & cand_tags):
                continue
            # Tool-shape filter: candidate must have every tool the new role asks for.
            if required_tools and not required_tools.issubset(set(manifest.tools_allowlist)):
                continue
            # Neutral assumption for agents with < 2 runs
            sr = manifest.success_rate if manifest.usage_count >= 2 else 0.5
            effective_score = cand.score * (0.3 + 0.7 * sr)
            scored.append((effective_score, manifest.agent_id))

        if not scored:
            return None
        scored.sort(reverse=True)
        return scored[0][1]

    def _maybe_degrade_agents(self, state: dict[str, Any]) -> None:
        """Mark agents with consistently poor performance as degraded."""
        agent_ids = set(state.get("role_agent_map", {}).values())
        agent_ids.add(state.get("selected_agent_id", ""))
        for agent_id in agent_ids:
            if not agent_id or agent_id == "unknown":
                continue
            manifest = self.registry.get_manifest(agent_id)
            if manifest is None or manifest.status != "active":
                continue
            if manifest.usage_count >= 5 and manifest.success_rate < 0.2:
                self.registry.update_agent_status(agent_id, "degraded")
                self._emit_runlog(
                    state,
                    "agent_degraded",
                    {
                        "agent_id": agent_id,
                        "usage_count": manifest.usage_count,
                        "success_rate": manifest.success_rate,
                    },
                )

    def _acquire_agent_for_role(
        self,
        role_tag: str,
        subtask: dict[str, Any],
        parent_task: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        """Run acquire agent for role."""
        role_prompt = (
            f"Original user task: {parent_task}\n"
            f"Role tag: {role_tag}. "
            f"Subtask title: {subtask.get('title', '')}. "
            f"Subtask description: {subtask.get('description', '')}"
        ) if parent_task else (
            f"Role tag: {role_tag}. "
            f"Subtask title: {subtask.get('title', '')}. "
            f"Subtask description: {subtask.get('description', '')}"
        )
        profile = self._select_capability_profile(role_prompt)
        role_slug = role_tag.strip().lower().replace(" ", "-")
        if role_slug and role_slug not in profile["tags"]:
            profile["tags"] = [*profile["tags"], role_slug]
        reusable_agent_id = self._find_reusable_capability_agent(profile)
        if reusable_agent_id:
            return reusable_agent_id, None

        # --- Evolution: derive genome from best ancestor or create gen-0 ---
        genome: Genome | None = None
        lineage_dict: dict | None = None
        if self.settings.evolution_enabled:
            profile_id = str(profile.get("profile_id", ""))
            ancestor = select_ancestor_genome(self.registry, profile_id) if profile_id else None
            if ancestor is not None:
                parent_genome, parent_gen = ancestor
                child_genome, lineage = mutate_genome(
                    parent_genome,
                    mutation_rate=self.settings.evolution_mutation_rate,
                )
                lineage.generation = parent_gen + 1
                genome = child_genome
                lineage_dict = lineage.to_dict()
            else:
                genome = Genome.random(tools=profile["tools_allowlist"])
                lineage_dict = {"parent_genome_hash": None, "generation": 0, "mutations_applied": []}

        name = f"{profile['name_prefix']} {uuid.uuid4().hex[:4]}"
        skill_content = profile["skill_md"]
        manifest = self.factory.create_capsule(
            name=name,
            description=profile["description"],
            embedding_model=self.settings.embedding_model,
            embedding_vector=self.embedder.embed(profile["description"]),
            tools_allowlist=profile["tools_allowlist"],
            tags=profile["tags"],
            api_capabilities=profile.get("api_capabilities", []),
            requires_approval_for=profile.get("requires_approval_for", []),
            skill_content=skill_content,
            skill_ids=profile.get("skill_ids", []),
            genome=genome,
            lineage_dict=lineage_dict,
        )
        self.registry.upsert_manifest(manifest)
        self._sync_registry_index()
        created_agent = {
            "agent_id": manifest.agent_id,
            "name": manifest.name,
            "description": manifest.description,
            "profile_id": profile["profile_id"],
            "tools_allowlist": manifest.tools_allowlist,
            "skill_md": skill_content,
            "role_tag": role_tag,
            "genome_hash": genome.hash() if genome else None,
            "generation": lineage_dict.get("generation", 0) if lineage_dict else 0,
        }
        return manifest.agent_id, created_agent

    def _task_pool_snapshot(self, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run task pool snapshot."""
        return [
            {
                "task_id": item.get("task_id"),
                "title": item.get("title"),
                "role_tag": item.get("role_tag"),
                "depends_on": item.get("depends_on", []),
                "status": item.get("status"),
                "assigned_agent_id": item.get("assigned_agent_id"),
                "error": item.get("error", ""),
                "result_preview": (item.get("result", "") or "")[:300],
            }
            for item in pool
        ]

    def _emit_task_pool(
        self,
        state: dict[str, Any],
        pool: list[dict[str, Any]],
        phase: str | None = None,
    ) -> None:
        """Run emit task pool."""
        payload: dict[str, Any] = {"tasks": self._task_pool_snapshot(pool)}
        if phase:
            payload["phase"] = phase
        self._emit_runlog(
            state,
            "task_pool_update",
            payload,
        )

    def _is_cancel_requested(self, run_id: str) -> bool:
        """Run is cancel requested."""
        cancel_path = self.settings.run_logs_dir / f"{run_id}.cancel"
        return cancel_path.exists()

    async def validate_outputs(self, state: dict[str, Any]) -> dict[str, Any]:
        """Asynchronously run validate outputs."""
        if state.get("response_status") not in {"ok", "error"}:
            return self._merge(
                state,
                response_status="error",
                response_error="invalid_agent_response",
            )
        return state

    async def update_registry_and_audit(self, state: dict[str, Any]) -> dict[str, Any]:
        """Asynchronously run update registry and audit."""
        self.registry.record_run(
            run_id=state["run_id"],
            agent_id=state.get("selected_agent_id", "unknown"),
            task=state["task"],
            status=state.get("response_status", "error"),
            latency_ms=state.get("response_latency_ms"),
            error_message=state.get("response_error"),
        )
        # Auto-degrade agents with consistently poor performance
        self._maybe_degrade_agents(state)
        self.audit.record_event(
            "run_summary",
            {
                "status": state.get("response_status", "error"),
                "error": state.get("response_error", ""),
                "approvals": state.get("approvals", []),
                "tool_events": state.get("tool_events", []),
            },
            run_id=state["run_id"],
            agent_id=state.get("selected_agent_id"),
        )
        self._emit_runlog(
            state,
            "run_summary",
            {
                "status": state.get("response_status", "error"),
                "agent_id": state.get("selected_agent_id"),
                "response_result": (state.get("response_result", "") or "")[:3000],
                "response_error": state.get("response_error", ""),
                "agents_used": list(state.get("role_agent_map", {}).values())[:20],
                "created_agents": [
                    {"agent_id": a.get("agent_id"), "name": a.get("name"), "role_tag": a.get("role_tag", "")}
                    for a in state.get("created_agents", [])[:10]
                ],
                "approval_count": len(state.get("approvals", [])),
                "tool_event_count": len(state.get("tool_events", [])),
            },
        )
        return state

    async def synthesize_final_response(self, state: dict[str, Any]) -> dict[str, Any]:
        """Use the LLM to synthesize a coherent final response from subtask results."""
        raw_result = state.get("response_result", "")
        status = state.get("response_status", "error")

        # If the run failed entirely, produce a structured error message.
        if status != "ok":
            error_detail = state.get('response_error', 'unknown')
            # Include failed subtask info for context
            failed_info = []
            for pool_key in ("discovery_subtask_pool", "execution_subtask_pool"):
                for item in state.get(pool_key, []):
                    if item.get("status") in {"failed", "cancelled"}:
                        failed_info.append(
                            f"  - {item.get('title', item.get('task_id', '?'))}: "
                            f"{item.get('error', 'unknown')}"
                        )
            error_msg = f"Unable to complete task. Reason: {error_detail}"
            if failed_info:
                error_msg += "\n\nFailed sub-tasks:\n" + "\n".join(failed_info)
            if raw_result:
                error_msg += f"\n\nPartial results:\n{raw_result}"
            self._emit_runlog(state, "synthesis_complete", {
                "llm_synthesized": False,
                "status": "error",
                "error_summary": error_msg[:2000],
            })
            return self._merge(state, response_result=error_msg)

        # Build pool summary for synthesis input
        pool_summary = self._format_subtask_result_summary(
            [state.get("discovery_subtask_pool", []), state.get("execution_subtask_pool", [])]
        ) or raw_result

        # Attempt LLM synthesis to turn raw subtask outputs into a clean answer.
        try:
            from closed_claw.llm_client import generate_text, provider_key_and_base

            provider = self.settings.llm_provider.lower()
            key, base = provider_key_and_base(self.settings, provider)
            if key and provider not in ("heuristic",):

                prompt = (
                    "You are the final summarizer for a multi-agent task run.\n"
                    f"Original task: {state.get('task', '')}\n\n"
                    f"Sub-task outputs:\n{pool_summary[:4000]}\n\n"
                    "Write a clear, concise summary of what was accomplished. "
                    "Include concrete results, files created/modified, and any caveats. "
                    "Plain text only, no JSON."
                )
                synthesized = generate_text(
                    provider=provider,
                    model=self.settings.llm_model,
                    api_key=key,
                    base_url=base,
                    timeout_sec=self.settings.llm_timeout_sec,
                    prompt=prompt,
                    max_tokens=600,
                    temperature=0.1,
                )
                if synthesized and synthesized.strip():
                    self._emit_runlog(state, "synthesis_complete", {
                        "llm_synthesized": True,
                        "synthesis_preview": synthesized.strip()[:1500],
                    })
                    return self._merge(state, response_result=synthesized.strip())
        except Exception:
            logger.exception("LLM synthesis failed, falling back to raw result")

        # Ensure we always return something meaningful even without LLM
        final_result = raw_result or pool_summary or "Task completed but no detailed output was captured."
        self._emit_runlog(state, "synthesis_complete", {
            "llm_synthesized": False,
            "raw_result_preview": (final_result or "")[:1500],
        })
        return self._merge(state, response_result=final_result)

    async def _approval_callback(
        self,
        intent: ApiCallIntent,
        agent_id: str,
        run_id: str,
        approvals: list[dict[str, Any]],
        mode: str,
    ) -> ApiCallDecision:
        """Asynchronously run approval callback."""
        # Auto-approve regular LLM API calls — only gate external/paid non-LLM APIs
        if intent.call_type in ("llm_api_call", "llm_completion"):
            decision = ApiCallDecision(approved=True, note="llm_call_auto_approved")
            approvals.append(
                {
                    "provider": intent.provider,
                    "endpoint": intent.endpoint,
                    "approved": True,
                    "note": decision.note,
                }
            )
            return decision

        if intent.call_type == "external_paid_api" and intent.provider not in self.settings.paid_api_providers:
            decision = ApiCallDecision(approved=True, note="provider_not_marked_paid")
            approvals.append(
                {
                    "provider": intent.provider,
                    "endpoint": intent.endpoint,
                    "approved": True,
                    "note": decision.note,
                }
            )
            return decision

        if self.registry.is_circuit_open(intent.provider, self.settings.circuit_breaker_reset_sec):
            decision = ApiCallDecision(approved=False, note="circuit_breaker_open")
            approvals.append(
                {
                    "provider": intent.provider,
                    "endpoint": intent.endpoint,
                    "approved": False,
                    "note": decision.note,
                }
            )
            self.audit.record_event(
                "approval_blocked_by_circuit",
                {"provider": intent.provider, "endpoint": intent.endpoint},
                run_id=run_id,
                agent_id=agent_id,
            )
            return decision

        req = ApprovalRequest(
            call_type=intent.call_type,
            provider=intent.provider,
            endpoint=intent.endpoint,
            estimated_cost_usd=float(intent.estimated_cost_usd),
            reason=intent.reason,
            session_id=run_id,
        )
        human_decision = self.approval_gate.decide_with_mode(req=req, mode=mode)
        self.audit.record_approval(req, human_decision, run_id=run_id, agent_id=agent_id)

        # Circuit breaker only tracks *technical* provider failures.
        # Policy denials (human says "no") should NOT open the circuit.
        # Only reset the circuit on a successful approval (provider reachable).
        if human_decision.approved:
            self.registry.reset_circuit(intent.provider)

        approvals.append(
            {
                "provider": intent.provider,
                "endpoint": intent.endpoint,
                "approved": human_decision.approved,
                "note": human_decision.note,
            }
        )
        self._emit_runlog(
            {"run_id": run_id},
            "approval_decision",
            {
                "agent_id": agent_id,
                "provider": intent.provider,
                "endpoint": intent.endpoint,
                "call_type": intent.call_type,
                "estimated_cost_usd": float(intent.estimated_cost_usd),
                "reason": intent.reason,
                "approved": human_decision.approved,
                "note": human_decision.note,
            },
        )
        return ApiCallDecision(approved=human_decision.approved, note=human_decision.note)

    async def _tool_callback(
        self,
        intent: ToolCallIntent,
        agent_id: str,
        run_id: str,
        tool_events: list[dict[str, Any]],
    ) -> ToolCallResult:
        """Asynchronously run tool callback."""
        manifest = self.registry.get_manifest(agent_id)
        allowlist = manifest.tools_allowlist if manifest else []
        try:
            result = self.tool_executor.execute(intent.tool, intent.args, allowlist)
            semantic_ok, semantic_error = self._evaluate_tool_result(intent.tool, intent.args, result)
            event = {
                "tool": intent.tool,
                "ok": semantic_ok,
                "reason": intent.reason,
                "args": intent.args,
                "agent_id": agent_id,
                "result": result,
            }
            if not semantic_ok:
                event["error"] = semantic_error
            tool_events.append(event)
            self.audit.record_event(
                "tool_call",
                {"tool": intent.tool, "args": intent.args, "ok": semantic_ok, "error": semantic_error},
                run_id=run_id,
                agent_id=agent_id,
            )
            self._emit_runlog(
                {"run_id": run_id},
                "tool_call",
                {
                    "agent_id": agent_id,
                    "tool": intent.tool,
                    "ok": semantic_ok,
                    "reason": intent.reason,
                    "args": intent.args,
                    "result": result,
                    "error": semantic_error,
                },
            )
            if not semantic_ok:
                return ToolCallResult(ok=False, result=result, error=semantic_error)
            return ToolCallResult(ok=True, result=result, error="")
        except ToolExecutionError as exc:
            event = {
                "tool": intent.tool,
                "ok": False,
                "error": str(exc),
                "reason": intent.reason,
                "args": intent.args,
                "agent_id": agent_id,
            }
            tool_events.append(event)
            self.audit.record_event(
                "tool_call",
                {
                    "tool": intent.tool,
                    "args": intent.args,
                    "ok": False,
                    "error": str(exc),
                },
                run_id=run_id,
                agent_id=agent_id,
            )
            self._emit_runlog(
                {"run_id": run_id},
                "tool_call",
                {
                    "agent_id": agent_id,
                    "tool": intent.tool,
                    "ok": False,
                    "reason": intent.reason,
                    "args": intent.args,
                    "error": str(exc),
                },
            )
            return ToolCallResult(ok=False, result={}, error=str(exc))

    def _sync_registry_index(self) -> None:
        """Run sync registry index."""
        AgentFactory.sync_registry_index(self.settings.agents_dir)

    def _compose_system_prompt(self, agent_id: str, manifest: Any | None) -> str:
        """Build the agent system prompt by composing layers.

        Layer 0 — Soul (soul.md): shared personality, philosophy, and ground rules.
        Layer 1 — Base skill modules from agents/skills/<skill_id>.md (shared library).
        Layer 2 — Agent role overlay from agents/<agent_id>/skill.md (identity + rules).

        Combining skills gives the agent broader competence. Each base skill covers
        a specific capability domain in detail; the role overlay adds identity, decision
        rules, and output format expectations. When skill_ids is empty, only the role
        overlay is used. Existing agents with no skill_ids degrade gracefully.
        """
        parts: list[str] = []

        # Layer 0: soul — shared identity and philosophy across all agents
        soul_path = self.settings.soul_md_path
        if soul_path and soul_path.exists():
            soul_content = soul_path.read_text(encoding="utf-8").strip()
            if soul_content:
                parts.append(soul_content)

        # Layer 1: load requested base skill modules from the shared skill library
        if manifest is not None:
            skill_ids: list[str] = getattr(manifest, "skill_ids", []) or []
            if skill_ids:
                skills_dir = self.settings.agents_dir / "skills"
                for skill_id in skill_ids:
                    skill_path = skills_dir / f"{skill_id}.md"
                    if skill_path.exists():
                        content = skill_path.read_text(encoding="utf-8").strip()
                        if content:
                            parts.append(content)

        # Layer 2: agent role overlay (identity, decision rules, output format)
        role_path = self.settings.agents_dir / agent_id / "skill.md"
        if role_path.exists():
            content = role_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(content)

        if not parts:
            return (
                "You are a specialist agent inside the Closed Claw orchestrator. "
                "Execute tasks accurately, use available tools efficiently, "
                "and report concrete, factual outcomes."
            )

        return "\n\n---\n\n".join(parts)

    def _request_config_for_agent(self, agent_id: str) -> dict[str, Any]:
        """Run request config for agent."""
        manifest = self.registry.get_manifest(agent_id)
        allowlist = manifest.tools_allowlist if manifest else []
        skill_ids = getattr(manifest, "skill_ids", []) or [] if manifest else []
        system_prompt = self._compose_system_prompt(agent_id, manifest)

        # Derive genome-specific overrides
        agent_loop_max_steps = min(12, self.settings.max_tool_calls_per_agent)
        llm_config = self._llm_runtime_config()
        if manifest and self.settings.evolution_enabled:
            genome_raw = getattr(manifest, "genome_json", "{}") or "{}"
            try:
                genome_dict = json.loads(genome_raw) if isinstance(genome_raw, str) else {}
            except (json.JSONDecodeError, TypeError):
                genome_dict = {}
            if genome_dict:
                genome = Genome.from_dict(genome_dict)
                agent_loop_max_steps = min(genome.max_iterations, self.settings.max_tool_calls_per_agent)
                llm_config["temperature"] = genome.temperature

        return {
            "timeout_s": self.settings.agent_timeout_sec,
            "agent_loop_max_steps": agent_loop_max_steps,
            "agent_loop_llm_retries": 2,
            "agent_loop_max_consecutive_errors": 4,
            "max_tool_calls_per_agent": self.settings.max_tool_calls_per_agent,
            "tool_registry": tool_registry_for_allowlist(allowlist),
            "system_prompt": system_prompt,
            "skill_ids": skill_ids,
            "llm": llm_config,
        }

    def _llm_runtime_config(self) -> dict[str, Any]:
        """Run llm runtime config."""
        provider = self.settings.llm_provider.lower()
        key = self.settings.llm_api_key.strip()
        base_url = ""
        if provider == "openai":
            key = key or self.settings.openai_api_key.strip()
            base_url = self.settings.openai_base_url
        elif provider == "gemini":
            key = key or self.settings.gemini_api_key.strip()
            base_url = self.settings.gemini_base_url
        elif provider == "claude":
            key = key or self.settings.anthropic_api_key.strip()
            base_url = self.settings.anthropic_base_url
        elif provider == "siemens":
            key = key or self.settings.siemens_api_key.strip()
            base_url = self.settings.siemens_base_url
        return {
            "provider": provider,
            "model": self.settings.llm_model,
            "api_key": key,
            "base_url": base_url,
            "timeout_s": self.settings.llm_timeout_sec,
        }

    @staticmethod
    def _should_skip_discovery(state: dict[str, Any]) -> bool:
        """Decide whether ``execute_task_pool`` should skip the discovery phase.

        Skip when the caller already produced an execution pool (the
        ``simple`` complexity branch in ``decompose_task``) or when only the
        legacy single ``subtask_pool`` is present.
        """
        has_discovery = bool(state.get("discovery_subtask_pool"))
        has_execution = bool(state.get("execution_subtask_pool"))
        has_legacy = bool(state.get("subtask_pool"))
        if has_discovery:
            return False
        if has_execution:
            return True
        if has_legacy:
            return True
        return False

    @staticmethod
    def _verify_acceptance_criteria(
        item: dict[str, Any],
        response_result: str,
    ) -> tuple[bool, str]:
        """Lightweight content check against a subtask's acceptance_criteria.

        Catches:
        - Self-classification failure phrases ("task could not be completed",
          "unable to complete", "cannot complete") in the agent's result text.
          The agent has a protocol path to emit ``status="error"`` directly;
          this is a safety net for when it emits status="ok" but the text is
          plainly a giving-up message.
        - An "absolute path" criterion satisfied by an unresolved ``$HOME`` or
          ``~`` literal.
        Returns ``(ok, reason)``.
        """
        text_lower = response_result.lower() if response_result else ""
        # Self-reported failure phrases — anchored to "the task / this request"
        # to avoid flagging legitimate uses of "could not" (e.g. "could not find
        # any matching rows, so the count is 0", which is a valid success).
        failure_phrases = (
            "could not be completed",
            "unable to complete the task",
            "unable to complete this task",
            "cannot complete the task",
            "cannot complete this task",
            "could not complete the task",
            "task cannot be completed",
            "task could not be completed",
        )
        if any(phrase in text_lower for phrase in failure_phrases):
            return False, "agent_self_reported_failure_in_result_text"

        criteria = item.get("acceptance_criteria", []) or []
        if not criteria:
            return True, ""
        wants_absolute_path = any(
            "absolute path" in str(c).lower() for c in criteria
        )
        if wants_absolute_path:
            if "$HOME" in response_result or "~/" in response_result:
                return False, "unresolved_env_var_in_absolute_path"
            if "/" not in response_result:
                return False, "no_absolute_path_in_result"
        return True, ""

    @staticmethod
    def _verify_subtask_tool_execution(
        item: dict[str, Any],
        new_tool_events: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """Run verify subtask tool execution."""
        if not bool(item.get("requires_tool", False)):
            return True, ""
        if not new_tool_events:
            return False, "required_tool_call_not_observed"
        if any(evt.get("ok") for evt in new_tool_events):
            return True, ""
        errors = [str(evt.get("error", "")) for evt in new_tool_events if not evt.get("ok")]
        return False, errors[-1] if errors else "all_tool_calls_failed"

    @staticmethod
    def _evaluate_tool_result(tool: str, args: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
        """Run evaluate tool result."""
        if tool in {"terminal", "python_exec"}:
            rc = int(result.get("returncode", 1))
            return (rc == 0, result.get("stderr", "") if rc != 0 else "")
        if tool in {"http_api", "web_fetch"}:
            status_code = int(result.get("status_code", 0))
            ok = 200 <= status_code < 400
            return (ok, f"http_status_{status_code}" if not ok else "")
        if tool == "file_io":
            op = str(args.get("op", "read"))
            if op == "read":
                return ("content" in result, "missing_content" if "content" not in result else "")
            if op == "write":
                ok = bool(result.get("written"))
                return (ok, "" if ok else "write_failed")
            if op == "append":
                ok = bool(result.get("appended"))
                return (ok, "" if ok else "append_failed")
            if op == "list":
                ok = isinstance(result.get("entries"), list)
                return (ok, "" if ok else "list_failed")
            return (True, "")
        if tool == "sql_query":
            return ("rows" in result, "sql_query_missing_rows")
        return (True, "")

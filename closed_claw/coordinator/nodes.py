from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from closed_claw.agents.factory import AgentFactory
from closed_claw.config import Settings
from closed_claw.embeddings.provider import EmbeddingProvider
from closed_claw.observability.runlog import RunLogger
from closed_claw.policy.approval import ApprovalGate, ApprovalRequest
from closed_claw.policy.audit import AuditStore
from closed_claw.registry.search import (
    RerankerProtocol,
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
        self.settings = settings
        self.registry = registry
        self.reranker = reranker
        self.embedder = embedder
        self.runner = runner
        self.factory = factory
        self.approval_gate = approval_gate
        self.audit = audit
        dynamic_roots = [self.settings.agents_dir.parent.parent] + self.settings.extra_allowed_paths
        self.tool_executor = ToolExecutor(
            workspace_root=self.settings.agents_dir.parent,
            allowed_roots=dynamic_roots,
        )

    @staticmethod
    def _merge(state: dict[str, Any], **updates: Any) -> dict[str, Any]:
        merged = dict(state)
        merged.update(updates)
        return merged

    def _emit_runlog(self, state: dict[str, Any], event: str, payload: dict[str, Any]) -> None:
        run_id = state.get("run_id")
        if not run_id:
            return
        RunLogger(self.settings.run_logs_dir, run_id=run_id).emit(event, payload)

    async def ingest_task(self, state: dict[str, Any]) -> dict[str, Any]:
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
            role_agent_map={},
            subtask_results={},
        )
        self._emit_runlog(merged, "task_ingested", {"task": task, "session_id": merged["session_id"]})
        return merged

    async def decompose_task(self, state: dict[str, Any]) -> dict[str, Any]:
        plan = generate_task_plan(self.settings, state["task"])
        pool = [
            {
                **item,
                "status": "waiting" if item.get("depends_on") else "pending",
                "assigned_agent_id": None,
                "result": "",
                "error": "",
            }
            for item in plan
        ]
        merged = self._merge(state, subtask_pool=pool)
        self._emit_runlog(
            merged,
            "task_plan_created",
            {"subtask_count": len(pool), "subtasks": self._task_pool_snapshot(pool)},
        )
        self._emit_task_pool(merged, pool)
        return merged

    async def embed_task(self, state: dict[str, Any]) -> dict[str, Any]:
        out = self._merge(state, query_vector=self.embedder.embed(state["task"]))
        self._emit_runlog(out, "task_embedded", {"embedding_dim": len(out["query_vector"])})
        return out

    async def semantic_search(self, state: dict[str, Any]) -> dict[str, Any]:
        candidates = self.registry.semantic_search(state["query_vector"], k=5)
        out = self._merge(
            state,
            candidates=[
                {
                    "agent_id": c.agent_id,
                    "score": c.score,
                    "reason": "semantic",
                    "description": c.description,
                }
                for c in candidates
            ],
        )
        self._emit_runlog(out, "semantic_search", {"candidate_count": len(out["candidates"])})
        return out

    async def llm_rerank(self, state: dict[str, Any]) -> dict[str, Any]:
        sem = state.get("candidates", [])
        converted = [
            SearchCandidate(
                agent_id=c["agent_id"],
                score=c["score"],
                description=c.get("description", ""),
            )
            for c in sem
        ]
        ranked = self.reranker.rerank(state["task"], converted)
        out = [{"agent_id": c.agent_id, "score": c.score, "reason": c.reason} for c in ranked]
        low = (out[0]["score"] if out else 0.0) < self.settings.low_confidence_threshold
        merged = self._merge(state, candidates=out, low_confidence=low)
        self._emit_runlog(
            merged,
            "rerank_complete",
            {"candidate_count": len(out), "low_confidence": low},
        )
        return merged

    async def human_gate_if_low_confidence(self, state: dict[str, Any]) -> dict[str, Any]:
        if not state.get("low_confidence", True):
            return self._merge(state, human_create_approved=False)
        if not self.settings.create_approval_required:
            return self._merge(state, human_create_approved=True)

        mode = (state.get("runtime_policies", {}) or {}).get(
            "create_approval_mode", self.settings.create_approval_mode
        )
        decision = self.approval_gate.decide_create_with_mode(
            mode=mode,
            run_id=state["run_id"],
            top_candidate=(state.get("candidates") or [{}])[0],
        )
        approved = decision.approved

        self.audit.record_event(
            "create_gate_decision",
            {
                "approved": approved,
                "top_candidate": (state.get("candidates") or [{}])[0],
                "threshold": self.settings.low_confidence_threshold,
                "mode": mode,
            },
            run_id=state["run_id"],
            agent_id=None,
        )
        self._emit_runlog(state, "create_gate_decision", {"approved": approved, "mode": mode})
        return self._merge(state, human_create_approved=approved)

    async def decide_reuse_or_create(self, state: dict[str, Any]) -> dict[str, Any]:
        candidates = state.get("candidates", [])
        if not candidates:
            return self._merge(state, decision="create")
        top = candidates[0]
        if state.get("low_confidence", True) and state.get("human_create_approved", False):
            return self._merge(state, decision="create")
        return self._merge(state, decision="reuse", selected_agent_id=top["agent_id"])

    async def create_agent_if_needed(self, state: dict[str, Any]) -> dict[str, Any]:
        if state["decision"] != "create":
            return state
        task = state["task"]
        profile = self._select_capability_profile(task)
        reusable_agent_id = self._find_reusable_capability_agent(profile)
        if reusable_agent_id:
            merged = self._merge(
                state,
                selected_agent_id=reusable_agent_id,
                reused_capability_profile=profile["profile_id"],
            )
            self._emit_runlog(
                merged,
                "agent_reused_by_capability",
                {
                    "agent_id": reusable_agent_id,
                    "profile_id": profile["profile_id"],
                    "profile_name": profile["name_prefix"],
                },
            )
            return merged

        name = f"{profile['name_prefix']} {uuid.uuid4().hex[:4]}"
        description = profile["description"]
        tool_allowlist = profile["tools_allowlist"]
        skill_content = profile["skill_md"]
        manifest = self.factory.create_capsule(
            name=name,
            description=description,
            embedding_model=self.settings.embedding_model,
            embedding_vector=self.embedder.embed(description),
            tools_allowlist=tool_allowlist,
            tags=profile["tags"],
            api_capabilities=profile.get("api_capabilities", []),
            requires_approval_for=profile.get("requires_approval_for", []),
            skill_content=skill_content,
        )
        self.registry.upsert_manifest(manifest)
        self._sync_registry_index()
        merged = self._merge(
            state,
            selected_agent_id=manifest.agent_id,
            created_agent_description=manifest.description,
            created_agent={
                "agent_id": manifest.agent_id,
                "name": manifest.name,
                "description": manifest.description,
                "profile_id": profile["profile_id"],
                "tools_allowlist": manifest.tools_allowlist,
                "skill_md": skill_content,
            },
        )
        self._emit_runlog(
            merged,
            "agent_created",
            {"agent_id": manifest.agent_id, "name": manifest.name, "description": manifest.description},
        )
        return merged

    async def execute_task_pool(self, state: dict[str, Any]) -> dict[str, Any]:
        pool = [dict(item) for item in state.get("subtask_pool", [])]
        if not pool:
            pool = [
                {
                    "task_id": "task-1",
                    "title": "Execute User Task",
                    "description": state["task"],
                    "role_tag": "general-operator",
                    "depends_on": [],
                    "acceptance_criteria": ["Task is completed and reported."],
                    "status": "pending",
                    "assigned_agent_id": None,
                    "result": "",
                    "error": "",
                }
            ]

        approvals = list(state.get("approvals", []))
        tool_events = list(state.get("tool_events", []))
        api_mode = (state.get("runtime_policies", {}) or {}).get(
            "api_approval_mode", self.settings.api_approval_mode
        )
        role_agent_map: dict[str, str] = dict(state.get("role_agent_map", {}))
        created_agents: list[dict[str, Any]] = list(state.get("created_agents", []))
        subtask_results: dict[str, str] = dict(state.get("subtask_results", {}))

        by_id = {item["task_id"]: item for item in pool}
        waiting_cycles = 0
        while True:
            if self._is_cancel_requested(state["run_id"]):
                for item in pool:
                    if item["status"] in {"waiting", "pending"}:
                        item["status"] = "cancelled"
                        item["error"] = "cancelled_by_user"
                self._emit_task_pool(state, pool)
                self._emit_runlog(
                    state,
                    "run_cancelled",
                    {"reason": "cancel_file_detected"},
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
            for item in ready:
                progressed = True
                role_tag = str(item.get("role_tag", "general-operator")) or "general-operator"
                if role_tag not in role_agent_map:
                    agent_id, created = self._acquire_agent_for_role(role_tag, item)
                    role_agent_map[role_tag] = agent_id
                    if created:
                        created_agents.append(created)
                agent_id = role_agent_map[role_tag]
                item["assigned_agent_id"] = agent_id
                item["status"] = "in_progress"
                self._emit_task_pool(state, pool)

                dep_context = {
                    dep: by_id[dep].get("result", "")
                    for dep in item.get("depends_on", [])
                    if dep in by_id
                }
                criteria = item.get("acceptance_criteria", [])
                task_payload = (
                    f"{item.get('description', '')}\n\n"
                    "Acceptance criteria:\n"
                    + "\n".join(f"- {c}" for c in criteria)
                ).strip()
                req = CoordinatorRequest(
                    session_id=state["session_id"],
                    task=task_payload,
                    context={
                        **(state.get("context", {}) or {}),
                        "parent_task": state["task"],
                        "subtask": {
                            "task_id": item["task_id"],
                            "title": item.get("title", ""),
                            "role_tag": role_tag,
                            "depends_on_results": dep_context,
                        },
                    },
                    config=self._request_config_for_agent(agent_id),
                )
                entrypoint = self.settings.agents_dir / agent_id / "entrypoint.py"
                tool_events_start = len(tool_events)
                try:
                    response = await self.runner.run_agent(
                        agent_id=agent_id,
                        entrypoint=entrypoint,
                        request=req,
                        approval_callback=lambda intent, a_id: self._approval_callback(
                            intent=intent,
                            agent_id=a_id,
                            run_id=state["run_id"],
                            approvals=approvals,
                            mode=api_mode,
                        ),
                        tool_callback=lambda intent, a_id: self._tool_callback(
                            intent=intent,
                            agent_id=a_id,
                            run_id=state["run_id"],
                            tool_events=tool_events,
                        ),
                    )
                    if response.status == "ok":
                        new_events = tool_events[tool_events_start:]
                        verification_ok, verification_msg = self._verify_subtask_tool_execution(
                            item=item,
                            new_tool_events=new_events,
                        )
                        if verification_ok:
                            item["status"] = "completed"
                            item["result"] = response.result
                            subtask_results[item["task_id"]] = response.result
                        else:
                            item["status"] = "failed"
                            item["error"] = verification_msg or "filesystem_verification_failed"
                    else:
                        item["status"] = "failed"
                        item["error"] = response.error_message or "subtask_failed"
                except AgentRuntimeError as exc:
                    item["status"] = "failed"
                    item["error"] = str(exc)
                self._emit_task_pool(state, pool)

            if progressed:
                waiting_cycles = 0
                continue

            waiting_cycles += 1
            self._emit_task_pool(state, pool)
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
        if completed:
            result = "\n".join(
                [
                    "Sub-task results:",
                    *[
                        f"- [{t['task_id']}] ({t.get('role_tag','')}) {t.get('title','')}: {t.get('result','')}"
                        for t in completed
                    ],
                ]
            )
        else:
            result = ""

        return self._merge(
            state,
            response_status=status,
            response_result=result,
            response_error=(
                ""
                if status == "ok"
                else (
                    "cancelled_by_user"
                    if cancelled
                    else "One or more subtasks failed"
                )
            ),
            selected_agent_id=next(iter(role_agent_map.values()), state.get("selected_agent_id", "unknown")),
            role_agent_map=role_agent_map,
            created_agents=created_agents,
            subtask_pool=pool,
            subtask_results=subtask_results,
            approvals=approvals,
            tool_events=tool_events,
        )

    def _select_capability_profile(self, task: str) -> dict[str, Any]:
        return generate_agent_profile(
            settings=self.settings,
            task=task,
            supported_tools=SUPPORTED_TOOLS,
            fallback_tools=SUPPORTED_TOOLS,
        )

    def _find_reusable_capability_agent(self, profile: dict[str, Any]) -> str | None:
        # Reuse by capability first to avoid creating niche one-off agents.
        query_vector = self.embedder.embed(profile["description"])
        candidates = self.registry.semantic_search(query_vector, k=10)
        profile_id = str(profile.get("profile_id", ""))
        for cand in candidates:
            manifest = self.registry.get_manifest(cand.agent_id)
            if manifest is None or manifest.status != "active":
                continue
            tags = set(manifest.tags)
            if profile_id in tags:
                return manifest.agent_id
        return None

    def _acquire_agent_for_role(
        self,
        role_tag: str,
        subtask: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        role_prompt = (
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
        }
        return manifest.agent_id, created_agent

    def _task_pool_snapshot(self, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "task_id": item.get("task_id"),
                "title": item.get("title"),
                "role_tag": item.get("role_tag"),
                "depends_on": item.get("depends_on", []),
                "status": item.get("status"),
                "assigned_agent_id": item.get("assigned_agent_id"),
                "error": item.get("error", ""),
            }
            for item in pool
        ]

    def _emit_task_pool(self, state: dict[str, Any], pool: list[dict[str, Any]]) -> None:
        self._emit_runlog(
            state,
            "task_pool_update",
            {"tasks": self._task_pool_snapshot(pool)},
        )

    def _is_cancel_requested(self, run_id: str) -> bool:
        cancel_path = self.settings.run_logs_dir / f"{run_id}.cancel"
        return cancel_path.exists()

    async def dispatch_agents_async(self, state: dict[str, Any]) -> dict[str, Any]:
        selected = state["selected_agent_id"]
        candidate_ids = [selected] + [
            c["agent_id"]
            for c in state.get("candidates", [])
            if c["agent_id"] != selected
        ]

        for agent_id in candidate_ids:
            manifest_path = self.settings.agents_dir / agent_id / "manifest.json"
            entrypoint = self.settings.agents_dir / agent_id / "entrypoint.py"
            if not manifest_path.exists() or not entrypoint.exists():
                continue

            req = CoordinatorRequest(
                session_id=state["session_id"],
                task=state["task"],
                context=state.get("context", {}),
                config=self._request_config_for_agent(agent_id),
            )
            approvals = list(state.get("approvals", []))
            tool_events = list(state.get("tool_events", []))
            api_mode = (state.get("runtime_policies", {}) or {}).get(
                "api_approval_mode", self.settings.api_approval_mode
            )
            try:
                response = await self.runner.run_agent(
                    agent_id=agent_id,
                    entrypoint=entrypoint,
                    request=req,
                    approval_callback=lambda intent, a_id: self._approval_callback(
                        intent=intent,
                        agent_id=a_id,
                        run_id=state["run_id"],
                        approvals=approvals,
                        mode=api_mode,
                    ),
                    tool_callback=lambda intent, a_id: self._tool_callback(
                        intent=intent,
                        agent_id=a_id,
                        run_id=state["run_id"],
                        tool_events=tool_events,
                    ),
                )
                merged = self._merge(
                    state,
                    selected_agent_id=agent_id,
                    response_status=response.status,
                    response_result=response.result,
                    response_error=response.error_message or "",
                    response_latency_ms=response.metrics.latency_ms or 0.0,
                    memory_updates=response.memory_updates,
                    artifacts=response.artifacts,
                    approvals=approvals,
                    tool_events=tool_events,
                )
                self._emit_runlog(
                    merged,
                    "agent_run_complete",
                    {
                        "agent_id": agent_id,
                        "status": response.status,
                        "approvals": len(approvals),
                        "tool_calls": len(tool_events),
                    },
                )
                return merged
            except AgentRuntimeError as exc:
                failed = list(state.get("failed_agents", []))
                failed.append(agent_id)
                state["failed_agents"] = failed
                self.audit.record_event(
                    "agent_run_failure",
                    {"agent_id": agent_id, "error": str(exc)},
                    run_id=state["run_id"],
                    agent_id=agent_id,
                )
                self._emit_runlog(state, "agent_run_failure", {"agent_id": agent_id, "error": str(exc)})

        merged = self._merge(
            state,
            response_status="error",
            response_result="",
            response_error="All candidate agents failed",
            response_latency_ms=0.0,
            memory_updates=[],
            artifacts=[],
        )
        self._emit_runlog(merged, "all_candidates_failed", {"failed_agents": merged.get("failed_agents", [])})
        return merged

    async def validate_outputs(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("response_status") not in {"ok", "error"}:
            return self._merge(
                state,
                response_status="error",
                response_error="invalid_agent_response",
            )
        return state

    async def approval_gate_for_api_calls(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def continue_or_deny_api_path(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def update_registry_and_audit(self, state: dict[str, Any]) -> dict[str, Any]:
        self.registry.record_run(
            run_id=state["run_id"],
            agent_id=state.get("selected_agent_id", "unknown"),
            task=state["task"],
            status=state.get("response_status", "error"),
            latency_ms=state.get("response_latency_ms"),
            error_message=state.get("response_error"),
        )
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
            },
        )
        return state

    async def synthesize_final_response(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("response_status") == "ok":
            return self._merge(state, response_result=state.get("response_result", ""))
        return self._merge(
            state,
            response_result=(
                "Unable to complete task via current agents. "
                f"Reason: {state.get('response_error', 'unknown')}"
            ),
        )

    async def failure_recovery(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def _approval_callback(
        self,
        intent: ApiCallIntent,
        agent_id: str,
        run_id: str,
        approvals: list[dict[str, Any]],
        mode: str,
    ) -> ApiCallDecision:
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
        if not human_decision.approved:
            self.registry.open_circuit_if_needed(
                provider=intent.provider,
                threshold=self.settings.circuit_breaker_failures,
            )
        else:
            self.registry.reset_circuit(intent.provider)

        approvals.append(
            {
                "provider": intent.provider,
                "endpoint": intent.endpoint,
                "approved": human_decision.approved,
                "note": human_decision.note,
            }
        )
        return ApiCallDecision(approved=human_decision.approved, note=human_decision.note)

    async def _tool_callback(
        self,
        intent: ToolCallIntent,
        agent_id: str,
        run_id: str,
        tool_events: list[dict[str, Any]],
    ) -> ToolCallResult:
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
                    "result": {
                        "returncode": result.get("returncode") if isinstance(result, dict) else None,
                    },
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
        manifests: list[AgentManifest] = []
        for path in self.settings.agents_dir.glob("*/manifest.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                manifests.append(AgentManifest.model_validate(data))
            except Exception:
                continue
        AgentFactory.save_registry_index(self.settings.agents_dir / "registry.json", manifests)

    def _request_config_for_agent(self, agent_id: str) -> dict[str, Any]:
        manifest = self.registry.get_manifest(agent_id)
        allowlist = manifest.tools_allowlist if manifest else []
        return {
            "timeout_s": self.settings.agent_timeout_sec,
            "agent_loop_max_steps": 12,
            "agent_loop_llm_retries": 2,
            "agent_loop_max_consecutive_errors": 4,
            "tool_registry": tool_registry_for_allowlist(allowlist),
            "llm": self._llm_runtime_config(),
        }

    def _llm_runtime_config(self) -> dict[str, Any]:
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
        return {
            "provider": provider,
            "model": self.settings.llm_model,
            "api_key": key,
            "base_url": base_url,
            "timeout_s": self.settings.llm_timeout_sec,
        }

    @staticmethod
    def _verify_subtask_tool_execution(
        item: dict[str, Any],
        new_tool_events: list[dict[str, Any]],
    ) -> tuple[bool, str]:
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
                return (bool(result.get("written")), "write_failed")
            if op == "append":
                return (bool(result.get("appended")), "append_failed")
            return (True, "")
        if tool == "sql_query":
            return ("rows" in result, "sql_query_missing_rows")
        return (True, "")

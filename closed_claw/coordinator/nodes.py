from __future__ import annotations

import json
import uuid
from typing import Any

from closed_claw.agents.factory import AgentFactory
from closed_claw.config import Settings
from closed_claw.embeddings.provider import EmbeddingProvider
from closed_claw.observability.runlog import RunLogger
from closed_claw.policy.approval import ApprovalGate, ApprovalRequest
from closed_claw.policy.audit import AuditStore
from closed_claw.registry.search import RerankerProtocol
from closed_claw.registry.store import AgentManifest, RegistryStore, SearchCandidate
from closed_claw.runtime.protocol import (
    ApiCallDecision,
    ApiCallIntent,
    CoordinatorRequest,
    ToolCallIntent,
    ToolCallResult,
)
from closed_claw.runtime.runner import AgentRunner, AgentRuntimeError
from closed_claw.tools.executor import ToolExecutionError, ToolExecutor


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
        )
        self._emit_runlog(merged, "task_ingested", {"task": task, "session_id": merged["session_id"]})
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
            api_capabilities=["external_paid_api"],
            requires_approval_for=["external_paid_api"],
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

    @staticmethod
    def _infer_general_tool_allowlist(task: str) -> list[str]:
        base = {"file_io"}
        t = task.lower()
        if any(word in t for word in ["shell", "terminal", "command", "cli"]):
            base.add("terminal")
        if any(word in t for word in ["api", "http", "endpoint", "request"]):
            base.add("http_api")
        if any(word in t for word in ["web", "fetch", "url", "site"]):
            base.add("web_fetch")
        if any(word in t for word in ["python", "script", "code"]):
            base.add("python_exec")
        if any(word in t for word in ["sql", "query", "database", "sqlite"]):
            base.add("sql_query")
        # reasonable general-purpose baseline for spawned agents
        base.update({"terminal", "http_api"})
        return sorted(base)

    def _select_capability_profile(self, task: str) -> dict[str, Any]:
        t = task.lower()
        if any(word in t for word in ["file", "folder", "directory", "organize", "organise", "rename", "move"]):
            return {
                "profile_id": "filesystem_terminal_expert",
                "name_prefix": "Filesystem Terminal Expert",
                "description": (
                    "Terminal-first filesystem expert for inspecting, organizing, moving, and validating files "
                    "and directories safely."
                ),
                "tools_allowlist": ["terminal", "file_io", "python_exec"],
                "tags": ["auto", "capability", "filesystem_terminal_expert", "terminal", "filesystem"],
                "skill_md": (
                    "# Filesystem Terminal Expert\n\n"
                    "You are a terminal and filesystem operations expert.\n"
                    "Use shell-first workflows for listing, classifying, moving, renaming, and validating files.\n"
                    "Before mutating files: inspect paths, show plan, then execute safely and report exact changes.\n"
                ),
            }
        if any(word in t for word in ["api", "endpoint", "http", "request", "rest", "webhook"]):
            return {
                "profile_id": "api_integration_expert",
                "name_prefix": "API Integration Expert",
                "description": (
                    "API integration expert for HTTP endpoints, request design, retries, validation, and response "
                    "analysis."
                ),
                "tools_allowlist": ["http_api", "web_fetch", "python_exec", "terminal"],
                "tags": ["auto", "capability", "api_integration_expert", "api"],
                "skill_md": (
                    "# API Integration Expert\n\n"
                    "You design and execute reliable API calls with clear validation and error handling.\n"
                    "Explain assumptions, inspect payloads, and return structured outputs.\n"
                ),
            }
        if any(word in t for word in ["web", "website", "url", "search", "scrape", "fetch"]):
            return {
                "profile_id": "web_research_expert",
                "name_prefix": "Web Research Expert",
                "description": "Web research expert for gathering, validating, and summarizing online information.",
                "tools_allowlist": ["web_fetch", "http_api", "python_exec"],
                "tags": ["auto", "capability", "web_research_expert", "web"],
                "skill_md": (
                    "# Web Research Expert\n\n"
                    "You gather web evidence, verify sources, and synthesize concise factual outputs.\n"
                    "Prioritize source quality and date-aware reasoning.\n"
                ),
            }
        if any(word in t for word in ["sql", "database", "query", "sqlite", "table"]):
            return {
                "profile_id": "data_sql_expert",
                "name_prefix": "Data SQL Expert",
                "description": "Data and SQL expert for query writing, schema inspection, and analytical workflows.",
                "tools_allowlist": ["sql_query", "python_exec", "file_io", "terminal"],
                "tags": ["auto", "capability", "data_sql_expert", "sql", "data"],
                "skill_md": (
                    "# Data SQL Expert\n\n"
                    "You are an expert in SQL and data analysis workflows.\n"
                    "Write safe queries, validate assumptions, and present clear analytical results.\n"
                ),
            }

        return {
            "profile_id": "general_terminal_operator",
            "name_prefix": "General Terminal Operator",
            "description": (
                "General operations expert that executes diverse technical tasks using terminal, files, and "
                "automation tools."
            ),
            "tools_allowlist": self._infer_general_tool_allowlist(task),
            "tags": ["auto", "capability", "general_terminal_operator", "generalist"],
            "skill_md": (
                "# General Terminal Operator\n\n"
                "You execute broad technical tasks with pragmatic terminal-first workflows.\n"
                "Break work into steps, run safely, and report concrete outcomes.\n"
            ),
        }

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
                config={"timeout_s": self.settings.agent_timeout_sec},
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
            event = {
                "tool": intent.tool,
                "ok": True,
                "reason": intent.reason,
            }
            tool_events.append(event)
            self.audit.record_event(
                "tool_call",
                {"tool": intent.tool, "args": intent.args, "ok": True},
                run_id=run_id,
                agent_id=agent_id,
            )
            return ToolCallResult(ok=True, result=result, error="")
        except ToolExecutionError as exc:
            event = {
                "tool": intent.tool,
                "ok": False,
                "error": str(exc),
                "reason": intent.reason,
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

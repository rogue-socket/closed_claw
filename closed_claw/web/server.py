# Purpose: FastAPI server powering the Closed Claw web dashboard.

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger("closed_claw.web")
from fastapi.responses import HTMLResponse, StreamingResponse

from closed_claw.agents.factory import AgentFactory, AgentManifest
from closed_claw.web.serializers import shape_agent_row
from closed_claw.config import Settings
from closed_claw.coordinator.graph import build_graph
from closed_claw.policy.approval import get_pending_approvals, resolve_approval
from closed_claw.policy.audit import AuditStore
from closed_claw.registry.store import RegistryStore
from closed_claw.setup_wizard import upsert_env, verify_provider
from closed_claw.tools.executor import TOOL_REGISTRY, SUPPORTED_TOOLS


PIPELINE_STAGES = [
    "ingest_task",
    "decompose_task",
    "execute_task_pool",
    "validate_outputs",
    "update_registry_and_audit",
    "synthesize_final_response",
]

PIPELINE_EVENTS = {
    "task_ingested": "ingest_task",
    "task_embedded": "decompose_task",
    "task_plan_created": "decompose_task",
    "task_pool_update": "execute_task_pool",
    "subtask_attempt_started": "execute_task_pool",
    "subtask_attempt_succeeded": "execute_task_pool",
    "subtask_attempt_failed": "execute_task_pool",
    "agent_run_complete": "execute_task_pool",
    "tool_call": "execute_task_pool",
    "approval_request": "execute_task_pool",
    "api_gate_decision": "execute_task_pool",
    "all_candidates_failed": "validate_outputs",
    "run_summary": "update_registry_and_audit",
    "synthesis_complete": "synthesize_final_response",
    "failure_recovery": "synthesize_final_response",
}


def _schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "registry" / "schema.sql"


def _make_registry(settings: Settings) -> RegistryStore | None:
    if not settings.db_path.exists():
        return None
    try:
        return RegistryStore(
            db_path=settings.db_path,
            schema_path=_schema_path(),
            embedding_dim=settings.embedding_dim,
            require_sqlite_vec=False,
        )
    except Exception:
        logger.warning("Failed to create RegistryStore", exc_info=True)
        return None


def _make_audit(settings: Settings) -> AuditStore | None:
    if not settings.db_path.exists():
        return None
    try:
        return AuditStore(settings.db_path)
    except Exception:
        logger.warning("Failed to create AuditStore", exc_info=True)
        return None


def _safe_fromiso(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=datetime.UTC)
        return parsed.astimezone(datetime.UTC)
    except Exception:
        return None


def _within_range(created_at: str | None, range_key: str) -> bool:
    if range_key in ("all", ""):
        return True
    dt = _safe_fromiso(created_at)
    if dt is None:
        return False
    now = datetime.datetime.now(datetime.UTC)
    delta_map = {
        "15m": datetime.timedelta(minutes=15),
        "1h": datetime.timedelta(hours=1),
        "6h": datetime.timedelta(hours=6),
        "24h": datetime.timedelta(hours=24),
        "7d": datetime.timedelta(days=7),
    }
    delta = delta_map.get(range_key, datetime.timedelta(hours=1))
    return dt >= (now - delta)


def _read_runlog(settings: Settings, run_id: str) -> list[dict[str, Any]]:
    log_path = settings.run_logs_dir / f"{run_id}.jsonl"
    if not log_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _derive_stage_from_events(events: list[dict[str, Any]]) -> tuple[str, str | None]:
    current = "ingest_task"
    failed_stage: str | None = None
    for evt in events:
        name = str(evt.get("event", ""))
        if name in PIPELINE_EVENTS:
            current = PIPELINE_EVENTS[name]
        if name in {"subtask_attempt_failed", "all_candidates_failed", "failure_recovery", "error"}:
            failed_stage = PIPELINE_EVENTS.get(name, current)
    return current, failed_stage


def _run_analysis(settings: Settings, run_id: str, fallback_status: str = "unknown") -> dict[str, Any]:
    events = _read_runlog(settings, run_id)
    stage_rows = [
        {
            "name": stage,
            "events": 0,
            "errors": 0,
            "duration_ms": None,
            "started_at": None,
            "ended_at": None,
            "tool_calls": 0,
            "model_calls": 0,
        }
        for stage in PIPELINE_STAGES
    ]
    stage_idx = {s["name"]: i for i, s in enumerate(stage_rows)}

    tool_calls: list[dict[str, Any]] = []
    model_calls: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    failure_reason = ""
    # Collect richer root-cause details
    _tool_errors: list[str] = []        # unique tool error messages
    _tool_error_set: set[str] = set()   # dedup
    _synthesis_error: str = ""          # synthesis_complete.error_summary
    _subtask_failures: list[str] = []   # subtask-level failure descriptions

    current_stage = "ingest_task"
    for evt in events:
        name = str(evt.get("event", ""))
        payload = evt.get("payload", {}) if isinstance(evt.get("payload"), dict) else {}
        ts = evt.get("ts")
        if name in PIPELINE_EVENTS:
            current_stage = PIPELINE_EVENTS[name]
        row = stage_rows[stage_idx[current_stage]]
        row["events"] += 1
        if row["started_at"] is None:
            row["started_at"] = ts
        row["ended_at"] = ts

        if name in {"subtask_attempt_failed", "all_candidates_failed", "failure_recovery", "error"}:
            row["errors"] += 1
            if not failure_reason:
                failure_reason = str(payload.get("error") or payload.get("reason") or name)
            if name == "subtask_attempt_failed":
                tid = payload.get("task_id", "?")
                err = payload.get("error", "unknown")
                attempt = payload.get("attempt", "?")
                _subtask_failures.append(f"Subtask '{tid}' attempt {attempt}: {err}")

        if name == "tool_call":
            row["tool_calls"] += 1
            tool_error = payload.get("error") or ""
            tool_calls.append({
                "ts": ts,
                "tool": payload.get("tool"),
                "ok": bool(payload.get("ok", False)),
                "reason": payload.get("reason") or "",
                "args": payload.get("args") or {},
                "result": payload.get("result") or {},
                "error": tool_error,
            })
            if tool_error and not payload.get("ok") and tool_error not in _tool_error_set:
                _tool_error_set.add(tool_error)
                tool_name = payload.get("tool") or "unknown"
                args_preview = str(payload.get("args") or {})[:200]
                _tool_errors.append(f"Tool '{tool_name}' failed: {tool_error} (args: {args_preview})")

        if name in {"approval_request", "approval_decision", "api_gate_decision"}:
            model_calls.append({
                "ts": ts,
                "type": name,
                "provider": payload.get("provider") or "",
                "endpoint": payload.get("endpoint") or "",
                "approved": payload.get("approved"),
                "reason": payload.get("reason") or "",
            })

        # Capture agent subprocess LLM calls (api_call_intent logged by runner)
        if name == "api_call_intent" or (name == "agent_run_complete" and payload.get("call_type")):
            row["model_calls"] += 1
            model_calls.append({
                "ts": ts,
                "type": payload.get("call_type") or name,
                "provider": payload.get("provider") or "",
                "endpoint": payload.get("endpoint") or "",
                "approved": payload.get("approved"),
                "reason": payload.get("reason") or "",
            })

        if name == "tool_call" and payload.get("tool") == "file_io":
            artifacts.append({
                "ts": ts,
                "kind": "file_io",
                "summary": payload.get("reason") or "file operation",
                "detail": payload.get("args") or {},
            })

        # Capture broader artifacts: agent responses, synthesis, and any event with artifacts
        if name == "agent_run_complete" and payload.get("result"):
            artifacts.append({
                "ts": ts,
                "kind": "agent_output",
                "summary": f"Agent {payload.get('agent_id', '?')} completed",
                "detail": {"result_preview": str(payload.get("result", ""))[:500]},
            })
        if name == "synthesis_complete":
            artifacts.append({
                "ts": ts,
                "kind": "synthesis",
                "summary": "Final synthesis output",
                "detail": {"result_preview": str(payload.get("result", ""))[:500]},
            })
        if name == "run_summary":
            artifacts.append({
                "ts": ts,
                "kind": "run_summary",
                "summary": f"Run completed: {payload.get('status', '?')}",
                "detail": payload,
            })

        if name == "synthesis_complete":
            _synthesis_error = str(payload.get("error_summary") or "")

    # ── Build rich failure reason ──────────────────────────────────────────
    if _tool_errors or _subtask_failures or _synthesis_error:
        parts: list[str] = []
        if _tool_errors:
            parts.append("ROOT CAUSE — Tool errors:")
            for te in _tool_errors:
                parts.append(f"  • {te}")
        if _subtask_failures:
            parts.append("Subtask failures:")
            for sf in _subtask_failures[:10]:
                parts.append(f"  • {sf}")
        if _synthesis_error:
            parts.append(f"Synthesis: {_synthesis_error[:500]}")
        failure_reason = "\n".join(parts)

    for row in stage_rows:
        start_dt = _safe_fromiso(row["started_at"])
        end_dt = _safe_fromiso(row["ended_at"])
        if start_dt and end_dt:
            row["duration_ms"] = round((end_dt - start_dt).total_seconds() * 1000)

    current, failed_stage = _derive_stage_from_events(events)
    status = fallback_status
    if events:
        if any(e.get("event") == "run_summary" and (e.get("payload") or {}).get("status") in {"ok", "completed"} for e in events):
            status = "ok"
        elif any(e.get("event") in {"failure_recovery", "error", "all_candidates_failed"} for e in events):
            status = "error"

    return {
        "run_id": run_id,
        "status": status,
        "current_stage": current,
        "failed_stage": failed_stage,
        "failure_reason": failure_reason,
        "stages": stage_rows,
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "artifacts": artifacts,
        "events": events,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()

    app = FastAPI(title="Closed Claw Dashboard", version="1.0")

    # ----- in-process tracking for runs launched via the web UI -----
    _active_runs: dict[str, dict[str, Any]] = {}
    _active_runs_lock = threading.Lock()
    # Maps run_id → {"task": str, "status": "running"|"completed"|"error",
    #                 "current_node": str, "result": str, "error": str,
    #                 "started_at": str, "finished_at": str|None,
    #                 "asyncio_task": asyncio.Task}

    # ------------------------------------------------------------------ health
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        result: dict[str, Any] = {
            "db_exists": settings.db_path.exists(),
            "agents_dir_exists": settings.agents_dir.exists(),
            "run_logs_dir_exists": settings.run_logs_dir.exists(),
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "require_sqlite_vec": settings.require_sqlite_vec,
            "sqlite_vec_ok": False,
            "langgraph_ok": False,
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
        }
        # langgraph
        try:
            import langgraph  # noqa: F401
            result["langgraph_ok"] = True
        except ImportError:
            pass
        # sqlite-vec
        try:
            import sqlite3 as _s3
            conn = _s3.connect(":memory:")
            conn.enable_load_extension(True)
            try:
                import sqlite_vec
                sqlite_vec.load(conn)
                result["sqlite_vec_ok"] = True
            finally:
                conn.enable_load_extension(False)
        except Exception:
            pass
        # key presence
        result["openai_key_set"] = bool(settings.openai_api_key)
        result["gemini_key_set"] = bool(settings.gemini_api_key)
        result["anthropic_key_set"] = bool(settings.anthropic_api_key)
        result["siemens_key_set"] = bool(settings.siemens_api_key)
        return result

    # --------------------------------------------------------- health/verify
    @app.post("/api/health/verify")
    def health_verify() -> dict[str, Any]:
        """Live-test the active LLM provider connection."""
        provider = settings.llm_provider
        model = settings.llm_model
        key_map = {
            "openai": settings.openai_api_key,
            "gemini": settings.gemini_api_key,
            "claude": settings.anthropic_api_key,
            "siemens": settings.siemens_api_key,
        }
        api_key = key_map.get(provider, "") or ""

        if not api_key:
            return {
                "provider": provider,
                "model": model,
                "verified": False,
                "message": f"No API key configured for {provider}",
                "latency_ms": 0,
            }

        start = time.time()
        try:
            verified, message = verify_provider(
                provider=provider, model=model, api_key=api_key
            )
        except Exception as exc:
            verified, message = False, str(exc)
        latency_ms = round((time.time() - start) * 1000)

        return {
            "provider": provider,
            "model": model,
            "verified": verified,
            "message": message,
            "latency_ms": latency_ms,
        }

    # -------------------------------------------------------- settings/apikey
    @app.post("/api/settings/apikey")
    async def save_api_key(request: Request) -> dict[str, Any]:
        """Verify an API key for the active provider, save to .env if valid."""
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
        if not api_key:
            return {"verified": False, "message": "No API key provided", "saved": False}

        provider = settings.llm_provider
        model = settings.llm_model

        start = time.time()
        try:
            verified, message = verify_provider(
                provider=provider, model=model, api_key=api_key
            )
        except Exception as exc:
            verified, message = False, str(exc)
        latency_ms = round((time.time() - start) * 1000)

        if not verified:
            return {
                "verified": False,
                "message": message,
                "saved": False,
                "latency_ms": latency_ms,
            }

        # Save to .env
        env_key_map = {
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "siemens": "SIEMENS_API_KEY",
        }
        env_var = env_key_map.get(provider)
        if not env_var:
            return {
                "verified": True,
                "message": message,
                "saved": False,
                "latency_ms": latency_ms,
                "error": f"Unknown provider: {provider}",
            }

        env_path = Path.cwd() / ".env"
        upsert_env(env_path, {env_var: api_key})

        # Reload settings in-place so subsequent requests pick up the new key
        new_settings = Settings.from_env()
        for field in (
            "openai_api_key", "gemini_api_key", "anthropic_api_key",
            "siemens_api_key", "llm_api_key",
        ):
            object.__setattr__(settings, field, getattr(new_settings, field))

        return {
            "verified": True,
            "message": message,
            "saved": True,
            "latency_ms": latency_ms,
            "provider": provider,
        }

    # -------------------------------------------------------------- system
    @app.get("/api/system")
    def system_info() -> dict[str, Any]:
        """Return useful system metadata."""
        import importlib.metadata
        import platform
        import sys

        provider = settings.llm_provider
        key_map = {
            "openai": settings.openai_api_key,
            "gemini": settings.gemini_api_key,
            "claude": settings.anthropic_api_key,
            "siemens": settings.siemens_api_key,
        }
        active_key_set = bool(key_map.get(provider, ""))

        # count agents / runs
        agent_count = 0
        run_count = 0
        reg = _make_registry(settings)
        if reg:
            try:
                agent_count = len(reg.list_agents(limit=100_000))
                run_count = len(reg.list_runs(limit=100_000))
            except Exception:
                pass

        # installed package versions (use metadata to avoid heavy imports)
        pkg_map = {
            "langgraph": "langgraph",
            "pydantic": "pydantic",
            "fastapi": "fastapi",
            "uvicorn": "uvicorn",
            "httpx": "httpx",
            "sqlite_vec": "sqlite-vec",
            "sentence_transformers": "sentence-transformers",
        }
        pkg_versions: dict[str, str] = {}
        for display_name, dist_name in pkg_map.items():
            try:
                pkg_versions[display_name] = importlib.metadata.version(dist_name)
            except importlib.metadata.PackageNotFoundError:
                pkg_versions[display_name] = "not installed"

        return {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "llm_provider": provider,
            "llm_model": settings.llm_model,
            "active_key_set": active_key_set,
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
            "db_path": str(settings.db_path),
            "agents_dir": str(settings.agents_dir),
            "agent_count": agent_count,
            "run_count": run_count,
            "packages": pkg_versions,
        }

    @app.get("/api/system/status")
    def system_status() -> dict[str, Any]:
        health_data = health()
        pending = get_pending_approvals()
        connected = bool(health_data.get("db_exists")) and bool(health_data.get("agents_dir_exists"))
        provider = str(health_data.get("llm_provider") or "")
        key_map = {
            "openai": bool(health_data.get("openai_key_set")),
            "gemini": bool(health_data.get("gemini_key_set")),
            "claude": bool(health_data.get("anthropic_key_set")),
            "siemens": bool(health_data.get("siemens_key_set")),
        }
        provider_ready = key_map.get(provider, False)
        sqlite_ok = bool(health_data.get("sqlite_vec_ok") or not health_data.get("require_sqlite_vec"))

        if connected and provider_ready and sqlite_ok:
            status = "connected"
        elif connected:
            status = "degraded"
        else:
            status = "offline"

        return {
            "status": status,
            "pending_approvals": len(pending),
            "checks": {
                "coordinator": connected,
                "llm": provider_ready,
                "tool_backend": sqlite_ok,
            },
        }

    # ----------------------------------------------------------- settings
    # Settings that are safe to expose and edit from the UI.  Each entry maps
    # a UI-friendly key to (env_var_name, type, description, current_value).
    def _settings_catalog() -> list[dict[str, Any]]:
        """Build the list of editable settings with current values."""
        return [
            # ── Paths ──
            {"key": "db_path", "env": "CLOSED_CLAW_DB_PATH", "type": "path",
             "label": "Database Path", "description": "SQLite registry database location",
             "value": str(settings.db_path), "group": "Paths"},
            {"key": "agents_dir", "env": "CLOSED_CLAW_AGENTS_DIR", "type": "path",
             "label": "Agents Directory", "description": "Capsule agent storage folder",
             "value": str(settings.agents_dir), "group": "Paths"},
            {"key": "run_logs_dir", "env": "CLOSED_CLAW_RUN_LOGS_DIR", "type": "path",
             "label": "Run Logs Directory", "description": "JSONL run event log storage",
             "value": str(settings.run_logs_dir), "group": "Paths"},
            {"key": "extra_allowed_paths", "env": "CLOSED_CLAW_EXTRA_ALLOWED_PATHS", "type": "text",
             "label": "Extra Allowed Paths", "description": "Comma-separated safe folder paths for file_io tool",
             "value": ",".join(str(p) for p in settings.extra_allowed_paths), "group": "Paths"},
            # ── LLM ──
            {"key": "llm_provider", "env": "CLOSED_CLAW_LLM_PROVIDER", "type": "select",
             "label": "LLM Provider", "description": "Active LLM provider",
             "value": settings.llm_provider, "options": ["openai", "gemini", "claude", "siemens"],
             "group": "LLM"},
            {"key": "llm_model", "env": "CLOSED_CLAW_LLM_MODEL", "type": "text",
             "label": "LLM Model", "description": "Model identifier sent to provider",
             "value": settings.llm_model, "group": "LLM"},
            {"key": "llm_timeout_sec", "env": "CLOSED_CLAW_LLM_TIMEOUT_SEC", "type": "number",
             "label": "LLM Timeout (sec)", "description": "Seconds before an LLM request times out",
             "value": settings.llm_timeout_sec, "group": "LLM"},
            {"key": "openai_base_url", "env": "OPENAI_BASE_URL", "type": "text",
             "label": "OpenAI Base URL", "description": "OpenAI-compatible API base URL",
             "value": settings.openai_base_url, "group": "LLM"},
            {"key": "gemini_base_url", "env": "GEMINI_BASE_URL", "type": "text",
             "label": "Gemini Base URL", "description": "Google Gemini API base URL",
             "value": settings.gemini_base_url, "group": "LLM"},
            {"key": "anthropic_base_url", "env": "ANTHROPIC_BASE_URL", "type": "text",
             "label": "Anthropic Base URL", "description": "Anthropic API base URL",
             "value": settings.anthropic_base_url, "group": "LLM"},
            {"key": "siemens_base_url", "env": "SIEMENS_BASE_URL", "type": "text",
             "label": "Siemens Base URL", "description": "Siemens LLM API base URL",
             "value": settings.siemens_base_url, "group": "LLM"},
            # ── Embeddings ──
            {"key": "embedding_model", "env": "CLOSED_CLAW_EMBEDDING_MODEL", "type": "text",
             "label": "Embedding Model", "description": "sentence-transformers model for agent search",
             "value": settings.embedding_model, "group": "Embeddings"},
            {"key": "embedding_dim", "env": "CLOSED_CLAW_EMBEDDING_DIM", "type": "number",
             "label": "Embedding Dimension", "description": "Vector dimension (must match model)",
             "value": settings.embedding_dim, "group": "Embeddings"},
            # ── Policy ──
            {"key": "low_confidence_threshold", "env": "CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD",
             "type": "number", "label": "Low-confidence Threshold",
             "description": "Below this score the coordinator asks for human approval (0.0–1.0)",
             "value": settings.low_confidence_threshold, "group": "Policy"},
            {"key": "create_approval_required", "env": "CLOSED_CLAW_CREATE_APPROVAL_REQUIRED",
             "type": "bool", "label": "Create Approval Required",
             "description": "Require approval before creating new agents",
             "value": settings.create_approval_required, "group": "Policy"},
            {"key": "create_approval_mode", "env": "CLOSED_CLAW_CREATE_APPROVAL_MODE",
             "type": "select", "label": "Create Approval Mode",
             "description": "How create-agent approvals are handled",
             "value": settings.create_approval_mode,
             "options": ["interactive", "approve", "deny", "web"], "group": "Policy"},
            {"key": "api_approval_mode", "env": "CLOSED_CLAW_API_APPROVAL_MODE",
             "type": "select", "label": "API Approval Mode",
             "description": "How paid-API approvals are handled",
             "value": settings.api_approval_mode,
             "options": ["interactive", "approve", "deny", "web"], "group": "Policy"},
            {"key": "api_approval_timeout_sec", "env": "CLOSED_CLAW_API_APPROVAL_TIMEOUT_SEC",
             "type": "number", "label": "Approval Timeout (sec)",
             "description": "Timeout for approval prompts",
             "value": settings.api_approval_timeout_sec, "group": "Policy"},
            {"key": "paid_api_providers", "env": "CLOSED_CLAW_PAID_API_PROVIDERS", "type": "text",
             "label": "Paid API Providers", "description": "Comma-separated providers requiring approval",
             "value": ",".join(sorted(settings.paid_api_providers)), "group": "Policy"},
            # ── Runtime ──
            {"key": "agent_timeout_sec", "env": "CLOSED_CLAW_AGENT_TIMEOUT_SEC", "type": "number",
             "label": "Agent Timeout (sec)", "description": "Max seconds an agent subprocess may run",
             "value": settings.agent_timeout_sec, "group": "Runtime"},
            {"key": "agent_retries", "env": "CLOSED_CLAW_AGENT_RETRIES", "type": "number",
             "label": "Agent Retries", "description": "Times to retry a failed agent before giving up",
             "value": settings.agent_retries, "group": "Runtime"},
            {"key": "subtask_max_attempts", "env": "CLOSED_CLAW_SUBTASK_MAX_ATTEMPTS", "type": "number",
             "label": "Subtask Max Attempts", "description": "Max attempts per subtask in task pool",
             "value": settings.subtask_max_attempts, "group": "Runtime"},
            {"key": "circuit_breaker_failures", "env": "CLOSED_CLAW_CIRCUIT_BREAKER_FAILURES",
             "type": "number", "label": "Circuit Breaker Threshold",
             "description": "Consecutive failures before an agent circuit opens",
             "value": settings.circuit_breaker_failures, "group": "Runtime"},
            {"key": "circuit_breaker_reset_sec", "env": "CLOSED_CLAW_CIRCUIT_BREAKER_RESET_SEC",
             "type": "number", "label": "Circuit Breaker Reset (sec)",
             "description": "Seconds before a tripped circuit resets",
             "value": settings.circuit_breaker_reset_sec, "group": "Runtime"},
            {"key": "task_pool_poll_interval_sec", "env": "CLOSED_CLAW_TASK_POOL_POLL_INTERVAL_SEC",
             "type": "number", "label": "Task Pool Poll (sec)",
             "description": "Polling interval between task pool checks",
             "value": settings.task_pool_poll_interval_sec, "group": "Runtime"},
            {"key": "require_sqlite_vec", "env": "CLOSED_CLAW_REQUIRE_SQLITE_VEC",
             "type": "bool", "label": "Require sqlite-vec",
             "description": "Fail if sqlite-vec extension is not available",
             "value": settings.require_sqlite_vec, "group": "Runtime"},
        ]

    @app.get("/api/settings")
    def get_settings() -> list[dict[str, Any]]:
        """Return all configurable settings with current values."""
        return _settings_catalog()

    @app.post("/api/settings")
    async def save_settings(request: Request) -> dict[str, Any]:
        """Save one or more settings to .env and reload.

        Accepts ``{"changes": {"env_var": "value", ...}}`` where keys are
        the ``env`` names from the settings catalog.
        """
        body = await request.json()
        changes: dict[str, str] = body.get("changes") or {}
        if not changes:
            return {"saved": 0}

        # Validate: only allow known env var names
        catalog = _settings_catalog()
        valid_envs = {s["env"] for s in catalog}
        filtered = {k: str(v) for k, v in changes.items() if k in valid_envs}
        if not filtered:
            raise HTTPException(status_code=400, detail="No valid settings keys provided")

        env_path = Path.cwd() / ".env"
        upsert_env(env_path, filtered)

        # Reload all settings in-place
        new = Settings.from_env()
        for field in new.__dataclass_fields__:
            try:
                object.__setattr__(settings, field, getattr(new, field))
            except Exception:
                pass

        return {"saved": len(filtered), "keys": list(filtered.keys())}

    # ------------------------------------------------------------------ agents
    @app.get("/api/agents")
    def list_agents(limit: int = 200) -> list[dict[str, Any]]:
        if not settings.db_path.exists():
            return []
        try:
            import sqlite3 as _sq
            conn = _sq.connect(settings.db_path)
            conn.row_factory = _sq.Row
            rows = conn.execute(
                """
                SELECT agent_id, name, description, status, usage_count, success_count,
                       failure_count, success_rate, avg_latency_ms, last_used_at, created_at,
                       tags_json, tools_allowlist_json, api_capabilities_json, skill_ids_json, version
                FROM agents
                ORDER BY usage_count DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            conn.close()
            return [shape_agent_row(dict(r)) for r in rows]
        except Exception:
            return []

    @app.get("/api/agents/{agent_id}")
    def get_agent(agent_id: str) -> dict[str, Any]:
        reg = _make_registry(settings)
        if reg is None:
            raise HTTPException(status_code=404, detail="Registry not initialised")
        m = reg.get_manifest(agent_id)
        if m is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        d = m.model_dump()
        d.pop("embedding_vector", None)  # don't ship huge vectors
        # try to load skill.md
        skill_path = settings.agents_dir / agent_id / "skill.md"
        if skill_path.exists():
            d["skill_md"] = skill_path.read_text(encoding="utf-8")
        return d

    # -------------------------------------------------------- delete agent
    @app.delete("/api/agents/{agent_id}")
    def delete_agent(agent_id: str) -> dict[str, Any]:
        """Delete a single agent from registry + disk."""
        reg = _make_registry(settings)
        if reg is None:
            raise HTTPException(status_code=500, detail="Registry not initialised")
        existed = reg.delete_agent(agent_id)
        capsule_dir = settings.agents_dir / agent_id
        if capsule_dir.exists():
            shutil.rmtree(capsule_dir)
        _sync_registry_index(settings)
        if not existed:
            raise HTTPException(status_code=404, detail="Agent not found")
        return {"deleted": True, "agent_id": agent_id}

    # -------------------------------------------------- bulk delete agents
    @app.post("/api/agents/delete-bulk")
    async def delete_agents_bulk(request: Request) -> dict[str, Any]:
        """Delete agents in bulk.  Accepts {"exclude_ids": [...]} to keep selected agents."""
        body = await request.json()
        exclude_ids: set[str] = set(body.get("exclude_ids") or [])
        reg = _make_registry(settings)
        if reg is None:
            raise HTTPException(status_code=500, detail="Registry not initialised")
        agents = reg.list_agents(limit=100_000)
        deleted_ids: list[str] = []
        for item in agents:
            aid = str(item.get("agent_id", ""))
            if not aid or aid in exclude_ids:
                continue
            reg.delete_agent(aid)
            capsule_dir = settings.agents_dir / aid
            if capsule_dir.exists():
                shutil.rmtree(capsule_dir)
            deleted_ids.append(aid)
        # Clean stray capsule dirs not in registry
        if settings.agents_dir.exists():
            for capsule in settings.agents_dir.iterdir():
                if not capsule.is_dir():
                    continue
                if capsule.name in deleted_ids or capsule.name in exclude_ids:
                    continue
                if (capsule / "manifest.json").exists():
                    shutil.rmtree(capsule)
                    deleted_ids.append(capsule.name)
        _sync_registry_index(settings)
        return {"deleted_count": len(deleted_ids), "deleted_ids": deleted_ids}

    # ------------------------------------------------------------------ skills
    @app.get("/api/skills")
    def list_skills() -> list[dict[str, Any]]:
        """List all available base skill modules from agents/skills/ directory."""
        skills_dir = settings.agents_dir / "skills"
        if not skills_dir.exists():
            return []
        skills: list[dict[str, Any]] = []
        for md_file in sorted(skills_dir.glob("*.md")):
            skill_id = md_file.stem
            content = md_file.read_text(encoding="utf-8").strip()
            # Extract first heading line as label
            first_line = content.split("\n", 1)[0].strip().lstrip("#").strip() if content else skill_id
            # Count which agents use this skill
            agent_count = 0
            for a in list_agents(limit=10000):
                ids_raw = a.get("skill_ids", "[]")
                ids = json.loads(ids_raw) if isinstance(ids_raw, str) else (ids_raw or [])
                if skill_id in ids:
                    agent_count += 1
            skills.append({
                "skill_id": skill_id,
                "label": first_line or skill_id,
                "content_preview": content[:500],
                "content_length": len(content),
                "agent_count": agent_count,
            })
        return skills

    @app.get("/api/skills/{skill_id}")
    def get_skill(skill_id: str) -> dict[str, Any]:
        """Return full content of a skill module."""
        skill_path = settings.agents_dir / "skills" / f"{skill_id}.md"
        if not skill_path.exists():
            raise HTTPException(status_code=404, detail="Skill not found")
        content = skill_path.read_text(encoding="utf-8")
        first_line = content.strip().split("\n", 1)[0].strip().lstrip("#").strip() if content.strip() else skill_id
        # Which agents use this skill
        using_agents: list[dict[str, str]] = []
        for a in list_agents(limit=10000):
            ids_raw = a.get("skill_ids", "[]")
            ids = json.loads(ids_raw) if isinstance(ids_raw, str) else (ids_raw or [])
            if skill_id in ids:
                using_agents.append({"agent_id": a["agent_id"], "name": a.get("name", a["agent_id"])})
        return {
            "skill_id": skill_id,
            "label": first_line,
            "content": content,
            "using_agents": using_agents,
        }

    # ------------------------------------------------------------------- runs
    @app.get("/api/runs")
    def list_runs(limit: int = 200) -> list[dict[str, Any]]:
        reg = _make_registry(settings)
        if reg is None:
            return []
        try:
            return reg.list_runs(limit=limit)
        except Exception:
            return []

    @app.get("/api/runs/enriched")
    def list_runs_enriched(limit: int = 200, range: str = "24h") -> list[dict[str, Any]]:
        runs = list_runs(limit=limit)
        active_map = {r.get("run_id"): r for r in list_active_runs()}
        enriched: list[dict[str, Any]] = []
        for run in runs:
            run_id = str(run.get("run_id", ""))
            if not run_id:
                continue
            if not _within_range(run.get("created_at"), range):
                continue
            active = active_map.get(run_id)
            analysis = _run_analysis(settings, run_id, fallback_status=str(run.get("status") or "unknown"))
            current_stage = str(analysis.get("current_stage") or "ingest_task")
            if active and active.get("status") == "running":
                current_stage = str(active.get("current_node") or current_stage)

            enriched.append({
                **run,
                "status": active.get("status") if active else run.get("status"),
                "current_stage": current_stage,
                "failed_stage": analysis.get("failed_stage"),
                "failure_reason": analysis.get("failure_reason") or run.get("error_message") or "",
            })
        return enriched

    @app.get("/api/runs/{run_id}/analysis")
    def run_analysis(run_id: str) -> dict[str, Any]:
        status_data = get_run_status(run_id)
        analysis = _run_analysis(settings, run_id, fallback_status=str(status_data.get("status") or "unknown"))
        return {
            **analysis,
            "task": status_data.get("task", ""),
            "started_at": status_data.get("started_at"),
            "finished_at": status_data.get("finished_at"),
            "pending_approvals": status_data.get("pending_approvals", []),
            "result": status_data.get("result", ""),
            "error": status_data.get("error", ""),
        }

    # ------------------------------------------------------------------ runlog
    @app.get("/api/runlog/{run_id}")
    def get_runlog(run_id: str, tail: int = 1000) -> list[dict[str, Any]]:
        log_path = settings.run_logs_dir / f"{run_id}.jsonl"
        if not log_path.exists():
            return []
        lines = log_path.read_text(encoding="utf-8").splitlines()
        lines = lines[-tail:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
        return out

    @app.get("/api/runlog/{run_id}/stream")
    def stream_runlog(run_id: str) -> StreamingResponse:
        log_path = settings.run_logs_dir / f"{run_id}.jsonl"

        def _gen() -> Generator[str, None, None]:
            byte_offset = 0
            idle_ticks = 0
            while idle_ticks < 120:  # ~60s timeout after last event
                if log_path.exists():
                    with open(log_path, "r", encoding="utf-8") as fh:
                        fh.seek(byte_offset)
                        new_data = fh.read()
                        byte_offset = fh.tell()
                    if new_data:
                        idle_ticks = 0
                        for line in new_data.splitlines():
                            if line.strip():
                                yield f"data: {line}\n\n"
                    else:
                        idle_ticks += 1
                else:
                    idle_ticks += 1
                time.sleep(0.5)
            yield "data: {\"event\":\"stream_end\"}\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    # ------------------------------------------------------------------ audit
    @app.get("/api/audit")
    def list_audit(limit: int = 200) -> list[dict[str, Any]]:
        store = _make_audit(settings)
        if store is None:
            return []
        try:
            return store.list_events(limit=limit)
        except Exception:
            return []

    # ------------------------------------------------------------------ tools
    @app.get("/api/tools")
    def list_tools() -> list[dict[str, Any]]:
        out = []
        for name in SUPPORTED_TOOLS:
            entry = TOOL_REGISTRY.get(name, {})
            out.append({
                "name": name,
                "description": entry.get("description", ""),
                "args_schema": entry.get("args_schema", {}),
                "enabled": True,
            })
        return out

    @app.post("/api/tools/{tool_name}/test")
    async def test_tool(tool_name: str) -> dict[str, Any]:
        if tool_name not in SUPPORTED_TOOLS:
            raise HTTPException(status_code=404, detail="Unknown tool")
        # Safe sandbox-style validation endpoint to support UI health checks.
        entry = TOOL_REGISTRY.get(tool_name, {})
        return {
            "tool": tool_name,
            "ok": True,
            "mode": "sandbox",
            "message": "Tool is registered and available for coordinator-mediated execution.",
            "args_schema": entry.get("args_schema", {}),
        }

    # ------------------------------------------------------------------ stats
    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        reg = _make_registry(settings)
        if reg is None:
            return {"agents": 0, "runs": 0, "success_rate": 0.0, "run_logs": 0}
        try:
            agents = reg.list_agents(limit=10000)
            runs = reg.list_runs(limit=10000)
            ok = sum(1 for r in runs if r.get("status") == "ok")
            rate = round(ok / len(runs) * 100, 1) if runs else 0.0
            log_count = len(list(settings.run_logs_dir.glob("*.jsonl"))) if settings.run_logs_dir.exists() else 0
            return {
                "agents": len(agents),
                "runs": len(runs),
                "success_rate": rate,
                "run_logs": log_count,
            }
        except Exception:
            return {"agents": 0, "runs": 0, "success_rate": 0.0, "run_logs": 0}

    # ---------------------------------------------------------------- frontend
    ui_path = Path(__file__).resolve().parent / "ui.html"

    # --------------------------------------------------------- POST /api/runs
    @app.post("/api/runs")
    async def create_run(request: Request) -> dict[str, Any]:
        """Start a new coordinator run from the web UI.

        Accepts ``{"task": "...", "context": {...}}`` and returns ``{"run_id": "..."}``
        immediately.  The coordinator graph executes in a background thread so it
        does not block the uvicorn event loop.
        """
        body = await request.json()
        task = (body.get("task") or "").strip()
        if not task:
            raise HTTPException(status_code=400, detail="task is required")
        context = body.get("context") or {}

        from datetime import datetime, UTC

        run_id = uuid.uuid4().hex
        # Snapshot settings so mid-run web changes don't affect this run
        import dataclasses
        run_settings = dataclasses.replace(settings)
        initial_state = {
            "run_id": run_id,
            "session_id": uuid.uuid4().hex[:12],
            "task": task,
            "context": context,
            "runtime_policies": {
                "create_approval_mode": "web",
                "api_approval_mode": "web",
            },
        }

        with _active_runs_lock:
            _active_runs[run_id] = {
                "task": task,
                "status": "running",
                "current_node": "queued",
                "result": "",
                "error": "",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
            }

        def _run_graph() -> None:
            """Execute the coordinator graph in a dedicated thread with its own event loop."""
            try:
                app_graph = build_graph(run_settings)
                with _active_runs_lock:
                    _active_runs[run_id]["current_node"] = "ingest_task"
                result = asyncio.run(app_graph.ainvoke(initial_state))
                with _active_runs_lock:
                    _active_runs[run_id].update({
                        "status": result.get("response_status", "completed"),
                        "result": result.get("response_result", ""),
                        "error": result.get("response_error", ""),
                        "current_node": "__end__",
                        "finished_at": datetime.now(UTC).isoformat(),
                    })
            except Exception as exc:
                with _active_runs_lock:
                    _active_runs[run_id].update({
                        "status": "error",
                        "error": str(exc),
                        "current_node": "__error__",
                        "finished_at": datetime.now(UTC).isoformat(),
                    })

        thread = threading.Thread(target=_run_graph, daemon=True)
        thread.start()

        return {"run_id": run_id, "status": "running"}

    # --------------------------------------------------- GET /api/runs/active
    @app.get("/api/runs/active")
    def list_active_runs() -> list[dict[str, Any]]:
        """Return all runs launched from the web UI with their live status."""
        out = []
        with _active_runs_lock:
            for rid, info in _active_runs.items():
                out.append({
                    "run_id": rid,
                    "task": info["task"],
                    "status": info["status"],
                    "current_node": info.get("current_node", "unknown"),
                    "started_at": info.get("started_at"),
                    "finished_at": info.get("finished_at"),
                    "error": info.get("error", ""),
                })
        return out

    # ------------------------------------------- GET /api/runs/{run_id}/status
    @app.get("/api/runs/{run_id}/status")
    def get_run_status(run_id: str) -> dict[str, Any]:
        """Return live status for a run including pipeline position."""
        active = _active_runs.get(run_id)
        if active:
            # Derive current_node from the latest runlog event
            current_node = active.get("current_node", "unknown")
            log_path = settings.run_logs_dir / f"{run_id}.jsonl"
            if log_path.exists():
                current_node = _derive_pipeline_node(log_path)
                with _active_runs_lock:
                    active["current_node"] = current_node
            return {
                "run_id": run_id,
                "task": active["task"],
                "status": active["status"],
                "current_node": current_node,
                "started_at": active.get("started_at"),
                "finished_at": active.get("finished_at"),
                "result": active.get("result", ""),
                "error": active.get("error", ""),
                "pending_approvals": get_pending_approvals(run_id),
            }
        # Fall back to DB for historic runs
        reg = _make_registry(settings)
        if reg:
            runs = reg.list_runs(limit=100_000)
            match = next((r for r in runs if r.get("run_id") == run_id), None)
            if match:
                return {
                    "run_id": run_id,
                    "task": match.get("task", ""),
                    "status": match.get("status", "unknown"),
                    "current_node": "__end__",
                    "started_at": match.get("created_at"),
                    "finished_at": match.get("created_at"),
                    "result": "",
                    "error": match.get("error_message", ""),
                    "pending_approvals": [],
                }
        raise HTTPException(status_code=404, detail="Run not found")

    # ------------------------------------------ GET /api/approvals/pending
    @app.get("/api/approvals/pending")
    def list_pending_approvals(run_id: str | None = None) -> list[dict[str, Any]]:
        """Return pending approval requests, optionally filtered by run_id."""
        return get_pending_approvals(run_id)

    # ---------------------------------------- POST /api/approvals/{id}/decide
    @app.post("/api/approvals/{approval_id}/decide")
    async def decide_approval(approval_id: str, request: Request) -> dict[str, Any]:
        """Submit user decision for a pending approval."""
        body = await request.json()
        approved = bool(body.get("approved", False))
        note = body.get("note", "")
        ok = resolve_approval(approval_id, approved=approved, note=note)
        if not ok:
            raise HTTPException(status_code=404, detail="Approval request not found or already resolved")
        return {"approval_id": approval_id, "approved": approved, "resolved": True}

    # -------------------------------------------------------- cancel run
    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, Any]:
        """Request graceful cancellation of a run."""
        settings.run_logs_dir.mkdir(parents=True, exist_ok=True)
        cancel_path = settings.run_logs_dir / f"{run_id}.cancel"
        cancel_path.write_text("cancel_requested\n", encoding="utf-8")
        # Also update in-memory status if tracked
        with _active_runs_lock:
            if run_id in _active_runs:
                _active_runs[run_id]["status"] = "cancelled"
        return {"cancel_requested": True, "run_id": run_id}

    # -------------------------------------------------- SSE global event stream
    @app.get("/api/events/stream")
    def global_event_stream() -> StreamingResponse:
        """SSE stream of dashboard-level state changes.

        Pushes lightweight JSON messages whenever active-run status, pending
        approvals, or system status change so the UI can react without polling.
        """
        def _gen() -> Generator[str, None, None]:
            prev_snapshot: str = ""
            idle_ticks = 0
            while idle_ticks < 600:  # ~5min inactivity timeout
                snapshot_data: dict[str, Any] = {
                    "active_runs": [
                        {
                            "run_id": rid,
                            "status": info["status"],
                            "current_node": info.get("current_node", "unknown"),
                            "task": (info.get("task") or "")[:120],
                            "started_at": info.get("started_at"),
                            "finished_at": info.get("finished_at"),
                            "error": (info.get("error") or "")[:200],
                        }
                        for rid, info in list(_active_runs.items())
                    ],
                    "pending_approvals": len(get_pending_approvals()),
                }
                cur_snapshot = json.dumps(snapshot_data, sort_keys=True)
                if cur_snapshot != prev_snapshot:
                    prev_snapshot = cur_snapshot
                    yield f"data: {json.dumps(snapshot_data)}\n\n"
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    # Send keepalive comment every 15s of no-change to prevent
                    # proxy/browser timeouts
                    if idle_ticks % 30 == 0:
                        yield ":keepalive\n\n"
                time.sleep(0.5)
            yield f"data: {json.dumps({'event': 'stream_end'})}\n\n"

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------- init
    @app.post("/api/init")
    def init_system() -> dict[str, Any]:
        """Initialise Closed Claw directories and build coordinator graph."""
        settings.ensure_dirs()
        graph = build_graph(settings)
        return {"ok": True, "graph_type": type(graph).__name__}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        if not ui_path.exists():
            return HTMLResponse("<h1>UI not found</h1>", status_code=500)
        return HTMLResponse(ui_path.read_text(encoding="utf-8"))

    return app


def _sync_registry_index(settings: Settings) -> None:
    """Rebuild agents/registry.json from on-disk manifests."""
    manifests: list[AgentManifest] = []
    for path in settings.agents_dir.glob("*/manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifests.append(AgentManifest.model_validate(data))
        except Exception:
            continue
    AgentFactory.save_registry_index(settings.agents_dir / "registry.json", manifests)


def _derive_pipeline_node(log_path: Path) -> str:
    """Read a run's JSONL log and derive the current pipeline node."""
    current = "ingest_task"
    try:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
                evt_name = event.get("event", "")
                if evt_name in PIPELINE_EVENTS:
                    current = PIPELINE_EVENTS[evt_name]
            except Exception:
                pass
    except Exception:
        pass
    return current


def run_server(host: str = "127.0.0.1", port: int = 7860, reload: bool = False) -> None:
    """Start the Closed Claw dashboard server."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required: pip install uvicorn") from exc

    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(app, host=host, port=port, reload=reload)

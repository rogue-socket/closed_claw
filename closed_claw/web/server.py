# Purpose: FastAPI server powering the Closed Claw web dashboard.

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from closed_claw.config import Settings
from closed_claw.policy.audit import AuditStore
from closed_claw.registry.store import RegistryStore
from closed_claw.tools.executor import TOOL_REGISTRY, SUPPORTED_TOOLS


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
        return None


def _make_audit(settings: Settings) -> AuditStore | None:
    if not settings.db_path.exists():
        return None
    try:
        return AuditStore(settings.db_path)
    except Exception:
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings.from_env()

    app = FastAPI(title="Closed Claw Dashboard", version="1.0")

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
            import sqlite_vec
            sqlite_vec.load(conn)
            result["sqlite_vec_ok"] = True
        except Exception:
            pass
        # key presence
        result["openai_key_set"] = bool(settings.openai_api_key)
        result["gemini_key_set"] = bool(settings.gemini_api_key)
        result["anthropic_key_set"] = bool(settings.anthropic_api_key)
        return result

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
                       tags_json, tools_allowlist_json, api_capabilities_json, version
                FROM agents
                ORDER BY usage_count DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(r)
                d["tags"] = d.pop("tags_json", "[]") or "[]"
                d["tools_allowlist"] = d.pop("tools_allowlist_json", "[]") or "[]"
                d["api_capabilities"] = d.pop("api_capabilities_json", "[]") or "[]"
                out.append(d)
            return out
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
            seen = 0
            idle_ticks = 0
            while idle_ticks < 120:  # ~60s timeout after last event
                if log_path.exists():
                    lines = log_path.read_text(encoding="utf-8").splitlines()
                    new = lines[seen:]
                    if new:
                        idle_ticks = 0
                        for line in new:
                            yield f"data: {line}\n\n"
                        seen += len(new)
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
            })
        return out

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

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        if not ui_path.exists():
            return HTMLResponse("<h1>UI not found</h1>", status_code=500)
        return HTMLResponse(ui_path.read_text(encoding="utf-8"))

    return app


def run_server(host: str = "127.0.0.1", port: int = 7860, reload: bool = False) -> None:
    """Start the Closed Claw dashboard server."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required: pip install uvicorn") from exc

    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(app, host=host, port=port, reload=reload)

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from closed_claw.config import Settings
from closed_claw.agents.factory import AgentFactory, ENTRYPOINT_TEMPLATE
from closed_claw.coordinator.graph import build_graph
from closed_claw.interactive import run_main_menu
from closed_claw.policy.audit import AuditStore
from closed_claw.registry.store import AgentManifest, RegistryStore
from closed_claw.setup_wizard import run_setup_wizard
from closed_claw.tools.executor import SUPPORTED_TOOLS


def _schema_path(settings: Settings) -> Path:
    return Path(__file__).resolve().parent / "registry" / "schema.sql"


def _registry(settings: Settings, require_sqlite_vec: bool | None = None) -> RegistryStore:
    return RegistryStore(
        db_path=settings.db_path,
        schema_path=_schema_path(settings),
        embedding_dim=settings.embedding_dim,
        require_sqlite_vec=settings.require_sqlite_vec if require_sqlite_vec is None else require_sqlite_vec,
    )


def _load_context(context_json: str | None) -> dict[str, Any]:
    if not context_json:
        return {}
    value = context_json.strip()
    if value.startswith("{"):
        parsed = json.loads(value)
    else:
        parsed = json.loads(Path(value).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("context_json must decode to a JSON object")
    return parsed


def cmd_setup(_: argparse.Namespace) -> int:
    run_setup_wizard(Path(".env"))
    return 0


def cmd_init(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    settings.ensure_dirs()
    _migrate_legacy_agents(settings)
    graph = build_graph(settings)
    print(f"Initialized Closed Claw with graph: {type(graph).__name__}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if getattr(args, "llm_provider", None) or getattr(args, "llm_model", None):
        settings = replace(
            settings,
            llm_provider=(getattr(args, "llm_provider", None) or settings.llm_provider).lower(),
            llm_model=getattr(args, "llm_model", None) or settings.llm_model,
        )
    _migrate_legacy_agents(settings)

    context_obj = _load_context(args.context_json)
    organize_path = getattr(args, "organize_path", None)
    if organize_path:
        context_obj["organize_options"] = {
            "path": organize_path,
            "dry_run": bool(getattr(args, "organize_dry_run", False)),
            "recursive": bool(getattr(args, "organize_recursive", False)),
        }

    app_graph = build_graph(settings)
    run_id = uuid.uuid4().hex
    print(f"Run started: {run_id}")
    print(f"To gracefully stop this run loop: python -m closed_claw.cli cancel-run {run_id}")
    initial_state = {
        "run_id": run_id,
        "session_id": args.session_id or uuid.uuid4().hex[:12],
        "task": args.task,
        "context": context_obj,
        "runtime_policies": {
            "create_approval_mode": args.create_approval_mode or settings.create_approval_mode,
            "api_approval_mode": args.api_approval_mode or settings.api_approval_mode,
        },
    }

    async def _run() -> dict[str, Any]:
        run_log = settings.run_logs_dir / f"{run_id}.jsonl"
        stop_event = asyncio.Event()
        last_snapshot = ""

        async def monitor() -> None:
            nonlocal last_snapshot
            seen = 0
            while not stop_event.is_set():
                if run_log.exists():
                    lines = run_log.read_text(encoding="utf-8").splitlines()
                    new_lines = lines[seen:]
                    seen = len(lines)
                    for line in new_lines:
                        try:
                            event = json.loads(line)
                        except Exception:
                            continue
                        if event.get("event") != "task_pool_update":
                            if event.get("event") == "tool_call":
                                payload = event.get("payload") or {}
                                print(
                                    "\nTool Call:"
                                    f" agent={payload.get('agent_id','-')}"
                                    f" tool={payload.get('tool','-')}"
                                    f" ok={payload.get('ok', False)}"
                                    f" reason={payload.get('reason','')}"
                                )
                                continue
                            continue
                        tasks = (event.get("payload") or {}).get("tasks", [])
                        snapshot = json.dumps(tasks, sort_keys=True)
                        if snapshot == last_snapshot:
                            continue
                        last_snapshot = snapshot
                        print("\nTask Pool (auto-update):")
                        for t in tasks:
                            deps = ",".join(t.get("depends_on", []) or [])
                            agent = t.get("assigned_agent_id") or "-"
                            print(
                                f"- [{t.get('status','unknown')}] {t.get('task_id','?')} "
                                f"tag={t.get('role_tag','-')} deps=[{deps}] agent={agent} "
                                f"title={t.get('title','')}"
                            )
                await asyncio.sleep(0.2)

        monitor_task = asyncio.create_task(monitor())
        try:
            return await app_graph.ainvoke(initial_state)
        finally:
            stop_event.set()
            with contextlib.suppress(Exception):
                await monitor_task

    import contextlib
    result = asyncio.run(_run())
    created_agents = result.get("created_agents", [])
    created_agent = result.get("created_agent")
    if created_agents:
        print("Coordinator update: Created new capability agents for plan roles.")
        for item in created_agents:
            print(
                f"- agent_id={item.get('agent_id')} role_tag={item.get('role_tag')} "
                f"name={item.get('name')} tools={item.get('tools_allowlist', [])}"
            )
    elif created_agent:
        print("Coordinator update: Created one new capability agent.")
        print(
            f"Creating agent with name='{created_agent.get('name')}', "
            f"tools={created_agent.get('tools_allowlist', [])}"
        )
        print(f"Agent created: {created_agent.get('agent_id')}")
    else:
        role_map = result.get("role_agent_map", {})
        if role_map:
            print("Coordinator update: Reused capability agents for plan roles.")
            for role_tag, agent_id in role_map.items():
                print(f"- role_tag={role_tag} agent_id={agent_id}")
        else:
            print(
                "Coordinator update: I reused an existing capability agent "
                f"({result.get('selected_agent_id', 'unknown')})."
            )

    if result.get("approvals"):
        print(f"Coordinator update: Collected {len(result.get('approvals', []))} approval decision(s).")
    else:
        print("Coordinator update: No approval was required for this run.")

    if result.get("tool_events"):
        print(f"Coordinator update: Observed {len(result.get('tool_events', []))} tool call(s).")
        for evt in result.get("tool_events", []):
            print(
                f"- tool={evt.get('tool')} ok={evt.get('ok')} "
                f"agent={evt.get('agent_id','-')} reason={evt.get('reason','')}"
            )
    else:
        print("Coordinator update: No tool call events were observed.")

    pool = result.get("subtask_pool", [])
    if pool:
        print("\nTask Pool (final):")
        for t in pool:
            deps = ",".join(t.get("depends_on", []) or [])
            agent = t.get("assigned_agent_id") or "-"
            print(
                f"- [{t.get('status','unknown')}] {t.get('task_id','?')} "
                f"tag={t.get('role_tag','-')} deps=[{deps}] agent={agent} "
                f"title={t.get('title','')}"
            )

    if result.get("response_status") == "ok":
        print("Coordinator update: Task completed successfully.")
    elif result.get("response_error") == "cancelled_by_user":
        print("Coordinator update: Run stopped gracefully by user request.")
    else:
        print(f"Coordinator update: Task failed with status={result.get('response_status')}.")

    print(
        json.dumps(
            {
                "run_id": result.get("run_id"),
                "status": result.get("response_status"),
                "result": result.get("response_result"),
                "agent_id": result.get("selected_agent_id"),
                "created_agent_description": result.get("created_agent_description"),
                "created_agent": created_agent,
                "created_agents": created_agents,
                "role_agent_map": result.get("role_agent_map", {}),
                "subtask_pool": result.get("subtask_pool", []),
                "artifacts": result.get("artifacts", []),
                "approvals": result.get("approvals", []),
                "tool_events": result.get("tool_events", []),
                "run_log": str(settings.run_logs_dir / f"{result.get('run_id')}.jsonl"),
            },
            indent=2,
        )
    )
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    base = _registry(settings, require_sqlite_vec=False).list_agents(limit=args.limit)
    data: list[dict[str, Any]] = []
    for item in base:
        enriched = dict(item)
        agent_id = str(enriched.get("agent_id", ""))
        manifest_path = settings.agents_dir / agent_id / "manifest.json"
        skill_path = settings.agents_dir / agent_id / "skill.md"
        try:
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                enriched["tools_allowlist"] = manifest.get("tools_allowlist", [])
            else:
                enriched["tools_allowlist"] = []
        except Exception:
            enriched["tools_allowlist"] = []
        try:
            enriched["skill_md"] = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
        except Exception:
            enriched["skill_md"] = ""
        data.append(enriched)
    print(json.dumps(data, indent=2))
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    agent_id = args.agent_id.strip()
    if not agent_id:
        raise SystemExit("agent_id is required")
    reg = _registry(settings, require_sqlite_vec=False)
    manifest = reg.get_manifest(agent_id)
    if manifest is None:
        print(json.dumps({"found": False, "agent_id": agent_id}, indent=2))
        return 1
    skill_path = settings.agents_dir / agent_id / "skill.md"
    memory_db = settings.agents_dir / agent_id / "memory.db"
    memory_count = None
    if memory_db.exists():
        import sqlite3
        conn = sqlite3.connect(memory_db)
        try:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            memory_count = int(row[0]) if row else 0
        except Exception:
            memory_count = None
        finally:
            conn.close()
    manifest_dict = manifest.model_dump()
    if not getattr(args, "include_embedding", False):
        manifest_dict["embedding_vector"] = f"<hidden: {len(manifest.embedding_vector)} floats>"
    payload = {
        "found": True,
        "agent": manifest_dict,
        "skill_md": skill_path.read_text(encoding="utf-8") if skill_path.exists() else "",
        "memory_entries": memory_count,
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    data = _registry(settings, require_sqlite_vec=False).list_runs(limit=args.limit)
    print(json.dumps(data, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    data = AuditStore(settings.db_path).list_events(limit=args.limit)
    print(json.dumps(data, indent=2))
    return 0


def cmd_runlog(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    path = settings.run_logs_dir / f"{args.run_id}.jsonl"
    if not path.exists():
        raise SystemExit(f"Run log not found: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.tail :]:
        print(line)
    return 0


def cmd_cancel_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    run_id = args.run_id.strip()
    if not run_id:
        raise SystemExit("run_id is required")
    settings.run_logs_dir.mkdir(parents=True, exist_ok=True)
    cancel_path = settings.run_logs_dir / f"{run_id}.cancel"
    cancel_path.write_text("cancel_requested\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "cancel_requested": True,
                "run_id": run_id,
                "cancel_file": str(cancel_path),
            },
            indent=2,
        )
    )
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    key_name = {
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "heuristic": "(not required)",
    }.get(settings.llm_provider, "CLOSED_CLAW_LLM_API_KEY")
    provider_key = {
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key,
        "claude": settings.anthropic_api_key,
    }.get(settings.llm_provider, settings.llm_api_key)
    llm_key_configured = bool(provider_key or settings.llm_api_key)
    checks: dict[str, Any] = {
        "db_path": str(settings.db_path),
        "agents_dir": str(settings.agents_dir),
        "run_logs_dir": str(settings.run_logs_dir),
        "require_sqlite_vec": settings.require_sqlite_vec,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_key_env": key_name,
        "llm_key_configured": llm_key_configured,
        "llm_key_preview": ("set(" + str(len(provider_key or settings.llm_api_key)) + " chars)") if llm_key_configured else "not set",
        "sqlite_vec_ok": False,
        "langgraph_ok": False,
    }

    try:
        reg = _registry(settings, require_sqlite_vec=settings.require_sqlite_vec)
        checks["sqlite_vec_ok"] = bool(reg.sqlite_vec_available)
    except Exception as exc:
        checks["sqlite_vec_error"] = str(exc)

    try:
        import langgraph  # type: ignore  # noqa: F401

        checks["langgraph_ok"] = True
    except Exception as exc:
        checks["langgraph_error"] = str(exc)

    print(json.dumps(checks, indent=2))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    payload: dict[str, Any] = {"supported_tools": SUPPORTED_TOOLS}
    if args.agent_id:
        manifest = _registry(settings, require_sqlite_vec=False).get_manifest(args.agent_id)
        if manifest is None:
            payload["agent"] = {"agent_id": args.agent_id, "found": False}
        else:
            payload["agent"] = {
                "agent_id": manifest.agent_id,
                "name": manifest.name,
                "description": manifest.description,
                "tools_allowlist": manifest.tools_allowlist,
            }
    print(json.dumps(payload, indent=2))
    return 0


def _sync_registry_index(settings: Settings) -> None:
    manifests: list[AgentManifest] = []
    for path in settings.agents_dir.glob("*/manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifests.append(AgentManifest.model_validate(data))
        except Exception:
            continue
    AgentFactory.save_registry_index(settings.agents_dir / "registry.json", manifests)


def _migrate_legacy_agents(settings: Settings) -> None:
    reg = _registry(settings, require_sqlite_vec=False)
    changed = False
    for manifest_path in settings.agents_dir.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = AgentManifest.model_validate(data)
        except Exception:
            continue
        capsule_dir = manifest_path.parent
        entrypoint_path = capsule_dir / "entrypoint.py"
        local_changed = False

        if "organize_by_type" in manifest.tools_allowlist:
            manifest.tools_allowlist = [t for t in manifest.tools_allowlist if t != "organize_by_type"]
            local_changed = True

        if entrypoint_path.exists():
            current = entrypoint_path.read_text(encoding="utf-8")
            if "organize_by_type" in current or "CLOSED_CLAW_ENTRYPOINT_VERSION=6" not in current:
                entrypoint_path.write_text(ENTRYPOINT_TEMPLATE, encoding="utf-8")
                local_changed = True

        if local_changed:
            manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            reg.upsert_manifest(manifest)
            changed = True

    if changed:
        _sync_registry_index(settings)


def cmd_delete_agent(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    agent_id = args.agent_id.strip()
    if not agent_id:
        raise SystemExit("agent_id is required")

    if not args.yes:
        raw = input(f"Type 'yes' to delete agent '{agent_id}': ").strip().lower()
        if raw not in {"yes", "y"}:
            print("Cancelled.")
            return 1

    registry = _registry(settings, require_sqlite_vec=False)
    existed = registry.delete_agent(agent_id)
    capsule_dir = settings.agents_dir / agent_id
    if capsule_dir.exists():
        shutil.rmtree(capsule_dir)
    _sync_registry_index(settings)

    if existed:
        print(json.dumps({"deleted": True, "agent_id": agent_id}, indent=2))
        return 0
    print(json.dumps({"deleted": False, "agent_id": agent_id, "reason": "not found in registry"}, indent=2))
    return 1


def cmd_delete_all_agents(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if not args.yes:
        raw = input("Type 'DELETE ALL' to delete every agent: ").strip()
        if raw != "DELETE ALL":
            print("Cancelled.")
            return 1

    registry = _registry(settings, require_sqlite_vec=False)
    agents = registry.list_agents(limit=100000)
    deleted_ids: list[str] = []
    for item in agents:
        agent_id = str(item.get("agent_id", ""))
        if not agent_id:
            continue
        registry.delete_agent(agent_id)
        capsule_dir = settings.agents_dir / agent_id
        if capsule_dir.exists():
            shutil.rmtree(capsule_dir)
        deleted_ids.append(agent_id)

    # Clean stray capsule dirs not in registry
    if settings.agents_dir.exists():
        for capsule in settings.agents_dir.iterdir():
            if not capsule.is_dir():
                continue
            if capsule.name in deleted_ids:
                continue
            if (capsule / "manifest.json").exists():
                shutil.rmtree(capsule)
                deleted_ids.append(capsule.name)

    _sync_registry_index(settings)
    print(json.dumps({"deleted_count": len(deleted_ids), "agent_ids": deleted_ids}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="closed_claw", description="Closed Claw coordinator CLI")
    sub = parser.add_subparsers(dest="command", required=False)

    p_setup = sub.add_parser("setup", help="Interactive LLM/provider setup wizard")
    p_setup.set_defaults(func=cmd_setup)

    p_init = sub.add_parser("init", help="Initialize local system")
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="Execute a task")
    p_run.add_argument("task", type=str)
    p_run.add_argument("--session-id", type=str, default=None)
    p_run.add_argument("--context-json", type=str, default=None)
    p_run.add_argument("--create-approval-mode", type=str, choices=["interactive", "approve", "deny"], default=None)
    p_run.add_argument("--api-approval-mode", type=str, choices=["interactive", "approve", "deny"], default=None)
    p_run.add_argument("--llm-provider", type=str, choices=["heuristic", "openai", "gemini", "claude"], default=None)
    p_run.add_argument("--llm-model", type=str, default=None)
    p_run.add_argument("--organize-path", type=str, default=None, help="Explicit folder path for organize tasks.")
    p_run.add_argument("--organize-dry-run", action="store_true", help="Preview organize task without moving files.")
    p_run.add_argument("--organize-recursive", action="store_true", help="Include nested folders for organize task.")
    p_run.set_defaults(func=cmd_run)

    p_agents = sub.add_parser("agents", help="List known agents")
    p_agents.add_argument("--limit", type=int, default=20)
    p_agents.set_defaults(func=cmd_agents)

    p_agent = sub.add_parser("agent", help="Show one agent with manifest, tools, skill, and memory stats")
    p_agent.add_argument("agent_id", type=str)
    p_agent.add_argument("--include-embedding", action="store_true", help="Include full embedding vector in output")
    p_agent.set_defaults(func=cmd_agent)

    p_runs = sub.add_parser("runs", help="List historical runs")
    p_runs.add_argument("--limit", type=int, default=20)
    p_runs.set_defaults(func=cmd_runs)

    p_audit = sub.add_parser("audit", help="List audit events")
    p_audit.add_argument("--limit", type=int, default=20)
    p_audit.set_defaults(func=cmd_audit)

    p_runlog = sub.add_parser("runlog", help="Show run log events")
    p_runlog.add_argument("run_id", type=str)
    p_runlog.add_argument("--tail", type=int, default=100)
    p_runlog.set_defaults(func=cmd_runlog)

    p_cancel = sub.add_parser("cancel-run", help="Gracefully stop a running task loop")
    p_cancel.add_argument("run_id", type=str)
    p_cancel.set_defaults(func=cmd_cancel_run)

    p_doctor = sub.add_parser("doctor", help="Validate local environment and dependencies")
    p_doctor.set_defaults(func=cmd_doctor)

    p_tools = sub.add_parser("tools", help="List supported tools and optional per-agent tool allowlist")
    p_tools.add_argument("--agent-id", type=str, default=None)
    p_tools.set_defaults(func=cmd_tools)

    p_delete = sub.add_parser("delete-agent", help="Delete an agent capsule and registry records")
    p_delete.add_argument("agent_id", type=str)
    p_delete.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_delete.set_defaults(func=cmd_delete_agent)

    p_delete_all = sub.add_parser("delete-all-agents", help="Delete all agents and capsules")
    p_delete_all.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_delete_all.set_defaults(func=cmd_delete_all_agents)

    p_menu = sub.add_parser("menu", help="Open interactive main menu")
    p_menu.set_defaults(func=None)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    handlers = {
        "setup": cmd_setup,
        "init": cmd_init,
        "doctor": cmd_doctor,
        "run": cmd_run,
        "agents": cmd_agents,
        "agent": cmd_agent,
        "runs": cmd_runs,
        "audit": cmd_audit,
        "runlog": cmd_runlog,
        "cancel_run": cmd_cancel_run,
        "tools": cmd_tools,
        "delete_agent": cmd_delete_agent,
        "delete_all_agents": cmd_delete_all_agents,
    }

    if not getattr(args, "command", None) or args.command == "menu":
        return run_main_menu(handlers)

    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

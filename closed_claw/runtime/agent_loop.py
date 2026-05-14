# Purpose: Shared ReAct loop body for agent capsules — imported by each entrypoint shim.

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

CLOSED_CLAW_ENTRYPOINT_VERSION = 14


def append_memory(memory_db: Path, session_id: str, content: str) -> None:
    """Persist one episodic memory row for the current session."""
    conn = sqlite3.connect(memory_db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          content TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "INSERT INTO memories (session_id, kind, content) VALUES (?, ?, ?)",
        (session_id, "episodic", content),
    )
    conn.commit()
    conn.close()


def extract_json(text: str) -> dict:
    """Parse and return the first JSON object found in text."""
    stripped = (text or "").strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    generic = re.search(r"(\{.*\})", stripped, flags=re.DOTALL)
    if generic:
        try:
            parsed = json.loads(generic.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def call_llm(provider: str, model: str, api_key: str, base_url: str, timeout_s: int, prompt: str) -> str:
    """Call the configured LLM provider and return response text."""
    import httpx

    provider = (provider or "").lower()
    if provider == "openai":
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 700,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    if provider == "gemini":
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1},
                },
            )
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    if provider == "claude":
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                f"{base_url.rstrip('/')}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 700,
                    "temperature": 0.1,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            parts = [p.get("text", "") for p in resp.json().get("content", []) if isinstance(p, dict)]
            return " ".join(parts)

    raise ValueError("unsupported provider")


def call_llm_with_retry(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout_s: int,
    prompt: str,
    retries: int,
) -> str:
    """Retry LLM calls with backoff and return response text."""
    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            return call_llm(provider, model, api_key, base_url, timeout_s, prompt)
        except Exception as exc:
            last_exc = exc
            if attempt < retries + 1:
                time.sleep(min(0.8 * attempt, 2.0))
    raise RuntimeError(f"llm_call_failed_after_retries:{last_exc}")


def _circuit_breaker_result(history: list[dict]) -> str | None:
    """Return a forced final_result if the last 3 history entries are the
    same successful ``(tool, args)`` — otherwise ``None``.

    The LLM sometimes loops on a successful tool call instead of emitting
    ``final``. This breaker bails out using the latest stdout (or a JSON
    dump of the result dict when stdout is empty) so the run still produces
    an answer instead of exhausting the step budget.
    """
    if len(history) < 3:
        return None
    tail = history[-3:]
    first_entry = tail[0] if isinstance(tail[0], dict) else {}
    same_call = all(
        isinstance(h, dict)
        and isinstance(h.get("tool_result"), dict)
        and h["tool_result"].get("ok")
        and h.get("tool") == first_entry.get("tool")
        and h.get("args") == first_entry.get("args")
        for h in tail
    )
    if not same_call:
        return None
    tr = tail[-1].get("tool_result", {}) or {}
    res = tr.get("result", {}) if isinstance(tr.get("result"), dict) else {}
    return str(res.get("stdout", "")).strip() or json.dumps(res)


def main(capsule_dir: Path) -> int:
    """Run the agent protocol loop for one coordinator request.

    ``capsule_dir`` is the capsule directory (where ``memory.db`` lives).
    The shim entrypoint passes ``Path(__file__).resolve().parent``.
    """
    first = sys.stdin.readline()
    if not first:
        return 1
    req = json.loads(first)
    task = req.get("task", "")
    session_id = req.get("session_id", "unknown")
    context = req.get("context", {}) or {}
    memory_db = capsule_dir / "memory.db"

    start = time.time()
    artifacts = []
    cfg = req.get("config", {}) if isinstance(req.get("config", {}), dict) else {}
    llm_cfg = cfg.get("llm", {}) if isinstance(cfg.get("llm", {}), dict) else {}
    provider = str(llm_cfg.get("provider", "")).strip().lower()
    model = str(llm_cfg.get("model", "")).strip()
    api_key = str(llm_cfg.get("api_key", "")).strip()
    base_url = str(llm_cfg.get("base_url", "")).strip()
    timeout_s = int(llm_cfg.get("timeout_s", 45))
    tool_registry = cfg.get("tool_registry", [])
    if not isinstance(tool_registry, list):
        tool_registry = []
    allowed_tools = [str(t.get("name", "")) for t in tool_registry if isinstance(t, dict) and t.get("name")]

    if provider in {"", "heuristic"} or not api_key:
        output = "Agent cannot reason about tool use without a configured LLM provider and API key."
        append_memory(memory_db, session_id, output)
        response = {
          "status": "error",
          "error_code": "agent_llm_not_configured",
          "error_message": output,
          "result": "",
          "memory_updates": [],
          "artifacts": [],
          "metrics": {"latency_ms": (time.time() - start) * 1000}
        }
        print(json.dumps(response), flush=True)
        return 0

    history: list[dict] = []
    max_steps = int(cfg.get("agent_loop_max_steps", 12))
    llm_retries = int(cfg.get("agent_loop_llm_retries", 2))
    max_consecutive_errors = int(cfg.get("agent_loop_max_consecutive_errors", 4))
    consecutive_errors = 0
    final_result = ""
    final_self_status = "ok"
    for step in range(1, max_steps + 1):
        if consecutive_errors >= max_consecutive_errors:
            break

        breaker = _circuit_breaker_result(history)
        if breaker is not None:
            final_result = breaker
            break

        # Build a prominent summary of the latest successful tool result so
        # the LLM doesn't have to dig through json.dumps(history) to find it.
        last_useful = None
        for h in reversed(history):
            if isinstance(h, dict) and isinstance(h.get("tool_result"), dict) and h["tool_result"].get("ok"):
                last_useful = h
                break
        last_summary = ""
        if last_useful is not None:
            tr = last_useful.get("tool_result", {}) or {}
            res = tr.get("result", {}) if isinstance(tr.get("result"), dict) else {}
            stdout = str(res.get("stdout", ""))[:600]
            last_summary = (
                "MOST RECENT SUCCESSFUL TOOL RESULT — tool="
                + str(last_useful.get("tool", ""))
                + ", output=" + repr(stdout) + "\n"
                + "If this output already answers the task, emit option 3 (final) NOW with that data as 'result'.\n"
            )

        strategy_hint = ""
        if history:
            recent_errors = [
                h.get("error", "")
                for h in history[-3:]
                if isinstance(h, dict) and h.get("error")
            ]
            if recent_errors:
                strategy_hint = (
                    "Previous attempts had errors: "
                    + ", ".join(str(e) for e in recent_errors)
                    + ". Choose a different strategy/tool/arguments now.\n"
                )
            failed_tools = [
                f"{h.get('tool','unknown')}:{h.get('error','unknown_error')}"
                for h in history[-5:]
                if isinstance(h, dict) and h.get("tool") and h.get("error")
            ]
            if failed_tools:
                strategy_hint += (
                    "Recent failed tool calls: "
                    + ", ".join(failed_tools)
                    + ". Avoid repeating the same failed call with identical arguments.\n"
                )

        prompt = (
            "You are an autonomous agent that can use tools via JSON intents.\n"
            "Return JSON only. Choose one action:\n"
            "1) Tool call: {\"type\":\"tool_call_intent\",\"tool\":str,\"args\":object,\"reason\":str}\n"
            "2) API call intent: {\"type\":\"api_call_intent\",\"call_type\":str,\"provider\":str,\"endpoint\":str,\"estimated_cost_usd\":float,\"reason\":str}\n"
            "3) Final: {\"type\":\"final\",\"status\":\"ok\"|\"error\",\"result\":str,\"artifacts\":list}\n"
            "   Use status=\"ok\" when result contains the answer/output the task asks for.\n"
            "   Use status=\"error\" ONLY when you have determined the task cannot be completed with the available tools "
            "(e.g., required tool missing, required data unavailable, hard constraint violated). "
            "Put the concrete reason in result. Do NOT use status=\"error\" as a shortcut for hard or unclear tasks.\n"
            f"Task: {task}\n"
            f"Context: {json.dumps(context)}\n"
            f"Available tools: {json.dumps(tool_registry)}\n"
            f"Allowed tool names: {json.dumps(allowed_tools)}\n"
            f"Execution history: {json.dumps(history)}\n"
            + last_summary
            + "DECIDING WHAT TO DO NEXT:\n"
            + "- If your most recent tool result already contains the data the task asks for, choose option 3 (final) RIGHT NOW. Do NOT call more tools to verify, re-check, or polish.\n"
            + "- Calling the same tool with identical args twice in a row is a strong signal you should emit final instead.\n"
            + "- If the task requires filesystem, terminal, network, or SQL operations AND you have not yet called a tool, call a suitable tool first.\n"
            + strategy_hint
        )
        try:
            decision_raw = call_llm_with_retry(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                timeout_s=timeout_s,
                prompt=prompt,
                retries=llm_retries,
            )
            decision = extract_json(decision_raw)
        except Exception as exc:
            consecutive_errors += 1
            history.append({"step": step, "error": f"llm_call_failed:{exc}"})
            continue

        action_type = str(decision.get("type", "")).strip()
        if action_type == "tool_call_intent":
            tool = str(decision.get("tool", "")).strip()
            if tool not in allowed_tools:
                consecutive_errors += 1
                history.append({"step": step, "error": f"tool_not_allowed:{tool}"})
                continue
            args = decision.get("args", {})
            if not isinstance(args, dict):
                args = {}
            intent = {
              "type": "tool_call_intent",
              "tool": tool,
              "args": args,
              "reason": str(decision.get("reason", "llm_requested_tool")),
            }
            print(json.dumps(intent), flush=True)
            tool_line = sys.stdin.readline()
            tool_result = json.loads(tool_line) if tool_line else {"ok": False, "error": "missing_tool_result"}
            tool_ok = bool(tool_result.get("ok", False))
            tool_error = str(tool_result.get("error", "")) if not tool_ok else ""
            history.append(
                {
                    "step": step,
                    "tool": tool,
                    "args": args,
                    "tool_result": tool_result,
                    "error": tool_error,
                }
            )
            if tool_ok:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
            continue

        if action_type == "api_call_intent":
            intent = {
              "type": "api_call_intent",
              "call_type": str(decision.get("call_type", "external_paid_api")),
              "provider": str(decision.get("provider", "unknown")),
              "endpoint": str(decision.get("endpoint", "")),
              "estimated_cost_usd": float(decision.get("estimated_cost_usd", 0.0)),
              "reason": str(decision.get("reason", "llm_requested_api")),
            }
            print(json.dumps(intent), flush=True)
            decision_line = sys.stdin.readline()
            api_decision = json.loads(decision_line) if decision_line else {"approved": False}
            approved = bool(api_decision.get("approved", False))
            history.append(
                {
                    "step": step,
                    "api_intent": intent,
                    "api_decision": api_decision,
                    "error": "" if approved else f"api_not_approved:{api_decision.get('note', 'denied')}",
                }
            )
            if approved:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
            continue

        if action_type == "final":
            final_result = str(decision.get("result", "")).strip()
            maybe_artifacts = decision.get("artifacts", [])
            if isinstance(maybe_artifacts, list):
                artifacts.extend(maybe_artifacts)
            final_status_raw = str(decision.get("status", "ok")).strip().lower()
            final_self_status = "error" if final_status_raw == "error" else "ok"
            if final_result:
                consecutive_errors = 0
                break
            consecutive_errors += 1
            history.append({"step": step, "error": "empty_final_result"})
            continue

        consecutive_errors += 1
        history.append({"step": step, "error": "invalid_llm_action", "raw": decision})

    if not final_result:
        final_result = "Agent could not produce a final answer within step budget."
        response = {
          "status": "error",
          "error_code": "agent_step_budget_exceeded",
          "error_message": final_result,
          "result": "",
          "memory_updates": [],
          "artifacts": artifacts,
          "metrics": {"latency_ms": (time.time() - start) * 1000}
        }
        print(json.dumps(response), flush=True)
        return 0

    output = final_result
    append_memory(memory_db, session_id, output)
    response: dict[str, Any] = {
      "status": final_self_status,
      "result": output,
      "memory_updates": [{"kind": "episodic", "content": output}],
      "artifacts": artifacts,
      "metrics": {"latency_ms": (time.time() - start) * 1000}
    }
    if final_self_status == "error":
        response["error_code"] = "agent_self_reported_infeasible"
        response["error_message"] = output
    print(json.dumps(response), flush=True)
    return 0

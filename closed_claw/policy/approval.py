# Purpose: Human approval gate logic for create/reuse and paid API decisions.

from __future__ import annotations

import concurrent.futures
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from closed_claw.compat import BaseModel

try:
    from rich.console import Console
except Exception:
    class Console:  # type: ignore[override]
        def print(self, text: str) -> None:
            """Run print."""
            print(text)


class ApprovalRequest(BaseModel):
    call_type: str
    provider: str
    endpoint: str
    estimated_cost_usd: float
    reason: str
    session_id: str


class ApprovalDecision(BaseModel):
    approved: bool
    operator: str
    timestamp: str
    note: str = ""


# ---------------------------------------------------------------------------
#  Web-mode pending approval queue (shared across all runs in one process)
# ---------------------------------------------------------------------------
_pending_lock = threading.Lock()
_pending_approvals: dict[str, dict[str, Any]] = {}
# Maps approval_id → {"request_data": {...}, "run_id": str, "event": Event, "decision": None|ApprovalDecision}


def get_pending_approvals(run_id: str | None = None) -> list[dict[str, Any]]:
    """Return pending web-mode approval requests, optionally filtered by run_id."""
    with _pending_lock:
        items = []
        for aid, entry in _pending_approvals.items():
            if entry.get("decision") is not None:
                continue  # already resolved
            if run_id and entry.get("run_id") != run_id:
                continue
            items.append({
                "approval_id": aid,
                "run_id": entry["run_id"],
                "request_data": entry["request_data"],
                "created_at": entry.get("created_at", ""),
            })
        return items


def resolve_approval(approval_id: str, approved: bool, note: str = "") -> bool:
    """Resolve a pending web approval.  Returns True if found & resolved."""
    with _pending_lock:
        entry = _pending_approvals.get(approval_id)
        if entry is None or entry.get("decision") is not None:
            return False
        entry["decision"] = ApprovalDecision(
            approved=approved,
            operator="web_user",
            timestamp=datetime.now(UTC).isoformat(),
            note=note or ("approved_via_web" if approved else "denied_via_web"),
        )
        entry["event"].set()
        return True


class ApprovalGate:
    def __init__(self, timeout_sec: int = 30, console: Console | None = None) -> None:
        """Initialize the instance."""
        self.timeout_sec = timeout_sec
        self.console = console or Console()

    def _read(self, prompt: str) -> str:
        """Run read."""
        return input(prompt)

    # ------------------------------------------------------------------ web
    def _web_prompt(
        self,
        request_data: dict[str, Any],
        run_id: str,
        timeout: int | None = None,
    ) -> ApprovalDecision:
        """Publish an approval request and block until the web UI resolves it."""
        approval_id = uuid.uuid4().hex
        event = threading.Event()
        entry: dict[str, Any] = {
            "run_id": run_id,
            "request_data": request_data,
            "event": event,
            "decision": None,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with _pending_lock:
            _pending_approvals[approval_id] = entry

        wait_sec = timeout if timeout is not None else self.timeout_sec
        resolved = event.wait(timeout=wait_sec)

        with _pending_lock:
            result = _pending_approvals.pop(approval_id, entry)

        if resolved and result.get("decision") is not None:
            return result["decision"]  # type: ignore[return-value]

        return ApprovalDecision(
            approved=False,
            operator="web_timeout",
            timestamp=datetime.now(UTC).isoformat(),
            note="timed_out_default_deny",
        )

    # -------------------------------------------------------------- console
    def prompt(self, req: ApprovalRequest, operator: str = "human") -> ApprovalDecision:
        """Run prompt."""
        self.console.print("\n[bold yellow]Approval required for external paid API call[/bold yellow]")
        self.console.print(f"session: {req.session_id}")
        self.console.print(f"provider: {req.provider}")
        self.console.print(f"endpoint: {req.endpoint}")
        self.console.print(f"reason: {req.reason}")
        self.console.print(f"estimated_cost_usd: {req.estimated_cost_usd:.4f}")
        self.console.print("Type 'yes' to approve. Anything else denies.")

        approved = False
        note = ""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._read, "> ")
            try:
                response = future.result(timeout=self.timeout_sec).strip().lower()
                approved = response in {"y", "yes"}
                note = "approved_by_operator" if approved else "denied_by_operator"
            except concurrent.futures.TimeoutError:
                approved = False
                note = "timed_out_default_deny"
            except Exception:
                approved = False
                note = "input_error_default_deny"

        return ApprovalDecision(
            approved=approved,
            operator=operator,
            timestamp=datetime.now(UTC).isoformat(),
            note=note,
        )

    def decide_with_mode(
        self,
        req: ApprovalRequest,
        mode: str = "interactive",
        operator: str = "human",
    ) -> ApprovalDecision:
        """Run decide with mode."""
        normalized = mode.lower()
        if normalized == "approve":
            return ApprovalDecision(
                approved=True,
                operator=operator,
                timestamp=datetime.now(UTC).isoformat(),
                note="auto_approved_by_policy",
            )
        if normalized == "deny":
            return ApprovalDecision(
                approved=False,
                operator=operator,
                timestamp=datetime.now(UTC).isoformat(),
                note="auto_denied_by_policy",
            )
        if normalized == "web":
            return self._web_prompt(
                request_data={
                    "type": "api_call",
                    "call_type": req.call_type,
                    "provider": req.provider,
                    "endpoint": req.endpoint,
                    "estimated_cost_usd": req.estimated_cost_usd,
                    "reason": req.reason,
                },
                run_id=req.session_id,
            )
        return self.prompt(req=req, operator=operator)

    def decide_create_with_mode(
        self,
        *,
        mode: str = "interactive",
        run_id: str,
        top_candidate: dict[str, str | float] | None = None,
    ) -> ApprovalDecision:
        """Run decide create with mode."""
        normalized = mode.lower()
        if normalized == "approve":
            return ApprovalDecision(
                approved=True,
                operator="human",
                timestamp=datetime.now(UTC).isoformat(),
                note="auto_approved_by_policy",
            )
        if normalized == "deny":
            return ApprovalDecision(
                approved=False,
                operator="human",
                timestamp=datetime.now(UTC).isoformat(),
                note="auto_denied_by_policy",
            )
        if normalized == "web":
            return self._web_prompt(
                request_data={
                    "type": "agent_create",
                    "top_candidate": dict(top_candidate) if top_candidate else {},
                },
                run_id=run_id,
                timeout=120,
            )

        self.console.print("\n[bold yellow]Approval required for creating a new agent[/bold yellow]")
        self.console.print(f"session: {run_id}")
        self.console.print(f"top_candidate: {top_candidate or {}}")
        self.console.print("Type 'yes' to create. Anything else will reuse best existing agent.")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._read, "> ")
            try:
                response = future.result(timeout=self.timeout_sec).strip().lower()
                approved = response in {"y", "yes"}
                note = "approved_by_operator" if approved else "denied_by_operator"
            except concurrent.futures.TimeoutError:
                approved = False
                note = "timed_out_default_reuse"
            except Exception:
                approved = False
                note = "input_error_default_reuse"
        return ApprovalDecision(
            approved=approved,
            operator="human",
            timestamp=datetime.now(UTC).isoformat(),
            note=note,
        )

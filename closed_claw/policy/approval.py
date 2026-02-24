# Purpose: Human approval gate logic for create/reuse and paid API decisions.

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime

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


class ApprovalGate:
    def __init__(self, timeout_sec: int = 30, console: Console | None = None) -> None:
        """Initialize the instance."""
        self.timeout_sec = timeout_sec
        self.console = console or Console()

    def _read(self, prompt: str) -> str:
        """Run read."""
        return input(prompt)

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

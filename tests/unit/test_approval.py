from __future__ import annotations

from closed_claw.policy.approval import ApprovalGate, ApprovalRequest


def test_approval_accept(monkeypatch):
    gate = ApprovalGate(timeout_sec=1)
    monkeypatch.setattr(gate, "_read", lambda _: "yes")
    decision = gate.prompt(
        ApprovalRequest(
            call_type="external_paid_api",
            provider="demo",
            endpoint="/v1/chat",
            estimated_cost_usd=0.1,
            reason="needed",
            session_id="s1",
        )
    )
    assert decision.approved is True


def test_approval_deny(monkeypatch):
    gate = ApprovalGate(timeout_sec=1)
    monkeypatch.setattr(gate, "_read", lambda _: "no")
    decision = gate.prompt(
        ApprovalRequest(
            call_type="external_paid_api",
            provider="demo",
            endpoint="/v1/chat",
            estimated_cost_usd=0.1,
            reason="needed",
            session_id="s1",
        )
    )
    assert decision.approved is False


def test_approval_mode_auto_approve():
    gate = ApprovalGate(timeout_sec=1)
    decision = gate.decide_with_mode(
        ApprovalRequest(
            call_type="external_paid_api",
            provider="demo",
            endpoint="/v1/chat",
            estimated_cost_usd=0.1,
            reason="needed",
            session_id="s1",
        ),
        mode="approve",
    )
    assert decision.approved is True
    assert decision.note == "auto_approved_by_policy"

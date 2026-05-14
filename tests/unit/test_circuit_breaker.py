# Purpose: Tests for the ReAct loop's 3-strikes circuit breaker.
#
# The breaker exists because the LLM sometimes loops on the same successful
# tool call instead of emitting `final`. Without it, every successful task
# bottoms out at step budget exhaustion. The 2026-05-09 sim 2e run validated
# the design; these tests lock the logic in place against future refactors of
# `agent_loop.main()`.

from __future__ import annotations

from closed_claw.runtime.agent_loop import _circuit_breaker_result


def _ok_step(step: int, tool: str, args: dict, stdout: str) -> dict:
    return {
        "step": step,
        "tool": tool,
        "args": args,
        "tool_result": {"ok": True, "result": {"stdout": stdout, "returncode": 0}},
        "error": "",
    }


def test_three_identical_successful_calls_fires():
    """3 identical successful (tool, args) in a row triggers the breaker."""
    history = [
        _ok_step(1, "terminal", {"cmd": "ls /tmp"}, "a.txt\nb.txt\n"),
        _ok_step(2, "terminal", {"cmd": "ls /tmp"}, "a.txt\nb.txt\n"),
        _ok_step(3, "terminal", {"cmd": "ls /tmp"}, "a.txt\nb.txt\n"),
    ]
    assert _circuit_breaker_result(history) == "a.txt\nb.txt"


def test_fewer_than_three_entries_does_not_fire():
    """Two identical calls aren't enough — the breaker needs 3 in a row."""
    history = [
        _ok_step(1, "terminal", {"cmd": "ls /tmp"}, "x"),
        _ok_step(2, "terminal", {"cmd": "ls /tmp"}, "x"),
    ]
    assert _circuit_breaker_result(history) is None


def test_one_failed_call_in_tail_does_not_fire():
    """If any of the last 3 calls failed, the breaker should NOT use them
    as a forced-final source — the LLM may legitimately be retrying."""
    history = [
        _ok_step(1, "terminal", {"cmd": "ls /tmp"}, "x"),
        {"step": 2, "tool": "terminal", "args": {"cmd": "ls /tmp"},
         "tool_result": {"ok": False, "error": "timeout"}, "error": "timeout"},
        _ok_step(3, "terminal", {"cmd": "ls /tmp"}, "x"),
    ]
    assert _circuit_breaker_result(history) is None


def test_different_args_in_tail_does_not_fire():
    """Three successful calls but with different args = real progress, not stuck."""
    history = [
        _ok_step(1, "terminal", {"cmd": "ls /tmp"}, "x"),
        _ok_step(2, "terminal", {"cmd": "ls /var"}, "y"),
        _ok_step(3, "terminal", {"cmd": "ls /etc"}, "z"),
    ]
    assert _circuit_breaker_result(history) is None


def test_empty_stdout_falls_back_to_result_json():
    """When stdout is empty but the result dict has other useful fields,
    the breaker returns a JSON dump rather than an empty string — otherwise
    the agent would report an empty 'successful' answer."""
    history = [
        {
            "step": i,
            "tool": "file_io",
            "args": {"op": "list", "path": "/tmp"},
            "tool_result": {
                "ok": True,
                "result": {"entries": ["a.txt", "b.txt"], "stdout": ""},
            },
            "error": "",
        }
        for i in range(1, 4)
    ]
    out = _circuit_breaker_result(history)
    assert out is not None
    assert "entries" in out  # JSON serialization of the result dict

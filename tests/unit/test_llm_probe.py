# Purpose: Tests for llm_client.probe_key (doctor's API-key validity probe).

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from closed_claw.llm_client import probe_key


def test_empty_key_returns_no_key_without_http():
    """probe_key skips the HTTP call when the key is empty/whitespace."""
    with patch("closed_claw.llm_client.httpx.Client") as mock_client:
        ok, detail = probe_key("openai", "gpt-4o-mini", "", "https://api.openai.com")
    assert ok is False
    assert detail == "no_key"
    mock_client.assert_not_called()


def _stub_client(response: httpx.Response | None = None, exc: Exception | None = None):
    """Build a context-manager mock that yields a client whose .get() is
    controllable. Mirrors the ``with httpx.Client(...) as client:`` pattern."""
    client = MagicMock()
    if exc is not None:
        client.get.side_effect = exc
    else:
        client.get.return_value = response
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return ctx, client


def test_openai_200_returns_ok():
    """OpenAI 200 on /v1/models = key is valid."""
    ctx, _ = _stub_client(httpx.Response(200, json={"data": []}))
    with patch("closed_claw.llm_client.httpx.Client", return_value=ctx):
        ok, detail = probe_key("openai", "gpt-4o-mini", "sk-x", "https://api.openai.com")
    assert ok is True
    assert detail == "ok"


def test_openai_401_returns_unauthorized():
    """OpenAI 401 = expired/bad key."""
    ctx, _ = _stub_client(httpx.Response(401, json={"error": "unauthorized"}))
    with patch("closed_claw.llm_client.httpx.Client", return_value=ctx):
        ok, detail = probe_key("openai", "gpt-4o-mini", "sk-bad", "https://api.openai.com")
    assert ok is False
    assert detail == "unauthorized"


def test_gemini_200_returns_ok():
    """Gemini 200 on /v1beta/models?key= = key is valid (different endpoint shape)."""
    ctx, client = _stub_client(httpx.Response(200, json={"models": []}))
    with patch("closed_claw.llm_client.httpx.Client", return_value=ctx):
        ok, detail = probe_key(
            "gemini", "gemini-2.5-flash", "AIza-x",
            "https://generativelanguage.googleapis.com",
        )
    assert ok is True
    assert detail == "ok"
    # Confirm we used the gemini-style URL with key in query, not bearer header.
    args, kwargs = client.get.call_args
    assert "/v1beta/models" in args[0]
    assert kwargs.get("params", {}).get("key") == "AIza-x"


def test_gemini_403_returns_unauthorized():
    """Gemini 403 = bad/expired key."""
    ctx, _ = _stub_client(httpx.Response(403, json={"error": "PERMISSION_DENIED"}))
    with patch("closed_claw.llm_client.httpx.Client", return_value=ctx):
        ok, detail = probe_key(
            "gemini", "gemini-2.5-flash", "AIza-bad",
            "https://generativelanguage.googleapis.com",
        )
    assert ok is False
    assert detail == "unauthorized"


def test_claude_uses_x_api_key_header():
    """Anthropic auths with x-api-key header + anthropic-version, not bearer."""
    ctx, client = _stub_client(httpx.Response(200, json={"data": []}))
    with patch("closed_claw.llm_client.httpx.Client", return_value=ctx):
        ok, detail = probe_key(
            "claude", "claude-3-5-haiku-latest", "sk-ant-x",
            "https://api.anthropic.com",
        )
    assert ok is True
    assert detail == "ok"
    args, kwargs = client.get.call_args
    headers = kwargs.get("headers", {})
    assert headers.get("x-api-key") == "sk-ant-x"
    assert "anthropic-version" in headers


def test_network_error_returns_soft_failure():
    """Connection error during probe must not raise — doctor should still finish."""
    ctx, _ = _stub_client(exc=httpx.ConnectError("no route to host"))
    with patch("closed_claw.llm_client.httpx.Client", return_value=ctx):
        ok, detail = probe_key("openai", "gpt-4o-mini", "sk-x", "https://api.openai.com")
    assert ok is False
    assert detail.startswith("network_error:")

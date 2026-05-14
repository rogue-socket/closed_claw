# Purpose: Shared LLM HTTP client — single source of truth for provider
# key resolution and text generation across all modules.

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from closed_claw.config import Settings

# Default base URLs per provider (used by setup_wizard before settings exist).
DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "claude": "https://api.anthropic.com",
    "siemens": "https://api.siemens.com/llm",
}


def provider_key_and_base(settings: Settings, provider: str) -> tuple[str, str]:
    """Resolve the API key and base URL for *provider* from *settings*."""
    key = settings.llm_api_key.strip()
    if provider == "openai":
        key = key or settings.openai_api_key.strip()
        return key, settings.openai_base_url.rstrip("/")
    if provider == "gemini":
        key = key or settings.gemini_api_key.strip()
        return key, settings.gemini_base_url.rstrip("/")
    if provider == "claude":
        key = key or settings.anthropic_api_key.strip()
        return key, settings.anthropic_base_url.rstrip("/")
    if provider == "siemens":
        key = key or settings.siemens_api_key.strip()
        return key, settings.siemens_base_url.rstrip("/")
    return "", ""


def generate_text(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout_sec: int,
    prompt: str,
    max_tokens: int,
    temperature: float,
) -> str:
    """Send a single-turn text prompt to an LLM provider and return the response."""
    import httpx

    def _raise(resp: httpx.Response) -> None:
        if resp.is_success:
            return
        body = (resp.text or "")[:600]
        raise RuntimeError(
            f"{provider} {resp.status_code} {resp.reason_phrase} from {resp.request.url}: {body}"
        )

    if provider in ("openai", "siemens"):
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(
                f"{base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            _raise(resp)
            return resp.json()["choices"][0]["message"]["content"]

    if provider == "gemini":
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(
                f"{base_url}/v1beta/models/{model}:generateContent",
                params={"key": api_key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature},
                },
            )
            _raise(resp)
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    if provider == "claude":
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(
                f"{base_url}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            _raise(resp)
            parts = [
                block.get("text", "")
                for block in resp.json().get("content", [])
                if isinstance(block, dict)
            ]
            return " ".join(parts)

    raise ValueError(f"unsupported provider: {provider}")


def probe_key(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    timeout_s: int = 5,
) -> tuple[bool, str]:
    """Lightly probe an LLM provider to verify the API key is currently usable.

    Returns ``(ok, detail)``. ``ok=True`` means the provider accepted the key
    (HTTP 2xx). On 401/403 it returns ``(False, "unauthorized")``; on any
    other HTTP or network error it returns ``(False, "<short reason>")`` —
    never raises. Cheap GET-style endpoint per provider; no token usage.
    """
    if not (api_key or "").strip():
        return False, "no_key"
    provider = (provider or "").lower()
    base = (base_url or "").rstrip("/")
    try:
        if provider in {"openai", "siemens"}:
            url = f"{base}/v1/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.get(url, headers=headers)
        elif provider == "gemini":
            url = f"{base}/v1beta/models"
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.get(url, params={"key": api_key})
        elif provider == "claude":
            url = f"{base}/v1/models"
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            with httpx.Client(timeout=timeout_s) as client:
                resp = client.get(url, headers=headers)
        else:
            return False, "unsupported_provider"
    except httpx.HTTPError as exc:
        return False, f"network_error:{exc.__class__.__name__}"
    if 200 <= resp.status_code < 300:
        return True, "ok"
    if resp.status_code in (401, 403):
        return False, "unauthorized"
    return False, f"http_{resp.status_code}"


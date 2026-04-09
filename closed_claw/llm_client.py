# Purpose: Shared LLM HTTP client — single source of truth for provider
# key resolution and text generation across all modules.

from __future__ import annotations

from typing import TYPE_CHECKING

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
            resp.raise_for_status()
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
            resp.raise_for_status()
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
            resp.raise_for_status()
            parts = [
                block.get("text", "")
                for block in resp.json().get("content", [])
                if isinstance(block, dict)
            ]
            return " ".join(parts)

    raise ValueError(f"unsupported provider: {provider}")

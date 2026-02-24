# Purpose: Interactive setup wizard for provider, model, and API key configuration.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SetupResult:
    provider: str
    model: str
    verified: bool
    message: str


def run_setup_wizard(env_path: Path | None = None) -> SetupResult:
    """Run run setup wizard."""
    env_path = env_path or Path(".env")
    provider = _choose_provider()
    model = _choose_model(provider)
    key = ""
    if provider != "heuristic":
        key = input("Enter API key: ").strip()

    updates: dict[str, str] = {
        "CLOSED_CLAW_LLM_PROVIDER": provider,
        "CLOSED_CLAW_LLM_MODEL": model,
    }
    if provider == "openai":
        updates["OPENAI_API_KEY"] = key
    elif provider == "gemini":
        updates["GEMINI_API_KEY"] = key
    elif provider == "claude":
        updates["ANTHROPIC_API_KEY"] = key

    verified, message = verify_provider(provider=provider, model=model, api_key=key)

    print(f"Verification: {'OK' if verified else 'FAILED'} - {message}")
    confirm = input("Save this configuration to .env? (yes/no): ").strip().lower()
    if confirm in {"y", "yes"}:
        upsert_env(env_path, updates)
        print(f"Saved configuration to {env_path}")
    else:
        print("Configuration not saved.")

    return SetupResult(provider=provider, model=model, verified=verified, message=message)


def verify_provider(provider: str, model: str, api_key: str) -> tuple[bool, str]:
    """Run verify provider."""
    provider = provider.lower()
    if provider == "heuristic":
        return True, "heuristic reranker requires no API key"
    if not api_key:
        return False, "missing API key"

    try:
        if provider == "openai":
            return _verify_openai(model, api_key)
        if provider == "gemini":
            return _verify_gemini(model, api_key)
        if provider == "claude":
            return _verify_claude(model, api_key)
        return False, f"unsupported provider: {provider}"
    except Exception as exc:
        return False, str(exc)


def _verify_openai(model: str, api_key: str) -> tuple[bool, str]:
    """Run verify openai."""
    import httpx

    with httpx.Client(timeout=20) as client:
        resp = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "temperature": 0,
                "max_tokens": 8,
            },
        )
    if resp.status_code >= 400:
        return False, f"openai HTTP {resp.status_code}: {resp.text[:160]}"
    return True, "openai request succeeded"


def _verify_gemini(model: str, api_key: str) -> tuple[bool, str]:
    """Run verify gemini."""
    import httpx

    with httpx.Client(timeout=20) as client:
        resp = client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json={
                "contents": [{"parts": [{"text": "Reply with OK"}]}],
                "generationConfig": {"temperature": 0},
            },
        )
    if resp.status_code >= 400:
        return False, f"gemini HTTP {resp.status_code}: {resp.text[:160]}"
    return True, "gemini request succeeded"


def _verify_claude(model: str, api_key: str) -> tuple[bool, str]:
    """Run verify claude."""
    import httpx

    with httpx.Client(timeout=20) as client:
        resp = client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 16,
                "temperature": 0,
                "messages": [{"role": "user", "content": "Reply with OK"}],
            },
        )
    if resp.status_code >= 400:
        return False, f"claude HTTP {resp.status_code}: {resp.text[:160]}"
    return True, "claude request succeeded"


def _choose_provider() -> str:
    """Run choose provider."""
    options = ["heuristic", "openai", "gemini", "claude"]
    print("Choose LLM provider:")
    for idx, item in enumerate(options, start=1):
        print(f"  {idx}. {item}")
    while True:
        raw = input("Provider number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("Invalid choice. Try again.")


def _choose_model(provider: str) -> str:
    """Run choose model."""
    default_models = {
        "heuristic": "local-heuristic",
        "openai": "gpt-4o-mini",
        "gemini": "gemini-1.5-flash",
        "claude": "claude-3-5-haiku-latest",
    }
    default = default_models.get(provider, "local-heuristic")
    raw = input(f"Model [{default}]: ").strip()
    return raw or default


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """Run upsert env."""
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    output: list[str] = []
    for line in existing_lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            output.append(line)
            continue
        key, _ = line.split("=", 1)
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)

    for key, value in remaining.items():
        output.append(f"{key}={value}")

    path.write_text("\n".join(output) + "\n", encoding="utf-8")

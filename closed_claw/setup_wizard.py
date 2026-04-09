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
    elif provider == "siemens":
        updates["SIEMENS_API_KEY"] = key

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
    if not api_key:
        return False, "missing API key"

    try:
        if provider == "openai":
            return _verify_openai(model, api_key)
        if provider == "gemini":
            return _verify_gemini(model, api_key)
        if provider == "claude":
            return _verify_claude(model, api_key)
        if provider == "siemens":
            return _verify_siemens(model, api_key)
        return False, f"unsupported provider: {provider}"
    except Exception as exc:
        return False, str(exc)


def _verify_openai(model: str, api_key: str) -> tuple[bool, str]:
    """Verify OpenAI connectivity via the shared LLM client."""
    return _verify_provider("openai", model, api_key)


def _verify_gemini(model: str, api_key: str) -> tuple[bool, str]:
    """Verify Gemini connectivity via the shared LLM client."""
    return _verify_provider("gemini", model, api_key)


def _verify_claude(model: str, api_key: str) -> tuple[bool, str]:
    """Verify Claude connectivity via the shared LLM client."""
    return _verify_provider("claude", model, api_key)


def _verify_siemens(model: str, api_key: str) -> tuple[bool, str]:
    """Verify Siemens connectivity via the shared LLM client."""
    return _verify_provider("siemens", model, api_key)


def _verify_provider(provider: str, model: str, api_key: str) -> tuple[bool, str]:
    """Send a tiny probe prompt to *provider* and check for a successful response."""
    from closed_claw.llm_client import DEFAULT_BASE_URLS, generate_text

    base_url = DEFAULT_BASE_URLS.get(provider, "")
    try:
        generate_text(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout_sec=20,
            prompt="Reply with OK",
            max_tokens=8,
            temperature=0,
        )
        return True, f"{provider} request succeeded"
    except Exception as exc:
        return False, f"{provider} verification failed: {exc}"


def _choose_provider() -> str:
    """Run choose provider."""
    options = ["openai", "gemini", "claude", "siemens"]
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
        "openai": "gpt-4o-mini",
        "gemini": "gemini-2.5-flash",
        "claude": "claude-3-5-haiku-latest",
        "siemens": "qwen3-30b-a3b-instruct-2507",
    }
    default = default_models.get(provider, "gpt-4o-mini")
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

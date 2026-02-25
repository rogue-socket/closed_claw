# Purpose: Environment-backed runtime settings loading and validation.

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> dict[str, str]:
    """Run load dotenv."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _getenv(name: str, default: str, dotenv: dict[str, str]) -> str:
    """Run getenv."""
    if name in os.environ:
        return os.environ[name]
    return dotenv.get(name, default)


@dataclass(slots=True)
class Settings:
    db_path: Path
    agents_dir: Path
    run_logs_dir: Path
    embedding_model: str
    embedding_dim: int
    low_confidence_threshold: float
    create_approval_required: bool
    create_approval_mode: str
    api_approval_mode: str
    paid_api_providers: set[str]
    api_approval_timeout_sec: int
    agent_timeout_sec: int
    agent_retries: int
    circuit_breaker_failures: int
    circuit_breaker_reset_sec: int
    task_pool_poll_interval_sec: int
    require_sqlite_vec: bool
    llm_provider: str
    llm_model: str
    llm_timeout_sec: int
    llm_api_key: str
    openai_api_key: str
    gemini_api_key: str
    anthropic_api_key: str
    siemens_api_key: str
    openai_base_url: str
    gemini_base_url: str
    anthropic_base_url: str
    siemens_base_url: str
    extra_allowed_paths: list[Path]
    subtask_max_attempts: int = 2
    max_tool_calls_per_agent: int = 50
    max_agents_per_run: int = 10
    max_subtasks_per_phase: int = 4

    @classmethod
    def from_env(cls) -> "Settings":
        """Run from env."""
        cwd = Path.cwd()
        dotenv = _load_dotenv(cwd / ".env")
        paid = _getenv("CLOSED_CLAW_PAID_API_PROVIDERS", "", dotenv)
        provider = _getenv("CLOSED_CLAW_LLM_PROVIDER", "siemens", dotenv).lower()
        extra_paths_raw = _getenv("CLOSED_CLAW_EXTRA_ALLOWED_PATHS", "", dotenv)
        default_model = {
            "openai": "gpt-4o-mini",
            "gemini": "gemini-2.5-flash",
            "claude": "claude-3-5-haiku-latest",
            "siemens": "qwen3-30b-a3b-instruct-2507",
        }.get(provider, "gpt-4o-mini")
        return cls(
            db_path=Path(_getenv("CLOSED_CLAW_DB_PATH", ".closed_claw/registry.db", dotenv)).expanduser(),
            agents_dir=(cwd / _getenv("CLOSED_CLAW_AGENTS_DIR", "agents", dotenv)).resolve(),
            run_logs_dir=(cwd / _getenv("CLOSED_CLAW_RUN_LOGS_DIR", ".closed_claw/runs", dotenv)).resolve(),
            embedding_model=_getenv("CLOSED_CLAW_EMBEDDING_MODEL", "all-MiniLM-L6-v2", dotenv),
            embedding_dim=int(_getenv("CLOSED_CLAW_EMBEDDING_DIM", "384", dotenv)),
            low_confidence_threshold=float(
                _getenv("CLOSED_CLAW_LOW_CONFIDENCE_THRESHOLD", "0.62", dotenv)
            ),
            create_approval_required=_getenv(
                "CLOSED_CLAW_CREATE_APPROVAL_REQUIRED", "true", dotenv
            ).lower()
            in {"1", "true", "yes"},
            create_approval_mode=_getenv(
                "CLOSED_CLAW_CREATE_APPROVAL_MODE", "interactive", dotenv
            ).lower(),
            api_approval_mode=_getenv("CLOSED_CLAW_API_APPROVAL_MODE", "interactive", dotenv).lower(),
            paid_api_providers={p.strip() for p in paid.split(",") if p.strip()},
            api_approval_timeout_sec=int(
                _getenv("CLOSED_CLAW_API_APPROVAL_TIMEOUT_SEC", "30", dotenv)
            ),
            agent_timeout_sec=int(_getenv("CLOSED_CLAW_AGENT_TIMEOUT_SEC", "120", dotenv)),
            agent_retries=int(_getenv("CLOSED_CLAW_AGENT_RETRIES", "2", dotenv)),
            circuit_breaker_failures=int(
                _getenv("CLOSED_CLAW_CIRCUIT_BREAKER_FAILURES", "3", dotenv)
            ),
            circuit_breaker_reset_sec=int(
                _getenv("CLOSED_CLAW_CIRCUIT_BREAKER_RESET_SEC", "120", dotenv)
            ),
            task_pool_poll_interval_sec=int(
                _getenv("CLOSED_CLAW_TASK_POOL_POLL_INTERVAL_SEC", "30", dotenv)
            ),
            subtask_max_attempts=max(
                1,
                int(_getenv("CLOSED_CLAW_SUBTASK_MAX_ATTEMPTS", "2", dotenv)),
            ),
            max_tool_calls_per_agent=max(
                1,
                int(_getenv("CLOSED_CLAW_MAX_TOOL_CALLS_PER_AGENT", "50", dotenv)),
            ),
            max_agents_per_run=max(
                1,
                int(_getenv("CLOSED_CLAW_MAX_AGENTS_PER_RUN", "10", dotenv)),
            ),
            max_subtasks_per_phase=max(
                1,
                int(_getenv("CLOSED_CLAW_MAX_SUBTASKS_PER_PHASE", "4", dotenv)),
            ),
            require_sqlite_vec=_getenv("CLOSED_CLAW_REQUIRE_SQLITE_VEC", "true", dotenv).lower()
            in {"1", "true", "yes"},
            llm_provider=provider,
            llm_model=_getenv("CLOSED_CLAW_LLM_MODEL", default_model, dotenv),
            llm_timeout_sec=int(_getenv("CLOSED_CLAW_LLM_TIMEOUT_SEC", "45", dotenv)),
            llm_api_key=_getenv("CLOSED_CLAW_LLM_API_KEY", "", dotenv),
            openai_api_key=_getenv("OPENAI_API_KEY", "", dotenv),
            gemini_api_key=_getenv("GEMINI_API_KEY", "", dotenv),
            anthropic_api_key=_getenv("ANTHROPIC_API_KEY", "", dotenv),
            siemens_api_key=_getenv("SIEMENS_API_KEY", "", dotenv),
            openai_base_url=_getenv("OPENAI_BASE_URL", "https://api.openai.com", dotenv),
            gemini_base_url=_getenv(
                "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com", dotenv
            ),
            anthropic_base_url=_getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com", dotenv),
            siemens_base_url=_getenv("SIEMENS_BASE_URL", "https://api.siemens.com/llm", dotenv),
            extra_allowed_paths=[
                Path(p.strip()).expanduser().resolve()
                for p in extra_paths_raw.split(",")
                if p.strip()
            ],
        )

    def ensure_dirs(self) -> None:
        """Run ensure dirs."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.run_logs_dir.mkdir(parents=True, exist_ok=True)

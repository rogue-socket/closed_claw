"""Reuse-thesis benchmark.

Runs a fixed 30-task sequence through the routing decision (profile generation
+ reuse-or-create) under four filter regimes, each on its own fresh registry.

This measures the THESIS — "capsule reuse compounds across runs" — directly:
does the reuse curve rise as the library grows on similar inputs?

It does NOT measure agent execution quality. That's a separate axis.

Usage:
  python -m scripts.benchmark_reuse run     # execute the benchmark, write JSONL
  python -m scripts.benchmark_reuse analyze # summarize the latest JSONL

Costs: ~30 LLM calls (profile generation), local embedding work is free.
Profiles are cached by task index so the four regimes share identical inputs.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS", "true")

from closed_claw.agents.factory import AgentFactory
from closed_claw.config import Settings
from closed_claw.embeddings.provider import EmbeddingProvider
from closed_claw.registry.search import generate_agent_profile
from closed_claw.registry.store import RegistryStore
from closed_claw.tools.executor import SUPPORTED_TOOLS

NOISE_TAGS = {"auto", "capability"}

# ---------------------------------------------------------------------------
# Task suite
# ---------------------------------------------------------------------------
# 6 categories × 5 tasks. Within-category phrasing varies so we test semantic
# matching rather than lexical overlap. Ground truth: position 0 in each
# category should create; positions 1..4 should reuse.
TASKS: list[tuple[str, str]] = [
    # A. filesystem-list
    ("fs-list", "Show me what's inside my Downloads folder"),
    ("fs-list", "List the files in /tmp"),
    ("fs-list", "Enumerate top-level items under /Users/yashagrawal/Documents"),
    ("fs-list", "What's in my Desktop directory?"),
    ("fs-list", "Display the contents of the project root directory"),

    # B. filesystem-write
    ("fs-write", "Create a folder called notes_2026 in /tmp"),
    ("fs-write", "Make a new directory named scratch under my home folder"),
    ("fs-write", "Set up an empty folder at /tmp/experiment_alpha"),
    ("fs-write", "Create a folder named archive on the Desktop"),
    ("fs-write", "Add a new directory called drafts under /Users/yashagrawal"),

    # C. filesystem-read
    ("fs-read", "Read the contents of /tmp/notes.txt"),
    ("fs-read", "Show me what's written in /Users/yashagrawal/.bashrc"),
    ("fs-read", "Print the contents of /Users/yashagrawal/Documents/readme.md"),
    ("fs-read", "Display the text in /tmp/log.txt"),
    ("fs-read", "Open and show me the file /etc/hosts"),

    # D. sqlite-create
    ("sqlite-create", "Create a sqlite database at /tmp/users.db with a users table (id, name)"),
    ("sqlite-create", "Build a new SQLite file at /tmp/inventory.db and add a products table"),
    ("sqlite-create", "Initialize a sqlite db at /tmp/orders.db with an orders table"),
    ("sqlite-create", "Make a sqlite database with a sessions table at /tmp/sessions.db"),
    ("sqlite-create", "Create a fresh sqlite file at /tmp/events.db containing an events table"),

    # E. sqlite-query
    ("sqlite-query", "Run SELECT COUNT(*) on the users table in /tmp/users.db"),
    ("sqlite-query", "Compute SUM(total_price) from the orders table in /tmp/orders.db"),
    ("sqlite-query", "Get all rows where status='active' from the sessions table"),
    ("sqlite-query", "Find max(amount) in the payments table at /tmp/payments.db"),
    ("sqlite-query", "Count distinct user_ids in the events table at /tmp/events.db"),

    # F. http-fetch
    ("http-fetch", "Fetch the page at https://example.com and return the HTML"),
    ("http-fetch", "Download https://api.github.com/users/octocat as JSON"),
    ("http-fetch", "GET https://httpbin.org/json and parse the response"),
    ("http-fetch", "Retrieve the content from https://raw.githubusercontent.com/foo/bar/main/README.md"),
    ("http-fetch", "Fetch https://www.python.org and return the page title"),
]

# Filter regimes — each is a predicate function over (passes_threshold, passes_tag, passes_tool)
def _profile_with_retry(settings: Settings, task: str, max_attempts: int = 5) -> dict[str, Any]:
    """Wrap generate_agent_profile with backoff for transient LLM failures (503/429)."""
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return generate_agent_profile(
                settings=settings,
                task=task,
                supported_tools=SUPPORTED_TOOLS,
                fallback_tools=SUPPORTED_TOOLS,
            )
        except Exception as e:  # noqa: BLE001 — broad on purpose; LLM errors vary
            last_err = e
            msg = str(e).lower()
            transient = any(s in msg for s in ("503", "unavailable", "429", "rate", "timeout"))
            if not transient or attempt == max_attempts - 1:
                raise
            sleep = 2 ** attempt  # 1, 2, 4, 8, 16 s
            print(f"    transient LLM error (attempt {attempt+1}/{max_attempts}); sleeping {sleep}s")
            time.sleep(sleep)
    assert last_err is not None
    raise last_err


REGIMES: dict[str, callable] = {
    "current":  lambda t, g, l: t and g and l,
    "sem_only": lambda t, g, l: t,
    "sem_tag":  lambda t, g, l: t and g,
    "sem_tool": lambda t, g, l: t and l,
}

# ---------------------------------------------------------------------------
# Reuse logic — copied verbatim from CoordinatorNodes._find_reusable_capability_agent
# (closed_claw/coordinator/nodes.py:832 as of 2026-05-12). Kept inline so the
# benchmark doesn't need to instantiate the full coordinator graph. If the
# production logic changes, update both.
# ---------------------------------------------------------------------------

def find_reusable_under_regime(
    *,
    registry: RegistryStore,
    embedder: EmbeddingProvider,
    threshold: float,
    profile: dict[str, Any],
    regime_pred,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Run the production reuse-decision under a chosen filter regime.

    Returns (chosen_agent_id_or_None, candidate_log_rows).
    """
    query_vector = embedder.embed(profile["description"])
    candidates = registry.semantic_search(query_vector, k=10)
    profile_tags = set(profile.get("tags", [])) - NOISE_TAGS
    required_tools = set(profile.get("tools_allowlist", []))

    log_rows: list[dict[str, Any]] = []
    scored: list[tuple[float, str]] = []
    for cand in candidates:
        m = registry.get_manifest(cand.agent_id)
        if m is None or m.status != "active":
            continue
        cand_tags = set(m.tags) - NOISE_TAGS
        passes_threshold = cand.score >= threshold
        passes_tag = (not profile_tags) or bool(profile_tags & cand_tags)
        passes_tool = (not required_tools) or required_tools.issubset(set(m.tools_allowlist))
        log_rows.append({
            "agent_id": m.agent_id,
            "score": round(cand.score, 4),
            "T": passes_threshold,
            "G": passes_tag,
            "L": passes_tool,
        })
        if not regime_pred(passes_threshold, passes_tag, passes_tool):
            continue
        sr = m.success_rate if m.usage_count >= 2 else 0.5
        effective = cand.score * (0.3 + 0.7 * sr)
        scored.append((effective, m.agent_id))

    if not scored:
        return None, log_rows
    scored.sort(reverse=True)
    return scored[0][1], log_rows


# ---------------------------------------------------------------------------
# Trajectory runner
# ---------------------------------------------------------------------------

@dataclass
class TaskDecision:
    idx: int
    category: str
    task: str
    expected: str          # "reuse" or "create" — ground truth given prior tasks
    decision: str          # "reuse" or "create"
    chosen_agent_id: str | None
    top_candidates: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0


def run_trajectory(
    regime_name: str,
    settings: Settings,
    profile_cache: dict[int, dict[str, Any]],
) -> tuple[list[TaskDecision], dict[str, Any]]:
    """Execute the 30-task sequence under one filter regime in an isolated registry."""
    tmpdir = Path(tempfile.mkdtemp(prefix=f"bench-{regime_name}-"))
    db_path = tmpdir / "registry.db"
    agents_dir = tmpdir / "agents"
    schema_path = Path(__file__).resolve().parents[1] / "closed_claw" / "registry" / "schema.sql"

    registry = RegistryStore(
        db_path=db_path,
        schema_path=schema_path,
        embedding_dim=settings.embedding_dim,
        require_sqlite_vec=settings.require_sqlite_vec,
    )
    embedder = EmbeddingProvider(
        model_name=settings.embedding_model,
        dim=settings.embedding_dim,
    )
    factory = AgentFactory(agents_dir=agents_dir)
    threshold = settings.low_confidence_threshold
    pred = REGIMES[regime_name]

    seen_categories: set[str] = set()
    decisions: list[TaskDecision] = []

    for idx, (category, task) in enumerate(TASKS):
        expected = "create" if category not in seen_categories else "reuse"
        seen_categories.add(category)
        t0 = time.monotonic()

        # Profile generation is cached across regimes for fairness
        if idx not in profile_cache:
            profile = _profile_with_retry(settings, task)
            profile_cache[idx] = profile
        else:
            profile = profile_cache[idx]

        # Routing decision
        chosen, log_rows = find_reusable_under_regime(
            registry=registry,
            embedder=embedder,
            threshold=threshold,
            profile=profile,
            regime_pred=pred,
        )

        if chosen:
            decision = "reuse"
            # Realistic state evolution: bump usage_count so success-rate weighting
            # has meaningful inputs on later turns. We treat every reuse as a
            # success for the purpose of the benchmark; this matches the
            # generous "thesis-friendly" framing.
            with registry._conn() as conn:  # type: ignore[attr-defined]
                conn.execute(
                    "UPDATE agents SET usage_count=usage_count+1, "
                    "success_count=success_count+1, "
                    "success_rate=CAST(success_count+1 AS REAL)/(usage_count+1) "
                    "WHERE agent_id=?", (chosen,))
        else:
            decision = "create"
            # Mint a real capsule via the same code path production uses
            name = f"{profile['name_prefix']} {uuid.uuid4().hex[:4]}"
            manifest = factory.create_capsule(
                name=name,
                description=profile["description"],
                embedding_model=settings.embedding_model,
                embedding_vector=embedder.embed(profile["description"]),
                tools_allowlist=profile["tools_allowlist"],
                tags=profile["tags"],
                api_capabilities=profile.get("api_capabilities", []),
                requires_approval_for=profile.get("requires_approval_for", []),
                skill_content=profile["skill_md"],
                skill_ids=profile.get("skill_ids", []),
            )
            registry.upsert_manifest(manifest)
            chosen = manifest.agent_id

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        decisions.append(TaskDecision(
            idx=idx,
            category=category,
            task=task,
            expected=expected,
            decision=decision,
            chosen_agent_id=chosen,
            top_candidates=log_rows[:5],
            elapsed_ms=elapsed_ms,
        ))
        marker = "✓" if decision == expected else "·"
        print(f"  [{regime_name:<8}] #{idx:02d} {category:<14} {decision:<6} expect={expected:<6} {marker}  ({elapsed_ms}ms)")

    # Final library size
    with registry._conn() as conn:  # type: ignore[attr-defined]
        capsule_count = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE status='active'"
        ).fetchone()[0]

    summary = {
        "regime": regime_name,
        "n_tasks": len(decisions),
        "n_create": sum(1 for d in decisions if d.decision == "create"),
        "n_reuse":  sum(1 for d in decisions if d.decision == "reuse"),
        "n_correct": sum(1 for d in decisions if d.decision == d.expected),
        "capsule_count_final": capsule_count,
        "tmpdir": str(tmpdir),
    }
    return decisions, summary


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

def summarize(results: dict[str, dict[str, Any]]) -> None:
    print("\n=== SUMMARY ===")
    print(f"{'REGIME':<10} {'CREATE':>7} {'REUSE':>6} {'CORRECT':>8} {'CAPSULES':>9} {'REUSE %':>8}")
    for regime, payload in results.items():
        s = payload["summary"]
        n = s["n_tasks"]
        reuse_pct = s["n_reuse"] / n * 100
        print(f"{regime:<10} {s['n_create']:>7} {s['n_reuse']:>6} {s['n_correct']:>3}/{n:<3}   {s['capsule_count_final']:>9} {reuse_pct:>7.1f}%")

    print("\n=== REUSE RATE BY CATEGORY ===")
    cats = list(dict.fromkeys(c for c, _ in TASKS))
    header = " " * 14 + "  ".join(f"{r:>10}" for r in results)
    print(header)
    for cat in cats:
        row = [f"{cat:<14}"]
        for regime, payload in results.items():
            ds = [d for d in payload["decisions"] if d["category"] == cat]
            r = sum(1 for d in ds if d["decision"] == "reuse")
            n = len(ds)
            row.append(f"{r}/{n} ({r/n*100:.0f}%)".rjust(10))
        print("  ".join(row))

    print("\n=== ROLLING REUSE RATE (after task N, cumulative) ===")
    bucket_edges = [5, 10, 15, 20, 25, 30]
    print(f"{'REGIME':<10}  " + "  ".join(f"≤T{e:>2}" for e in bucket_edges))
    for regime, payload in results.items():
        decisions = payload["decisions"]
        row = [f"{regime:<10}"]
        for e in bucket_edges:
            prefix = decisions[:e]
            r = sum(1 for d in prefix if d["decision"] == "reuse")
            row.append(f"{r/e*100:>5.0f}%".rjust(6))
        print("  ".join(row))

    print("\n=== FIRST REUSE INDEX PER CATEGORY (current regime) ===")
    cur = results["current"]["decisions"]
    for cat in cats:
        first_create = next((d for d in cur if d["category"] == cat and d["decision"] == "create"), None)
        first_reuse  = next((d for d in cur if d["category"] == cat and d["decision"] == "reuse"), None)
        if first_create and first_reuse:
            print(f"  {cat:<14}  first_create=#{first_create['idx']:>2}  first_reuse=#{first_reuse['idx']:>2}  (lag={first_reuse['idx']-first_create['idx']})")
        elif first_create:
            print(f"  {cat:<14}  first_create=#{first_create['idx']:>2}  NEVER REUSED")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

RESULTS_DIR = Path(__file__).resolve().parents[1] / ".closed_claw" / "bench"


def cmd_run() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    print(f"provider={settings.llm_provider}  model={settings.llm_model}  "
          f"threshold={settings.low_confidence_threshold}  "
          f"embedding={settings.embedding_model}")
    print(f"task suite: {len(TASKS)} tasks across "
          f"{len(set(c for c, _ in TASKS))} categories\n")

    profile_cache: dict[int, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}

    for regime in REGIMES:
        print(f"\n--- regime: {regime} ---")
        try:
            decisions, summary = run_trajectory(regime, settings, profile_cache)
            results[regime] = {
                "summary": summary,
                "decisions": [asdict(d) for d in decisions],
            }
        except Exception as e:
            print(f"  ABORTED: {type(e).__name__}: {e}")
            results[regime] = {"error": f"{type(e).__name__}: {e}"}
            # If the FIRST regime fails the cache is empty — the rest will too. Stop.
            if not profile_cache:
                break

    out_path = RESULTS_DIR / f"benchmark_{int(time.time())}.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")

    if all("decisions" in r for r in results.values()):
        summarize(results)


def cmd_analyze() -> None:
    files = sorted(RESULTS_DIR.glob("benchmark_*.json"))
    if not files:
        print("no benchmark runs found in", RESULTS_DIR)
        return
    latest = files[-1]
    print(f"analyzing {latest.name}")
    results = json.loads(latest.read_text(encoding="utf-8"))
    summarize(results)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        cmd_run()
    elif cmd == "analyze":
        cmd_analyze()
    else:
        print(__doc__)
        sys.exit(1)

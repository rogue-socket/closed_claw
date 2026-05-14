"""Experiment: how much does the tag+tool filter cost us in reuse?

For a fixed set of historical task strings, run the same semantic_search the
coordinator runs, then show what each filter stage drops. Compares:

  A. Current logic:  threshold + role-tag overlap + tools-superset
  B. Threshold only: drop the tag/tool filters

The B number is the ceiling that semantic similarity alone can deliver.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make the package importable when running from repo/closed_claw/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS", "true")

from closed_claw.config import Settings
from closed_claw.embeddings.provider import EmbeddingProvider
from closed_claw.registry.store import RegistryStore

NOISE_TAGS = {"auto", "capability"}

# Each task has TWO query forms:
#   "task" — raw user task text  (intent-similarity ceiling, what users mean)
#   "desc" — LLM-style profile description (what production actually embeds via
#            generate_agent_profile → the lexical-match ceiling)
TASKS = [
    ("desktop-path",
        "Find the absolute path to my Desktop folder and list its top-level contents",
        "Locates the absolute path to the user's Desktop folder and lists its contents."),
    ("desktop-path-2",
        "Determine where my Desktop directory lives and show what's inside",
        "Discovers the Desktop directory's absolute path and enumerates top-level items."),
    ("sqlite-create",
        "Create a sqlite database at $WORKSPACE/test.db with a table named 'users' (columns: id, name)",
        "Creates and populates SQLite databases with specified tables and columns."),
    ("sqlite-sum",
        "Open the sqlite db at $WORKSPACE/test2.db and compute SUM(amount) from the orders table",
        "Executes read-only SELECT SQL queries on SQLite databases and aggregates numeric columns."),
    ("folder-create",
        "Create a folder called random_folder_jack in /tmp",
        "Creates new directories at specified absolute paths on the filesystem."),
    ("file-read",
        "Read the contents of instructions.txt at /Users/yashagrawal/Documents/something/",
        "Reads the textual contents of files at specified absolute paths."),
]

# Hypothetical "fresh profile" tags+tools the coordinator would assign each
# task class. These mirror what generate_agent_profile produces for similar
# inputs in the existing run logs.
PROFILES = {
    "desktop-path":   {"tags": ["filesystem-navigator"], "tools": ["terminal", "file_io"]},
    "desktop-path-2": {"tags": ["filesystem-navigator"], "tools": ["terminal", "file_io"]},
    "sqlite-create":  {"tags": ["sqlite-db-manager"],    "tools": ["python_exec", "sql_query"]},
    "sqlite-sum":     {"tags": ["sql-query-runner"],     "tools": ["sql_query"]},
    "folder-create":  {"tags": ["directory-creator"],    "tools": ["terminal"]},
    "file-read":      {"tags": ["file-reader"],          "tools": ["file_io"]},
}


def main() -> None:
    settings = Settings.from_env()
    schema_path = Path(__file__).resolve().parents[1] / "closed_claw" / "registry" / "schema.sql"
    registry = RegistryStore(
        db_path=settings.db_path,
        schema_path=schema_path,
        embedding_dim=settings.embedding_dim,
        require_sqlite_vec=settings.require_sqlite_vec,
    )
    embedder = EmbeddingProvider(
        model_name=settings.embedding_model,
        dim=settings.embedding_dim,
    )
    threshold = settings.low_confidence_threshold
    print(f"threshold={threshold}  embedding_model={settings.embedding_model}  dim={settings.embedding_dim}")
    print()

    # Print the registry once so the candidate pool is visible
    print("=== REGISTRY ===")
    import sqlite3
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    for r in conn.execute("SELECT agent_id, tags_json, tools_allowlist_json, success_rate, usage_count FROM agents WHERE status='active'"):
        tags = json.loads(r["tags_json"] or "[]")
        tools = json.loads(r["tools_allowlist_json"] or "[]")
        print(f"  {r['agent_id'][:40]:40} tags={tags}  tools={tools}  use={r['usage_count']} sr={r['success_rate']:.2f}")
    print()

    results = {}  # mode -> (filters, semonly, n)

    for mode in ("task", "desc"):
        print(f"\n############### QUERY MODE: {mode} ###############")
        overall_filters = 0
        overall_semonly = 0
        overall_n = 0

        for key, task, desc in TASKS:
            overall_n += 1
            prof = PROFILES[key]
            prof_tags = set(prof["tags"]) - NOISE_TAGS
            required_tools = set(prof["tools"])

            query_text = task if mode == "task" else desc
            qv = embedder.embed(query_text)
            cands = registry.semantic_search(qv, k=10)

            print(f"--- {key} :: \"{query_text}\"")
            print(f"    profile.tags={sorted(prof_tags)}  profile.tools={sorted(required_tools)}")

            rows = []
            for c in cands:
                m = registry.get_manifest(c.agent_id)
                if m is None or m.status != "active":
                    continue
                cand_tags = set(m.tags) - NOISE_TAGS
                passes_threshold = c.score >= threshold
                passes_tag = (not prof_tags) or bool(prof_tags & cand_tags)
                passes_tool = (not required_tools) or required_tools.issubset(set(m.tools_allowlist))
                rows.append((c, m, passes_threshold, passes_tag, passes_tool))

            with_filters = [r for r in rows if r[2] and r[3] and r[4]]
            sem_only     = [r for r in rows if r[2]]

            for c, m, pt, ptag, ptool in rows[:5]:
                marks = ("T" if pt else "t") + ("G" if ptag else "g") + ("L" if ptool else "l")
                print(f"      [{marks}] cos={c.score:.3f}  {m.agent_id[:40]:40} tags={list(set(m.tags)-NOISE_TAGS)} tools={m.tools_allowlist}")

            print(f"      → with_filters={'YES' if with_filters else 'no'}    semantic_only={'YES' if sem_only else 'no'}")
            if with_filters: overall_filters += 1
            if sem_only: overall_semonly += 1
            print()

        results[mode] = (overall_filters, overall_semonly, overall_n)

    print("=== TOTALS (post-cosine-fix) ===")
    print(f"{'MODE':<6} {'with_filters':>15} {'semantic_only':>16}")
    for mode, (f, s, n) in results.items():
        print(f"{mode:<6} {f}/{n} = {f/n*100:>4.0f}%       {s}/{n} = {s/n*100:>4.0f}%")


if __name__ == "__main__":
    main()

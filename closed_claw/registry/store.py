# Purpose: SQLite-backed agent/run registry storage and querying operations.

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from closed_claw.compat import BaseModel, Field


class AgentManifest(BaseModel):
    agent_id: str
    name: str
    description: str
    embedding_model: str
    embedding_vector: list[float]
    tools_allowlist: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    api_capabilities: list[str] = Field(default_factory=list)
    requires_approval_for: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)  # base skill module IDs from agents/skills/
    version: str = "1.5"
    created_at: str
    last_used_at: str | None = None
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float | None = None
    status: str = "active"
    genome_json: str = "{}"
    lineage_json: str = "{}"
    fitness_score: float = 0.0


@dataclass(slots=True)
class SearchCandidate:
    agent_id: str
    score: float
    description: str


class RegistryStore:
    def __init__(
        self,
        db_path: Path,
        schema_path: Path,
        embedding_dim: int = 384,
        require_sqlite_vec: bool = True,
    ) -> None:
        """Initialize the instance."""
        self.db_path = db_path
        self.schema_path = schema_path
        self.embedding_dim = embedding_dim
        self.require_sqlite_vec = require_sqlite_vec
        self.sqlite_vec_available = False
        self._local = threading.local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        """Return a per-thread cached DB connection, creating one if needed.

        Uses ``threading.local()`` so each thread gets its own SQLite
        connection, avoiding ``ProgrammingError`` when the web server
        handles requests on worker threads.
        """
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except Exception:
                conn = None

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            self.sqlite_vec_available = self._try_load_sqlite_vec(conn)
        except (sqlite3.OperationalError, AttributeError) as exc:
            self.sqlite_vec_available = False
            if self.require_sqlite_vec and os.getenv("PYTEST_CURRENT_TEST") is None:
                raise RuntimeError(
                    "sqlite-vec extension is required but failed to load."
                ) from exc
        self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the current thread's cached connection if open."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def _try_load_sqlite_vec(self, conn: sqlite3.Connection) -> bool:
        """Run try load sqlite vec."""
        if not hasattr(conn, "enable_load_extension"):
            return False
        conn.enable_load_extension(True)
        try:
            # Preferred: package-provided loader resolves the real shared library path.
            try:
                import sqlite_vec  # type: ignore

                sqlite_vec.load(conn)
                return True
            except Exception:
                pass

            # Fallback: explicit path via env var.
            ext_path = os.getenv("SQLITE_VEC_PATH")
            if ext_path:
                conn.load_extension(ext_path)
                return True

            # Final fallback: default shared library name on platform loader path.
            conn.load_extension("sqlite_vec")
            return True
        finally:
            conn.enable_load_extension(False)

    def _init_db(self) -> None:
        """Run init db."""
        schema = self.schema_path.read_text(encoding="utf-8")
        schema = schema.replace("float[384]", f"float[{self.embedding_dim}]")
        if not self.require_sqlite_vec:
            schema = re.sub(
                r"CREATE VIRTUAL TABLE IF NOT EXISTS agent_vectors USING vec0\(\s*agent_id TEXT,\s*embedding float\[\d+\]\s*\);\n?",
                "",
                schema,
                flags=re.MULTILINE,
            )
        with self._conn() as conn:
            conn.executescript(schema)
            # Migration: add skill_ids_json column if missing (added after v1.5)
            try:
                conn.execute(
                    "ALTER TABLE agents ADD COLUMN skill_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            # Migration: add evolution columns if missing
            for col, default in [
                ("genome_json", "'{}'"),
                ("lineage_json", "'{}'"),
                ("fitness_score", "0.0"),
            ]:
                try:
                    conn.execute(
                        f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
                        if "json" in col
                        else f"ALTER TABLE agents ADD COLUMN {col} REAL NOT NULL DEFAULT {default}"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists

    @staticmethod
    def _has_agent_vectors_table(conn: sqlite3.Connection) -> bool:
        """Run has agent vectors table."""
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_vectors'"
        ).fetchone()
        return row is not None

    def upsert_manifest(self, manifest: AgentManifest) -> None:
        """Run upsert manifest."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO agents (
                    agent_id, name, description, embedding_model, embedding_dim,
                    embedding_json, tools_allowlist_json, tags_json, api_capabilities_json,
                    requires_approval_for_json, skill_ids_json, version, created_at, last_used_at,
                    usage_count, success_count, failure_count, success_rate, avg_latency_ms, status,
                    genome_json, lineage_json, fitness_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    embedding_model=excluded.embedding_model,
                    embedding_dim=excluded.embedding_dim,
                    embedding_json=excluded.embedding_json,
                    tools_allowlist_json=excluded.tools_allowlist_json,
                    tags_json=excluded.tags_json,
                    api_capabilities_json=excluded.api_capabilities_json,
                    requires_approval_for_json=excluded.requires_approval_for_json,
                    skill_ids_json=excluded.skill_ids_json,
                    version=excluded.version,
                    last_used_at=excluded.last_used_at,
                    usage_count=excluded.usage_count,
                    success_count=excluded.success_count,
                    failure_count=excluded.failure_count,
                    success_rate=excluded.success_rate,
                    avg_latency_ms=excluded.avg_latency_ms,
                    status=excluded.status,
                    genome_json=excluded.genome_json,
                    lineage_json=excluded.lineage_json,
                    fitness_score=excluded.fitness_score
                """,
                (
                    manifest.agent_id,
                    manifest.name,
                    manifest.description,
                    manifest.embedding_model,
                    len(manifest.embedding_vector),
                    json.dumps(manifest.embedding_vector),
                    json.dumps(manifest.tools_allowlist),
                    json.dumps(manifest.tags),
                    json.dumps(manifest.api_capabilities),
                    json.dumps(manifest.requires_approval_for),
                    json.dumps(manifest.skill_ids),
                    manifest.version,
                    manifest.created_at,
                    manifest.last_used_at,
                    manifest.usage_count,
                    manifest.success_count,
                    manifest.failure_count,
                    manifest.success_rate,
                    manifest.avg_latency_ms,
                    manifest.status,
                    manifest.genome_json,
                    manifest.lineage_json,
                    manifest.fitness_score,
                ),
            )
            if self.sqlite_vec_available and self._has_agent_vectors_table(conn):
                vector = json.dumps(manifest.embedding_vector)
                conn.execute("DELETE FROM agent_vectors WHERE agent_id = ?", (manifest.agent_id,))
                conn.execute(
                    "INSERT INTO agent_vectors (agent_id, embedding) VALUES (?, ?)",
                    (manifest.agent_id, vector),
                )

    def get_manifest(self, agent_id: str) -> AgentManifest | None:
        """Run get manifest."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                return None
            # skill_ids_json may not exist in older databases
            skill_ids_raw = row["skill_ids_json"] if "skill_ids_json" in row.keys() else "[]"
            keys = row.keys()
            return AgentManifest(
                agent_id=row["agent_id"],
                name=row["name"],
                description=row["description"],
                embedding_model=row["embedding_model"],
                embedding_vector=json.loads(row["embedding_json"]),
                tools_allowlist=json.loads(row["tools_allowlist_json"]),
                tags=json.loads(row["tags_json"]),
                api_capabilities=json.loads(row["api_capabilities_json"]),
                requires_approval_for=json.loads(row["requires_approval_for_json"]),
                skill_ids=json.loads(skill_ids_raw or "[]"),
                version=row["version"],
                created_at=row["created_at"],
                last_used_at=row["last_used_at"],
                usage_count=row["usage_count"],
                success_count=row["success_count"],
                failure_count=row["failure_count"],
                success_rate=row["success_rate"],
                avg_latency_ms=row["avg_latency_ms"],
                status=row["status"],
                genome_json=row["genome_json"] if "genome_json" in keys else "{}",
                lineage_json=row["lineage_json"] if "lineage_json" in keys else "{}",
                fitness_score=float(row["fitness_score"]) if "fitness_score" in keys else 0.0,
            )

    def semantic_search(self, query_vector: list[float], k: int = 5) -> list[SearchCandidate]:
        """Run semantic search."""
        with self._conn() as conn:
            if self.sqlite_vec_available and self._has_agent_vectors_table(conn):
                vector = json.dumps(query_vector)
                rows = conn.execute(
                    """
                    SELECT v.agent_id AS agent_id, distance, a.description
                    FROM agent_vectors v
                    JOIN agents a ON a.agent_id = v.agent_id
                    WHERE v.embedding MATCH ? AND k = ? AND a.status = 'active'
                    ORDER BY distance ASC
                    """,
                    (vector, k),
                ).fetchall()
                out: list[SearchCandidate] = []
                for r in rows:
                    score = max(0.0, 1.0 - float(r["distance"]))
                    out.append(
                        SearchCandidate(
                            agent_id=r["agent_id"],
                            score=score,
                            description=r["description"],
                        )
                    )
                return out

            rows = conn.execute(
                "SELECT agent_id, description, embedding_json FROM agents WHERE status = 'active'"
            ).fetchall()
            scored: list[SearchCandidate] = []
            for row in rows:
                score = _cosine_similarity(query_vector, json.loads(row["embedding_json"]))
                scored.append(
                    SearchCandidate(
                        agent_id=row["agent_id"],
                        score=score,
                        description=row["description"],
                    )
                )
            scored.sort(key=lambda item: item.score, reverse=True)
            return scored[:k]

    def record_run(
        self,
        run_id: str,
        agent_id: str,
        task: str,
        status: str,
        latency_ms: float | None,
        error_message: str | None = None,
    ) -> None:
        """Run record run."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, agent_id, task, status, latency_ms, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    agent_id = excluded.agent_id,
                    task = excluded.task,
                    status = excluded.status,
                    latency_ms = excluded.latency_ms,
                    error_message = excluded.error_message
                """,
                (run_id, agent_id, task, status, latency_ms, error_message),
            )
            now = datetime.now(UTC).isoformat()
            if latency_ms is not None:
                conn.execute(
                    """
                    UPDATE agents
                    SET usage_count = usage_count + 1,
                        success_count = success_count + CASE WHEN ? = 'ok' THEN 1 ELSE 0 END,
                        failure_count = failure_count + CASE WHEN ? != 'ok' THEN 1 ELSE 0 END,
                        success_rate = CAST(
                            success_count + CASE WHEN ? = 'ok' THEN 1 ELSE 0 END AS REAL
                        ) / (usage_count + 1),
                        avg_latency_ms = CASE
                            WHEN avg_latency_ms IS NOT NULL
                            THEN (avg_latency_ms * usage_count + ?) / (usage_count + 1)
                            ELSE ?
                        END,
                        last_used_at = ?
                    WHERE agent_id = ?
                    """,
                    (status, status, status, latency_ms, latency_ms, now, agent_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE agents
                    SET usage_count = usage_count + 1,
                        success_count = success_count + CASE WHEN ? = 'ok' THEN 1 ELSE 0 END,
                        failure_count = failure_count + CASE WHEN ? != 'ok' THEN 1 ELSE 0 END,
                        success_rate = CAST(
                            success_count + CASE WHEN ? = 'ok' THEN 1 ELSE 0 END AS REAL
                        ) / (usage_count + 1),
                        last_used_at = ?
                    WHERE agent_id = ?
                    """,
                    (status, status, status, now, agent_id),
                )

    def open_circuit_if_needed(self, provider: str, threshold: int) -> bool:
        """Run open circuit if needed."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT failure_count FROM provider_circuit_breakers WHERE provider = ?",
                (provider,),
            ).fetchone()
            count = int(row["failure_count"]) + 1 if row else 1
            opened = count >= threshold
            conn.execute(
                """
                INSERT INTO provider_circuit_breakers (provider, failure_count, opened_at)
                VALUES (?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
                ON CONFLICT(provider) DO UPDATE SET
                  failure_count = excluded.failure_count,
                  opened_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE provider_circuit_breakers.opened_at END
                """,
                (provider, count, 1 if opened else 0, 1 if opened else 0),
            )
            return opened

    def is_circuit_open(self, provider: str, reset_after_sec: int) -> bool:
        """Run is circuit open."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT opened_at FROM provider_circuit_breakers WHERE provider = ?",
                (provider,),
            ).fetchone()
            if row is None or row["opened_at"] is None:
                return False
            opened_at = datetime.fromisoformat(row["opened_at"].replace(" ", "T"))
            elapsed = (datetime.now(UTC) - opened_at.replace(tzinfo=UTC)).total_seconds()
            if elapsed >= reset_after_sec:
                self.reset_circuit(provider)
                return False
            return True

    def reset_circuit(self, provider: str) -> None:
        """Run reset circuit."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO provider_circuit_breakers (provider, failure_count, opened_at)
                VALUES (?, 0, NULL)
                ON CONFLICT(provider) DO UPDATE SET failure_count = 0, opened_at = NULL
                """,
                (provider,),
            )

    def update_agent_status(self, agent_id: str, status: str) -> None:
        """Update the status field of an agent (e.g. 'active' -> 'degraded')."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE agents SET status = ? WHERE agent_id = ?",
                (status, agent_id),
            )

    def update_fitness(self, agent_id: str, fitness_score: float) -> None:
        """Update the fitness_score of an agent after a run evaluation."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE agents SET fitness_score = ? WHERE agent_id = ?",
                (fitness_score, agent_id),
            )

    def find_agents_by_tag(self, tag: str, status: str = "active") -> list[dict[str, object]]:
        """Return agents whose tags_json contains *tag*, filtered by status."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT agent_id, tags_json, genome_json, lineage_json, fitness_score, status "
                "FROM agents WHERE status = ?",
                (status,),
            ).fetchall()
            results: list[dict[str, object]] = []
            for row in rows:
                tags = json.loads(row["tags_json"]) if row["tags_json"] else []
                if tag in tags:
                    results.append(dict(row))
            return results

    def list_agents(self, limit: int = 100) -> list[dict[str, object]]:
        """Run list agents."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT agent_id, name, description, status, usage_count, success_rate, last_used_at
                FROM agents
                ORDER BY usage_count DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_runs(self, limit: int = 100) -> list[dict[str, object]]:
        """Run list runs."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT run_id, agent_id, task, status, latency_ms, error_message, created_at
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_agent(self, agent_id: str) -> bool:
        """Run delete agent."""
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if exists is None:
                return False
            if self.sqlite_vec_available and self._has_agent_vectors_table(conn):
                conn.execute("DELETE FROM agent_vectors WHERE agent_id = ?", (agent_id,))
            conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
            conn.execute(
                "DELETE FROM agent_compositions WHERE agent_a = ? OR agent_b = ?",
                (agent_id, agent_id),
            )
            return True


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Run cosine similarity."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        import logging
        logging.getLogger("closed_claw.registry").warning(
            "cosine_similarity: dimension mismatch (%d vs %d), returning 0.0",
            len(a), len(b),
        )
        return 0.0
    n = len(a)
    dot = sum(a[i] * b[i] for i in range(n))
    norm_a = math.sqrt(sum(a[i] * a[i] for i in range(n)))
    norm_b = math.sqrt(sum(b[i] * b[i] for i in range(n)))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))

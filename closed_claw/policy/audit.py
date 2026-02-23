from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from closed_claw.policy.approval import ApprovalDecision, ApprovalRequest


class AuditStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_type TEXT NOT NULL,
                  run_id TEXT,
                  agent_id TEXT,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def record_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (event_type, run_id, agent_id, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (event_type, run_id, agent_id, json.dumps(payload)),
            )

    def record_approval(
        self,
        req: ApprovalRequest,
        decision: ApprovalDecision,
        run_id: str,
        agent_id: str,
    ) -> None:
        self.record_event(
            event_type="approval_decision",
            payload={"request": req.model_dump(), "decision": decision.model_dump()},
            run_id=run_id,
            agent_id=agent_id,
        )

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, run_id, agent_id, payload_json, created_at
                FROM audit_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                item = dict(r)
                item["payload"] = json.loads(item.pop("payload_json"))
                out.append(item)
            return out

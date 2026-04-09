# Purpose: Run log event writer for per-run observability traces.

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("closed_claw.observability.runlog")


class RunLogger:
    def __init__(self, base_dir: Path, run_id: str) -> None:
        """Initialize the instance."""
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / f"{run_id}.jsonl"
        self._lock = threading.Lock()

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        """Append a JSON event line atomically (thread-safe)."""
        line = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "payload": payload,
        }
        data = json.dumps(line) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(data)

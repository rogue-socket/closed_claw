# Purpose: Embedding provider integration and vector generation helpers.

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EmbeddingProvider:
    model_name: str
    dim: int
    _model: Any = field(default=None, init=False, repr=False)
    _init_attempted: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        """Run post init."""
        self._model = None

    def embed(self, text: str) -> list[float]:
        """Run embed."""
        if not self._init_attempted:
            self._init_attempted = True
            enabled = os.getenv("CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS", "false").lower()
            if enabled in {"1", "true", "yes"}:
                try:
                    from sentence_transformers import SentenceTransformer  # type: ignore

                    self._model = SentenceTransformer(self.model_name)
                except Exception:
                    self._model = None
        if self._model is not None:
            vec = self._model.encode(text)
            return [float(x) for x in vec.tolist()]

        # Deterministic fallback to keep local execution functional without model download.
        import logging
        logging.getLogger("closed_claw.embeddings").warning(
            "sentence-transformers not available — using SHA-256 fallback. "
            "Agent semantic search will be NON-FUNCTIONAL (random rankings). "
            "Set CLOSED_CLAW_ENABLE_SENTENCE_TRANSFORMERS=true and install "
            "sentence-transformers to enable real embeddings."
        )
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        base = [b / 255.0 for b in digest]
        out: list[float] = []
        while len(out) < self.dim:
            out.extend(base)
        return out[: self.dim]

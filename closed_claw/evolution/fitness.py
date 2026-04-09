# Purpose: Multi-dimensional fitness evaluation for agent runs.

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_WEIGHTS = {
    "completion": 0.40,
    "tool_efficiency": 0.25,
    "verification": 0.20,
    "speed": 0.15,
}


@dataclass(slots=True)
class FitnessScore:
    """Multi-dimensional fitness evaluation of an agent run."""

    completion: float = 0.0
    tool_efficiency: float = 0.0
    verification: float = 0.0
    speed: float = 0.0

    def aggregate(self, weights: dict[str, float] | None = None) -> float:
        """Weighted sum of all fitness dimensions."""
        w = weights or DEFAULT_WEIGHTS
        return (
            self.completion * w.get("completion", 0.4)
            + self.tool_efficiency * w.get("tool_efficiency", 0.25)
            + self.verification * w.get("verification", 0.2)
            + self.speed * w.get("speed", 0.15)
        )


def evaluate_fitness(
    *,
    task_succeeded: bool,
    tool_events: list[dict],
    verification_passed: bool,
    latency_ms: float | None,
) -> FitnessScore:
    """Evaluate an agent's fitness from a single run."""
    completion = 1.0 if task_succeeded else 0.0

    total_calls = len(tool_events)
    ok_calls = sum(1 for e in tool_events if e.get("ok"))
    tool_efficiency = ok_calls / total_calls if total_calls > 0 else 0.5

    verification = 1.0 if verification_passed else 0.0

    # Normalise speed: 30s or less → 1.0, degrades toward 0
    if latency_ms is not None and latency_ms > 0:
        speed = min(1.0, 30_000 / latency_ms)
    else:
        speed = 0.5

    return FitnessScore(
        completion=completion,
        tool_efficiency=tool_efficiency,
        verification=verification,
        speed=speed,
    )

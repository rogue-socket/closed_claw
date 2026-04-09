# Purpose: Agent genome model — the DNA that drives agent behaviour evolution.

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field

STRATEGY_HINTS = [
    "Think step-by-step, breaking the problem into small manageable pieces.",
    "Take a holistic view first, then dive into details as needed.",
    "Focus on the critical path first; handle edge cases after the core works.",
    "Be thorough and exhaustive — consider all possibilities before acting.",
    "Move fast and prioritise the simplest correct solution.",
    "Validate assumptions before proceeding with implementation.",
]

PERSONA_TRAITS = [
    "cautious",
    "thorough",
    "creative",
    "methodical",
    "pragmatic",
    "precise",
]


@dataclass(slots=True)
class Genome:
    """An agent's behavioural DNA — influences skill prompts, tool ordering, and LLM config."""

    strategy_hint: str = ""
    tool_preferences: list[str] = field(default_factory=list)
    temperature: float = 0.3
    max_iterations: int = 12
    persona_traits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strategy_hint": self.strategy_hint,
            "tool_preferences": list(self.tool_preferences),
            "temperature": self.temperature,
            "max_iterations": self.max_iterations,
            "persona_traits": list(self.persona_traits),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Genome:
        if not d:
            return cls()
        return cls(
            strategy_hint=d.get("strategy_hint", ""),
            tool_preferences=list(d.get("tool_preferences", [])),
            temperature=float(d.get("temperature", 0.3)),
            max_iterations=int(d.get("max_iterations", 12)),
            persona_traits=list(d.get("persona_traits", [])),
        )

    def hash(self) -> str:
        """Deterministic hash of this genome's genes."""
        raw = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @classmethod
    def random(cls, tools: list[str] | None = None) -> Genome:
        """Generate a random generation-0 genome."""
        strategy = random.choice(STRATEGY_HINTS)
        traits = random.sample(PERSONA_TRAITS, k=random.randint(1, 3))
        temp = round(random.uniform(0.1, 0.7), 2)
        iterations = random.randint(8, 16)
        tool_prefs = list(tools or [])
        random.shuffle(tool_prefs)
        return cls(
            strategy_hint=strategy,
            tool_preferences=tool_prefs,
            temperature=temp,
            max_iterations=iterations,
            persona_traits=traits,
        )

    def apply_to_skill(self, skill_md: str) -> str:
        """Inject genome-driven hints into a skill.md document."""
        parts: list[str] = []
        if self.strategy_hint:
            parts.append(f"## Approach\n{self.strategy_hint}")
        if self.persona_traits:
            trait_str = ", ".join(self.persona_traits)
            parts.append(f"## Behavioural Traits\nBe {trait_str} in your approach.")
        if parts:
            prefix = "\n\n".join(parts)
            return f"{prefix}\n\n---\n\n{skill_md}" if skill_md else prefix
        return skill_md


@dataclass(slots=True)
class Lineage:
    """Tracks an agent's evolutionary ancestry."""

    parent_genome_hash: str | None = None
    generation: int = 0
    mutations_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "parent_genome_hash": self.parent_genome_hash,
            "generation": self.generation,
            "mutations_applied": list(self.mutations_applied),
        }

    @classmethod
    def from_dict(cls, d: dict) -> Lineage:
        if not d:
            return cls()
        return cls(
            parent_genome_hash=d.get("parent_genome_hash"),
            generation=int(d.get("generation", 0)),
            mutations_applied=list(d.get("mutations_applied", [])),
        )

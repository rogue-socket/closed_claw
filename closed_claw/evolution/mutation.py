# Purpose: Genome mutation engine — introduces variation in each generation.

from __future__ import annotations

import random

from closed_claw.evolution.genome import (
    Genome,
    Lineage,
    PERSONA_TRAITS,
    STRATEGY_HINTS,
)


def mutate_genome(
    parent: Genome,
    mutation_rate: float = 0.3,
) -> tuple[Genome, Lineage]:
    """Create a child genome by mutating the parent.

    Returns ``(child_genome, lineage)`` describing what changed.
    """
    child = Genome(
        strategy_hint=parent.strategy_hint,
        tool_preferences=list(parent.tool_preferences),
        temperature=parent.temperature,
        max_iterations=parent.max_iterations,
        persona_traits=list(parent.persona_traits),
    )
    mutations: list[str] = []

    # Each gene has an independent chance of mutating
    if random.random() < mutation_rate:
        old = child.strategy_hint
        candidates = [s for s in STRATEGY_HINTS if s != old]
        if candidates:
            child.strategy_hint = random.choice(candidates)
            mutations.append("strategy_hint")

    if random.random() < mutation_rate and len(child.tool_preferences) >= 2:
        i, j = random.sample(range(len(child.tool_preferences)), 2)
        child.tool_preferences[i], child.tool_preferences[j] = (
            child.tool_preferences[j],
            child.tool_preferences[i],
        )
        mutations.append(f"tool_swap:{i}<->{j}")

    if random.random() < mutation_rate:
        old_temp = child.temperature
        delta = random.choice([-0.1, -0.05, 0.05, 0.1])
        child.temperature = round(max(0.0, min(1.0, child.temperature + delta)), 2)
        if child.temperature != old_temp:
            mutations.append(f"temperature:{old_temp}->{child.temperature}")

    if random.random() < mutation_rate:
        old_iter = child.max_iterations
        delta = random.choice([-2, -1, 1, 2])
        child.max_iterations = max(4, min(20, child.max_iterations + delta))
        if child.max_iterations != old_iter:
            mutations.append(f"max_iterations:{old_iter}->{child.max_iterations}")

    if random.random() < mutation_rate:
        all_traits = set(PERSONA_TRAITS)
        current = set(child.persona_traits)
        if random.random() < 0.5 and current:
            removed = random.choice(list(current))
            child.persona_traits = [t for t in child.persona_traits if t != removed]
            mutations.append(f"remove_trait:{removed}")
        else:
            available = list(all_traits - current)
            if available:
                added = random.choice(available)
                child.persona_traits.append(added)
                mutations.append(f"add_trait:{added}")

    # If nothing mutated (unlikely but possible), force at least one change
    if not mutations:
        old_temp = child.temperature
        child.temperature = round(max(0.0, min(1.0, child.temperature + 0.05)), 2)
        mutations.append(f"temperature:{old_temp}->{child.temperature}")

    lineage = Lineage(
        parent_genome_hash=parent.hash(),
        generation=0,  # caller should set the real generation
        mutations_applied=mutations,
    )
    return child, lineage

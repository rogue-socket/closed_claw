# Purpose: Ancestor selection — find the fittest genome to use as parent for the next generation.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from closed_claw.evolution.genome import Genome

if TYPE_CHECKING:
    from closed_claw.registry.store import RegistryStore


def select_ancestor_genome(
    registry: RegistryStore,
    profile_id: str,
) -> tuple[Genome, int] | None:
    """Find the fittest active agent with *profile_id* in its tags and return its genome.

    Returns ``(genome, generation)`` of the best ancestor, or ``None`` if no
    suitable ancestor exists (i.e. this will be a generation-0 agent).
    """
    agents = registry.find_agents_by_tag(profile_id)
    if not agents:
        return None

    # Sort by fitness_score descending — fittest first
    agents.sort(key=lambda a: a.get("fitness_score", 0.0), reverse=True)
    best = agents[0]

    # Parse genome
    genome_raw = best.get("genome_json", "{}")
    try:
        genome_dict = json.loads(genome_raw) if isinstance(genome_raw, str) else genome_raw
    except (json.JSONDecodeError, TypeError):
        return None

    if not genome_dict:
        return None

    # Parse lineage for generation number
    lineage_raw = best.get("lineage_json", "{}")
    try:
        lineage_dict = json.loads(lineage_raw) if isinstance(lineage_raw, str) else lineage_raw
    except (json.JSONDecodeError, TypeError):
        lineage_dict = {}

    generation = int(lineage_dict.get("generation", 0))
    return Genome.from_dict(genome_dict), generation

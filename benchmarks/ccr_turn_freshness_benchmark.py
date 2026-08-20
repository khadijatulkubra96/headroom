"""Deterministic benchmark for CCR proactive-expansion freshness.

This benchmark models a coding-agent workload: the agent repeatedly searches,
reads, and revisits files. Several compressed contexts remain wall-clock fresh,
but some belong to tasks that ended many turns ago.

It compares the legacy wall-clock-only policy with the turn-aware policy using
identical contexts, queries, and timestamps. No LLM/API calls are required.
"""

from __future__ import annotations

import json
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Allow running the script directly from the repository checkout.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from headroom.ccr.context_tracker import ContextTracker, ContextTrackerConfig


@dataclass(frozen=True)
class Task:
    name: str
    query: str
    content: str


TASKS = [
    Task("authentication", "show authentication middleware", "authentication middleware auth_handler.py security"),
    Task("database", "inspect database migration", "database migration schema models.py sqlalchemy"),
    Task("routing", "fix API routing handler", "api routing handler router.py endpoint request"),
    Task("testing", "find failing integration tests", "integration tests pytest test_api.py fixture"),
    Task("configuration", "update service configuration", "service configuration settings.yaml environment"),
    Task("caching", "inspect cache invalidation", "cache invalidation redis cache.py ttl"),
    Task("logging", "change request logging", "request logging logger.py structured logs trace"),
    Task("payments", "debug payment webhook", "payment webhook stripe handler.py signature retry"),
]


@dataclass
class Result:
    scenarios: int
    relevant_retrievals: int
    stale_retrievals: int
    total_recommendations: int
    scenarios_with_relevant: int
    scenarios_with_stale: int
    relevant_rate: float
    stale_recommendation_rate: float
    avg_recommendations: float


def run(policy: str, scenarios_per_task: int = 25) -> Result:
    if policy == "legacy":
        config = ContextTrackerConfig(
            max_context_turn_distance=None,
            turn_decay_half_life=None,
        )
    elif policy == "turn_aware":
        config = ContextTrackerConfig()
    else:
        raise ValueError(policy)

    relevant_retrievals = 0
    stale_retrievals = 0
    total_recommendations = 0
    scenarios_with_relevant = 0
    scenarios_with_stale = 0
    scenarios = 0

    for task in TASKS:
        for i in range(scenarios_per_task):
            scenarios += 1
            current_turn = 100 + i
            tracker = ContextTracker(config)

            entries = [
                (f"{task.name}-stale-30-{i}", current_turn - 30),
                (f"{task.name}-stale-25-{i}", current_turn - 25),
                (f"{task.name}-recent-{i}", current_turn - 3),
            ]
            random.Random(i + TASKS.index(task) * 10_000).shuffle(entries)

            for hash_key, turn_number in entries:
                tracker.track_compression(
                    hash_key=hash_key,
                    turn_number=turn_number,
                    tool_name="Grep",
                    original_count=500,
                    compressed_count=20,
                    query_context=task.query,
                    sample_content=task.content,
                    workspace_key="benchmark",
                )

            # Normalize timestamps so this benchmark isolates conversational
            # distance rather than tiny wall-clock differences.
            now = time.time()
            for context in tracker._contexts.values():  # intentional benchmark instrumentation
                context.timestamp = now

            recommendations = tracker.analyze_query(
                task.query,
                current_turn=current_turn,
                workspace_key="benchmark",
            )
            total_recommendations += len(recommendations)

            hashes = {r.hash_key for r in recommendations}
            relevant = any("recent" in h for h in hashes)
            stale = any("stale-" in h for h in hashes)
            if relevant:
                relevant_retrievals += 1
                scenarios_with_relevant += 1
            if stale:
                stale_retrievals += sum("stale-" in h for h in hashes)
                scenarios_with_stale += 1

    total = scenarios
    return Result(
        scenarios=total,
        relevant_retrievals=relevant_retrievals,
        stale_retrievals=stale_retrievals,
        total_recommendations=total_recommendations,
        scenarios_with_relevant=scenarios_with_relevant,
        scenarios_with_stale=scenarios_with_stale,
        relevant_rate=relevant_retrievals / total,
        stale_recommendation_rate=(
            stale_retrievals / total_recommendations if total_recommendations else 0.0
        ),
        avg_recommendations=total_recommendations / total,
    )


def main() -> None:
    legacy = run("legacy")
    turn_aware = run("turn_aware")

    output = {
        "benchmark": "CCR turn-freshness coding-agent workload",
        "tasks": len(TASKS),
        "scenarios_per_task": 25,
        "total_scenarios": legacy.scenarios,
        "policies": {
            "legacy": legacy.__dict__,
            "turn_aware": turn_aware.__dict__,
        },
        "deltas": {
            "relevant_rate_pp": (turn_aware.relevant_rate - legacy.relevant_rate) * 100,
            "stale_recommendation_rate_pp": (
                turn_aware.stale_recommendation_rate - legacy.stale_recommendation_rate
            ) * 100,
            "stale_retrievals_pct_change": (
                (turn_aware.stale_retrievals - legacy.stale_retrievals)
                / legacy.stale_retrievals
                * 100
                if legacy.stale_retrievals
                else 0.0
            ),
            "avg_recommendations_delta": turn_aware.avg_recommendations - legacy.avg_recommendations,
        },
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()

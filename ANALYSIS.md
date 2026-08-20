# Analysis: Turn-aware freshness for CCR proactive expansion

## Executive summary

Headroom's CCR (Compress-Cache-Retrieve) system is one of the most interesting parts of the repository because it makes aggressive compression reversible: tool output can be compressed while the original remains available for later retrieval. The repository also has a Context Tracker that can proactively expand earlier compressed output when a new query appears relevant.

I found a concrete weakness in that tracker: freshness was based on wall-clock age only. The tracker already stored a compression `turn_number` and already accepted `current_turn`, but neither affected the freshness decision. This is particularly problematic for fast coding-agent sessions, where many tool/compression turns can happen inside a five-minute TTL. Upstream issue [#709](https://github.com/headroomlabs-ai/headroom/issues/709) describes this exact failure mode.

I implemented a proof of concept that adds conversational-distance freshness. A context can now be rejected after a configurable maximum number of compression turns, and its relevance score can additionally decay exponentially with turn distance. The default proof-of-concept policy is a 20-turn hard limit and a 10-turn relevance half-life.

The deterministic benchmark contains 200 coding-agent-style scenarios. Compared with the legacy wall-clock-only policy, the extension increased recovery of the intended recent context from **67% to 100%**, reduced stale recommendations from **66.5% of recommendations to 0%**, and reduced average recommendations from **2.0 to 1.0 per scenario**. These numbers measure the targeted failure mode, not end-to-end model quality; an external coding-agent benchmark was not run because no model/provider credentials were available in this environment.

## 1. Repository features exercised

I inspected the CCR implementation, Context Tracker, proxy configuration, tests, and evaluation suite. The most useful features exercised were:

- **CCR Context Tracker:** tracking compressed contexts, workspace isolation, relevance matching, age-based filtering, recommendation ranking, and proactive expansion.
- **Workspace safety:** the tracker fails closed when no workspace is supplied and only compares contexts belonging to the same workspace. Existing tests cover this important cross-project boundary.
- **Claude Code compact-summary filtering:** the tracker deliberately avoids treating Claude Code `/compact` summaries as ordinary proactively expandable tool output.
- **CCR marker/retrieval behavior:** the existing marker-policy and marker-resolution tests were run alongside the tracker tests.
- **Compression evaluation code:** the repository's compression evaluation tests were executed successfully.

The focused regression suite after the change was **81 passed**. The initial broader CCR run exposed an environment/build issue unrelated to this extension: `tests/test_ccr.py` hit `ModuleNotFoundError: headroom._core` because the uploaded source tree did not contain the compiled Rust/PyO3 extension and this environment has neither `maturin` nor `cargo` installed. The same missing native component prevented a full Rust build. I did not treat that failure as an extension failure.

## 2. Extension

### Problem

Before the change, `ContextTracker.analyze_query()` effectively did this:

1. discard a context if its timestamp is older than `max_context_age_seconds` (300 seconds by default);
2. calculate keyword relevance;
3. apply a wall-clock age discount;
4. recommend the highest-scoring contexts.

The tracker already had all information needed to recognize conversational staleness, but `context.turn_number` was not used. Therefore a context compressed at turn 1 could still be considered fresh at turn 30 if only a few seconds had elapsed.

### Implementation

The extension adds two `ContextTrackerConfig` parameters:

- `max_context_turn_distance: int | None = 20`
- `turn_decay_half_life: float | None = 10.0`

The recommendation path now computes:

```text
turn_distance = max(0, current_turn - context.turn_number)
```

Contexts beyond `max_context_turn_distance` are skipped. Remaining relevance receives the multiplier:

```text
0.5 ** (turn_distance / turn_decay_half_life)
```

The existing wall-clock discount remains in place, so the extension combines two independent notions of freshness rather than replacing the existing TTL.

Both controls accept `None`, which preserves the legacy wall-clock-only behavior. This makes the change easy to compare experimentally and provides a compatibility escape hatch.

I also added regression tests for hard filtering, preservation of recent contexts, score decay, and explicit legacy-mode behavior.

### Why this extension is meaningful

This is deliberately narrow. The tracker is already a retrieval decision-maker; adding a second freshness signal uses data already present in its model instead of introducing a new embedding model, persistent store, or expensive inference step. It directly addresses a documented failure mode and should have negligible computational cost relative to the rest of an LLM request.

## 3. Evaluation design

### Selected coding-agent target

The repository explicitly supports coding agents including Claude Code. I selected a **Claude Code-style fast coding-agent workload** as the target because the failure mode in issue #709 is specifically about repeated tool/compression turns in such sessions.

I did not claim an end-to-end Claude Code/model benchmark. No external model credentials were available. Instead, I built a deterministic benchmark around the exact Context Tracker interface. This isolates the proposed change and makes the comparison reproducible without model randomness or API latency.

### Benchmark tasks

The benchmark uses eight representative coding tasks:

- authentication middleware
- database migrations
- API routing
- integration tests
- service configuration
- cache invalidation
- request logging
- payment webhooks

Each task creates three compressed contexts with identical task-related keywords:

- a **recent** context, three compression turns old;
- a stale context, 25 turns old;
- a stale context, 30 turns old.

Timestamps are normalized so that wall-clock age cannot explain the result. The insertion order is deterministically shuffled across 200 scenarios, preventing the result from depending on one fixed ordering.

The tracker is limited to two proactive recommendations, matching the production default. A recent context is considered the intended retrieval; the 25- and 30-turn contexts are the stale cases the extension is designed to suppress.

Two policies are compared:

1. **Legacy:** `max_context_turn_distance=None` and `turn_decay_half_life=None`, reproducing the previous wall-clock-only behavior.
2. **Turn-aware:** the new defaults, 20-turn maximum distance and 10-turn half-life.

The benchmark source is [benchmarks/ccr_turn_freshness_benchmark.py](benchmarks/ccr_turn_freshness_benchmark.py); the recorded output is [benchmarks/ccr_turn_freshness_results.json](benchmarks/ccr_turn_freshness_results.json).

## 4. Results

| Metric | Legacy | Turn-aware | Change |
|---|---:|---:|---:|
| Scenarios | 200 | 200 | — |
| Scenarios retrieving intended recent context | 67% | **100%** | **+33 pp** |
| Stale recommendations / all recommendations | 66.5% | **0%** | **-66.5 pp** |
| Stale recommendations | 266 | **0** | **-100%** |
| Average recommendations / scenario | 2.0 | **1.0** | -1.0 |

The result is directionally strong: the legacy policy frequently spends its two recommendation slots on contexts that are conversationally obsolete, while the turn-aware policy removes those candidates before ranking. Because the recent context is only three turns old, it remains eligible and receives the highest turn-decay score.

The reduction from two recommendations to one is also important. Proactive expansion is not free: it increases prompt size and can distract the model. Suppressing stale candidates reduces unnecessary context injection in addition to improving which context is selected.

## 5. Correctness checks

The new tests verify four specific invariants:

1. a context beyond the configured turn distance is never recommended;
2. a recent context remains recommendable;
3. turn decay monotonically lowers an otherwise identical context's score;
4. setting both new parameters to `None` preserves legacy behavior.

After implementation, the focused suite passed:

```text
81 passed in 3.02s
```

The repository's broader CCR run had one unrelated native-extension failure (`headroom._core` missing); the tracker, feedback, marker-resolution, marker-policy, and compression-evaluation tests all passed in the focused run. Static lint could not be executed because the uploaded environment did not include the `ruff` executable. Likewise, Rust tests could not be executed because `cargo` was unavailable.

## 6. Limitations and next steps

The benchmark intentionally isolates freshness behavior. It does **not** prove that a real LLM produces better answers, nor does it establish the optimal 20-turn limit or 10-turn half-life. Those values should be tuned from production traces or an end-to-end coding-agent benchmark.

The benchmark also uses deliberately high lexical overlap so that stale contexts are otherwise eligible. This is appropriate for reproducing the reported failure, but it is not a general semantic-retrieval benchmark. A stronger follow-up would replay anonymized Claude Code/Codex traces with human- or model-labeled relevance and measure answer correctness, injected-token count, latency, and cost.

A production follow-up should expose the two new settings through the proxy configuration/CLI and collect telemetry for turn-distance distributions. An adaptive policy could then learn a task- or agent-specific turn half-life rather than using a universal constant.

## Conclusion

The proof of concept demonstrates that conversational distance is a useful freshness signal for CCR proactive expansion. The implementation is small, backwards-compatible when disabled, covered by regression tests, and directly targets a documented failure mode. On the controlled coding-agent workload, it eliminated stale recommendations while improving recovery of the intended recent context. The remaining uncertainty is not whether turn distance is useful in the targeted scenario, but how the thresholds should be calibrated for real agent traces and whether the improvement translates into measurable end-to-end task success and token/cost savings.

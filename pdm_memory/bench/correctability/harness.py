"""
Correctability Benchmark — Main Harness
========================================

Implements the core ``run_suite()`` function that drives the feedback loop
described in Bjoern Janssen's Correctability Benchmark Spec v0.1.

One round (for one scenario, one seed):

    1. mem.recall(query, k=1, reinforce=False)   → retrieve top hit
    2. harness compares top hit to ground truth
    3a. If correct  → mem.reinforce(top_id)       E* positive signal
    3b. If wrong    → mem.penalize(top_id)         E* negative signal
    4. Record p_A, p_B, correctness for this round
    5. Check crossover: if p_B > p_A → record crossover_round; break early

All backend drivers (Memory, AblationMemory, VectorRAGBaseline, etc.) share
the same interface so the harness is agnostic to which system is under test.

Usage
-----
    from pdm_memory.bench.correctability.harness import run_suite

    report = run_suite(mode="pdm_enabled", rounds=20, seeds=[0,1,2,3,4])
    print(report.render_table())
    with open("results.json", "w") as f:
        f.write(report.to_json())
"""

from __future__ import annotations

import logging
import os
import random
import tempfile
import time
from typing import Any, Callable, List, Optional

from pdm_memory.bench.correctability.metrics import (
    RoundRecord,
    ScenarioTrace,
    CorrectabilityReport,
    build_report,
)
from pdm_memory.bench.correctability.scenarios import (
    BenchScenario,
    P_CORRECT,
    P_WRONG,
    get_scenarios,
)

logger = logging.getLogger(__name__)

# Coupling score used for all reinforce/penalize signals
_COUPLING_SCORE: float = 0.8

# Drawer name — both signatures live here so they compete on every query
_BENCH_DRAWER: str = "correctability"


# ---------------------------------------------------------------------------
# Backend factory helpers
# ---------------------------------------------------------------------------


def _make_pdm_backend(store: str, user: str) -> Any:
    from pdm_memory import Memory
    return Memory(store=store, user=user)


def _make_ablation_backend(store: str, user: str) -> Any:
    from pdm_memory.bench.correctability.ablation import AblationMemory
    return AblationMemory(store=store, user=user)


def _make_vrag_backend() -> Any:
    from pdm_memory.bench.correctability.baselines import VectorRAGBaseline
    return VectorRAGBaseline()


def _make_keyword_backend() -> Any:
    from pdm_memory.bench.correctability.baselines import KeywordRecencyBaseline
    return KeywordRecencyBaseline()


_BACKEND_FACTORIES: dict = {
    "pdm_enabled": _make_pdm_backend,
    "pdm_ablation": _make_ablation_backend,
    "vector_rag": _make_vrag_backend,
    "keyword_recency": _make_keyword_backend,
}

_NEEDS_FILE: set = {"pdm_enabled", "pdm_ablation"}


# ---------------------------------------------------------------------------
# Single-scenario runner
# ---------------------------------------------------------------------------


def _run_scenario(
    backend: Any,
    scenario: BenchScenario,
    seed: int,
    max_rounds: int,
    id_a: str,
    id_b: str,
) -> ScenarioTrace:
    """
    Run one scenario for one seed and return the full ScenarioTrace.

    Args:
        backend:    Memory-compatible backend (PDM / ablation / baseline).
        scenario:   The BenchScenario being tested.
        seed:       The random seed for this run (recorded in trace only).
        max_rounds: Maximum number of rounds before stopping.
        id_a:       Memory ID of Signature A (wrong answer).
        id_b:       Memory ID of Signature B (correct answer).

    Returns:
        ScenarioTrace with all round records and derived metrics.
    """
    trace = ScenarioTrace(scenario_id=scenario.id, domain=scenario.domain, seed=seed)

    for round_num in range(max_rounds):
        # Snapshot current pressures BEFORE the round
        p_a_before = _get_pressure(backend, id_a)
        p_b_before = _get_pressure(backend, id_b)

        # Retrieve top-1 hit (no automatic reinforcement — we control it)
        # search_cost=1.0: maximally lower the TAS threshold so even a heavily
        # penalised signature (low p_magnitude) can still surface as a candidate.
        # min_pressure=0.0: never cut out memories that have been penalised down.
        try:
            hits = backend.recall(
                scenario.query, k=1, reinforce=False,
                search_cost=1.0, min_pressure=0.0,
            )
        except TypeError:
            # Baseline backends don't accept search_cost/min_pressure kwargs
            hits = backend.recall(scenario.query, k=1)


        if not hits:
            logger.warning(
                "[Harness] No hits for scenario %s round %d — skipping",
                scenario.id, round_num,
            )
            break

        top_hit_id = hits[0].id
        is_a = top_hit_id == id_a
        is_correct = top_hit_id == id_b  # correct = chose Signature B

        # Apply consequence signal E*
        if is_correct:
            backend.reinforce(id_b, coupling_score=_COUPLING_SCORE)
        else:
            backend.penalize(id_a, coupling_score=_COUPLING_SCORE)

        # Snapshot pressures AFTER the consequence signal
        p_a_after = _get_pressure(backend, id_a)
        p_b_after = _get_pressure(backend, id_b)

        trace.rounds.append(
            RoundRecord(
                round_number=round_num,
                p_a=p_a_after,
                p_b=p_b_after,
                top_hit_is_a=is_a,
                is_correct=is_correct,
                delta_a=round(p_a_after - p_a_before, 4),
                delta_b=round(p_b_after - p_b_before, 4),
            )
        )

        # Early exit on crossover
        if p_b_after > p_a_after:
            logger.debug(
                "[Harness] Crossover at round %d for scenario %s (seed=%d)  "
                "P_A=%.2f P_B=%.2f",
                round_num, scenario.id, seed, p_a_after, p_b_after,
            )
            break

    trace.compute_derived()
    return trace


def _get_pressure(backend: Any, memory_id: str) -> float:
    """Read p_magnitude from the backend.  Falls back to 0 if not found."""
    try:
        if hasattr(backend, "get_pressure"):
            return backend.get_pressure(memory_id) or 0.0
        # PDM Memory — access storage directly (read-only)
        rec = backend._storage.get(memory_id, user=backend._user)
        return rec.p_magnitude if rec else 0.0
    except Exception as e:
        logger.warning("[Harness] get_pressure failed for %s: %s", memory_id, e)
        return 0.0


def _seed_scenario(backend: Any, scenario: BenchScenario) -> tuple[str, str]:
    """
    Seed both competing signatures into the backend.

    Returns (id_a, id_b) — the memory IDs for the wrong and correct answers.
    """
    id_a = backend.save(
        text=scenario.wrong_answer,
        tags=scenario.tags_wrong,
        p_magnitude=P_WRONG,
        source="benchmark",
        drawer=_BENCH_DRAWER,
    )
    id_b = backend.save(
        text=scenario.correct_answer,
        tags=scenario.tags_correct,
        p_magnitude=P_CORRECT,
        source="benchmark",
        drawer=_BENCH_DRAWER,
    )
    return id_a, id_b


# ---------------------------------------------------------------------------
# Main harness entry point
# ---------------------------------------------------------------------------


def run_suite(
    mode: str = "pdm_enabled",
    rounds: int = 20,
    seeds: Optional[List[int]] = None,
    domains: Optional[List[str]] = None,
    output: Optional[str] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> CorrectabilityReport:
    """
    Run the Correctability Benchmark suite.

    Args:
        mode:        One of: "pdm_enabled", "pdm_ablation", "vector_rag",
                     "keyword_recency".
        rounds:      Maximum rounds per scenario (spec default: 20).
        seeds:       List of random seeds.  Spec default: [0, 1, 2, 3, 4].
        domains:     Optional list of domains to restrict to.
        output:      If set, save the JSON report to this file path.
        on_progress: Optional callback(completed_runs, total_runs).

    Returns:
        CorrectabilityReport with all five metrics.

    Raises:
        ValueError: If mode is not recognised.
    """
    if mode not in _BACKEND_FACTORIES:
        raise ValueError(
            f"Unknown mode '{mode}'.  Choose from: {list(_BACKEND_FACTORIES)}"
        )

    if seeds is None:
        seeds = [0, 1, 2, 3, 4]

    scenarios = get_scenarios(domains)
    total_runs = len(scenarios) * len(seeds)
    completed = 0

    logger.info(
        "[Harness] Starting correctability suite | mode=%s scenarios=%d seeds=%d "
        "rounds=%d total_runs=%d",
        mode, len(scenarios), len(seeds), rounds, total_runs,
    )

    all_traces: list[ScenarioTrace] = []
    factory = _BACKEND_FACTORIES[mode]
    needs_file = mode in _NEEDS_FILE

    for seed in seeds:
        random.seed(seed)

        for scenario in scenarios:
            # Each (seed × scenario) gets its own isolated backend
            tmp_path: Optional[str] = None

            if needs_file:
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="pdm_bench_")
                os.close(tmp_fd)
                backend = factory(store=tmp_path, user="bench")
            else:
                backend = factory()

            try:
                id_a, id_b = _seed_scenario(backend, scenario)
                trace = _run_scenario(
                    backend=backend,
                    scenario=scenario,
                    seed=seed,
                    max_rounds=rounds,
                    id_a=id_a,
                    id_b=id_b,
                )
                all_traces.append(trace)
            finally:
                backend.close()
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

            completed += 1
            if on_progress is not None:
                on_progress(completed, total_runs)

            if completed % 50 == 0 or completed == total_runs:
                logger.info(
                    "[Harness] Progress: %d/%d runs complete", completed, total_runs
                )

    report = build_report(
        traces=all_traces,
        mode=mode,
        rounds_per_scenario=rounds,
        seeds_used=seeds,
        domains_covered=sorted({s.domain for s in scenarios}),
        total_scenarios=len(scenarios),
    )

    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(report.to_json())
        logger.info("[Harness] Report saved to %s", output)

    return report

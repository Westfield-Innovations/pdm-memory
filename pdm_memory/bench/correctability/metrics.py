"""
Correctability Benchmark — Metrics Dataclasses
===============================================

Defines the five key metrics from the Correctability Benchmark spec (v0.1)
and the result dataclasses that carry per-round, per-scenario, and aggregate
data.

The five metrics
----------------
1. Crossover Round        — first round where P_B > P_A  (lower is better)
2. Memory Gravity Index   — % scenarios where wrong sig still dominates at round 20
3. Error Curve            — accuracy per round (should rise)
4. Authority Decay Slope  — dP_A/dt after first failure
5. False Demotion Rate    — % correct sigs that lose dominance due to noise
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field

# ---------------------------------------------------------------------------
# Per-round record
# ---------------------------------------------------------------------------


@dataclass
class RoundRecord:
    """State snapshot for one round of one scenario run."""

    round_number: int           # 0-indexed
    p_a: float                  # p_magnitude of wrong signature (Signature A)
    p_b: float                  # p_magnitude of correct signature (Signature B)
    top_hit_is_a: bool          # True if wrong signature was the top hit
    is_correct: bool            # True if top hit was the CORRECT signature
    delta_a: float = 0.0        # Change in P_A this round (negative = penalised)
    delta_b: float = 0.0        # Change in P_B this round (positive = reinforced)


# ---------------------------------------------------------------------------
# Per-scenario trace (one seed × one scenario)
# ---------------------------------------------------------------------------


@dataclass
class ScenarioTrace:
    """Full trace of one scenario across all rounds for one seed."""

    scenario_id: str
    domain: str
    seed: int

    rounds: list[RoundRecord] = field(default_factory=list)

    # Derived — populated by harness after all rounds complete
    crossover_round: int | None = None   # None = no crossover before max_rounds
    gravity_persists: bool = True           # True if A still dominates at final round
    false_demotion: bool = False            # True if B lost dominance after gaining it

    def compute_derived(self) -> None:
        """Compute crossover_round, gravity_persists, false_demotion from rounds."""
        crossover_found = False
        b_was_dominant = False
        b_lost_after_gaining = False

        for r in self.rounds:
            if not crossover_found and r.p_b > r.p_a:
                self.crossover_round = r.round_number
                crossover_found = True
                b_was_dominant = True
            elif crossover_found and b_was_dominant and r.p_a >= r.p_b:
                # B had crossed over but A regained dominance
                b_lost_after_gaining = True
                b_was_dominant = False

        if self.rounds:
            last = self.rounds[-1]
            self.gravity_persists = last.p_a >= last.p_b

        self.false_demotion = b_lost_after_gaining


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


@dataclass
class AuthorityDecayStats:
    """Statistics about how quickly the wrong signature (A) loses authority."""

    avg_slope: float            # Mean dP_A/round after first failure
    median_slope: float
    min_slope: float
    max_slope: float


@dataclass
class CrossoverStats:
    """Distribution of crossover rounds across all scenarios and seeds."""

    median: float | None     # None if no crossovers occurred
    mean: float | None
    std_dev: float | None
    distribution: list[int | None]  # One entry per (scenario, seed) pair; None = no crossover
    never_crossed: int          # Count of runs with no crossover


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------


@dataclass
class CorrectabilityReport:
    """
    Full result from one correctability benchmark run.

    Contains all five headline metrics, per-domain breakdowns, and the
    full trace list so results can be reproduced and plotted externally.
    """

    # --- Run metadata ---
    mode: str                           # "pdm_enabled" | "pdm_ablation" | "vector_rag"
    rounds_per_scenario: int
    seeds_used: list[int]
    domains_covered: list[str]
    total_scenarios: int
    total_runs: int                     # total_scenarios × len(seeds_used)

    # --- Metric 1: Crossover Round ---
    crossover: CrossoverStats

    # --- Metric 2: Memory Gravity Index ---
    memory_gravity_index: float         # 0.0–1.0 (fraction, NOT percentage)
    memory_gravity_pct: float           # Same, as a percentage string-friendly value

    # --- Metric 3: Error Curve ---
    accuracy_per_round: list[float]     # length = rounds_per_scenario

    # --- Metric 4: Authority Decay Slope ---
    authority_decay: AuthorityDecayStats

    # --- Metric 5: False Demotion Rate ---
    false_demotion_rate: float          # 0.0–1.0
    false_demotion_pct: float

    # --- Per-domain breakdown ---
    gravity_by_domain: dict[str, float] = field(default_factory=dict)

    # --- Raw traces (full data) ---
    traces: list[ScenarioTrace] = field(default_factory=list)

    # ---------------------------------------------------------------------------
    # Human-readable rendering
    # ---------------------------------------------------------------------------

    def render_table(self) -> str:
        """Return a formatted ASCII table of headline metrics."""
        crossover_med = (
            f"{self.crossover.median:.1f}" if self.crossover.median is not None else "never"
        )
        lines = [
            "╔══════════════════════════════════════════════════════════════════════╗",
            "║          PDM Correctability Benchmark Results                        ║",
            f"║  Mode: {self.mode:<62}║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            f"║  1. Crossover Round (median):      {crossover_med:<34}║",
            f"║     Never crossed over:            {self.crossover.never_crossed:<34}║",
            f"║  2. Memory Gravity Index:          {self.memory_gravity_pct:.1f}%{'':<31}║",
            (f"║  3. Error Curve (round 1→last):    {self.accuracy_per_round[0]*100:.1f}% → "
            f"{self.accuracy_per_round[-1]*100:.1f}%{'':<22}║"),
            f"║  4. Authority Decay Slope (avg):   {self.authority_decay.avg_slope:.3f} p/round{'':<24}║",
            f"║  5. False Demotion Rate:           {self.false_demotion_pct:.1f}%{'':<31}║",
            "╠══════════════════════════════════════════════════════════════════════╣",
            "║  Per-domain Memory Gravity Index:                                    ║",
        ]
        for domain, grav in sorted(self.gravity_by_domain.items()):
            lines.append(f"║    {domain:<20}: {grav*100:.1f}%{'':<38}║")
        lines.append(
            "╠══════════════════════════════════════════════════════════════════════╣"
        )
        lines.append(
            f"║  Total runs: {self.total_runs}  "
            f"({self.total_scenarios} scenarios × {len(self.seeds_used)} seeds){'':<20}║"
        )
        lines.append(
            "╚══════════════════════════════════════════════════════════════════════╝"
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict of all metrics and traces."""
        return {
            "mode": self.mode,
            "rounds_per_scenario": self.rounds_per_scenario,
            "seeds_used": self.seeds_used,
            "domains_covered": self.domains_covered,
            "total_scenarios": self.total_scenarios,
            "total_runs": self.total_runs,
            "metrics": {
                "crossover_median": self.crossover.median,
                "crossover_mean": self.crossover.mean,
                "crossover_std_dev": self.crossover.std_dev,
                "crossover_never_count": self.crossover.never_crossed,
                "crossover_distribution": self.crossover.distribution,
                "memory_gravity_index": self.memory_gravity_index,
                "memory_gravity_pct": self.memory_gravity_pct,
                "accuracy_per_round": self.accuracy_per_round,
                "authority_decay_avg_slope": self.authority_decay.avg_slope,
                "authority_decay_median_slope": self.authority_decay.median_slope,
                "false_demotion_rate": self.false_demotion_rate,
                "false_demotion_pct": self.false_demotion_pct,
            },
            "gravity_by_domain": self.gravity_by_domain,
            "traces": [
                {
                    "scenario_id": t.scenario_id,
                    "domain": t.domain,
                    "seed": t.seed,
                    "crossover_round": t.crossover_round,
                    "gravity_persists": t.gravity_persists,
                    "false_demotion": t.false_demotion,
                    "rounds": [asdict(r) for r in t.rounds],
                }
                for t in self.traces
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialise the full report to a JSON string."""
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Metric calculation helpers
# ---------------------------------------------------------------------------


def compute_crossover_stats(traces: list[ScenarioTrace]) -> CrossoverStats:
    """Compute crossover round statistics from a list of scenario traces."""
    distribution = [t.crossover_round for t in traces]
    crossed_values = [v for v in distribution if v is not None]
    never_crossed = sum(1 for v in distribution if v is None)

    if not crossed_values:
        return CrossoverStats(
            median=None,
            mean=None,
            std_dev=None,
            distribution=distribution,
            never_crossed=never_crossed,
        )

    return CrossoverStats(
        median=statistics.median(crossed_values),
        mean=statistics.mean(crossed_values),
        std_dev=statistics.stdev(crossed_values) if len(crossed_values) > 1 else 0.0,
        distribution=distribution,
        never_crossed=never_crossed,
    )


def compute_memory_gravity_index(traces: list[ScenarioTrace]) -> float:
    """
    Fraction of runs where Signature A (wrong) still dominates at the final round.

    Returns a value in [0.0, 1.0].  Lower is better.
    """
    if not traces:
        return 0.0
    return sum(1 for t in traces if t.gravity_persists) / len(traces)


def compute_accuracy_per_round(
    traces: list[ScenarioTrace], rounds: int
) -> list[float]:
    """
    Compute mean accuracy (top-hit was correct) for each round index.

    Returns a list of length `rounds`.
    """
    accuracies: list[list[float]] = [[] for _ in range(rounds)]
    for trace in traces:
        for r in trace.rounds:
            if r.round_number < rounds:
                accuracies[r.round_number].append(1.0 if r.is_correct else 0.0)

    return [
        (statistics.mean(acc) if acc else 0.0)
        for acc in accuracies
    ]


def compute_authority_decay_slope(traces: list[ScenarioTrace]) -> AuthorityDecayStats:
    """
    Compute slope of P_A decay after the first penalty (first round where A was wrong).

    Slope = (P_A at last round - P_A at first failure) / rounds elapsed
    Negative means P_A fell — which is what we want.
    """
    slopes: list[float] = []

    for trace in traces:
        first_failure_idx: int | None = None
        p_a_at_failure: float | None = None

        for r in trace.rounds:
            if not r.is_correct and first_failure_idx is None:
                first_failure_idx = r.round_number
                p_a_at_failure = r.p_a
                break

        if first_failure_idx is None or p_a_at_failure is None:
            continue  # No failure in this trace — skip

        # Find the last round in the trace
        last_round = trace.rounds[-1]
        rounds_elapsed = last_round.round_number - first_failure_idx

        if rounds_elapsed <= 0:
            continue

        slope = (last_round.p_a - p_a_at_failure) / rounds_elapsed
        slopes.append(slope)

    if not slopes:
        return AuthorityDecayStats(
            avg_slope=0.0, median_slope=0.0, min_slope=0.0, max_slope=0.0
        )

    return AuthorityDecayStats(
        avg_slope=round(statistics.mean(slopes), 4),
        median_slope=round(statistics.median(slopes), 4),
        min_slope=round(min(slopes), 4),
        max_slope=round(max(slopes), 4),
    )


def compute_false_demotion_rate(traces: list[ScenarioTrace]) -> float:
    """
    Fraction of traces where Signature B (correct) gained dominance then lost it.

    Returns a value in [0.0, 1.0].  Lower is better.
    """
    if not traces:
        return 0.0
    return sum(1 for t in traces if t.false_demotion) / len(traces)


def compute_gravity_by_domain(traces: list[ScenarioTrace]) -> dict[str, float]:
    """Return Memory Gravity Index broken down by domain."""
    by_domain: dict[str, list[ScenarioTrace]] = {}
    for t in traces:
        by_domain.setdefault(t.domain, []).append(t)
    return {
        domain: compute_memory_gravity_index(domain_traces)
        for domain, domain_traces in by_domain.items()
    }


def build_report(
    traces: list[ScenarioTrace],
    mode: str,
    rounds_per_scenario: int,
    seeds_used: list[int],
    domains_covered: list[str],
    total_scenarios: int,
) -> CorrectabilityReport:
    """
    Compute all five metrics from a list of ScenarioTraces and return a
    fully-populated CorrectabilityReport.
    """
    crossover = compute_crossover_stats(traces)
    gravity_idx = compute_memory_gravity_index(traces)
    accuracy_curve = compute_accuracy_per_round(traces, rounds_per_scenario)
    decay = compute_authority_decay_slope(traces)
    false_dem = compute_false_demotion_rate(traces)
    by_domain = compute_gravity_by_domain(traces)

    return CorrectabilityReport(
        mode=mode,
        rounds_per_scenario=rounds_per_scenario,
        seeds_used=seeds_used,
        domains_covered=domains_covered,
        total_scenarios=total_scenarios,
        total_runs=len(traces),
        crossover=crossover,
        memory_gravity_index=gravity_idx,
        memory_gravity_pct=round(gravity_idx * 100, 2),
        accuracy_per_round=accuracy_curve,
        authority_decay=decay,
        false_demotion_rate=false_dem,
        false_demotion_pct=round(false_dem * 100, 2),
        gravity_by_domain=by_domain,
        traces=traces,
    )

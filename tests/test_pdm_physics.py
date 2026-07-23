"""
PDM physics stress tests — prove core mathematical invariants under extreme load.

Version 2.0 gate: these invariants MUST hold regardless of storage backend quirks.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory import Memory
from pdm_memory.core.math import (
    P_MAX,
    calculate_decay_factor,
    calculate_effective_spike,
    calculate_intent_weight,
    calculate_p_effective,
    calculate_v,
    infer_domain,
    resolve_half_life,
)
from pdm_memory.core.retrieval import RetrievalEngine

CEILING = 100.0
TAG_POOL = [
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "kappa", "lambda", "sigma", "omega", "plasma", "orbital", "tensor",
    "cipher", "vertex", "matrix", "quasar", "nebula", "photon", "fermion",
    "hadron", "boson", "synapse", "cortex", "axiom", "lemma", "corollary",
]


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "physics.db"), user="physics_stress")
    yield m
    m.close()


def _live_p_effective(rec, *, now: datetime | None = None, query: str | None = None) -> float:
    """Mirror Memory.explain() pressure math without building a full report."""
    now = now or datetime.now(tz=timezone.utc)
    anchor = rec.last_retrieved or rec.created_at or now
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    created = rec.created_at or now
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    days_since = max(0.0, (now - anchor).total_seconds() / 86400.0)
    days_since_created = max(0.0, (now - created).total_seconds() / 86400.0)
    domain = rec.domain or infer_domain(rec.intent_tags)
    half_life = resolve_half_life(domain)
    decay = calculate_decay_factor(
        days_since,
        half_life,
        days_since_created=days_since_created,
        t_persistence=rec.t_persistence,
    )
    v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)
    intent_weight = calculate_intent_weight(rec.intent_tags, query) if query else 1.0
    return calculate_p_effective(rec.p_magnitude, v, decay, intent_weight, quality=0.80)


def _random_fact(rng: random.Random) -> str:
    words = [rng.choice(TAG_POOL) for _ in range(rng.randint(6, 14))]
    return " ".join(words).capitalize()


def _random_tags(rng: random.Random) -> list[str]:
    return rng.sample(TAG_POOL, k=rng.randint(3, 6))


class TestCeilingLaw:
    """
    Invariant: stored pressure and live P_effective are hard-capped at P_MAX (100.0).

    Stress: 100 random signatures × 1000 reinforce() calls each (max coupling).
    """

    def test_ceiling_law_never_exceeds_100(self, mem: Memory) -> None:
        rng = random.Random(42)
        memory_ids: list[str] = []

        for _ in range(100):
            mid = mem.save(
                _random_fact(rng),
                tags=_random_tags(rng),
                p_magnitude=rng.uniform(1.0, 99.0),
                t_persistence=rng.uniform(1.0, 90.0),
                drawer=rng.choice(["facts", "prefs", "work", "research"]),
                regime=rng.choice(["neutral", "engineering", "personal"]),
            )
            memory_ids.append(mid)

        for mid in memory_ids:
            for _ in range(1000):
                mem.reinforce(mid, coupling_score=1.0)
                rec = mem._storage.get(mid, user="physics_stress")
                assert rec is not None
                assert rec.p_magnitude <= CEILING, (
                    f"p_magnitude exceeded ceiling: {rec.p_magnitude}"
                )
                spike = rec.effective_spike or calculate_effective_spike(
                    rec.p_magnitude, rec.t_persistence, rec.phase_privilege
                )
                assert spike <= CEILING, f"effective_spike exceeded ceiling: {spike}"
                p_eff = _live_p_effective(rec)
                assert p_eff <= CEILING, f"p_effective exceeded ceiling: {p_eff}"
                assert p_eff <= P_MAX

        for mid in memory_ids:
            report = mem.explain(mid)
            assert report.p_magnitude <= CEILING
            assert report.p_effective <= CEILING
            assert report.effective_spike <= CEILING


class TestArrowOfTime:
    """
    Invariant: without reinforcement, elapsed time strictly reduces P_effective
    once the persistence grace window has expired.
    """

    def test_decay_strictly_lowers_p_effective_after_100_days(self, mem: Memory) -> None:
        mid = mem.save(
            "Orbital telemetry calibration uses xenon thruster alignment protocols",
            tags=["orbital", "telemetry", "xenon", "thruster"],
            p_magnitude=88.0,
            t_persistence=1.0,
            drawer="engineering",
        )

        initial = mem.explain(mid)
        assert initial.p_effective > 0.0

        past = datetime.now(tz=timezone.utc) - timedelta(days=100)
        mem._storage.update(
            mid,
            user="physics_stress",
            created_at=past,
            last_retrieved=past,
        )

        future = mem.explain(mid)
        assert future.p_effective < initial.p_effective, (
            f"decay broken: initial={initial.p_effective}, "
            f"after_100d={future.p_effective}, "
            f"decay_factor={future.decay_factor}"
        )
        assert future.decay_factor > initial.decay_factor


class TestZeroKnowledgeGate:
    """
    Invariant: high stored pressure must NOT brute-force recall when the query
    shares zero semantic overlap with the memory (tags + fact tokens).
    """

    def test_high_pressure_memory_blocked_without_semantic_overlap(
        self, mem: Memory
    ) -> None:
        mem.save(
            "Quantum plasma confinement exceeds reactor shield tolerances",
            tags=["quantum", "plasma", "reactor", "shield"],
            p_magnitude=95.0,
            drawer="physics",
        )

        query = "gardening tulips weather forecast planting schedule"
        hits = mem.recall(
            query,
            k=5,
            reinforce=False,
            search_cost=1.0,
        )

        returned_ids = {h.id for h in hits}
        all_records = mem._storage.list(user="physics_stress", limit=10)
        high_p = all_records[0]
        assert high_p.p_magnitude == pytest.approx(95.0)

        assert high_p.id not in returned_ids, (
            "Zero-knowledge gate failed: unrelated P=95 memory leaked into recall "
            f"with search_cost=1.0. hits={[h.text for h in hits]}"
        )

        engine = RetrievalEngine()
        query_tags = engine._tokenize_query(query)
        overlap = RetrievalEngine._semantic_query_overlap(high_p, query_tags)
        assert overlap < 0.12, (
            f"test setup invalid: semantic overlap {overlap} >= 0.12; "
            "query is not zero-knowledge relative to memory"
        )

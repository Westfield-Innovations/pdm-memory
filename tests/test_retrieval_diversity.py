# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Retrieval diversity_bias — anti-noise drawer share cap."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import pytest

from pdm_memory.core.retrieval import NodeCoupling, RetrievalEngine
from pdm_memory.core.signature import MemoryHit, SignatureRecord


def _hit(drawer: str, hit_id: str) -> MemoryHit:
    now = datetime.now(tz=timezone.utc)
    return MemoryHit(
        id=hit_id,
        text=f"fact-{hit_id}",
        source="test",
        drawer=drawer,
        pressure=80.0,
        p_raw=80.0,
        p_effective=80.0,
        decay_factor=0.0,
        intent_weight=1.0,
        v_coefficient=1.0,
        quality=0.8,
        last_reinforced=now,
        retrieval_count=0,
        intent_tags=["alpha", "beta", "gamma"],
        domain="insight",
        coupling_score=1.0,
    )


def _coupling(hit_id: str) -> NodeCoupling:
    return NodeCoupling(
        signature_id=hit_id,
        p_effective=80.0,
        p_magnitude_raw=80.0,
        coupling_score=1.0,
        is_coupled=True,
        auto_fire_eligible=False,
    )


def _ranked(*drawers: str) -> list[tuple[NodeCoupling, MemoryHit]]:
    out: list[tuple[NodeCoupling, MemoryHit]] = []
    for i, drawer in enumerate(drawers):
        hid = f"{drawer}-{i}"
        out.append((_coupling(hid), _hit(drawer, hid)))
    return out


def _rec(
    text: str,
    *,
    drawer: str,
    p: float,
    tags: list[str],
) -> SignatureRecord:
    return SignatureRecord(
        compressed_fact=text,
        intent_tags=tags,
        drawer_domain=drawer,
        domain="insight",
        p_magnitude=p,
        question_regime="neutral",
    )


class TestSelectWithDiversity:
    def test_none_is_pure_topk(self) -> None:
        ranked = _ranked("work", "work", "work", "health", "finance")
        hits = RetrievalEngine._select_with_diversity(
            ranked, k=3, diversity_bias=None
        )
        assert [h.drawer for h in hits] == ["work", "work", "work"]

    def test_caps_dominant_drawer_when_others_exist(self) -> None:
        # k=5, bias=0.4 → max 2 per drawer while other drawers still supply hits
        ranked = _ranked(
            "work",
            "work",
            "work",
            "work",
            "work",
            "health",
            "health",
            "finance",
            "finance",
        )
        hits = RetrievalEngine._select_with_diversity(
            ranked, k=5, diversity_bias=0.4
        )
        counts = Counter(h.drawer for h in hits)
        assert len(hits) == 5
        assert counts["work"] == 2
        assert counts["health"] >= 1
        assert counts["finance"] >= 1
        assert counts["work"] + counts["health"] + counts["finance"] == 5

    def test_overflow_fills_when_no_other_drawers(self) -> None:
        ranked = _ranked("work", "work", "work", "work", "work")
        hits = RetrievalEngine._select_with_diversity(
            ranked, k=3, diversity_bias=0.4
        )
        assert len(hits) == 3
        assert all(h.drawer == "work" for h in hits)

    def test_bias_one_disables_cap(self) -> None:
        ranked = _ranked("work", "work", "work", "health")
        hits = RetrievalEngine._select_with_diversity(
            ranked, k=3, diversity_bias=1.0
        )
        assert [h.drawer for h in hits] == ["work", "work", "work"]

    def test_engine_default_diversity_bias_is_point_four(self) -> None:
        import inspect

        from pdm_memory.core.retrieval import DEFAULT_DIVERSITY_BIAS

        assert DEFAULT_DIVERSITY_BIAS == pytest.approx(0.4)
        param = inspect.signature(RetrievalEngine.recall).parameters["diversity_bias"]
        assert param.default == DEFAULT_DIVERSITY_BIAS


class TestRecallDiversityBias:
    def test_dominant_drawer_without_bias(self) -> None:
        tags = ["project", "deadline", "shipping", "release"]
        records = [
            _rec(f"work note {i} about project deadline shipping release", drawer="work", p=95.0 - i, tags=tags)
            for i in range(6)
        ] + [
            _rec(
                "health note about project deadline shipping release rest",
                drawer="health",
                p=70.0,
                tags=tags,
            ),
            _rec(
                "finance note about project deadline shipping release budget",
                drawer="finance",
                p=68.0,
                tags=tags,
            ),
        ]
        engine = RetrievalEngine()
        hits = engine.recall(
            records,
            query="project deadline shipping release",
            k=5,
            search_cost=1.0,
            diversity_bias=None,
        )
        assert len(hits) == 5
        assert all(h.drawer == "work" for h in hits)

    def test_diversity_bias_surfaces_other_drawers(self) -> None:
        tags = ["project", "deadline", "shipping", "release"]
        records = [
            _rec(f"work note {i} about project deadline shipping release", drawer="work", p=95.0 - i, tags=tags)
            for i in range(6)
        ] + [
            _rec(
                f"health note {i} about project deadline shipping release rest",
                drawer="health",
                p=72.0 - i,
                tags=tags,
            )
            for i in range(2)
        ] + [
            _rec(
                f"finance note {i} about project deadline shipping release budget",
                drawer="finance",
                p=70.0 - i,
                tags=tags,
            )
            for i in range(2)
        ]
        engine = RetrievalEngine()
        hits = engine.recall(
            records,
            query="project deadline shipping release",
            k=5,
            search_cost=1.0,
            diversity_bias=0.4,
        )
        drawers = {h.drawer for h in hits}
        counts = Counter(h.drawer for h in hits)
        assert len(hits) == 5
        assert "health" in drawers
        assert "finance" in drawers
        assert counts["work"] <= 2

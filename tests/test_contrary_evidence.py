"""Tests for Memory.apply_contrary_evidence — immediate V/P drop without history rewrite."""

from __future__ import annotations

import pytest

from pdm_memory import ContraryEvidenceResult, Memory
from pdm_memory.core.math import MEMORY_SHAPE_KEY, calculate_v


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "contrary.db"), user="test_user")
    yield m
    m.close()


class TestApplyContraryEvidence:
    def test_lowers_p_and_v_immediately(self, mem):
        mid = mem.save(
            "User lives in Lviv",
            tags=["identity", "city", "bio"],
            p_magnitude=80,
            shape="structural",
        )
        before = mem._storage.get(mid, user="test_user")
        v_before = calculate_v(
            before.validation_prediction_correct,
            before.validation_prediction_total,
        )

        result = mem.apply_contrary_evidence(
            mid,
            "User lives in Kyiv",
            coupling_score=0.8,
            evidence_tags=["identity", "city", "correction"],
        )

        assert isinstance(result, ContraryEvidenceResult)
        assert result.target_id == mid
        assert result.p_after < result.p_before
        assert result.v_after < v_before
        assert result.p_effective_after > 0

        after = mem._storage.get(mid, user="test_user")
        assert after.p_magnitude == pytest.approx(result.p_after)
        assert after.validation_prediction_total == (
            before.validation_prediction_total or 0
        ) + 1

    def test_does_not_rewrite_historical_fields(self, mem):
        mid = mem.save(
            "User was born in Kyiv",
            tags=["identity", "bio", "origin"],
            p_magnitude=70,
            shape="structural",
        )
        before = mem._storage.get(mid, user="test_user")
        created = before.created_at
        fact = before.compressed_fact
        correct = before.validation_prediction_correct

        result = mem.apply_contrary_evidence(mid, "User was born in Odesa")

        after = mem._storage.get(mid, user="test_user")
        assert after.compressed_fact == fact
        assert after.created_at == created
        assert after.validation_prediction_correct == correct
        assert result.compressed_fact == fact
        assert result.created_at == created
        assert result.validation_prediction_correct == correct

    def test_persists_evidence_with_contrary_to(self, mem):
        mid = mem.save(
            "User prefers Java",
            tags=["habit", "language", "prefs"],
            p_magnitude=60,
        )
        result = mem.apply_contrary_evidence(
            mid,
            {"text": "User prefers Python", "tags": ["habit", "language", "prefs"]},
            evidence_shape="behavioral",
        )
        assert result.evidence_id is not None
        evid = mem._storage.get(result.evidence_id, user="test_user")
        assert evid is not None
        assert evid.compressed_fact == "User prefers Python"
        assert evid.metadata.get("contrary_to") == mid
        assert evid.metadata.get(MEMORY_SHAPE_KEY) == "behavioral"
        assert evid.source == "contrary_evidence"

    def test_persist_evidence_false_skips_save(self, mem):
        mid = mem.save("Old fact", tags=["a", "b", "c"], p_magnitude=50)
        before_count = mem.count()
        result = mem.apply_contrary_evidence(
            mid, "New contrary fact", persist_evidence=False
        )
        assert result.evidence_id is None
        assert mem.count() == before_count

    def test_missing_target_raises(self, mem):
        with pytest.raises(KeyError, match="not found"):
            mem.apply_contrary_evidence(
                "00000000-0000-0000-0000-000000000000",
                "contrary",
            )

    def test_empty_evidence_raises(self, mem):
        mid = mem.save("Some fact", tags=["a", "b", "c"], p_magnitude=50)
        with pytest.raises(ValueError, match="empty"):
            mem.apply_contrary_evidence(mid, "   ")

    def test_penalize_shares_prediction_miss_path(self, mem):
        """penalize() must still lower P via the shared miss helper."""
        mid = mem.save("Wrong claim", tags=["a", "b", "c"], p_magnitude=70)
        before = mem._storage.get(mid, user="test_user").p_magnitude
        mem.penalize(mid, coupling_score=0.8)
        after = mem._storage.get(mid, user="test_user")
        assert after.p_magnitude < before
        assert after.validation_prediction_total == 1
        assert after.validation_prediction_correct == 0

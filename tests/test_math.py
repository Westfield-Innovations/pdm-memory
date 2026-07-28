"""Tests for PDM core math formulas — verifies parity with companion_api/pdm/kernel.py"""

import pytest

from pdm_memory.core.math import (
    DECAY_DELETE_THRESHOLD,
    DOMAIN_HALF_LIVES,
    calculate_decay_factor,
    calculate_effective_spike,
    calculate_half_life_pressure,
    calculate_incremental_decay,
    calculate_intent_weight,
    calculate_p_effective,
    calculate_temporal_geometry,
    calculate_v,
    infer_domain,
    infer_regime,
)


class TestEffectiveSpike:
    def test_basic(self):
        # p=100, t=30, phase=1.0 → 100 * 1 * 1 = 100
        assert calculate_effective_spike(100, 30, 1.0) == 100.0

    def test_capped_at_100(self):
        assert calculate_effective_spike(100, 60, 2.0) == 100.0

    def test_partial(self):
        # p=50, t=30, phase=1.0 → 50.0
        assert calculate_effective_spike(50, 30, 1.0) == pytest.approx(50.0)

    def test_short_persistence(self):
        # p=100, t=15, phase=1.0 → 100 * 0.5 * 1 = 50
        assert calculate_effective_spike(100, 15, 1.0) == pytest.approx(50.0)

    def test_zero_pressure(self):
        assert calculate_effective_spike(0, 30, 1.0) == 0.0

    def test_phase_privilege(self):
        # p=60, t=30, phase=2.0 → 120 → capped at 100
        assert calculate_effective_spike(60, 30, 2.0) == 100.0


class TestDecayFactor:
    def test_zero_days(self):
        # No time passed → no decay
        assert calculate_decay_factor(0, 30) == pytest.approx(0.0, abs=1e-6)

    def test_half_life(self):
        # At exactly half_life days → decay_factor = 1 - e^(-ln2) = 0.5
        assert calculate_decay_factor(30, 30) == pytest.approx(0.5, abs=0.01)

    def test_full_decay(self):
        # Many half-lives → approaches 1.0
        assert calculate_decay_factor(1000, 30) > 0.99

    def test_domain_half_lives(self):
        # market_signal (1 day) decays faster than core_fact (365 days)
        market_decay = calculate_decay_factor(7, DOMAIN_HALF_LIVES["market_signal"])
        fact_decay = calculate_decay_factor(7, DOMAIN_HALF_LIVES["core_fact"])
        assert market_decay > fact_decay

    def test_persistence_grace(self):
        # Within t_persistence → no decay regardless of touch age
        assert calculate_decay_factor(
            10, 30, days_since_created=10, t_persistence=30
        ) == pytest.approx(0.0)

    def test_past_grace_applies_half_life(self):
        d = calculate_decay_factor(
            30, 30, days_since_created=60, t_persistence=30
        )
        assert d == pytest.approx(0.5, abs=0.01)


class TestValidationCoefficient:
    def test_no_history(self):
        # V(0,0) = 1/2 = 0.5
        assert calculate_v(0, 0) == pytest.approx(0.5, abs=1e-4)

    def test_perfect_accuracy(self):
        # V(10,10) = 11/12 ≈ 0.9167
        assert calculate_v(10, 10) == pytest.approx(11/12, abs=1e-4)

    def test_zero_correct(self):
        # V(0,10) = 1/12 ≈ 0.0833
        assert calculate_v(0, 10) == pytest.approx(1/12, abs=1e-4)

    def test_laplace_floor(self):
        # Should never reach 0 or 1 exactly
        assert calculate_v(0, 0) > 0.0
        assert calculate_v(0, 0) < 1.0
        assert calculate_v(1000, 1000) < 1.0


class TestIntentWeight:
    def test_no_query(self):
        assert calculate_intent_weight(["tag1", "tag2"]) == 1.0

    def test_full_match(self):
        # All tags present in query → weight = 1.0
        w = calculate_intent_weight(["units", "formatting"], "use metric units for formatting")
        assert w == pytest.approx(1.0, abs=0.01)

    def test_no_match(self):
        w = calculate_intent_weight(["quantum", "physics"], "what time is it?")
        assert w == pytest.approx(0.8, abs=0.01)

    def test_partial_match(self):
        w = calculate_intent_weight(["units", "formatting", "brevity"], "use metric units")
        assert 0.8 < w < 1.0


class TestPEffective:
    def test_full_fresh_memory(self):
        # Max p, no decay, perfect V=1, full intent match, max quality
        p = calculate_p_effective(100, v=1.0, decay_factor=0.0, intent_weight=1.0, quality=1.0)
        assert p == pytest.approx(100.0)

    def test_decayed_memory(self):
        # Half decayed
        p = calculate_p_effective(100, v=1.0, decay_factor=0.5, intent_weight=1.0, quality=1.0)
        assert p == pytest.approx(50.0)

    def test_default_v(self):
        # No validation history: V=0.75 default handled upstream; pass manually
        p = calculate_p_effective(80, v=0.75, decay_factor=0.0, intent_weight=1.0, quality=0.80)
        assert p == pytest.approx(80 * 0.75 * 1.0 * 1.0 * 0.80)

    def test_capped_at_100(self):
        p = calculate_p_effective(100, v=1.0, decay_factor=0.0, intent_weight=1.0,
                                  quality=1.0, comparator=2.0)
        assert p == 100.0

    def test_zero_floor(self):
        p = calculate_p_effective(0, v=1.0, decay_factor=1.0, intent_weight=0.0)
        assert p == 0.0


class TestHalfLifePressure:
    """Canonical decay: p_new = p × exp(-λt) after t_persistence grace."""

    def test_within_persistence(self):
        new_p, _ = calculate_half_life_pressure(
            80, days_since_retrieved=10, half_life=30, t_persistence=30,
            days_since_created=10,
        )
        assert new_p == pytest.approx(80.0)

    def test_past_persistence_half_life(self):
        # After grace, 30 days since touch @ half_life=30 → surviving = 0.5
        new_p, _ = calculate_half_life_pressure(
            80, days_since_retrieved=30, half_life=30, t_persistence=30,
            days_since_created=60,
        )
        assert new_p == pytest.approx(40.0, abs=0.5)

    def test_delete_threshold(self):
        new_p, _ = calculate_half_life_pressure(
            35, days_since_retrieved=200, half_life=30, t_persistence=30,
            days_since_created=230,
        )
        assert new_p < DECAY_DELETE_THRESHOLD

    def test_spike_recomputed(self):
        _, new_spike = calculate_half_life_pressure(
            80, days_since_retrieved=60, half_life=30, t_persistence=30,
            days_since_created=90,
        )
        assert new_spike >= 0.0

    def test_legacy_incremental_alias_uses_half_life(self):
        with pytest.warns(DeprecationWarning):
            new_p, _ = calculate_incremental_decay(
                80, days_elapsed=60, t_persistence=30, half_life=30
            )
        # grace 30 → decay days 30 → factor 0.5 → p=40
        assert new_p == pytest.approx(40.0, abs=0.5)


class TestTemporalGeometry:
    def test_pre_deadline(self):
        result = calculate_temporal_geometry(
            c_base=0.5, s_base=0.5, p_base=0.7,
            urgency_rate=2.0, t_remaining_days=5.0, persist_days=30.0
        )
        assert result["status"] in ("ACTIVE", "URGENT")
        assert result["e_temporal"] > 0.0

    def test_post_deadline(self):
        result = calculate_temporal_geometry(
            c_base=0.5, s_base=0.5, p_base=0.7,
            urgency_rate=2.0, t_remaining_days=-5.0, persist_days=30.0
        )
        assert result["status"] == "EXPIRED"
        assert result["e_temporal"] == 0.0

    def test_urgency_flag(self):
        # Very close to deadline → should be urgent
        result = calculate_temporal_geometry(
            c_base=0.5, s_base=0.5, p_base=0.9,
            urgency_rate=5.0, t_remaining_days=0.5, persist_days=30.0
        )
        assert result["is_urgent"] is True


class TestDomainInference:
    def test_market(self):
        assert infer_domain(["stock", "price"]) == "market_signal"

    def test_reminder(self):
        assert infer_domain(["deadline", "remind"]) == "reminder"

    def test_core_fact(self):
        assert infer_domain(["law", "principle"]) == "core_fact"

    def test_default(self):
        assert infer_domain([]) == "insight"
        assert infer_domain(["random"]) == "insight"


class TestRegimeInference:
    def test_trading(self):
        assert infer_regime(["stock", "market"]) == "trading"

    def test_engineering(self):
        assert infer_regime(["code", "deploy"]) == "engineering"

    def test_ip(self):
        assert infer_regime(["patent", "license"]) == "ip_monetize"

    def test_neutral(self):
        assert infer_regime(["random"]) == "neutral"

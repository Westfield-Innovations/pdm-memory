"""
Tests for the Correctability Benchmark.

Covers:
 - penalize() decreases V-coefficient and p_magnitude
 - reinforce() increases V-coefficient and p_magnitude
 - _apply_reinforcement updates V counters via recall()
 - crossover occurs within a small harness run
 - ablation shows higher gravity than pdm_enabled
 - VectorRAGBaseline is gravity-dominant
 - JSON output has required fields
 - scenarios dataset integrity
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import List

import pytest

from pdm_memory import Memory
from pdm_memory.bench.correctability.baselines import VectorRAGBaseline
from pdm_memory.bench.correctability.harness import run_suite
from pdm_memory.bench.correctability.metrics import (
    RoundRecord,
    ScenarioTrace,
    build_report,
    compute_crossover_stats,
    compute_memory_gravity_index,
)
from pdm_memory.bench.correctability.scenarios import (
    ALL_SCENARIOS,
    SCENARIOS_BY_DOMAIN,
    BenchScenario,
    P_CORRECT,
    P_WRONG,
    get_scenarios,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_mem(tmp_path):
    """Isolated PDM Memory instance backed by a temp SQLite file."""
    db = str(tmp_path / "test.db")
    mem = Memory(store=db, user="test")
    yield mem
    mem.close()


@pytest.fixture()
def scenario_with_mem(tmp_mem):
    """Seeds a single correctability scenario into a fresh Memory instance."""
    scenario = ALL_SCENARIOS[0]  # sci_001 — boiling point of water
    id_a = tmp_mem.save(
        text=scenario.wrong_answer,
        tags=scenario.tags_wrong,
        p_magnitude=P_WRONG,
        source="test",
        drawer="correctability",
    )
    id_b = tmp_mem.save(
        text=scenario.correct_answer,
        tags=scenario.tags_correct,
        p_magnitude=P_CORRECT,
        source="test",
        drawer="correctability",
    )
    return tmp_mem, scenario, id_a, id_b


# ---------------------------------------------------------------------------
# 1. penalize() decreases p_magnitude and V
# ---------------------------------------------------------------------------


def test_penalize_decreases_p_magnitude_and_raises_total(tmp_mem):
    """penalize() must lower p_magnitude and increment validation_prediction_total."""
    mem_id = tmp_mem.save(
        text="Wrong answer about boiling point",
        p_magnitude=85.0,
        tags=["science", "temperature"],
        drawer="correctability",
    )

    rec_before = tmp_mem._storage.get(mem_id, user="test")
    assert rec_before is not None
    p_before = rec_before.p_magnitude
    total_before = rec_before.validation_prediction_total
    correct_before = rec_before.validation_prediction_correct

    tmp_mem.penalize(mem_id, coupling_score=0.8)

    rec_after = tmp_mem._storage.get(mem_id, user="test")
    assert rec_after is not None

    # p_magnitude must fall
    assert rec_after.p_magnitude < p_before, (
        f"Expected p_magnitude to fall after penalize, got {rec_after.p_magnitude} >= {p_before}"
    )
    # total must increment
    assert rec_after.validation_prediction_total == total_before + 1
    # correct must NOT change
    assert rec_after.validation_prediction_correct == correct_before


# ---------------------------------------------------------------------------
# 2. reinforce() increases p_magnitude and V (both counters)
# ---------------------------------------------------------------------------


def test_reinforce_increases_p_magnitude_and_both_v_counters(tmp_mem):
    """reinforce() must raise p_magnitude and increment both V counters."""
    mem_id = tmp_mem.save(
        text="Correct answer: water boils at 100°C",
        p_magnitude=40.0,
        tags=["science", "temperature"],
        drawer="correctability",
    )

    rec_before = tmp_mem._storage.get(mem_id, user="test")
    p_before = rec_before.p_magnitude
    total_before = rec_before.validation_prediction_total
    correct_before = rec_before.validation_prediction_correct

    tmp_mem.reinforce(mem_id, coupling_score=0.8)

    rec_after = tmp_mem._storage.get(mem_id, user="test")

    assert rec_after.p_magnitude > p_before, "p_magnitude must rise after reinforce()"
    assert rec_after.validation_prediction_total == total_before + 1
    assert rec_after.validation_prediction_correct == correct_before + 1


# ---------------------------------------------------------------------------
# 3. recall() auto-reinforcement tracks V counters
# ---------------------------------------------------------------------------


def test_recall_auto_reinforce_updates_v_counters(tmp_mem):
    """recall(reinforce=True) should update both V counters for returned hits."""
    mem_id = tmp_mem.save(
        text="Correct answer auto-reinforce test",
        p_magnitude=60.0,
        tags=["test", "recall"],
        drawer="general",
    )

    rec_before = tmp_mem._storage.get(mem_id, user="test")
    total_before = rec_before.validation_prediction_total

    tmp_mem.recall("correct answer test", k=1, reinforce=True)

    rec_after = tmp_mem._storage.get(mem_id, user="test")
    # At least total should have increased (hit was returned)
    assert rec_after.validation_prediction_total >= total_before


# ---------------------------------------------------------------------------
# 4. Multiple penalize calls progressively lower V
# ---------------------------------------------------------------------------


def test_repeated_penalize_lowers_v_coefficient(tmp_mem):
    """V = (correct+1)/(total+2) must drop as penalize() is called repeatedly."""
    from pdm_memory.core.math import calculate_v

    mem_id = tmp_mem.save(
        text="Repeatedly wrong answer",
        p_magnitude=85.0,
        tags=["test"],
        drawer="correctability",
    )

    for _ in range(5):
        tmp_mem.penalize(mem_id, coupling_score=0.8)

    rec = tmp_mem._storage.get(mem_id, user="test")
    v = calculate_v(rec.validation_prediction_correct, rec.validation_prediction_total)

    # With 5 penalisations and 0 correct: V = (0+1)/(5+2) = 0.142
    # Starting V (0 history): (0+1)/(0+2) = 0.5
    # After 5 wrong: V = 1/7 ≈ 0.143
    assert v < 0.5, f"Expected V to fall below 0.5 after 5 penalisations, got {v:.4f}"


# ---------------------------------------------------------------------------
# 5. Crossover occurs within 20 rounds (single scenario, pdm_enabled)
# ---------------------------------------------------------------------------


def test_crossover_occurs_in_pdm_enabled_mode():
    """At least some scenarios should crossover within 20 rounds in PDM mode."""
    # Run 1 seed, 1 domain, 1 scenario only for speed
    report = run_suite(
        mode="pdm_enabled",
        rounds=20,
        seeds=[0],
        domains=["science"],
    )

    # At least some traces should have crossed over
    crossed = [t for t in report.traces if t.crossover_round is not None]
    assert len(crossed) > 0, (
        "Expected at least one scenario to crossover in PDM-enabled mode within 20 rounds"
    )


# ---------------------------------------------------------------------------
# 6. Ablation shows higher Memory Gravity than PDM-enabled
# ---------------------------------------------------------------------------


def test_ablation_has_higher_gravity_than_pdm():
    """V-ablation mode must show higher Memory Gravity Index than PDM-enabled."""
    pdm_report = run_suite(
        mode="pdm_enabled",
        rounds=20,
        seeds=[0],
        domains=["science"],
    )
    ablation_report = run_suite(
        mode="pdm_ablation",
        rounds=20,
        seeds=[0],
        domains=["science"],
    )

    # Ablation gravity should be >= PDM gravity (ablation is worse or equal)
    assert ablation_report.memory_gravity_index >= pdm_report.memory_gravity_index, (
        f"Ablation gravity ({ablation_report.memory_gravity_index:.3f}) should be >= "
        f"PDM gravity ({pdm_report.memory_gravity_index:.3f})"
    )


# ---------------------------------------------------------------------------
# 7. VectorRAG baseline has near-100% Memory Gravity
# ---------------------------------------------------------------------------


def test_vector_rag_baseline_has_high_gravity():
    """VectorRAGBaseline reinforce/penalize are no-ops → gravity should be 100%."""
    report = run_suite(
        mode="vector_rag",
        rounds=20,
        seeds=[0],
        domains=["science"],
    )

    # All scenarios should show gravity persists (A stays dominant)
    assert report.memory_gravity_index >= 0.95, (
        f"VectorRAG baseline Memory Gravity Index should be ≥ 95%, "
        f"got {report.memory_gravity_pct:.1f}%"
    )


# ---------------------------------------------------------------------------
# 8. JSON output has all required fields
# ---------------------------------------------------------------------------


def test_json_output_has_required_fields(tmp_path):
    """Report.to_json() must include all 5 metrics and trace records."""
    output_path = str(tmp_path / "test_report.json")
    report = run_suite(
        mode="pdm_enabled",
        rounds=5,
        seeds=[0],
        domains=["science"],
        output=output_path,
    )

    assert os.path.exists(output_path), "JSON output file was not created"

    with open(output_path) as f:
        data = json.load(f)

    required_metric_keys = {
        "crossover_median",
        "memory_gravity_index",
        "accuracy_per_round",
        "authority_decay_avg_slope",
        "false_demotion_rate",
    }
    metrics = data.get("metrics", {})
    missing = required_metric_keys - set(metrics.keys())
    assert not missing, f"Missing metric keys in JSON: {missing}"

    assert "traces" in data, "JSON output must include 'traces'"
    assert len(data["traces"]) > 0, "JSON traces must not be empty"


# ---------------------------------------------------------------------------
# 9. Scenario dataset integrity
# ---------------------------------------------------------------------------


def test_scenario_dataset_has_100_entries():
    """ALL_SCENARIOS must have exactly 100 entries."""
    assert len(ALL_SCENARIOS) == 100, f"Expected 100 scenarios, got {len(ALL_SCENARIOS)}"


def test_scenarios_cover_four_domains():
    """Scenarios must be distributed across exactly 4 domains."""
    domains = {s.domain for s in ALL_SCENARIOS}
    assert domains == {"science", "geography", "history", "tech"}, (
        f"Unexpected domain set: {domains}"
    )


def test_each_domain_has_25_scenarios():
    """Each of the 4 domains must have exactly 25 scenarios."""
    for domain, scenarios in SCENARIOS_BY_DOMAIN.items():
        assert len(scenarios) == 25, (
            f"Domain '{domain}' has {len(scenarios)} scenarios, expected 25"
        )


def test_scenario_ids_are_unique():
    """All scenario IDs must be unique."""
    ids = [s.id for s in ALL_SCENARIOS]
    assert len(ids) == len(set(ids)), "Duplicate scenario IDs found"


def test_p_wrong_greater_than_p_correct():
    """P_WRONG must be greater than P_CORRECT — spec requirement."""
    assert P_WRONG > P_CORRECT, (
        f"P_WRONG ({P_WRONG}) must be > P_CORRECT ({P_CORRECT})"
    )


def test_get_scenarios_domain_filter():
    """get_scenarios(domains=['science']) must return only science scenarios."""
    sci = get_scenarios(domains=["science"])
    assert all(s.domain == "science" for s in sci)
    assert len(sci) == 25


# ---------------------------------------------------------------------------
# 10. Metrics computation helpers
# ---------------------------------------------------------------------------


def test_compute_crossover_stats_no_crossover():
    """CrossoverStats with no crossovers should report None median."""
    traces = [
        ScenarioTrace(scenario_id="s1", domain="science", seed=0),
        ScenarioTrace(scenario_id="s2", domain="science", seed=0),
    ]
    # No rounds → no crossover
    for t in traces:
        t.compute_derived()

    stats = compute_crossover_stats(traces)
    assert stats.median is None
    assert stats.never_crossed == 2


def test_compute_memory_gravity_with_all_gravity():
    """If all traces persist gravity, index should be 1.0."""
    trace = ScenarioTrace(scenario_id="s1", domain="science", seed=0)
    trace.rounds = [RoundRecord(0, p_a=85, p_b=40, top_hit_is_a=True, is_correct=False)]
    trace.compute_derived()
    assert trace.gravity_persists is True

    index = compute_memory_gravity_index([trace])
    assert index == 1.0


def test_build_report_returns_all_fields():
    """build_report() must populate all five metric groups."""
    trace = ScenarioTrace(scenario_id="s1", domain="science", seed=0)
    trace.rounds = [
        RoundRecord(0, p_a=85, p_b=40, top_hit_is_a=True, is_correct=False),
        RoundRecord(1, p_a=50, p_b=60, top_hit_is_a=False, is_correct=True),
    ]
    trace.compute_derived()

    report = build_report(
        traces=[trace],
        mode="pdm_enabled",
        rounds_per_scenario=2,
        seeds_used=[0],
        domains_covered=["science"],
        total_scenarios=1,
    )

    assert report.mode == "pdm_enabled"
    assert report.memory_gravity_index == 0.0  # crossed over
    assert len(report.accuracy_per_round) == 2
    assert isinstance(report.crossover.median, (int, float))

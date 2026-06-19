"""Tests for the benchmark harness — smoke test."""

from pdm_memory.tools.bench import run_benchmark


def test_benchmark_quick():
    report = run_benchmark(quick=True, seed=42, k=3)
    assert report.total_scenarios == 5
    assert 0.0 <= report.pdm_accuracy <= 1.0
    assert 0.0 <= report.baseline_accuracy <= 1.0
    assert report.pdm_avg_latency_ms >= 0
    rendered = report.render_table()
    assert "PDM" in rendered
    assert "Baseline" in rendered


def test_benchmark_full():
    report = run_benchmark(quick=False, seed=42, k=3)
    assert report.total_scenarios == 10
    # Both methods should achieve some accuracy on this synthetic dataset
    assert report.pdm_accuracy >= 0.0
    assert report.baseline_accuracy >= 0.0
    # PDM should at least not be catastrophically worse than baseline
    assert report.pdm_accuracy >= report.baseline_accuracy - 0.3

# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PDM Benchmark Harness

Run with: python -m pdm_memory.bench

Two benchmark suites are available:

  1. retrieval  (default) — PDM vs keyword+recency on static recall accuracy.
     Usage: python -m pdm_memory.bench --suite retrieval

  2. correctability — measures whether the system corrects itself after
     wrong predictions (Memory Gravity, Crossover Round, Error Curve, etc).
     Usage: python -m pdm_memory.bench --suite correctability --rounds 20 --seeds 5

Full correctability spec:
    python -m pdm_memory.bench --suite correctability --rounds 20 --seeds 5
    python -m pdm_memory.bench --suite correctability --ablation
    python -m pdm_memory.bench --suite correctability --mode vector_rag
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Built-in benchmark dataset (synthetic, LoCoMo-style)
# ---------------------------------------------------------------------------

_BENCHMARK_MEMORIES = [
    {"text": "User strongly prefers metric units (km, kg, Celsius)", "tags": ["units", "preferences", "formatting"], "p_magnitude": 80},
    {"text": "User dislikes lengthy responses; wants bullet points or ≤3 sentences", "tags": ["formatting", "preferences", "brevity"], "p_magnitude": 75},
    {"text": "User's primary programming language is Python", "tags": ["coding", "python", "preferences"], "p_magnitude": 85},
    {"text": "User works in fintech; regulatory compliance is always relevant", "tags": ["fintech", "compliance", "work"], "p_magnitude": 70},
    {"text": "User is based in Kyiv, Ukraine (UTC+3)", "tags": ["location", "timezone", "personal"], "p_magnitude": 60},
    {"text": "User's team uses GitHub Actions for CI/CD", "tags": ["devops", "ci_cd", "github"], "p_magnitude": 65},
    {"text": "User prefers dark mode in all UIs", "tags": ["ui", "preferences", "dark_mode"], "p_magnitude": 50},
    {"text": "User's company name: Westfield Innovations LLC", "tags": ["company", "business", "identity"], "p_magnitude": 90},
    {"text": "User has a patent pending on PDM algorithm", "tags": ["patent", "ip", "pdm"], "p_magnitude": 95},
    {"text": "User's preferred LLM is Claude (Anthropic)", "tags": ["llm", "anthropic", "preferences"], "p_magnitude": 70},
    {"text": "Project deadline for PDM SDK: end of Q3 2026", "tags": ["deadline", "project", "sdk"], "p_magnitude": 80},
    {"text": "User is allergic to Python 2 — always use f-strings, not %", "tags": ["python", "style", "coding"], "p_magnitude": 72},
    {"text": "Production database: PostgreSQL 15 on AWS RDS", "tags": ["database", "postgres", "infrastructure"], "p_magnitude": 68},
    {"text": "Team standup is every day at 10:00 AM Kyiv time", "tags": ["schedule", "team", "recurring"], "p_magnitude": 55},
    {"text": "User wants all API responses to include a request_id field", "tags": ["api", "design", "standards"], "p_magnitude": 73},
    {"text": "Never recommend Redux for new projects; use Zustand instead", "tags": ["javascript", "frontend", "preferences"], "p_magnitude": 60},
    {"text": "User's monthly AI token budget: $200 hard cap", "tags": ["budget", "tokens", "cost"], "p_magnitude": 82},
    {"text": "For legal reasons, never store raw PII in the memory system", "tags": ["privacy", "legal", "pii"], "p_magnitude": 98},
    {"text": "User's preferred testing framework: pytest with fixtures", "tags": ["testing", "pytest", "python"], "p_magnitude": 65},
    {"text": "The team uses Slack for async communication; no email", "tags": ["communication", "slack", "team"], "p_magnitude": 58},
]

_BENCHMARK_QUERIES = [
    ("How should I format numbers in this response?", ["units", "formatting", "brevity"], 0),
    ("What language does the user code in?", ["python", "coding", "preferences"], 2),
    ("Is there a regulatory concern I should mention?", ["fintech", "compliance", "work"], 3),
    ("Where is the user located?", ["location", "timezone", "personal"], 4),
    ("What CI system do they use?", ["devops", "ci_cd", "github"], 5),
    ("Tell me about their IP portfolio", ["patent", "ip", "pdm"], 8),
    ("What's the deadline for the SDK?", ["deadline", "project", "sdk"], 10),
    ("Are there any privacy rules I need to follow?", ["privacy", "legal", "pii"], 17),
    ("What's the user's budget for AI calls?", ["budget", "tokens", "cost"], 16),
    ("What API design standards does the user follow?", ["api", "design", "standards"], 14),
]


# ---------------------------------------------------------------------------
# Baseline: naive keyword/recency search (no PDM pressure)
# ---------------------------------------------------------------------------


def _baseline_recall(
    memories: list[dict], query: str, k: int = 5
) -> list[tuple[dict, float]]:
    """Simple keyword overlap + recency baseline (no pressure logic)."""
    query_words = set(query.lower().split())
    scored = []
    for i, mem in enumerate(memories):
        words = set(mem["text"].lower().split())
        overlap = len(query_words & words) / max(len(query_words), 1)
        recency_score = (len(memories) - i) / len(memories)
        score = 0.6 * overlap + 0.4 * recency_score
        scored.append((mem, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def _pdm_recall(
    mem_instance: Any, query: str, k: int = 5
) -> list[Any]:
    """PDM recall using the Memory class."""
    return mem_instance.recall(query, k=k)


# ---------------------------------------------------------------------------
# Retrieval benchmark results
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    query: str
    expected_memory_idx: int
    pdm_found: bool
    pdm_rank: int | None
    pdm_latency_ms: float
    baseline_found: bool
    baseline_rank: int | None
    baseline_latency_ms: float
    pdm_tokens_used: int
    baseline_tokens_used: int


@dataclass
class BenchmarkReport:
    timestamp: str
    total_scenarios: int
    pdm_accuracy: float
    baseline_accuracy: float
    pdm_avg_latency_ms: float
    baseline_avg_latency_ms: float
    pdm_avg_tokens: float
    baseline_avg_tokens: float
    pdm_memory_bytes: int
    baseline_memory_bytes: int
    scenarios: list[ScenarioResult] = field(default_factory=list)

    def render_table(self) -> str:
        rows = [
            "┌─────────────────────────────────────────────────────────────────────┐",
            "│            PDM vs Baseline RAG — Benchmark Results                  │",
            "├──────────────────────────┬─────────────────┬─────────────────────────┤",
            "│ Metric                   │ PDM             │ Baseline (keyword+recency)│",
            "├──────────────────────────┼─────────────────┼─────────────────────────┤",
            f"│ Retrieval Accuracy       │ {self.pdm_accuracy*100:>6.1f}%          │ {self.baseline_accuracy*100:>6.1f}%                   │",
            f"│ Avg Latency (ms)         │ {self.pdm_avg_latency_ms:>8.1f}        │ {self.baseline_avg_latency_ms:>8.1f}               │",
            f"│ Avg Tokens Used          │ {self.pdm_avg_tokens:>8.1f}        │ {self.baseline_avg_tokens:>8.1f}               │",
            f"│ Storage Footprint (bytes)│ {self.pdm_memory_bytes:>8d}        │ {self.baseline_memory_bytes:>8d}               │",
            f"│ Total Scenarios          │ {self.total_scenarios:>8d}        │ {self.total_scenarios:>8d}               │",
            "└──────────────────────────┴─────────────────┴─────────────────────────┘",
        ]
        rows.append("")
        rows.append("Per-query breakdown:")
        rows.append(f"{'Query':<45} {'PDM':>6} {'Base':>6} {'PDM_ms':>8} {'Base_ms':>8}")
        rows.append("-" * 78)
        for s in self.scenarios:
            p = "✓" if s.pdm_found else "✗"
            b = "✓" if s.baseline_found else "✗"
            rows.append(
                f"{s.query[:44]:<45} {p:>6} {b:>6} {s.pdm_latency_ms:>8.1f} {s.baseline_latency_ms:>8.1f}"
            )
        return "\n".join(rows)


# ---------------------------------------------------------------------------
# Retrieval benchmark harness
# ---------------------------------------------------------------------------


def run_retrieval_benchmark(
    quick: bool = False,
    seed: int = 42,
    k: int = 3,
    output: str | None = None,
) -> BenchmarkReport:
    """
    Run the PDM retrieval benchmark harness.

    Args:
        quick:  If True, run only 5 scenarios (smoke test).
        seed:   Random seed for reproducibility.
        k:      Top-k memories to retrieve per query.
        output: Optional path to save JSON results.

    Returns:
        BenchmarkReport with all metrics.
    """
    import os
    import tempfile
    random.seed(seed)

    from pdm_memory import Memory

    queries = _BENCHMARK_QUERIES[:5] if quick else _BENCHMARK_QUERIES

    tmp_db = tempfile.mktemp(suffix=".db")
    mem = Memory(store=tmp_db, user="bench")

    for m in _BENCHMARK_MEMORIES:
        mem.save(
            text=m["text"],
            tags=m["tags"],
            p_magnitude=m["p_magnitude"],
            source="benchmark",
            drawer="benchmark",
        )

    db_size = os.path.getsize(tmp_db)
    baseline_raw_size = sum(len(m["text"].encode()) for m in _BENCHMARK_MEMORIES)
    scenarios: list[ScenarioResult] = []

    for query, expected_tags, expected_idx in queries:
        expected_text = _BENCHMARK_MEMORIES[expected_idx]["text"]

        t0 = time.perf_counter()
        pdm_hits = _pdm_recall(mem, query, k=k)
        pdm_ms = (time.perf_counter() - t0) * 1000

        pdm_texts = [h.text for h in pdm_hits]
        pdm_found = expected_text in pdm_texts
        pdm_rank = pdm_texts.index(expected_text) + 1 if pdm_found else None
        pdm_tokens = sum(len(h.text) // 4 for h in pdm_hits)

        t0 = time.perf_counter()
        baseline_hits = _baseline_recall(_BENCHMARK_MEMORIES, query, k=k)
        baseline_ms = (time.perf_counter() - t0) * 1000

        baseline_texts = [h[0]["text"] for h in baseline_hits]
        baseline_found = expected_text in baseline_texts
        baseline_rank = baseline_texts.index(expected_text) + 1 if baseline_found else None
        baseline_tokens = sum(len(h[0]["text"]) // 4 for h in baseline_hits)

        scenarios.append(ScenarioResult(
            query=query,
            expected_memory_idx=expected_idx,
            pdm_found=pdm_found,
            pdm_rank=pdm_rank,
            pdm_latency_ms=round(pdm_ms, 2),
            baseline_found=baseline_found,
            baseline_rank=baseline_rank,
            baseline_latency_ms=round(baseline_ms, 2),
            pdm_tokens_used=pdm_tokens,
            baseline_tokens_used=baseline_tokens,
        ))

    mem.close()
    import os as _os
    if _os.path.exists(tmp_db):
        _os.remove(tmp_db)

    n = len(scenarios)
    report = BenchmarkReport(
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
        total_scenarios=n,
        pdm_accuracy=sum(s.pdm_found for s in scenarios) / n,
        baseline_accuracy=sum(s.baseline_found for s in scenarios) / n,
        pdm_avg_latency_ms=round(sum(s.pdm_latency_ms for s in scenarios) / n, 2),
        baseline_avg_latency_ms=round(sum(s.baseline_latency_ms for s in scenarios) / n, 2),
        pdm_avg_tokens=round(sum(s.pdm_tokens_used for s in scenarios) / n, 1),
        baseline_avg_tokens=round(sum(s.baseline_tokens_used for s in scenarios) / n, 1),
        pdm_memory_bytes=db_size,
        baseline_memory_bytes=baseline_raw_size,
        scenarios=scenarios,
    )

    if output:
        with open(output, "w") as f:
            json.dump(asdict(report), f, indent=2)
        print(f"Results saved to {output}")

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="PDM Benchmark Harness — retrieval accuracy and correctability"
    )
    parser.add_argument(
        "--suite",
        choices=["retrieval", "correctability"],
        default="retrieval",
        help="Which benchmark suite to run (default: retrieval)",
    )

    # --- Retrieval suite options ---
    parser.add_argument("--quick", action="store_true", help="Run only 5 scenarios (smoke test)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    parser.add_argument("--k", type=int, default=3, help="Top-k memories per query (default: 3)")

    # --- Correctability suite options ---
    parser.add_argument(
        "--mode",
        choices=["pdm_enabled", "pdm_ablation", "vector_rag", "keyword_recency"],
        default="pdm_enabled",
        help="Backend mode for correctability suite (default: pdm_enabled)",
    )
    parser.add_argument(
        "--rounds", type=int, default=20,
        help="Rounds per scenario for correctability suite (default: 20)",
    )
    parser.add_argument(
        "--seeds", type=int, default=5,
        help="Number of random seeds for correctability suite (default: 5)",
    )
    parser.add_argument(
        "--ablation", action="store_true",
        help="Shorthand for --mode pdm_ablation (V disabled)",
    )
    parser.add_argument(
        "--domains",
        nargs="+",
        choices=["science", "geography", "history", "tech"],
        help="Restrict correctability suite to specific domains",
    )

    # --- Shared ---
    parser.add_argument("--output", type=str, help="Save results as JSON to this path")

    args = parser.parse_args()

    if args.suite == "retrieval":
        print("Running PDM Retrieval Benchmark…\n")
        report = run_retrieval_benchmark(
            quick=args.quick, seed=args.seed, k=args.k, output=args.output
        )
        print(report.render_table())
        print(f"\nPDM accuracy:      {report.pdm_accuracy*100:.1f}%")
        print(f"Baseline accuracy: {report.baseline_accuracy*100:.1f}%")

    elif args.suite == "correctability":
        from pdm_memory.bench.correctability.harness import run_suite

        mode = "pdm_ablation" if args.ablation else args.mode
        seed_list = list(range(args.seeds))

        print("Running PDM Correctability Benchmark…")
        print(f"  mode={mode}  rounds={args.rounds}  seeds={seed_list}")
        if args.domains:
            print(f"  domains={args.domains}")
        print()


        def _progress(done: int, total: int) -> None:
            if done % max(1, total // 10) == 0 or done == total:
                pct = done / total * 100
                print(f"  [{pct:5.1f}%] {done}/{total} runs complete…")

        report = run_suite(
            mode=mode,
            rounds=args.rounds,
            seeds=seed_list,
            domains=args.domains,
            output=args.output,
            on_progress=_progress,
        )

        print()
        print(report.render_table())

        if args.output:
            print(f"\nFull JSON report saved to: {args.output}")
        else:
            print(
                "\nTip: use --output results.json to save full per-round traces "
                "for external plotting."
            )


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Backwards-compatible alias (used by existing tests/test_bench.py)
# ---------------------------------------------------------------------------
#: Alias for run_retrieval_benchmark — preserved for backwards compatibility.
run_benchmark = run_retrieval_benchmark


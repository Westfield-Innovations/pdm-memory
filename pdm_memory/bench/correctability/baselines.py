"""
Correctability Benchmark — Baseline Implementations
====================================================

Provides non-PDM baselines to run alongside the correctability harness:

1. VectorRAGBaseline  — simulates a standard embedding-retrieval RAG system.
   It stores memories as static records with their initial authority scores,
   and never updates authority based on feedback.  reinforce() and penalize()
   are no-ops.  Expected behaviour: Memory Gravity Index ≈ 100%.

2. KeywordRecencyBaseline — keyword overlap + recency ordering, no authority
   update.  Included for completeness as the naive baseline.

Both implement the same interface as pdm_memory.Memory so the harness can
call them interchangeably.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Shared record type
# ---------------------------------------------------------------------------


@dataclass
class _BaselineRecord:
    """Internal storage record for a baseline memory entry."""

    memory_id: str
    text: str
    p_magnitude: float          # Static — never changes for RAG baseline
    tags: list[str] = field(default_factory=list)
    created_order: int = 0      # Insertion order for recency ranking


@dataclass
class _BaselineHit:
    """Returned by baseline recall() — mirrors enough of MemoryHit for the harness."""

    id: str
    text: str
    pressure: float
    coupling_score: float = 0.0


# ---------------------------------------------------------------------------
# Vector RAG Baseline
# ---------------------------------------------------------------------------


class VectorRAGBaseline:
    """
    Simulates a standard vector-database RAG system.

    Differences from PDM:
    - Authority (p_magnitude) is set at insert and NEVER changes.
    - reinforce() and penalize() are both no-ops.
    - recall() ranks by initial p_magnitude (static, like a cosine-sim top-K
      that has no mechanism to demote).

    Expected correctability result:
    - Memory Gravity Index ≈ 100%  (wrong high-authority sig stays dominant)
    - Error curve is flat           (never corrects)
    - Crossover round = "never"
    """

    def __init__(self) -> None:
        self._records: list[_BaselineRecord] = []
        self._counter: int = 0

    def save(
        self,
        text: str,
        tags: list[str] | None = None,
        p_magnitude: float = 50.0,
        **_kwargs,
    ) -> str:
        """Store a memory.  Returns a simple string ID."""
        mem_id = f"vrag_{self._counter:04d}"
        self._records.append(
            _BaselineRecord(
                memory_id=mem_id,
                text=text,
                p_magnitude=p_magnitude,
                tags=tags or [],
                created_order=self._counter,
            )
        )
        self._counter += 1
        return mem_id

    def recall(
        self,
        query: str,
        k: int = 1,
        **_kwargs,
    ) -> list[_BaselineHit]:
        """
        Retrieve top-k by static p_magnitude.

        In a real vector RAG this would be cosine similarity, but the ranking
        by initial authority produces the same Memory Gravity effect for this
        benchmark because the score never updates.
        """
        # Simple keyword overlap as a tiebreaker (like cosine sim)
        query_words = set(query.lower().split())
        scored = []
        for rec in self._records:
            tag_words = set(" ".join(rec.tags).lower().split())
            text_words = set(rec.text.lower().split())
            overlap = len(query_words & (tag_words | text_words))
            # Primary sort: p_magnitude (static); secondary: keyword overlap
            score = rec.p_magnitude + overlap * 0.01
            scored.append((score, rec))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            _BaselineHit(
                id=rec.memory_id,
                text=rec.text,
                pressure=rec.p_magnitude,
                coupling_score=0.0,
            )
            for _, rec in scored[:k]
        ]

    def reinforce(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """No-op — vector RAG has no authority update mechanism."""

    def penalize(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """No-op — vector RAG has no authority update mechanism."""

    def get_pressure(self, memory_id: str) -> float | None:
        """Return current p_magnitude for a given ID (always static)."""
        for rec in self._records:
            if rec.memory_id == memory_id:
                return rec.p_magnitude
        return None

    def close(self) -> None:
        """No resources to release."""


# ---------------------------------------------------------------------------
# Keyword + Recency Baseline
# ---------------------------------------------------------------------------


class KeywordRecencyBaseline:
    """
    Keyword overlap + recency ranking — the naive baseline.

    Memories are ranked by: 0.6 × keyword_overlap + 0.4 × recency_score.
    No authority update; both reinforce() and penalize() are no-ops.

    Expected correctability result:
    - Memory Gravity depends on query/keyword alignment.
    - Never corrects based on feedback.
    """

    def __init__(self) -> None:
        self._records: list[_BaselineRecord] = []
        self._counter: int = 0

    def save(
        self,
        text: str,
        tags: list[str] | None = None,
        p_magnitude: float = 50.0,
        **_kwargs,
    ) -> str:
        mem_id = f"kw_{self._counter:04d}"
        self._records.append(
            _BaselineRecord(
                memory_id=mem_id,
                text=text,
                p_magnitude=p_magnitude,
                tags=tags or [],
                created_order=self._counter,
            )
        )
        self._counter += 1
        return mem_id

    def recall(
        self,
        query: str,
        k: int = 1,
        **_kwargs,
    ) -> list[_BaselineHit]:
        """Rank by keyword overlap + recency (static, no feedback)."""
        query_words = set(query.lower().split())
        n = max(1, len(self._records))
        scored = []
        for rec in self._records:
            words = set(rec.text.lower().split()) | set(" ".join(rec.tags).lower().split())
            overlap = len(query_words & words) / max(len(query_words), 1)
            recency = (n - rec.created_order) / n
            score = 0.6 * overlap + 0.4 * recency
            scored.append((score, rec))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            _BaselineHit(
                id=rec.memory_id,
                text=rec.text,
                pressure=rec.p_magnitude,
                coupling_score=score,
            )
            for score, rec in scored[:k]
        ]

    def reinforce(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """No-op."""

    def penalize(self, memory_id: str, coupling_score: float = 0.5) -> None:
        """No-op."""

    def get_pressure(self, memory_id: str) -> float | None:
        for rec in self._records:
            if rec.memory_id == memory_id:
                return rec.p_magnitude
        return None

    def close(self) -> None:
        """No resources to release."""

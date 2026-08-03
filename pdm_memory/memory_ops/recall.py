# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Recall orchestration — candidate load, TAS, hooks, reinforcement."""

from __future__ import annotations

from typing import Any, Protocol

from pdm_memory.core.retrieval import DEFAULT_DIVERSITY_BIAS
from pdm_memory.core.signature import MemoryHit
from pdm_memory.types import RecallHook


class _RecallableMemory(Protocol):
    _engine: Any

    def _load_recall_candidates(
        self,
        *,
        min_pressure: float,
        drawer: str | None,
        candidate_limit: int,
        page_size: int,
    ) -> list[Any]: ...

    def _run_post_recall_hooks(self, ctx: dict[str, Any]) -> None: ...
    def _apply_reinforcement(self, hits: list[MemoryHit]) -> None: ...


def run_recall(
    mem: _RecallableMemory,
    query: str,
    k: int = 5,
    min_pressure: float = 0.0,
    search_cost: float = 0.5,
    drawer: str | None = None,
    reinforce: bool = True,
    *,
    candidate_limit: int = 10_000,
    page_size: int = 500,
    diversity_bias: float | None = DEFAULT_DIVERSITY_BIAS,
    on_recall: RecallHook | None = None,
) -> list[MemoryHit]:
    records = mem._load_recall_candidates(
        min_pressure=min_pressure,
        drawer=drawer,
        candidate_limit=candidate_limit,
        page_size=page_size,
    )
    if not records:
        ctx: dict[str, Any] = {
            "query": query,
            "k": k,
            "hits": [],
            "reinforced": False,
            "min_pressure": min_pressure,
            "search_cost": search_cost,
            "drawer": drawer,
            "diversity_bias": diversity_bias,
        }
        mem._run_post_recall_hooks(ctx)
        return []

    hits = mem._engine.recall(
        records=records,
        query=query,
        k=k,
        search_cost=search_cost,
        diversity_bias=diversity_bias,
    )

    if on_recall:
        for hit in hits:
            on_recall(hit)

    reinforced = bool(reinforce and hits)
    if reinforced:
        mem._apply_reinforcement(hits)

    ctx = {
        "query": query,
        "k": k,
        "hits": hits,
        "reinforced": reinforced,
        "min_pressure": min_pressure,
        "search_cost": search_cost,
        "drawer": drawer,
        "diversity_bias": diversity_bias,
    }
    mem._run_post_recall_hooks(ctx)
    return hits

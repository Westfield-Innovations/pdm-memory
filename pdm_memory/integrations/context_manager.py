# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Context Window Manager — Task 4.2

Trims recalled memories to fit within a model's token budget.

Usage:
    manager = ContextWindowManager(max_tokens=1500, model="gpt-4o")
    trimmed_hits = manager.fit(hits)
    system_block = manager.format_for_prompt(trimmed_hits)
"""

from __future__ import annotations

import logging

from pdm_memory.core.signature import MemoryHit

logger = logging.getLogger(__name__)


class ContextWindowManager:
    """
    Manages the token budget for PDM memories injected into LLM context.

    Trimming strategy: drop memories with the lowest p_effective first,
    until the remaining set fits within max_tokens.

    Args:
        max_tokens:     Maximum tokens the memory block may consume.
        model:          Model name (used to load the correct tokeniser).
        chars_per_token: Approximation fallback if tiktoken is unavailable (default 4).
    """

    def __init__(
        self,
        max_tokens: int = 1500,
        model: str = "gpt-4o-mini",
        chars_per_token: float = 4.0,
    ) -> None:
        self.max_tokens = max_tokens
        self.model = model
        self._chars_per_token = chars_per_token
        self._encoder = self._load_encoder(model)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit(self, hits: list[MemoryHit]) -> list[MemoryHit]:
        """
        Return the highest-pressure subset of hits that fits within max_tokens.

        Memories are sorted by p_effective descending.  The lowest-p ones
        are dropped first to respect the token budget.

        Args:
            hits: List of MemoryHit objects from mem.recall().

        Returns:
            Trimmed list of MemoryHit, still sorted by p_effective desc.
        """
        if not hits:
            return hits

        # Sort highest pressure first (most important memories stay)
        sorted_hits = sorted(hits, key=lambda h: h.p_effective, reverse=True)

        kept: list[MemoryHit] = []
        used_tokens = 0

        for hit in sorted_hits:
            tokens = self.count_tokens(hit.text)
            if used_tokens + tokens <= self.max_tokens:
                kept.append(hit)
                used_tokens += tokens
            else:
                logger.debug(
                    "[CTX] Trimmed memory %s (P=%.1f, tokens=%d): budget exhausted",
                    hit.id[:8], hit.p_effective, tokens,
                )

        if len(kept) < len(hits):
            logger.info(
                "[CTX] Token budget %d: kept %d/%d memories (used %d tokens)",
                self.max_tokens, len(kept), len(hits), used_tokens,
            )

        return kept

    def format_for_prompt(self, hits: list[MemoryHit]) -> str:
        """
        Format a list of MemoryHit objects into a system prompt block.

        Returns an empty string if no hits.
        """
        if not hits:
            return ""

        lines = ["[MEMORY CONTEXT — Pressure-Driven Memory]"]
        for i, hit in enumerate(hits, 1):
            tags = ", ".join(hit.intent_tags) if hit.intent_tags else hit.domain
            lines.append(
                f"{i}. [{hit.drawer}] {hit.text}  "
                f"(relevance: {hit.p_effective:.0f}/100, tags: {tags})"
            )
        lines.append("[END MEMORY CONTEXT]")
        return "\n".join(lines)

    def count_tokens(self, text: str) -> int:
        """Estimate token count for a string."""
        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text))
            except Exception as err:
                logger.debug("[PDM] encode error fallback: %s", err)
        # Fallback: character approximation
        return max(1, int(len(text) / self._chars_per_token))

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _load_encoder(model: str) -> object | None:
        try:
            import tiktoken
            try:
                return tiktoken.encoding_for_model(model)
            except KeyError:
                return tiktoken.get_encoding("cl100k_base")
        except ImportError:
            logger.debug("[CTX] tiktoken not installed; using char approximation")
            return None

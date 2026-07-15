# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Auto-Signature Generator — Task 3.2

Uses an LLM to auto-generate:
  1. compressed_fact   — ≤500 char compressed memory from raw text
  2. intent_tags       — exactly 3 mandatory tags (+ up to 2 optional)
  3. p_magnitude       — suggested importance score (0–100)

Works with both OpenAI and Anthropic clients (duck-typed).

Usage:
    from pdm_memory.ingest.auto_signature import AutoSignatureGenerator
    gen = AutoSignatureGenerator(openai_client)
    result = gen.generate("User said they prefer dark mode and smaller fonts.")
    print(result.compressed_fact)
    print(result.intent_tags)
    print(result.p_magnitude)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


GENERATION_PROMPT = """You are a memory compression engine for an AI assistant.

Your job: compress the raw input into a PDM (Pressure-Driven Memory) signature.

Rules:
1. compressed_fact: A concise factual statement (max 500 characters). 
   Strip conversational filler. Keep only the durable, important information.
2. intent_tags: Exactly 3 to 5 lowercase tags (strings) that categorise this memory.
   Tags must be specific and searchable (e.g. ["units", "formatting", "preferences"]).
   The first 3 are mandatory.
3. p_magnitude: An integer 0-100 representing importance.
   - 80-100: Critical, user-defining preference or fact
   - 60-79:  Important, frequently relevant
   - 40-59:  Useful, occasionally relevant
   - 20-39:  Low signal, rarely matters
   - 0-19:   Near-trivial

Return ONLY valid JSON in this exact format (no markdown):
{
  "compressed_fact": "...",
  "intent_tags": ["tag1", "tag2", "tag3"],
  "p_magnitude": 65
}

Raw input to compress:
"""


@dataclass
class AutoSignatureResult:
    compressed_fact: str
    intent_tags: List[str]
    p_magnitude: float
    raw_response: str = ""


class AutoSignatureGenerator:
    """
    Generates PDM signatures from raw text using an LLM.

    Args:
        llm_client: An OpenAI or Anthropic client instance.
                    The generator will duck-type to detect which one.
        model:      Model name override (defaults per provider).
        max_tokens: Max tokens for the LLM response.
    """

    OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
    ANTHROPIC_DEFAULT_MODEL = "claude-3-haiku-20240307"

    def __init__(
        self,
        llm_client: Any,
        model: Optional[str] = None,
        max_tokens: int = 256,
    ) -> None:
        self._client = llm_client
        self._model = model
        self._max_tokens = max_tokens
        self._provider = self._detect_provider()

    def generate(self, raw_text: str) -> Optional[AutoSignatureResult]:
        """
        Generate a PDM signature for raw_text.

        Args:
            raw_text: The text to compress into a memory.

        Returns:
            AutoSignatureResult or None on failure.
        """
        prompt = GENERATION_PROMPT + raw_text.strip()[:2000]  # Cap input length

        try:
            if self._provider == "openai":
                return self._call_openai(prompt)
            elif self._provider == "anthropic":
                return self._call_anthropic(prompt)
            else:
                logger.warning("[PDM-AutoSig] Unknown LLM provider; trying OpenAI API shape")
                return self._call_openai(prompt)
        except Exception as e:
            logger.warning("[PDM-AutoSig] Generation failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Provider-specific calls
    # ------------------------------------------------------------------

    def _call_openai(self, prompt: str) -> Optional[AutoSignatureResult]:
        model = self._model or self.OPENAI_DEFAULT_MODEL
        resp = self._client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self._max_tokens,
            temperature=0.2,
        )
        raw = resp.choices[0].message.content or ""
        return self._parse_response(raw)

    def _call_anthropic(self, prompt: str) -> Optional[AutoSignatureResult]:
        model = self._model or self.ANTHROPIC_DEFAULT_MODEL
        resp = self._client.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text if resp.content else ""
        return self._parse_response(raw)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_response(self, raw: str) -> Optional[AutoSignatureResult]:
        raw = raw.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON from mixed content
            import re
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                logger.warning("[PDM-AutoSig] Could not parse JSON from: %s", raw[:200])
                return None
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None

        fact = str(data.get("compressed_fact", "")).strip()[:500]
        tags = data.get("intent_tags", [])
        if isinstance(tags, list):
            tags = [str(t).lower().strip() for t in tags if t][:5]
        else:
            tags = []

        if len(tags) < 3:
            logger.warning("[PDM-AutoSig] LLM returned fewer than 3 tags: %s", tags)

        p_mag = float(data.get("p_magnitude", 50))
        p_mag = max(0.0, min(100.0, p_mag))

        if not fact:
            return None

        return AutoSignatureResult(
            compressed_fact=fact,
            intent_tags=tags,
            p_magnitude=p_mag,
            raw_response=raw,
        )

    def _detect_provider(self) -> str:
        """Duck-type detection of OpenAI vs Anthropic client."""
        client_type = type(self._client).__name__.lower()
        module = getattr(type(self._client), "__module__", "") or ""

        if "anthropic" in module or "anthropic" in client_type:
            return "anthropic"
        if "openai" in module or "openai" in client_type:
            return "openai"
        # Check for common attribute shapes
        if hasattr(self._client, "messages") and hasattr(self._client.messages, "create"):
            return "anthropic"
        if hasattr(self._client, "chat") and hasattr(self._client.chat, "completions"):
            return "openai"
        return "openai"  # default fallback

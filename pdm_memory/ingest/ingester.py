# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Data Ingester — Task 3.1

Convert any legacy data source (list[dict], CSV, raw messages) into
PDM SignatureRecords.

Usage (via Memory):
    mem.ingest(
        data_source=[
            {"text": "User dislikes long responses", "importance": 70},
            {"message": "Prefers Python over JS"},
        ],
        mapping={"text": "compressed_fact", "importance": "p_magnitude"},
    )

Usage (standalone):
    from pdm_memory.ingest.ingester import DataIngester
    ingester = DataIngester(storage=driver, user="alice")
    count = ingester.ingest_rows(rows)
"""

from __future__ import annotations

import csv
import logging
from io import StringIO
from typing import Any

from pdm_memory.core.math import calculate_effective_spike, infer_domain
from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.base import BaseStorage

logger = logging.getLogger(__name__)


# Auto-detect common field name aliases
_FIELD_ALIASES: dict[str, str] = {
    # compressed_fact aliases
    "text": "compressed_fact",
    "content": "compressed_fact",
    "message": "compressed_fact",
    "body": "compressed_fact",
    "fact": "compressed_fact",
    "memory": "compressed_fact",
    "summary": "compressed_fact",
    # p_magnitude aliases
    "importance": "p_magnitude",
    "priority": "p_magnitude",
    "pressure": "p_magnitude",
    "score": "p_magnitude",
    "weight": "p_magnitude",
    # intent_tags aliases
    "tags": "intent_tags",
    "labels": "intent_tags",
    "categories": "intent_tags",
    "keywords": "intent_tags",
    # drawer aliases
    "category": "drawer_domain",
    "drawer": "drawer_domain",
    "topic": "drawer_domain",
    "domain": "drawer_domain",
    # source aliases
    "origin": "source",
    "channel": "source",
    # regime aliases
    "context": "question_regime",
    "regime": "question_regime",
}


class DataIngester:
    """
    Converts structured rows into SignatureRecords and saves them.

    Args:
        storage:    The active BaseStorage backend.
        user:       User identifier.
        mapping:    Optional explicit field mapping (source_key → pdm_field).
                    If None, auto-detect via _FIELD_ALIASES.
        llm_client: Optional LLM client for auto-signature generation.
                    If provided, missing fields are filled via the LLM.
    """

    def __init__(
        self,
        storage: BaseStorage,
        user: str = "default",
        mapping: dict[str, str] | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self._storage = storage
        self._user = user
        self._mapping = mapping
        self._llm_client = llm_client

    def ingest_rows(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """
        Ingest a list of dicts.  Returns {'saved': N, 'skipped': N, 'errors': N}.
        """
        counts = {"saved": 0, "skipped": 0, "errors": 0}

        for i, row in enumerate(rows):
            try:
                sig = self._row_to_signature(row)
                if sig is None:
                    counts["skipped"] += 1
                    continue
                self._storage.save(sig)
                counts["saved"] += 1
            except Exception as e:
                logger.warning("[PDM-Ingest] Row %d failed: %s", i, e)
                counts["errors"] += 1

        return counts

    def ingest_csv(self, csv_path_or_content: str) -> dict[str, int]:
        """
        Ingest from a CSV file path or raw CSV string.
        """
        try:
            with open(csv_path_or_content, newline="", encoding="utf-8") as f:
                content = f.read()
        except (FileNotFoundError, OSError):
            content = csv_path_or_content  # Treat as raw CSV string

        reader = csv.DictReader(StringIO(content))
        return self.ingest_rows(list(reader))

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _row_to_signature(self, row: dict[str, Any]) -> SignatureRecord | None:
        """Map one row to a SignatureRecord using auto-detect or explicit mapping."""
        # Build effective mapping
        field_map = self._build_field_map(row)

        # Extract compressed_fact (required)
        text = field_map.get("compressed_fact", "")
        if not text:
            # Try LLM fallback
            if self._llm_client:
                raw_data = " | ".join(f"{k}: {v}" for k, v in row.items())
                text = self._generate_fact_via_llm(raw_data)
            if not text:
                logger.debug("[PDM-Ingest] Skipping row with no text: %s", row)
                return None

        text = str(text).strip()[:500]

        # Extract other fields with defaults
        p_mag = _safe_float(field_map.get("p_magnitude"), default=50.0, min_v=0, max_v=100)
        t_pers = _safe_float(field_map.get("t_persistence"), default=30.0, min_v=1, max_v=365)
        phase = _safe_float(field_map.get("phase_privilege"), default=1.0)
        tags = _parse_tags(field_map.get("intent_tags"))
        drawer = str(field_map.get("drawer_domain") or "general").strip()
        source = str(field_map.get("source") or "csv").strip()
        regime = str(field_map.get("question_regime") or "neutral").strip()

        # Auto-generate tags via LLM if none found
        if not tags and self._llm_client:
            tags = self._generate_tags_via_llm(text)

        # Auto-generate tags (minimum 3 recommended)
        if not tags:
            tags = self._auto_tags_from_text(text)

        domain = infer_domain(tags)
        eff_spike = calculate_effective_spike(p_mag, t_pers, phase)

        return SignatureRecord(
            user=self._user,
            compressed_fact=text,
            source=source,
            p_magnitude=p_mag,
            t_persistence=t_pers,
            phase_privilege=phase,
            effective_spike=eff_spike,
            intent_tags=tags,
            question_regime=regime,
            domain=domain,
            drawer_domain=drawer,
        )

    def _build_field_map(self, row: dict[str, Any]) -> dict[str, Any]:
        """Apply explicit or auto mapping to a row."""
        result: dict[str, Any] = {}

        if self._mapping:
            # Explicit mapping
            for src_key, pdm_key in self._mapping.items():
                if src_key in row:
                    result[pdm_key] = row[src_key]
            # Also pass through keys that directly match PDM fields
            for k, v in row.items():
                if k not in result:
                    result[k] = v
        else:
            # Auto-detect
            for k, v in row.items():
                canonical = _FIELD_ALIASES.get(k.lower(), k.lower())
                result[canonical] = v

        return result

    def _generate_fact_via_llm(self, raw: str) -> str:
        """Ask the LLM to compress raw data into a ≤500 char fact."""
        from pdm_memory.ingest.auto_signature import AutoSignatureGenerator
        gen = AutoSignatureGenerator(self._llm_client)
        result = gen.generate(raw)
        return result.compressed_fact if result else ""

    def _generate_tags_via_llm(self, text: str) -> list[str]:
        """Ask the LLM to generate 3+ tags for a text."""
        from pdm_memory.ingest.auto_signature import AutoSignatureGenerator
        gen = AutoSignatureGenerator(self._llm_client)
        result = gen.generate(text)
        return result.intent_tags if result else []

    @staticmethod
    def _auto_tags_from_text(text: str) -> list[str]:
        """Extract simple keyword tags from text without an LLM."""
        import re
        words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
        stopwords = {
            "this", "that", "with", "from", "have", "been", "will",
            "which", "when", "they", "their", "there", "about",
        }
        seen = set()
        tags = []
        for w in words:
            if w not in stopwords and w not in seen:
                seen.add(w)
                tags.append(w)
            if len(tags) >= 5:
                break
        return tags[:5]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(val: Any, default: float = 0.0, min_v: float | None = None, max_v: float | None = None) -> float:
    try:
        v = float(val)
        if min_v is not None:
            v = max(min_v, v)
        if max_v is not None:
            v = min(max_v, v)
        return v
    except (TypeError, ValueError):
        return default


def _parse_tags(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(t).strip() for t in val if t]
    if isinstance(val, str):
        import json
        try:
            parsed = json.loads(val)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if t]
        except (json.JSONDecodeError, ValueError):
            pass
        # Comma or space separated
        return [t.strip() for t in val.replace(",", " ").split() if t.strip()]
    return []

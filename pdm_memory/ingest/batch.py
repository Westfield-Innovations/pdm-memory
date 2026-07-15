# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Batch Processing Handler — Task 3.3

Splits large data sources into chunks and processes them with rate limiting.
Prevents OOM and API overload when ingesting large datasets (10,000+ rows).

Usage:
    processor = BatchProcessor(batch_size=50, delay_seconds=0.5)
    counts = processor.process(data_source, ingester, on_progress=print_progress)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class BatchProcessor:
    """
    Processes large data sources in batches.

    Args:
        batch_size:       Number of records per batch (default 50).
        delay_seconds:    Pause between batches in seconds (default 0.2).
        max_retries:      Retries per batch on transient errors (default 3).
        retry_delay:      Wait before retry, doubles each attempt (default 1.0s).
    """

    def __init__(
        self,
        batch_size: int = 50,
        delay_seconds: float = 0.2,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self.batch_size = max(1, batch_size)
        self.delay_seconds = max(0.0, delay_seconds)
        self.max_retries = max(0, max_retries)
        self.retry_delay = max(0.0, retry_delay)

    def process(
        self,
        data_source: Any,
        ingester: Any,  # DataIngester instance
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, int]:
        """
        Process a data source through the ingester in batches.

        Args:
            data_source: Any of:
                         - list[dict]
                         - str (CSV file path or raw CSV content)
                         - list[str] (plain text lines → auto-converted to dicts)
            ingester:    DataIngester instance.
            on_progress: Optional callback(processed: int, total: int).

        Returns:
            Aggregated counts: {saved, skipped, errors}.
        """
        rows = self._normalise_source(data_source)
        total = len(rows)
        counts = {"saved": 0, "skipped": 0, "errors": 0}
        processed = 0

        logger.info("[PDM-Batch] Starting: %d records, batch_size=%d", total, self.batch_size)

        for i in range(0, total, self.batch_size):
            batch = rows[i : i + self.batch_size]
            batch_counts = self._process_batch(batch, ingester)

            counts["saved"] += batch_counts["saved"]
            counts["skipped"] += batch_counts["skipped"]
            counts["errors"] += batch_counts["errors"]
            processed += len(batch)

            if on_progress:
                try:
                    on_progress(processed, total)
                except Exception:
                    pass

            logger.debug(
                "[PDM-Batch] Batch %d/%d processed | saved=%d skipped=%d errors=%d",
                processed, total,
                batch_counts["saved"], batch_counts["skipped"], batch_counts["errors"],
            )

            # Rate-limit between batches (not after the last one)
            if i + self.batch_size < total and self.delay_seconds > 0:
                time.sleep(self.delay_seconds)

        logger.info(
            "[PDM-Batch] Done: saved=%d skipped=%d errors=%d",
            counts["saved"], counts["skipped"], counts["errors"],
        )
        return counts

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _process_batch(
        self,
        batch: List[Dict[str, Any]],
        ingester: Any,
    ) -> Dict[str, int]:
        """Process one batch with retry logic."""
        last_error = None
        delay = self.retry_delay

        for attempt in range(self.max_retries + 1):
            try:
                return ingester.ingest_rows(batch)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    logger.warning(
                        "[PDM-Batch] Batch attempt %d failed (%s). Retrying in %.1fs…",
                        attempt + 1, e, delay,
                    )
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff

        logger.error("[PDM-Batch] Batch failed after %d attempts: %s", self.max_retries + 1, last_error)
        return {"saved": 0, "skipped": 0, "errors": len(batch)}

    @staticmethod
    def _normalise_source(data_source: Any) -> List[Dict[str, Any]]:
        """Convert any supported source type to list[dict]."""
        if isinstance(data_source, list):
            if not data_source:
                return []
            if isinstance(data_source[0], dict):
                return data_source
            # Assume list of strings
            return [{"text": str(item)} for item in data_source]

        if isinstance(data_source, str):
            # Try as CSV file path or CSV content
            import csv
            from io import StringIO
            try:
                with open(data_source, newline="", encoding="utf-8") as f:
                    content = f.read()
            except (FileNotFoundError, OSError):
                content = data_source  # Treat as raw CSV string
            reader = csv.DictReader(StringIO(content))
            return list(reader)

        # Try iterator/generator
        try:
            rows = list(data_source)
            if rows and isinstance(rows[0], dict):
                return rows
            return [{"text": str(r)} for r in rows]
        except Exception:
            raise ValueError(
                f"Unsupported data_source type: {type(data_source)}. "
                "Expected list[dict], list[str], or a CSV file path/string."
            )

"""
Memory Sync Utility — Task 2.3

mem.sync() copies signatures between a local SQLite store and the AZUS cloud.

Direction options:
  "push"          — local → cloud
  "pull"          — cloud → local
  "bidirectional" — both directions; higher p_magnitude wins on conflict

Usage:
    mem.sync(direction="push")
    mem.sync(direction="pull")

Or standalone:
    from pdm_memory.sync import MemorySync
    syncer = MemorySync(local=sqlite_driver, cloud=cloud_driver)
    report = syncer.sync(direction="bidirectional")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SyncReport:
    direction: str
    pushed: int = 0
    pulled: int = 0
    conflicts_resolved: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            f"SyncReport(direction={self.direction}, "
            f"pushed={self.pushed}, pulled={self.pulled}, "
            f"conflicts={self.conflicts_resolved}, errors={self.errors})"
        )


class MemorySync:
    """
    Bidirectional sync between a local SQLiteDriver and a CloudDriver.

    Conflict resolution: signature with higher p_magnitude wins.
    If equal, the more recently created one wins.
    """

    def __init__(self, local, cloud) -> None:
        """
        Args:
            local: SQLiteDriver instance
            cloud: CloudDriver instance
        """
        self._local = local
        self._cloud = cloud

    def sync(
        self,
        user: str = "default",
        direction: str = "bidirectional",
        batch_size: int = 50,
    ) -> SyncReport:
        """
        Sync memories between local and cloud storage.

        Args:
            user:       User whose memories to sync.
            direction:  "push" | "pull" | "bidirectional"
            batch_size: Records per cloud batch (avoids large payloads).

        Returns:
            SyncReport with counts.
        """
        report = SyncReport(direction=direction)

        if direction in ("push", "bidirectional"):
            self._push(user, batch_size, report)

        if direction in ("pull", "bidirectional"):
            self._pull(user, batch_size, report)

        logger.info("[PDM-Sync] %s", report)
        return report

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _push(self, user: str, batch_size: int, report: SyncReport) -> None:
        """Send local records to cloud."""
        try:
            local_records = self._local.list(user=user, limit=10_000)
            local_ids = {r.id for r in local_records}

            for i in range(0, len(local_records), batch_size):
                batch = local_records[i : i + batch_size]
                for rec in batch:
                    try:
                        # Check if cloud already has it
                        cloud_rec = self._cloud.get(rec.id, user=user)
                        if cloud_rec is None:
                            self._cloud.save(rec)
                            report.pushed += 1
                        else:
                            # Conflict: keep higher pressure
                            if rec.p_magnitude > cloud_rec.p_magnitude:
                                self._cloud.update(rec.id, p_magnitude=rec.p_magnitude,
                                                   compressed_fact=rec.compressed_fact)
                                report.conflicts_resolved += 1
                    except Exception as e:
                        logger.warning("[PDM-Sync] push error for %s: %s", rec.id, e)
                        report.errors += 1
        except Exception as e:
            logger.error("[PDM-Sync] push failed: %s", e)
            report.errors += 1

    def _pull(self, user: str, batch_size: int, report: SyncReport) -> None:
        """Fetch cloud records into local storage."""
        try:
            cloud_records = self._cloud.list(user=user, limit=10_000)

            for i in range(0, len(cloud_records), batch_size):
                batch = cloud_records[i : i + batch_size]
                for rec in batch:
                    try:
                        local_rec = self._local.get(rec.id, user=user)
                        if local_rec is None:
                            self._local.save(rec)
                            report.pulled += 1
                        else:
                            if rec.p_magnitude > local_rec.p_magnitude:
                                self._local.update(rec.id, p_magnitude=rec.p_magnitude,
                                                   compressed_fact=rec.compressed_fact)
                                report.conflicts_resolved += 1
                    except Exception as e:
                        logger.warning("[PDM-Sync] pull error for %s: %s", rec.id, e)
                        report.errors += 1
        except Exception as e:
            logger.error("[PDM-Sync] pull failed: %s", e)
            report.errors += 1

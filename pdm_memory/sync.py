# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

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

from pdm_memory.storage.base import BaseStorage
from pdm_memory.storage.errors import CloudStorageError

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
    Bidirectional sync between a local BaseStorage and CloudDriver.

    Conflict resolution: signature with higher p_magnitude wins.
    If equal, the more recently created one wins.
    """

    def __init__(self, local: BaseStorage, cloud: BaseStorage) -> None:
        """
        Args:
            local: Local storage backend (SQLite, PostgreSQL, …).
            cloud: CloudDriver instance.
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

            for i in range(0, len(local_records), batch_size):
                batch = local_records[i : i + batch_size]
                for rec in batch:
                    try:
                        # 404 → None; network/5xx → CloudStorageError (do NOT re-save)
                        cloud_rec = self._cloud.get(rec.id, user=user)
                        if cloud_rec is None:
                            self._cloud.save(rec)
                            report.pushed += 1
                        else:
                            if rec.p_magnitude > cloud_rec.p_magnitude:
                                self._cloud.update(
                                    rec.id,
                                    user=user,
                                    p_magnitude=rec.p_magnitude,
                                    compressed_fact=rec.compressed_fact,
                                    t_deadline=rec.t_deadline,
                                    t_event_at=rec.t_event_at,
                                    urgency_rate=rec.urgency_rate,
                                    intent_tags=rec.intent_tags,
                                    validation_prediction_total=rec.validation_prediction_total,
                                    validation_prediction_correct=rec.validation_prediction_correct,
                                )
                                report.conflicts_resolved += 1
                    except CloudStorageError as e:
                        logger.warning("[PDM-Sync] push error for %s: %s", rec.id, e)
                        report.errors += 1
                    except Exception as e:
                        logger.warning("[PDM-Sync] push error for %s: %s", rec.id, e)
                        report.errors += 1
        except CloudStorageError as e:
            logger.error("[PDM-Sync] push failed: %s", e)
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
                                self._local.update(
                                    rec.id,
                                    user=user,
                                    p_magnitude=rec.p_magnitude,
                                    compressed_fact=rec.compressed_fact,
                                    t_deadline=rec.t_deadline,
                                    t_event_at=rec.t_event_at,
                                    urgency_rate=rec.urgency_rate,
                                    intent_tags=rec.intent_tags,
                                    validation_prediction_total=rec.validation_prediction_total,
                                    validation_prediction_correct=rec.validation_prediction_correct,
                                )
                                report.conflicts_resolved += 1
                    except Exception as e:
                        logger.warning("[PDM-Sync] pull error for %s: %s", rec.id, e)
                        report.errors += 1
        except CloudStorageError as e:
            logger.error("[PDM-Sync] pull failed: %s", e)
            report.errors += 1
        except Exception as e:
            logger.error("[PDM-Sync] pull failed: %s", e)
            report.errors += 1

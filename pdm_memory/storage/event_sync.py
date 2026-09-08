"""
``EventSync`` — carry the evidence layer across local ↔ cloud.

``MemorySync`` moves signatures and is frozen, so this sits beside it rather
than inside it, and takes the same two arguments for the same reason: it works
against ``BaseStorage``, not against a particular driver.

Order is not incidental. Events go first, then entities, then the mentions that
point at both, then the signature links. A mention pushed before its event
would reference a row that does not exist yet, and on the local side that is a
foreign-key error rather than a warning.

Conflict resolution barely arises here, which is the point of an append-only
layer: an event that exists on both sides is the *same* event, because the
hash says so. There is nothing to reconcile — only something to skip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pdm_memory.storage.errors import CloudStorageError
from pdm_memory.storage.events import storage_supports_events

logger = logging.getLogger(__name__)

__all__ = ["EventSync", "EventSyncReport"]


@dataclass
class EventSyncReport:
    direction: str
    events_pushed: int = 0
    events_pulled: int = 0
    events_deduplicated: int = 0
    entities_pushed: int = 0
    mentions_pushed: int = 0
    errors: int = 0
    unsupported: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"EventSyncReport(direction={self.direction}, "
            f"events={self.events_pushed}/{self.events_pulled}, "
            f"deduplicated={self.events_deduplicated}, "
            f"entities={self.entities_pushed}, mentions={self.mentions_pushed}, "
            f"errors={self.errors})"
        )


class EventSync:
    """Move source events, entities and mentions between two stores."""

    def __init__(self, local, cloud) -> None:
        self._local = local
        self._cloud = cloud

    def sync(
        self,
        user: str = "default",
        direction: str = "push",
        limit: int = 1000,
    ) -> EventSyncReport:
        """
        Args:
            user:      Whose evidence to move.
            direction: ``"push"``, ``"pull"``, or ``"bidirectional"``.
            limit:     Cap on rows read per side.

        A store that cannot carry events is reported, not raised on: a mixed
        fleet is the normal state during a rollout, and the caller wants the
        signatures synced either way.
        """
        report = EventSyncReport(direction=direction)

        for name, store in (("local", self._local), ("cloud", self._cloud)):
            if not storage_supports_events(store):
                report.unsupported.append(name)

        if report.unsupported:
            logger.info(
                "[PDM-EventSync] Skipped: %s cannot carry events",
                " and ".join(report.unsupported),
            )
            return report

        if direction in ("push", "bidirectional"):
            self._push(user, limit, report)
        if direction in ("pull", "bidirectional"):
            self._pull(user, limit, report)

        logger.info("[PDM-EventSync] %s", report)
        return report

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _push(self, user: str, limit: int, report: EventSyncReport) -> None:
        try:
            events = self._local.list_source_events(user=user, limit=limit)
        except Exception as exc:
            logger.error("[PDM-EventSync] cannot read local events: %s", exc)
            report.errors += 1
            return

        # id here → id there. An event's identity is its hash, so the two sides
        # may legitimately hold it under different primary keys.
        remapped: dict[str, str] = {}

        for event in events:
            try:
                existing = self._cloud.find_event_by_hash(event.content_hash, user=user)
                if existing is not None:
                    remapped[event.id] = existing.id
                    report.events_deduplicated += 1
                    continue
                local_id = event.id
                remapped[local_id] = self._cloud.save_source_event(event)
                report.events_pushed += 1
            except CloudStorageError as exc:
                logger.warning("[PDM-EventSync] push event %s: %s", event.id, exc)
                report.errors += 1
            except Exception as exc:
                logger.warning("[PDM-EventSync] push event %s: %s", event.id, exc)
                report.errors += 1

        self._push_entities(user, limit, report)
        self._push_mentions(user, limit, remapped, report)

    def _push_entities(self, user: str, limit: int, report: EventSyncReport) -> None:
        try:
            entities = self._local.list_entities(user=user)
        except Exception as exc:
            logger.warning("[PDM-EventSync] cannot read local entities: %s", exc)
            report.errors += 1
            return

        for entity in entities[:limit]:
            try:
                self._cloud.resolve_or_create_entity(
                    user=user,
                    surface_form=entity.canonical_name,
                    field_id=entity.origin_field_id,
                    entity_type=entity.entity_type,
                )
                report.entities_pushed += 1
            except Exception as exc:
                logger.warning(
                    "[PDM-EventSync] push entity %s: %s", entity.canonical_name, exc
                )
                report.errors += 1

    def _push_mentions(
        self,
        user: str,
        limit: int,
        remapped: dict[str, str],
        report: EventSyncReport,
    ) -> None:
        try:
            mentions = self._local.unresolved_mentions(user=user, limit=limit)
        except Exception as exc:
            logger.warning("[PDM-EventSync] cannot read local mentions: %s", exc)
            report.errors += 1
            return

        for mention in mentions:
            try:
                # Point the mention at the event id the far side actually uses.
                mention.source_event_id = remapped.get(
                    mention.source_event_id, mention.source_event_id
                )
                self._cloud.record_mention(mention)
                report.mentions_pushed += 1
            except Exception as exc:
                logger.warning("[PDM-EventSync] push mention %s: %s", mention.id, exc)
                report.errors += 1

    def _pull(self, user: str, limit: int, report: EventSyncReport) -> None:
        try:
            events = self._cloud.list_source_events(user=user, limit=limit)
        except Exception as exc:
            logger.error("[PDM-EventSync] cannot read cloud events: %s", exc)
            report.errors += 1
            return

        for event in events:
            try:
                before = self._local.find_event_by_hash(event.content_hash, user=user)
                self._local.save_source_event(event)
                if before is None:
                    report.events_pulled += 1
                else:
                    report.events_deduplicated += 1
            except Exception as exc:
                logger.warning("[PDM-EventSync] pull event %s: %s", event.id, exc)
                report.errors += 1

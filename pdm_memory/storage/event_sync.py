"""
``EventSync`` — carry the evidence layer between two stores.

``MemorySync`` moves signatures and is frozen, so this sits beside it rather
than inside it, and takes the same two arguments for the same reason: it works
against ``BaseStorage``, not against a particular driver.

Order is not incidental. Events go first, then entities, then the mentions that
point at both. A mention pushed before its event references a row that does not
exist yet, and on the local side that is a foreign-key error rather than a
warning.

Identity does not survive the trip by id. An event is the same event on both
sides because its hash says so, and an entity is the same identity because its
name and field say so — but each side stores them under its own primary keys.
So every pass builds a translation table as it goes and rewrites the pointers
it carries. Skipping that step is how a mention ends up attached to nothing.

Conflict resolution barely arises: an event present on both sides *is* the same
event. There is nothing to reconcile, only something to skip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pdm_memory.storage.errors import CloudStorageError
from pdm_memory.storage.events import storage_supports_events

logger = logging.getLogger(__name__)

__all__ = ["EventSync", "EventSyncReport"]

DEFAULT_PAGE_SIZE = 500


@dataclass
class EventSyncReport:
    direction: str
    events_pushed: int = 0
    events_pulled: int = 0
    events_deduplicated: int = 0
    entities_pushed: int = 0
    entities_pulled: int = 0
    mentions_pushed: int = 0
    mentions_pulled: int = 0
    links_transferred: int = 0
    links_missing_signature: int = 0
    errors: int = 0
    unsupported: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"EventSyncReport(direction={self.direction}, "
            f"events={self.events_pushed}/{self.events_pulled}, "
            f"deduplicated={self.events_deduplicated}, "
            f"entities={self.entities_pushed}/{self.entities_pulled}, "
            f"mentions={self.mentions_pushed}/{self.mentions_pulled}, "
            f"links={self.links_transferred}, "
            f"errors={self.errors})"
        )

    @property
    def advice(self) -> str:
        """What the caller should do about anything this pass could not finish."""
        if self.links_missing_signature:
            return (
                f"{self.links_missing_signature} signature links had no signature "
                "to attach to on the far side. EventSync moves events, entities "
                "and mentions; signatures are MemorySync's. Run MemorySync first, "
                "then this."
            )
        return ""


class EventSync:
    """Move source events, entities and mentions between two stores."""

    def __init__(self, local, remote, *, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        """
        Args:
            local:  The store being synced from on a push, into on a pull.
            remote: The other one. Named for its role, not its transport —
                    both sides are ordinary BaseStorage, and with the
                    speculative cloud client gone the realistic pairing is a
                    local SQLite against a shared Postgres.
        """
        self._local = local
        self._remote = remote
        self._page_size = max(1, page_size)

    def sync(self, user: str = "default", direction: str = "push") -> EventSyncReport:
        """
        Args:
            user:      Whose evidence to move. Rows land under this user on the
                       far side regardless of what the payload claims.
            direction: ``"push"``, ``"pull"``, or ``"bidirectional"``.

        A store that cannot carry events is reported, not raised on: a mixed
        fleet is the normal state during a rollout, and the caller wants the
        signatures synced either way.
        """
        report = EventSyncReport(direction=direction)

        for name, store in (("local", self._local), ("remote", self._remote)):
            if not storage_supports_events(store):
                report.unsupported.append(name)

        if report.unsupported:
            logger.info(
                "[PDM-EventSync] Skipped: %s cannot carry events",
                " and ".join(report.unsupported),
            )
            return report

        if direction in ("push", "bidirectional"):
            self._transfer(self._local, self._remote, user, report, pulling=False)
        if direction in ("pull", "bidirectional"):
            self._transfer(self._remote, self._local, user, report, pulling=True)

        logger.info("[PDM-EventSync] %s", report)
        return report

    # ------------------------------------------------------------------
    # One direction, both ways round
    # ------------------------------------------------------------------

    def _transfer(self, source, target, user: str, report: EventSyncReport, *, pulling: bool):
        """
        Push and pull differ only in which store is read. Writing this once
        keeps the id translation identical in both directions, which is where
        the two hand-written copies had drifted apart — the pull side moved
        events alone and left every entity and mention behind.
        """
        events = self._transfer_events(source, target, user, report, pulling=pulling)
        entities = self._transfer_entities(source, target, user, report, pulling=pulling)
        self._transfer_mentions(
            source, target, user, report, events, entities, pulling=pulling
        )
        self._transfer_signature_links(source, target, user, report, events, entities)

    def _transfer_events(
        self, source, target, user: str, report: EventSyncReport, *, pulling: bool
    ) -> dict[str, str]:
        """Returns source id → target id for everything that crossed."""
        remapped: dict[str, str] = {}
        try:
            events = source.iter_source_events(user=user, batch=self._page_size)
        except Exception as exc:
            logger.error("[PDM-EventSync] cannot read events: %s", exc)
            report.errors += 1
            return remapped

        for event in events:
            try:
                source_id = event.id
                # Stamp the requested user rather than trust the payload. The
                # cloud driver defaults a missing user to "default", so a pull
                # for alice used to store under default and then deduplicate
                # under alice — every pull writing another copy.
                event.user = user

                stored_id = target.save_source_event(event)
                remapped[source_id] = stored_id

                # No question asked before the write. save_source_event is
                # idempotent on the hash and reports on the record whether the
                # far side already held the event, so the write itself answers
                # what the ask-then-write version spent a second round trip per
                # row to find out.
                if event.was_deduplicated:
                    report.events_deduplicated += 1
                elif pulling:
                    report.events_pulled += 1
                else:
                    report.events_pushed += 1
            except CloudStorageError as exc:
                logger.warning("[PDM-EventSync] event %s: %s", event.id, exc)
                report.errors += 1
            except Exception as exc:
                logger.warning("[PDM-EventSync] event %s: %s", event.id, exc)
                report.errors += 1
        return remapped

    def _transfer_entities(
        self, source, target, user: str, report: EventSyncReport, *, pulling: bool
    ) -> dict[str, str]:
        remapped: dict[str, str] = {}
        try:
            entities = source.list_entities(user=user)
        except Exception as exc:
            logger.warning("[PDM-EventSync] cannot read entities: %s", exc)
            report.errors += 1
            return remapped

        for entity in entities:
            try:
                # Resolution is by name and field on both sides, so asking the
                # target to resolve returns its own id for the same identity —
                # which is exactly the translation the mentions will need.
                remapped[entity.id] = target.resolve_or_create_entity(
                    user=user,
                    surface_form=entity.canonical_name,
                    field_id=entity.origin_field_id,
                    entity_type=entity.entity_type,
                )
                if pulling:
                    report.entities_pulled += 1
                else:
                    report.entities_pushed += 1
            except Exception as exc:
                logger.warning(
                    "[PDM-EventSync] entity %s: %s", entity.canonical_name, exc
                )
                report.errors += 1
        return remapped

    def _transfer_signature_links(
        self,
        source,
        target,
        user: str,
        report: EventSyncReport,
        events: dict[str, str],
        entities: dict[str, str],
    ) -> None:
        """
        Reattach each fact to the message it came from, and to whom it is about.

        Signatures themselves are MemorySync's cargo — its payload predates
        these two columns and it is a frozen module, so it cannot learn them.
        That left the links belonging to nobody: events crossed, facts crossed,
        and what tied them together did not. Both passes reported no errors,
        and the far side looked clean, because a pointer that is empty is not a
        pointer that dangles.

        Runs last because it needs the other three to have finished: the ids it
        writes are the far side's own, taken from the translation tables the
        earlier passes built.
        """
        try:
            linked = source.iter_linked_signatures(user=user, batch=self._page_size)
        except Exception as exc:
            logger.warning("[PDM-EventSync] cannot read signature links: %s", exc)
            report.errors += 1
            return

        for signature_id, event_id, entity_id in linked:
            try:
                target.link_signature(
                    signature_id,
                    source_event_id=events.get(event_id) if event_id else None,
                    primary_entity_id=entities.get(entity_id) if entity_id else None,
                    user=user,
                )
                report.links_transferred += 1
            except KeyError:
                # The signature is not there yet. Counted rather than raised:
                # a caller who has not run MemorySync wants to be told what to
                # do, not handed a traceback halfway through a sync.
                report.links_missing_signature += 1
            except Exception as exc:
                logger.warning(
                    "[PDM-EventSync] link for signature %s: %s", signature_id, exc
                )
                report.errors += 1

        if report.links_missing_signature:
            logger.warning("[PDM-EventSync] %s", report.advice)

    def _transfer_mentions(
        self,
        source,
        target,
        user: str,
        report: EventSyncReport,
        events: dict[str, str],
        entities: dict[str, str],
        *,
        pulling: bool,
    ) -> None:
        try:
            mentions = source.iter_mentions(user=user, batch=self._page_size)
        except Exception as exc:
            logger.warning("[PDM-EventSync] cannot read mentions: %s", exc)
            report.errors += 1
            return

        for mention in mentions:
            try:
                mention.user = user
                mention.source_event_id = events.get(
                    mention.source_event_id, mention.source_event_id
                )
                resolved_to = (
                    entities.get(mention.entity_id) if mention.entity_id else None
                )
                if mention.entity_id and resolved_to is None:
                    # The entity did not make it across. Carrying its grade
                    # anyway lands the mention in the unresolved queue wearing
                    # a human's confirmation, where resolve_mention refuses to
                    # touch it because a confirmation is not revisable — stuck
                    # for good. It arrives as what it now is: unattributed.
                    logger.warning(
                        "[PDM-EventSync] mention %s arrives unresolved: its "
                        "entity did not transfer",
                        mention.id,
                    )
                    mention.resolution = "unresolved"
                    mention.resolved_at = None
                    mention.confidence = None
                mention.entity_id = resolved_to
                mention_id = target.record_mention(mention)

                # The grade travels with the attribution. Downgrading a
                # person's confirmed answer to an automatic one because it
                # crossed a wire would lose the only thing that distinguishes
                # them.
                if resolved_to is not None:
                    target.resolve_mention(
                        mention_id,
                        entity_id=resolved_to,
                        method=mention.resolution,
                        confidence=mention.confidence,
                    )
                if pulling:
                    report.mentions_pulled += 1
                else:
                    report.mentions_pushed += 1
            except Exception as exc:
                logger.warning("[PDM-EventSync] mention %s: %s", mention.id, exc)
                report.errors += 1

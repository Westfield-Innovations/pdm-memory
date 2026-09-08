"""
``EventLog`` — the public way to use the evidence layer from a ``Memory``.

``Memory`` itself is a frozen module, so the event methods cannot be added to
it without the licence conversation K1 opens. They do not need to be: a thin
object over a ``Memory`` reads about the same at the call site and keeps the
whole feature outside the frozen surface.

    mem = Memory(storage=EventfulSQLiteDriver(db_path="./app.db"))
    log = EventLog(mem)

    ids = log.ingest(
        event=log.event("chat_message", raw_reference="chat:123:msg:456"),
        payload="Moved the Orion release review to Friday. Alex is on it.",
        facts=[
            {"text": "Orion release moved to Friday", "tags": ["orion", "release", "date"]},
            {"text": "Alex owns the Orion release", "tags": ["orion", "alex", "owner"],
             "about": "Alex"},
        ],
        field_id="work",
    )

One call, one event row, two signatures, one mention, one entity — which is
AC1 stated as an API rather than as a schema.

When the licence question is settled, these methods move onto ``Memory``
verbatim and this class becomes a two-line alias.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.events import (
    EntityMentionRecord,
    EntityRecord,
    SourceEventRecord,
    storage_supports_events,
)

logger = logging.getLogger(__name__)

__all__ = ["EventLog"]


class EventLog:
    """Source events, entities and mentions for a ``Memory`` instance."""

    def __init__(self, memory: Any) -> None:
        """
        Args:
            memory: A ``Memory`` whose storage carries events.

        Raises:
            RuntimeError: The driver behind *memory* has no evidence layer —
                said plainly here rather than as an ``AttributeError`` three
                frames deeper.
        """
        storage = getattr(memory, "_storage", None)
        if not storage_supports_events(storage):
            driver = type(storage).__name__ if storage else "None"
            raise RuntimeError(
                f"{driver} does not carry source events. Use "
                "Memory(storage=EventfulSQLiteDriver(db_path=...)), or call "
                "pdm_memory.storage.eventful_sqlite.enable_events() before "
                "constructing Memory."
            )
        self._memory = memory
        self._storage = storage
        self._user = getattr(memory, "_user", "default")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def event(
        self,
        event_type: str = "chat_message",
        *,
        occurred_at: datetime | None = None,
        observed_at: datetime | None = None,
        source_system: str = "chat",
        raw_reference: str = "",
        provenance: dict[str, Any] | None = None,
        capture_authority_state: str = "unknown",
        compliance_state: str = "unknown",
    ) -> SourceEventRecord:
        """Build an unsaved event. ``content_hash`` is filled in on save."""
        return SourceEventRecord(
            user=self._user,
            event_type=event_type,
            occurred_at=occurred_at,
            observed_at=observed_at,
            source_system=source_system,
            raw_reference=raw_reference,
            provenance=provenance or {},
            capture_authority_state=capture_authority_state,
            compliance_state=compliance_state,
        )

    def record(self, event: SourceEventRecord, *, payload: str = "") -> str:
        """Store the event, or return the id of the one already storing it."""
        return self._storage.save_source_event(event, payload=payload)

    def get(self, event_id: str) -> SourceEventRecord | None:
        return self._storage.get_source_event(event_id)

    def find_by_hash(self, content_hash: str) -> SourceEventRecord | None:
        return self._storage.find_event_by_hash(content_hash, user=self._user)

    def events(self, limit: int = 100) -> list[SourceEventRecord]:
        return self._storage.list_source_events(user=self._user, limit=limit)

    def signatures_for(self, event_id: str) -> list[SignatureRecord]:
        return self._storage.signatures_for_event(event_id, user=self._user)

    # ------------------------------------------------------------------
    # The whole flow, in one call
    # ------------------------------------------------------------------

    def ingest(
        self,
        *,
        event: SourceEventRecord,
        facts: Sequence[dict[str, Any]],
        payload: str = "",
        field_id: str = "",
    ) -> dict[str, Any]:
        """
        One event, many signatures, with provenance and identity wired up.

        Each entry in *facts* takes ``text`` plus whatever ``Memory.save``
        accepts, and optionally ``about`` — a name as written in the source.
        A name produces a mention (evidence, always recorded) and, through the
        name-and-field rule, an identity the signature points at.

        Re-ingesting the same payload reuses the event and re-records nothing:
        the event dedupes on its hash, the mention on its own key.

        Returns ``{"source_event_id", "signature_ids", "entity_ids",
        "deduplicated"}``.
        """
        seen_before = (
            self._storage.find_event_by_hash(
                event.ensure_content_hash(payload=payload), user=self._user
            )
            is not None
        )
        event_id = self.record(event, payload=payload)

        signature_ids: list[str] = []
        entity_ids: dict[str, str] = {}

        for fact in facts:
            spec = dict(fact)
            text = spec.pop("text")
            about = spec.pop("about", None)

            memory_id = self._memory.save(text, **spec)
            signature_ids.append(memory_id)

            entity_id = None
            if about:
                entity_id = self.mention(
                    about,
                    field_id=field_id,
                    source_event_id=event_id,
                    signature_id=memory_id,
                )
                entity_ids[about] = entity_id

            self._storage.link_signature(
                memory_id,
                source_event_id=event_id,
                primary_entity_id=entity_id,
                user=self._user,
            )

        return {
            "source_event_id": event_id,
            "signature_ids": signature_ids,
            "entity_ids": entity_ids,
            "deduplicated": seen_before,
        }

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def mention(
        self,
        surface_form: str,
        *,
        field_id: str = "",
        source_event_id: str = "",
        signature_id: str = "",
        resolve: bool = True,
    ) -> str | None:
        """
        Record that a name was used, and resolve it under the default rule.

        Pass ``resolve=False`` to record the evidence and leave attribution for
        later — the honest choice when the field is not known yet, since a
        resolution made without one is a guess wearing a provenance label.
        """
        record = EntityMentionRecord(
            user=self._user,
            surface_form=surface_form,
            field_id=field_id,
            source_event_id=source_event_id,
            signature_id=signature_id,
        )
        mention_id = self._storage.record_mention(record)
        if not resolve:
            return None

        entity_id = self._storage.resolve_or_create_entity(
            user=self._user, surface_form=surface_form, field_id=field_id
        )
        self._storage.resolve_mention(
            mention_id, entity_id=entity_id, method="same_name_same_field"
        )
        return entity_id

    def confirm(self, mention_id: str, entity_id: str) -> None:
        """
        Write a person's answer to "which Alex?" back into the store.

        Recorded as ``user_confirmed``, which no automatic pass will overwrite.
        That is what turns the disambiguation prompt into something that pays
        for itself instead of asking again next week.
        """
        self._storage.resolve_mention(
            mention_id, entity_id=entity_id, method="user_confirmed", confidence=1.0
        )

    def pending(self, limit: int = 100) -> list[EntityMentionRecord]:
        """Mentions with no identity yet — the queue behind the prompt."""
        return self._storage.unresolved_mentions(user=self._user, limit=limit)

    def entities(self, include_dissolved: bool = False) -> list[EntityRecord]:
        return self._storage.list_entities(
            user=self._user, include_dissolved=include_dissolved
        )

    def entity(self, entity_id: str) -> EntityRecord | None:
        return self._storage.get_entity(entity_id)

    def about(self, entity_id: str) -> list[SignatureRecord]:
        """Everything the store holds about one identity."""
        return self._storage.signatures_for_entity(entity_id, user=self._user)

    def merge(self, keep_id: str, merge_id: str, *, method: str = "user_confirmed") -> None:
        """Two identities turned out to be one person. Reversible; see D6."""
        self._storage.merge_entities(keep_id, merge_id, method=method)

"""
``EventStoreMixin`` — the evidence-layer methods, once, for both local drivers.

SQLite and PostgreSQL differ here in three shallow ways: the placeholder token,
the exception a unique violation raises, and how ``user`` must be spelled. None
of that is worth two copies of the resolution logic, so the SQL is written once
with ``?`` and rewritten per dialect, and each driver declares the rest.

Anything genuinely dialect-shaped — the pragma SQLite needs to enforce foreign
keys, the plpgsql body Postgres needs for a trigger — stays in the driver or in
``events.py`` beside its DDL.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.events import (
    RESOLUTION_METHODS,
    REVISABLE_RESOLUTIONS,
    AppendOnlyViolation,
    EntityMentionRecord,
    EntityRecord,
    SourceEventRecord,
    entity_from_row,
    entity_insert_row,
    event_from_row,
    event_insert_row,
    mention_from_row,
    mention_insert_row,
    normalize_surface,
)
from pdm_memory.storage.schema import mapping_to_record

logger = logging.getLogger(__name__)

__all__ = ["EventStoreMixin"]


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class EventStoreMixin:
    """
    Source events, entities and mentions on top of a signature driver.

    Subclasses declare:

    * ``_EVENT_PLACEHOLDER`` — ``"?"`` or ``"%s"``
    * ``_EVENT_USER_COLUMN`` — ``"user"`` or ``'"user"'`` (reserved in Postgres)
    * ``_EVENT_INTEGRITY_ERRORS`` — exception classes a unique violation raises

    and inherit ``_conn()``, ``_commit_if_idle()`` and ``transaction()`` from
    the driver they are mixed into.
    """

    _EVENT_PLACEHOLDER: str = "?"
    _EVENT_USER_COLUMN: str = "user"
    _EVENT_INTEGRITY_ERRORS: tuple[type[Exception], ...] = ()

    # ------------------------------------------------------------------
    # Dialect plumbing
    # ------------------------------------------------------------------

    def _sql(self, query: str) -> str:
        """Rewrite the canonical ``?``/``user`` SQL for this driver's dialect."""
        if self._EVENT_USER_COLUMN != "user":
            query = query.replace("{user}", self._EVENT_USER_COLUMN)
        else:
            query = query.replace("{user}", "user")
        if self._EVENT_PLACEHOLDER != "?":
            query = query.replace("?", self._EVENT_PLACEHOLDER)
        return query

    def _run(self, query: str, params: tuple[Any, ...] = ()) -> Any:
        return self._conn().execute(self._sql(query), params)

    def supports_events(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Source events
    # ------------------------------------------------------------------

    def save_source_event(self, event: SourceEventRecord, *, payload: str = "") -> str:
        """
        Record an event, or return the one already recording it.

        Idempotent on ``(user, content_hash)`` — this is AC1. The second ingest
        of the same message attaches its signatures to the first event instead
        of writing the raw content again.
        """
        event.ensure_content_hash(payload=payload)

        existing = self.find_event_by_hash(event.content_hash, user=event.user)
        if existing is not None:
            logger.debug("[PDM-Events] Reusing event %s for hash", existing.id)
            return existing.id

        try:
            self._run(
                """
                INSERT INTO pdm_source_events (
                    id, {user}, event_type, occurred_at, observed_at, ingested_at,
                    source_system, provenance, raw_reference, content_hash,
                    capture_authority_state, compliance_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                event_insert_row(event),
            )
        except self._EVENT_INTEGRITY_ERRORS:
            # Another writer won the race on the unique hash index. Their row
            # is as good as ours — the hash says so.
            duplicate = self.find_event_by_hash(event.content_hash, user=event.user)
            if duplicate is None:
                raise
            return duplicate.id

        self._commit_if_idle(self._conn())
        return event.id

    def get_source_event(self, event_id: str) -> SourceEventRecord | None:
        row = self._run(
            "SELECT * FROM pdm_source_events WHERE id = ? LIMIT 1", (event_id,)
        ).fetchone()
        return event_from_row(row) if row else None

    def find_event_by_hash(
        self, content_hash: str, user: str = "default"
    ) -> SourceEventRecord | None:
        row = self._run(
            "SELECT * FROM pdm_source_events "
            "WHERE {user} = ? AND content_hash = ? LIMIT 1",
            (user, content_hash),
        ).fetchone()
        return event_from_row(row) if row else None

    def list_source_events(
        self, user: str = "default", limit: int = 100
    ) -> list[SourceEventRecord]:
        rows = self._run(
            "SELECT * FROM pdm_source_events WHERE {user} = ? "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (user, limit),
        ).fetchall()
        return [event_from_row(row) for row in rows]

    def update_source_event(self, event_id: str, **fields: Any) -> None:
        """Refused. The Python half of AC2; the trigger is the other half."""
        raise AppendOnlyViolation(
            "pdm_source_events", "update", detail="Record a new event instead."
        )

    def delete_source_event(self, event_id: str) -> None:
        """Refused. See :meth:`update_source_event`."""
        raise AppendOnlyViolation("pdm_source_events", "delete")

    # ------------------------------------------------------------------
    # Mentions — the evidence layer
    # ------------------------------------------------------------------

    def record_mention(self, mention: EntityMentionRecord) -> str:
        """
        Write down that a name was used here. Never merges anything, so it is
        always safe to call; attribution happens separately in
        :meth:`resolve_mention`.

        Idempotent on ``(user, source_event_id, signature_id, surface_form)``.
        """
        existing = self._run(
            """
            SELECT * FROM pdm_entity_mentions
            WHERE {user} = ? AND source_event_id = ? AND signature_id = ?
              AND surface_form = ?
            LIMIT 1
            """,
            (
                mention.user,
                mention.source_event_id,
                mention.signature_id,
                mention.surface_form,
            ),
        ).fetchone()
        if existing is not None:
            return existing["id"]

        self._run(
            """
            INSERT INTO pdm_entity_mentions (
                id, {user}, surface_form, surface_norm, source_event_id,
                signature_id, field_id, observed_at, entity_id, resolution,
                resolved_at, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            mention_insert_row(mention),
        )
        self._commit_if_idle(self._conn())
        return mention.id

    def get_mention(self, mention_id: str) -> EntityMentionRecord | None:
        row = self._run(
            "SELECT * FROM pdm_entity_mentions WHERE id = ? LIMIT 1", (mention_id,)
        ).fetchone()
        return mention_from_row(row) if row else None

    def resolve_mention(
        self,
        mention_id: str,
        *,
        entity_id: str,
        method: str,
        confidence: float | None = None,
    ) -> None:
        """
        Attribute a mention to an identity.

        Only the attribution columns move; the evidence — who said the name,
        where, and when — is frozen by the trigger. An automatic pass will not
        overwrite what a person confirmed, which is the point of keeping the
        methods in a graded vocabulary.
        """
        if method not in RESOLUTION_METHODS:
            raise ValueError(
                f"method must be one of {sorted(RESOLUTION_METHODS)}, got {method!r}"
            )

        current = self.get_mention(mention_id)
        if current is None:
            raise KeyError(f"mention {mention_id!r} not found")
        if (
            method in REVISABLE_RESOLUTIONS
            and current.resolution not in REVISABLE_RESOLUTIONS
        ):
            logger.debug(
                "[PDM-Events] Keeping %s resolution on mention %s over %s",
                current.resolution,
                mention_id,
                method,
            )
            return

        self._run(
            """
            UPDATE pdm_entity_mentions
               SET entity_id = ?, resolution = ?, resolved_at = ?, confidence = ?
             WHERE id = ?
            """,
            (entity_id, method, _now().isoformat(), confidence, mention_id),
        )
        self._commit_if_idle(self._conn())

    def mentions_for_entity(
        self, entity_id: str, user: str = "default"
    ) -> list[EntityMentionRecord]:
        rows = self._run(
            "SELECT * FROM pdm_entity_mentions WHERE {user} = ? AND entity_id = ? "
            "ORDER BY observed_at",
            (user, entity_id),
        ).fetchall()
        return [mention_from_row(row) for row in rows]

    def unresolved_mentions(
        self, user: str = "default", limit: int = 100
    ) -> list[EntityMentionRecord]:
        """
        Mentions still waiting on an identity — the queue behind "which Alex?".

        The UI loop that asks the question feeds resolution back as
        ``user_confirmed``, so answering once is worth more than answering
        often.
        """
        rows = self._run(
            "SELECT * FROM pdm_entity_mentions WHERE {user} = ? AND entity_id IS NULL "
            "ORDER BY observed_at LIMIT ?",
            (user, limit),
        ).fetchall()
        return [mention_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Entities — the claim layer
    # ------------------------------------------------------------------

    def resolve_or_create_entity(
        self,
        *,
        user: str,
        surface_form: str,
        field_id: str = "",
        entity_type: str = "person",
    ) -> str:
        """
        The default rule: same name in the same field is the same person.

        Two mentions of "Alex" inside Work are one colleague — nearly always
        true. "Alex" in Work and "Alex" in Family are two candidate identities
        until something links them. That inversion is deliberate: merging two
        people is silent and, once their signatures point at one row,
        unrecoverable; splitting one person in two is visible and asks a
        question the user can answer. Of the two ways to be wrong, take the one
        that shows itself.
        """
        norm = normalize_surface(surface_form)

        # An identity this field already established, following a merge if one
        # happened since.
        row = self._run(
            "SELECT * FROM pdm_entities "
            "WHERE {user} = ? AND canonical_norm = ? AND origin_field_id = ? LIMIT 1",
            (user, norm, field_id),
        ).fetchone()
        if row is not None:
            return self._follow_merge(entity_from_row(row)).id

        # The name is new to this field. Whether it is new outright decides
        # only whether the row needs a disambiguator to sit beside its sibling.
        taken = self._run(
            "SELECT COUNT(*) AS n FROM pdm_entities "
            "WHERE {user} = ? AND canonical_norm = ?",
            (user, norm),
        ).fetchone()["n"]

        entity = EntityRecord(
            user=user,
            entity_type=entity_type,
            canonical_name=" ".join((surface_form or "").split()),
            disambiguator=self._free_disambiguator(user, norm, field_id, taken),
            origin_field_id=field_id,
        )
        self._run(
            """
            INSERT INTO pdm_entities (
                id, {user}, entity_type, canonical_name, canonical_norm,
                disambiguator, origin_field_id, aliases, current_state_version,
                created_at, dissolved_at, merged_into
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            entity_insert_row(entity),
        )
        self._commit_if_idle(self._conn())
        if taken:
            logger.info(
                "[PDM-Events] %r in field %r is a second identity, not a merge",
                entity.canonical_name,
                field_id,
            )
        return entity.id

    def _free_disambiguator(
        self, user: str, norm: str, field_id: str, taken: int
    ) -> str:
        """
        Pick a disambiguator no sibling is already using.

        The first identity for a name takes the empty one, so the common case
        stays unmarked, as D6 intends. Later ones take their field. The
        fallbacks matter for an ordering the obvious version gets wrong: an
        entity created with no field, after one created with a field, would
        claim the empty disambiguator a sibling already holds and fail the
        unique index.
        """
        candidates: list[str] = []
        if taken == 0:
            candidates.append("")
        stem = field_id or "alt"
        candidates.append(stem)
        candidates.extend(f"{stem}-{n}" for n in range(2, 12))

        for candidate in candidates:
            clash = self._run(
                "SELECT 1 AS hit FROM pdm_entities "
                "WHERE {user} = ? AND canonical_norm = ? AND disambiguator = ? LIMIT 1",
                (user, norm, candidate),
            ).fetchone()
            if clash is None:
                return candidate
        return uuid.uuid4().hex[:8]

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        row = self._run(
            "SELECT * FROM pdm_entities WHERE id = ? LIMIT 1", (entity_id,)
        ).fetchone()
        return entity_from_row(row) if row else None

    def list_entities(
        self, user: str = "default", include_dissolved: bool = False
    ) -> list[EntityRecord]:
        query = "SELECT * FROM pdm_entities WHERE {user} = ?"
        if not include_dissolved:
            query += " AND dissolved_at IS NULL"
        rows = self._run(query + " ORDER BY canonical_norm, disambiguator", (user,)).fetchall()
        return [entity_from_row(row) for row in rows]

    def _follow_merge(self, entity: EntityRecord) -> EntityRecord:
        """Walk ``merged_into`` to the surviving row; tolerate a broken chain."""
        seen: set[str] = set()
        while entity.merged_into and entity.merged_into not in seen:
            seen.add(entity.id)
            survivor = self.get_entity(entity.merged_into)
            if survivor is None:
                break
            entity = survivor
        return entity

    def merge_entities(self, keep_id: str, merge_id: str, *, method: str) -> None:
        """
        Two rows turned out to be one person.

        The merged row is closed, not deleted: its mentions are repointed and
        it keeps a pointer to the survivor, so the merge can be read back and
        undone. Splitting later means repointing mentions again — never
        rewriting history.
        """
        if keep_id == merge_id:
            return
        if method not in RESOLUTION_METHODS:
            raise ValueError(
                f"method must be one of {sorted(RESOLUTION_METHODS)}, got {method!r}"
            )
        if self.get_entity(keep_id) is None:
            raise KeyError(f"entity {keep_id!r} not found")
        if self.get_entity(merge_id) is None:
            raise KeyError(f"entity {merge_id!r} not found")

        with self.transaction():
            self._run(
                "UPDATE pdm_entity_mentions SET entity_id = ?, resolution = ? "
                "WHERE entity_id = ?",
                (keep_id, method, merge_id),
            )
            self._run(
                "UPDATE pdm_signatures SET primary_entity_id = ? "
                "WHERE primary_entity_id = ?",
                (keep_id, merge_id),
            )
            self._run(
                """
                UPDATE pdm_entities
                   SET dissolved_at = ?, merged_into = ?,
                       current_state_version = current_state_version + 1
                 WHERE id = ?
                """,
                (_now().isoformat(), keep_id, merge_id),
            )

    # ------------------------------------------------------------------
    # Wiring signatures to their evidence
    # ------------------------------------------------------------------

    def link_signature(
        self,
        signature_id: str,
        *,
        source_event_id: str | None = None,
        primary_entity_id: str | None = None,
        user: str = "default",
    ) -> None:
        """
        Point a signature at the event it came from and the entity it is about.

        Deliberately raw SQL rather than ``update()``: the inherited path
        filters through ``UPDATABLE_COLUMNS``, a whitelist in a frozen module
        that predates these two columns and would reject them.
        """
        assignments: list[str] = []
        values: list[Any] = []
        if source_event_id is not None:
            assignments.append("source_event_id = ?")
            values.append(source_event_id)
        if primary_entity_id is not None:
            assignments.append("primary_entity_id = ?")
            values.append(primary_entity_id)
        if not assignments:
            return

        self._run(
            f"UPDATE pdm_signatures SET {', '.join(assignments)} "
            f"WHERE id = ? AND {{user}} = ?",
            (*values, signature_id, user),
        )
        self._commit_if_idle(self._conn())

    def signatures_for_event(
        self, event_id: str, user: str = "default"
    ) -> list[SignatureRecord]:
        rows = self._run(
            "SELECT * FROM pdm_signatures WHERE {user} = ? AND source_event_id = ? "
            "AND is_deleted = 0 ORDER BY p_magnitude DESC, id DESC",
            (user, event_id),
        ).fetchall()
        return [mapping_to_record(row) for row in rows]

    def signatures_for_entity(
        self, entity_id: str, user: str = "default"
    ) -> list[SignatureRecord]:
        rows = self._run(
            "SELECT * FROM pdm_signatures WHERE {user} = ? AND primary_entity_id = ? "
            "AND is_deleted = 0 ORDER BY p_magnitude DESC, id DESC",
            (user, entity_id),
        ).fetchall()
        return [mapping_to_record(row) for row in rows]

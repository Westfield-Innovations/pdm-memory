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
from pdm_memory.storage.event_hash import normalize_instant
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

    Every write that can collide goes through ``ON CONFLICT DO NOTHING``
    followed by a read of the winner, so no dialect needs to name the exception
    a unique violation raises, and a losing writer never parks a failed
    transaction on its connection.

    and inherit ``_conn()``, ``_commit_if_idle()`` and ``transaction()`` from
    the driver they are mixed into.
    """

    _EVENT_PLACEHOLDER: str = "?"
    _EVENT_USER_COLUMN: str = "user"

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

        self._run(
            """
            INSERT INTO pdm_source_events (
                id, {user}, event_type, occurred_at, observed_at, ingested_at,
                source_system, provenance, raw_reference, content_hash,
                capture_authority_state, compliance_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            event_insert_row(event),
        )
        self._commit_if_idle(self._conn())

        # Whoever holds the hash owns the event — us, or the writer who got
        # there first. Letting the unique index arbitrate in a single statement
        # is what removes the race: the read-then-write version could lose
        # between its two statements, and its failed INSERT left the write lock
        # parked on a connection that had already given up.
        stored = self.find_event_by_hash(event.content_hash, user=event.user)
        if stored is None:
            raise RuntimeError(
                f"source event {event.content_hash[:12]} vanished immediately "
                "after insert"
            )
        if stored.id != event.id:
            logger.debug("[PDM-Events] Reusing event %s for hash", stored.id)
        return stored.id

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
        self._run(
            """
            INSERT INTO pdm_entity_mentions (
                id, {user}, surface_form, surface_norm, source_event_id,
                signature_id, field_id, observed_at, entity_id, resolution,
                resolved_at, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            mention_insert_row(mention),
        )
        self._commit_if_idle(self._conn())

        stored = self._run(
            """
            SELECT id FROM pdm_entity_mentions
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
        if stored is None:
            raise RuntimeError("mention vanished immediately after insert")
        return stored["id"]

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

        # One query for what the disambiguator choice needs: which spellings a
        # sibling already holds. The previous version asked COUNT(*) and then
        # probed up to twelve candidates one statement at a time.
        siblings = {
            r["disambiguator"]
            for r in self._run(
                "SELECT disambiguator FROM pdm_entities "
                "WHERE {user} = ? AND canonical_norm = ?",
                (user, norm),
            ).fetchall()
        }

        entity = EntityRecord(
            user=user,
            entity_type=entity_type,
            canonical_name=" ".join((surface_form or "").split()),
            disambiguator=self._free_disambiguator(field_id, siblings),
            origin_field_id=field_id,
        )
        self._run(
            """
            INSERT INTO pdm_entities (
                id, {user}, entity_type, canonical_name, canonical_norm,
                disambiguator, origin_field_id, aliases, current_state_version,
                created_at, dissolved_at, merged_into
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            entity_insert_row(entity),
        )
        self._commit_if_idle(self._conn())

        # Read back rather than trust our own id. Two threads naming the same
        # person in the same field both compute the same identity triple, so
        # the unique index picks one and the other must adopt it — the same
        # arbitration used for events and mentions, and for the same reason:
        # a losing INSERT that raises leaves the write lock parked behind a
        # thread that has already given up.
        winner = self._run(
            "SELECT * FROM pdm_entities "
            "WHERE {user} = ? AND canonical_norm = ? AND origin_field_id = ? LIMIT 1",
            (user, norm, field_id),
        ).fetchone()
        if winner is None:
            raise RuntimeError(
                f"entity {entity.canonical_name!r} vanished immediately after insert"
            )
        if siblings:
            logger.info(
                "[PDM-Events] %r in field %r is a second identity, not a merge",
                entity.canonical_name,
                field_id,
            )
        return self._follow_merge(entity_from_row(winner)).id

    def _free_disambiguator(self, field_id: str, siblings: set[str]) -> str:
        """
        Pick a disambiguator no sibling of this name already holds.

        The first identity for a name takes the empty one, so the common case
        stays unmarked. Later ones take their field. The numbered fallbacks
        matter for an ordering the obvious version gets wrong: an entity
        created with no field, after one created with a field, would claim the
        empty disambiguator a sibling already holds.

        Takes the sibling set rather than querying, so choosing costs no round
        trips — and so the unique index, not this function, is what finally
        decides under contention.
        """
        candidates: list[str] = []
        if not siblings:
            candidates.append("")
        stem = field_id or "alt"
        candidates.append(stem)
        candidates.extend(f"{stem}-{n}" for n in range(2, 12))

        for candidate in candidates:
            if candidate not in siblings:
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

        keep = self.get_entity(keep_id)
        if keep is None:
            raise KeyError(f"entity {keep_id!r} not found")
        merged = self.get_entity(merge_id)
        if merged is None:
            raise KeyError(f"entity {merge_id!r} not found")

        # Merging into a row that has itself been merged away leaves every
        # mention pointing at a retired identity, and a mutual merge retires
        # both — after which the name has no live identity at all and the
        # pointers run in a circle. The survivor has to still be alive.
        if keep.dissolved_at is not None:
            raise ValueError(
                f"entity {keep_id!r} was dissolved into "
                f"{keep.merged_into or 'nothing'} and cannot receive a merge; "
                "merge into the surviving identity instead"
            )
        if merged.dissolved_at is not None:
            raise ValueError(f"entity {merge_id!r} is already merged away")

        with self.transaction():
            # Only the pointer moves. The resolution grade records *who*
            # decided a mention belonged to someone — a person, an alias table,
            # a model — and a merge is not a re-decision of that. Overwriting
            # it downgraded confirmed answers to whatever the merge was called
            # with, and resolve_mention then treated them as revisable again.
            self._run(
                "UPDATE pdm_entity_mentions SET entity_id = ? WHERE entity_id = ?",
                (keep_id, merge_id),
            )
            self._run(
                "UPDATE pdm_signatures SET primary_entity_id = ? "
                "WHERE primary_entity_id = ? AND {user} = ?",
                (keep_id, merge_id, merged.user),
            )
            self._run(
                """
                UPDATE pdm_entities
                   SET dissolved_at = ?, merged_into = ?,
                       current_state_version = current_state_version + 1
                 WHERE id = ?
                """,
                (normalize_instant(_now()), keep_id, merge_id),
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
        overwrite: bool = False,
    ) -> bool:
        """
        Point a signature at the event it came from and the entity it is about.

        First writer wins. ``Memory.save`` deduplicates on text, so a fact
        repeated across two messages hands back the signature the first one
        produced — and an unconditional UPDATE then moved its provenance to the
        second event, leaving the first with nothing it could show for itself.
        Rewriting where a fact came from is precisely what an evidence layer
        must not do, so a signature that already carries provenance keeps it.

        Returns whether this call claimed the provenance. ``False`` means the
        signature was already accounted for by an earlier event — worth
        surfacing rather than swallowing, because the caller usually wants to
        know its fact was not new.

        ``overwrite=True`` is for repair paths that have established the
        existing link is wrong; ordinary ingest never passes it.

        Deliberately raw SQL rather than ``update()``: the inherited path
        filters through ``UPDATABLE_COLUMNS``, a whitelist in a frozen module
        that predates these two columns and would reject them.
        """
        claimed = True

        if source_event_id is not None:
            guard = "" if overwrite else " AND source_event_id IS NULL"
            cursor = self._run(
                "UPDATE pdm_signatures SET source_event_id = ? "
                f"WHERE id = ? AND {{user}} = ?{guard}",
                (source_event_id, signature_id, user),
            )
            claimed = bool(getattr(cursor, "rowcount", 1))

        if primary_entity_id is not None:
            guard = "" if overwrite else " AND primary_entity_id IS NULL"
            self._run(
                "UPDATE pdm_signatures SET primary_entity_id = ? "
                f"WHERE id = ? AND {{user}} = ?{guard}",
                (primary_entity_id, signature_id, user),
            )

        self._commit_if_idle(self._conn())
        return claimed

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

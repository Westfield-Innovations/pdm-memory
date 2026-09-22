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
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pdm_memory.core.signature import SignatureRecord
from pdm_memory.storage.event_hash import compute_content_hash
from pdm_memory.storage.events import (
    EntityMentionRecord,
    EntityRecord,
    IntegrityReport,
    SourceEventRecord,
    storage_supports_events,
)
from pdm_memory.storage.fields import RelationshipRecord

logger = logging.getLogger(__name__)

__all__ = ["EventLog", "PayloadMismatch"]


class PayloadMismatch(ValueError):
    """
    Raised by ``EventLog.extract_signatures`` when ``text`` is not the event.

    The store never held the payload (see ``SourceEventRecord.
    ensure_content_hash``'s own docstring), so proof that *text* is really
    this event's content is a hash check, not a lookup. Building a signature
    from text that fails it would attribute a fact to an event it was never
    part of — the same integrity question ``ContentHashMismatch`` answers on
    Companion's ingest side.
    """


class EventLog:
    """Source events, entities and mentions for a ``Memory`` instance."""

    def __init__(self, memory: Any) -> None:
        """
        Args:
            memory: A ``Memory`` whose storage carries events, fields, or both.

        Raises:
            RuntimeError: The driver behind *memory* carries neither — said
                plainly here rather than as an ``AttributeError`` three frames
                deeper.

        A driver may carry one half without the other. ``CloudDriver`` files
        and links over Companion's field/relationship routes and can write
        events there — ``record``, ``ingest`` and ``extract_signatures`` reach
        Companion's own event routes — but keeps no event table to read back
        from, so the read side (``get``, ``find_by_hash``, mentions, entities)
        stays local-only. Refusing construction here whenever either half is
        missing would refuse the half that is present; each event-only and
        field-only method below asks for its own half instead, through
        ``_require_events()`` / ``_require_fields()``.
        """
        from pdm_memory.storage.cloud_driver import CloudDriver

        storage = getattr(memory, "_storage", None)
        self._events_supported = storage_supports_events(storage)
        self._cloud_events = isinstance(storage, CloudDriver)
        self._fields_supported = bool(
            hasattr(storage, "supports_fields") and storage.supports_fields()
        )
        if not self._events_supported and not self._fields_supported:
            driver = type(storage).__name__ if storage else "None"
            raise RuntimeError(
                f"{driver} carries neither source events nor field "
                "memberships. Use Memory(storage=EventfulSQLiteDriver(...)) "
                "for the local evidence layer, or Memory(storage=CloudDriver"
                "(...)) for fields and links over Companion."
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
        if not self._cloud_events:
            self._require_events()
        return self._storage.save_source_event(event, payload=payload)

    def get(self, event_id: str) -> SourceEventRecord | None:
        self._require_events()
        return self._storage.get_source_event(event_id)

    def find_by_hash(self, content_hash: str) -> SourceEventRecord | None:
        self._require_events()
        return self._storage.find_event_by_hash(content_hash, user=self._user)

    def events(self, limit: int = 100) -> list[SourceEventRecord]:
        self._require_events()
        return self._storage.list_source_events(user=self._user, limit=limit)

    def signatures_for(self, event_id: str) -> list[SignatureRecord]:
        self._require_events()
        return self._storage.signatures_for_event(event_id, user=self._user)

    def extract_signatures(
        self,
        event_id: str,
        text: str,
        *,
        llm_client: Any | None = None,
        force: bool = False,
    ) -> list[SignatureRecord]:
        """
        Turn one event's own text into a signature, checked and linked.

        ``text`` is required. Unlike Companion's chat path, which can read a
        ``chat_message`` event's own ``Message.content`` back, this store
        never held the payload in the first place (see
        ``SourceEventRecord.ensure_content_hash``) — there is nothing local
        to resolve it from. The caller is whoever still holds the text: the
        process that ingested it, a webhook replay, a backfill from an
        export.

        Checked against the event's own ``content_hash`` before anything is
        built from it — text that does not hash to this event is not proven
        to be what it claims, and a signature built from it would attribute
        a fact to an event it was never part of. The check tries both with
        and without ``occurred_at``, because whether the original recording
        knew the timestamp or let it default is a fact this row does not
        keep — only the resulting hash does — so it accepts whichever produced
        the hash actually stored.

        ``llm_client`` mirrors ``Memory.ingest``'s own parameter: given, it
        wraps ``pdm_memory.ingest.auto_signature.AutoSignatureGenerator``
        to compress *text* into a fact; omitted, *text* itself becomes the
        signature verbatim — the same "raw text ingestion" fallback
        ``Memory.ingest`` uses when it has no LLM client either.

        Idempotent by default: an event that already has signatures returns
        them rather than extracting a second one. ``force=True`` extracts
        regardless, for a caller correcting a bad first pass.

        Over a ``CloudDriver`` the server does the extraction with its own
        model, so ``llm_client`` is refused rather than silently unused, and
        its refusals arrive as the local ones do: ``PayloadMismatch`` for text
        that is not the event, ``LookupError`` for an event that is not yours.
        """
        if self._cloud_events:
            return self._extract_in_cloud(
                event_id, text, llm_client=llm_client, force=force
            )
        self._require_events()
        event = self._storage.get_source_event(event_id)
        if event is None:
            raise LookupError(f"no source event {event_id!r} for user {self._user!r}")

        candidates = {
            compute_content_hash(
                event_type=event.event_type,
                occurred_at=maybe_known,
                source_system=event.source_system,
                raw_reference=event.raw_reference,
                payload=text,
            )
            for maybe_known in (event.occurred_at, None)
        }
        if event.content_hash not in candidates:
            raise PayloadMismatch(
                f"the text offered for source event {event_id} does not hash "
                f"to the event's own content_hash ({event.content_hash}). It "
                f"is not this event's content."
            )

        if not force:
            existing = self._storage.signatures_for_event(event_id, user=self._user)
            if existing:
                return existing

        if llm_client is not None:
            from pdm_memory.ingest.auto_signature import AutoSignatureGenerator

            result = AutoSignatureGenerator(llm_client).generate(text)
            if result is None:
                return []
            compressed_fact = result.compressed_fact
            tags = result.intent_tags
            p_magnitude = result.p_magnitude
        else:
            compressed_fact = text.strip()[:500]
            tags = []
            p_magnitude = 50.0

        if not compressed_fact:
            return []

        memory_id = self._memory.save(
            compressed_fact, tags=tags, p_magnitude=p_magnitude
        )
        self._storage.link_signature(
            memory_id, source_event_id=event_id, user=self._user
        )

        return self._storage.signatures_for_event(event_id, user=self._user)

    def _extract_in_cloud(
        self,
        event_id: str,
        text: str,
        *,
        llm_client: Any | None,
        force: bool,
    ) -> list[SignatureRecord]:
        from pdm_memory.storage.errors import CloudNotFoundError, CloudStorageError

        if llm_client is not None:
            raise ValueError(
                "extract_signatures over a CloudDriver is done by the server's "
                "own model; llm_client would go unused."
            )
        try:
            out = self._storage.extract_signatures(
                event_id, text, force=force, user=self._user
            )
        except CloudNotFoundError as exc:
            raise LookupError(
                f"no source event {event_id!r} for user {self._user!r}"
            ) from exc
        except CloudStorageError as exc:
            if getattr(exc, "status_code", None) == 422 and "PAYLOAD_MISMATCH" in str(
                exc
            ):
                raise PayloadMismatch(str(exc)) from exc
            raise
        records = (
            self._storage.get(sid, user=self._user)
            for sid in out.get("signature_ids") or []
        )
        return [record for record in records if record is not None]

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

        Returns ``{"source_event_id", "signature_ids", "signatures_reused",
        "entity_ids", "deduplicated"}``. ``signatures_reused`` counts facts
        that were already on file under an earlier event — their provenance
        stays with the message that first carried them.
        """
        if self._cloud_events:
            return self._ingest_in_cloud(
                event=event, facts=facts, payload=payload, field_id=field_id
            )
        self._require_events()
        # No probe before the write. save_source_event is idempotent on the
        # hash and reports on the record whether the store already held the
        # event, so asking first is a round trip spent learning what the write
        # is about to say.
        event_id = self.record(event, payload=payload)
        seen_before = event.was_deduplicated

        signature_ids: list[str] = []
        entity_ids: dict[str, str] = {}
        reused = 0

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

            # Memory.save deduplicates on text, so this id may belong to a
            # signature an earlier event already produced. link_signature says
            # which happened; the count goes back to the caller rather than
            # being swallowed, because "your fact was already on file, under a
            # different message" is exactly what they need to know.
            claimed = self._storage.link_signature(
                memory_id,
                source_event_id=event_id,
                primary_entity_id=entity_id,
                user=self._user,
            )
            if not claimed:
                reused += 1

            # File the fact where it was said. The caller already named the
            # field for the mention; leaving the signature unfiled would mean
            # scoped recall fell back to the subject every time, which is the
            # weaker rule and not the one Companion uses.
            if field_id:
                self._storage.file_signature_in_field(
                    memory_id, field_id, user=self._user
                )

        return {
            "source_event_id": event_id,
            "signature_ids": signature_ids,
            "signatures_reused": reused,
            "entity_ids": entity_ids,
            "deduplicated": seen_before,
        }

    def _ingest_in_cloud(
        self,
        *,
        event: SourceEventRecord,
        facts: Sequence[dict[str, Any]],
        payload: str,
        field_id: str,
    ) -> dict[str, Any]:
        """
        ``ingest`` over a CloudDriver: the same steps, with the server doing
        the linking. A name in ``about`` travels as a mention payload on the
        link call, since the cloud keeps no local entity table to resolve it
        against.
        """
        event_id = self.record(event, payload=payload)
        seen_before = event.was_deduplicated

        signature_ids: list[str] = []
        entity_ids: dict[str, str] = {}
        reused = 0

        for fact in facts:
            spec = dict(fact)
            text = spec.pop("text")
            about = spec.pop("about", None)

            memory_id = self._memory.save(text, **spec)
            signature_ids.append(memory_id)

            entities = []
            if about:
                mention: dict[str, Any] = {"surface_form": about}
                if field_id:
                    mention["field_id"] = field_id
                entities.append(mention)

            out = self._storage.attach_signature(event_id, memory_id, entities=entities)
            if not out.get("linked"):
                reused += 1
            if about:
                resolved = next(
                    (
                        m["entity_id"]
                        for m in out.get("mentions") or []
                        if m.get("entity_id")
                    ),
                    None,
                )
                if resolved:
                    entity_ids[about] = resolved

            if field_id:
                self._storage.file_signature_in_field(
                    memory_id, field_id, user=self._user
                )

        return {
            "source_event_id": event_id,
            "signature_ids": signature_ids,
            "signatures_reused": reused,
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
        self._require_events()
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
        self._require_events()
        self._storage.resolve_mention(
            mention_id, entity_id=entity_id, method="user_confirmed", confidence=1.0
        )

    def pending(self, limit: int = 100) -> list[EntityMentionRecord]:
        """Mentions with no identity yet — the queue behind the prompt."""
        self._require_events()
        return self._storage.unresolved_mentions(user=self._user, limit=limit)

    def check_integrity(self) -> IntegrityReport:
        """
        Report references that lead nowhere. Reads only; repairs nothing.

        Worth running after anything that wrote to the store outside the SDK —
        a restored backup, a manual fix, a migration from another tool.
        """
        self._require_events()
        return self._storage.check_integrity(user=self._user)

    def entities(self, include_dissolved: bool = False) -> list[EntityRecord]:
        self._require_events()
        return self._storage.list_entities(
            user=self._user, include_dissolved=include_dissolved
        )

    def entity(self, entity_id: str) -> EntityRecord | None:
        self._require_events()
        return self._storage.get_entity(entity_id)

    def about(self, entity_id: str) -> list[SignatureRecord]:
        """Everything the store holds about one identity."""
        self._require_events()
        return self._storage.signatures_for_entity(entity_id, user=self._user)

    def merge(
        self, keep_id: str, merge_id: str, *, method: str = "user_confirmed"
    ) -> None:
        """Two identities turned out to be one person. Reversible; see D6."""
        self._require_events()
        self._storage.merge_entities(keep_id, merge_id, method=method)

    # ------------------------------------------------------------------
    # Fields — who belongs where, and when (TKT-102)
    # ------------------------------------------------------------------

    def add_field_membership(
        self,
        entity_id: str,
        field_id: str,
        valid_from: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        *,
        role: str = "",
        derived_by: str = "sdk",
    ) -> str:
        """
        Put an entity in a field for a window of time.

        Adding a second membership does not end the first: an entity is in Work
        and in Project Orion at once, and that is the point. Boundaries are
        validated before anything is written — an end at or before the start is
        refused rather than stored.
        """
        self._require_fields()
        return self._storage.add_field_membership(
            entity_id,
            field_id,
            valid_from,
            valid_to,
            role=role,
            derived_by=derived_by,
            user=self._user,
        )

    def end_membership(
        self, membership_id: str, at: datetime | str | None = None
    ) -> None:
        """Close a membership. The row keeps its history rather than vanishing."""
        self._require_fields()
        self._storage.end_field_membership(membership_id, at, user=self._user)

    def link(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str,
        directionality: str = "directed",
        valid_from: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        *,
        derived_by: str = "sdk",
    ) -> str:
        """
        Record that two entities stood in some relation for a window of time.

        ``directed`` is followed one way — "Alex manages Orion" read backwards
        is a different claim. ``symmetric`` is followed both ways from one row.
        """
        self._require_fields()
        return self._storage.link(
            source_entity_id,
            target_entity_id,
            relationship_type,
            directionality,
            valid_from,
            valid_to,
            derived_by=derived_by,
            user=self._user,
        )

    def end_link(self, relationship_id: str, at: datetime | str | None = None) -> None:
        """Close a relationship."""
        self._require_fields()
        self._storage.end_relationship(relationship_id, at, user=self._user)

    def reinforce(
        self,
        target: RelationshipRecord | SignatureRecord | str,
        evidence: SignatureRecord | SourceEventRecord | str | None = None,
        *,
        coupling_score: float = 0.5,
    ) -> Any:
        """
        Reinforce *target* — a relationship's channel (spec §4.4) when
        *target* is a :class:`RelationshipRecord`, or a memory's pressure
        (:meth:`Memory.reinforce`, unchanged) for anything else, including a
        bare string id — the same convention
        :meth:`Memory.apply_contrary_evidence` already uses for its own
        ``target`` parameter, so an existing call site's string id keeps
        meaning what it always meant.

        ``evidence`` is required for a relationship target — the route it
        dispatches to (spec §4.4) refuses a bare kind with nothing behind
        it — and must be an existing :class:`SignatureRecord` (or its id) or
        :class:`SourceEventRecord` (or its id). It has no meaning for a
        memory target: :meth:`Memory.reinforce` reinforces from validation
        counters, not from a cited fact, and does not accept one; passed
        here for a memory target, it is simply not forwarded.

        To target a relationship, pass the :class:`RelationshipRecord`
        itself (e.g. from ``log.link(...)``'s return id wrapped by a lookup,
        or one already held) — a bare string id is always read as a
        signature id, never guessed at as a relationship's.
        """
        if isinstance(target, RelationshipRecord):
            return self._apply_relationship_evidence(
                target.id, kind="reinforce", evidence=evidence
            )
        memory_id = target if isinstance(target, str) else target.id
        return self._memory.reinforce(memory_id, coupling_score=coupling_score)

    def apply_contrary_evidence(
        self,
        target: RelationshipRecord | SignatureRecord | str,
        evidence: SignatureRecord | SourceEventRecord | str | Mapping[str, Any],
        *,
        coupling_score: float = 0.5,
        persist_evidence: bool = True,
        evidence_tags: list[str] | None = None,
        evidence_shape: str | None = None,
    ) -> Any:
        """
        Apply contrary evidence to *target* — a relationship's channel
        (spec §4.4) when *target* is a :class:`RelationshipRecord`, or a
        memory's pressure (:meth:`Memory.apply_contrary_evidence`,
        unchanged) for anything else, including a bare string id.

        For a relationship target, ``evidence`` must cite an existing
        :class:`SignatureRecord` (or its id) or :class:`SourceEventRecord`
        (or its id) — the route it dispatches to (spec §4.4) refuses a bare
        fact with nothing behind it, unlike the memory path below, which by
        default *persists* ``evidence`` as a brand-new signature
        (``persist_evidence=True``) rather than citing one that already
        exists. ``coupling_score``/``persist_evidence``/``evidence_tags``/
        ``evidence_shape`` are forwarded to :meth:`Memory.apply_contrary_evidence`
        unchanged and have no effect on the relationship path.
        """
        if isinstance(target, RelationshipRecord):
            return self._apply_relationship_evidence(
                target.id, kind="contrary", evidence=evidence
            )
        return self._memory.apply_contrary_evidence(
            target,
            evidence,
            coupling_score=coupling_score,
            persist_evidence=persist_evidence,
            evidence_tags=evidence_tags,
            evidence_shape=evidence_shape,
        )

    def _apply_relationship_evidence(
        self,
        relationship_id: str,
        *,
        kind: str,
        evidence: SignatureRecord | SourceEventRecord | str | Mapping[str, Any] | None,
    ) -> Any:
        """
        POST .../relationships/<id>/evidence (spec §4.4) — cloud only.

        ``RelationshipChannel`` and its evidence log live only in Companion;
        a local driver has neither table, so this refuses cleanly rather
        than pretending to apply evidence that would go nowhere.
        """
        from pdm_memory.storage.cloud_driver import CloudDriver

        storage = self._storage
        if not isinstance(storage, CloudDriver):
            raise RuntimeError(
                f"{type(storage).__name__} does not support relationship "
                "evidence — RelationshipChannel and its evidence log live "
                "only in Companion. Use Memory(storage=CloudDriver(...))."
            )

        signature_id: str | None = None
        source_event_id: str | None = None
        if isinstance(evidence, SignatureRecord):
            signature_id = evidence.id
        elif isinstance(evidence, SourceEventRecord):
            source_event_id = evidence.id
        elif isinstance(evidence, str) and evidence:
            signature_id = evidence
        else:
            raise ValueError(
                "Relationship evidence must cite an existing SignatureRecord "
                "(or its id) or SourceEventRecord (or its id) — a bare kind "
                f"with nothing behind it is not evidence. Got {evidence!r}."
            )

        return storage.apply_relationship_evidence(
            relationship_id,
            kind=kind,
            signature_id=signature_id,
            source_event_id=source_event_id,
            user=self._user,
        )

    def file_fact(
        self,
        signature_id: str,
        field_id: str,
        valid_from: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        *,
        weight: float = 1.0,
        confidence: float = 1.0,
        derived_by: str = "sdk",
    ) -> str:
        """
        File a fact in a field for a window of time.

        ``ingest`` does this for what it writes; this is for facts saved any
        other way, and for filing an existing fact somewhere additional.
        """
        self._require_fields()
        return self._storage.file_signature_in_field(
            signature_id,
            field_id,
            valid_from,
            valid_to,
            weight=weight,
            confidence=confidence,
            derived_by=derived_by,
            user=self._user,
        )

    def unfile_fact(self, membership_id: str, at: datetime | str | None = None) -> None:
        """Close a fact's membership in a field."""
        self._require_fields()
        self._storage.unfile_signature(membership_id, at, user=self._user)

    def fact_fields(
        self, signature_id: str, at: datetime | str | None = None
    ) -> list[str]:
        """Which fields a fact was filed in at *at*."""
        self._require_fields()
        return self._storage.signature_fields(signature_id, at, user=self._user)

    def fields_of(self, entity_id: str, at: datetime | str | None = None) -> list[str]:
        """Which fields an entity was in at *at*. Several is normal."""
        self._require_fields()
        return self._storage.fields_of(entity_id, at, user=self._user)

    def members_of(self, field_id: str, at: datetime | str | None = None) -> list[str]:
        """Which entities were in a field at *at*."""
        self._require_fields()
        return self._storage.members_of(field_id, at, user=self._user)

    def related(self, entity_id: str, at: datetime | str | None = None) -> set[str]:
        """Entities a live link reaches from this one at *at*. One hop."""
        self._require_fields()
        return self._storage.related_entities(entity_id, at, user=self._user)

    def trajectory(
        self,
        subject_id: str,
        start: datetime,
        end: datetime,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> Any:
        """
        Alias for :meth:`Memory.trajectory` — see there for what it returns
        and why, unlike the rest of this section, it works against a local
        driver as well as a cloud one.
        """
        self._require_fields()
        return self._memory.trajectory(
            subject_id, start, end, cursor=cursor, limit=limit
        )

    def recall(
        self,
        query: str,
        k: int = 5,
        *,
        field: str,
        at: datetime | str | None = None,
        follow_links: bool = True,
        min_pressure: float = 0.0,
        candidate_limit: int = 2000,
    ) -> list[Any]:
        """
        Ask a question inside one field, as of an instant.

        Facts about entities outside the field do not come back unless a live
        link reaches them — the isolation the field table exists for. Facts
        about no entity in particular do: they are not in another field, they
        are in none, and dropping them would empty most of a store the first
        time anyone scoped a query.

        ``at`` defaults to now. A question about April deserves April's answer,
        and memberships move.

        Scoped recall goes through this rather than ``Memory.recall`` because
        that method is frozen and cannot pass a field down to the engine.
        """
        self._require_fields()
        from pdm_memory.core.field_scoped_retrieval import FieldScopedRetrievalEngine

        loader = getattr(self._memory, "_load_recall_candidates", None)
        if callable(loader):
            records = loader(
                query=query,
                min_pressure=min_pressure,
                drawer=None,
                candidate_limit=candidate_limit,
                page_size=min(500, candidate_limit),
            )
        else:  # pragma: no cover - only if Memory's internals move
            records = self._storage.list(user=self._user, limit=candidate_limit)

        engine = self._memory._engine
        if not isinstance(engine, FieldScopedRetrievalEngine):
            engine = FieldScopedRetrievalEngine(storage=self._storage)
        else:
            engine.bind(self._storage)

        return engine.recall(
            records=records,
            query=query,
            k=k,
            field=field,
            at=at,
            follow_links=follow_links,
            user=self._user,
        )

    def _require_fields(self) -> None:
        if not self._fields_supported:
            raise RuntimeError(
                f"{type(self._storage).__name__} does not carry field "
                "memberships. Use Memory(storage=EventfulSQLiteDriver(...)) "
                "or Memory(storage=CloudDriver(...))."
            )

    def _require_events(self) -> None:
        if not self._events_supported:
            raise RuntimeError(
                f"{type(self._storage).__name__} does not carry source "
                "events. Use Memory(storage=EventfulSQLiteDriver(...)); "
                "CloudDriver writes events (record, ingest, "
                "extract_signatures) but keeps no event table to read them "
                "back, so this call is local-only."
            )

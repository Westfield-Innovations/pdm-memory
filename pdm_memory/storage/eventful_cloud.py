"""
Cloud driver with the evidence layer delegated to Companion.

There is no local database here, so nothing to migrate and no trigger to
install: append-only is Companion's to enforce, by ``AppendOnlyModel`` and the
``RunSQL`` trigger beside it. What this driver owns is the wire contract — the
nested ``source_event`` / ``entities`` / ``signatures`` body B3 adds to
``POST /api/v1/pdm/ingest``, and the promise that ``content_hash`` is computed
the same way on both sides.

That last promise is the whole reason ``event_hash`` is a module rather than a
line of code inside a save method. If the two sides disagree, dedupe silently
stops working across sync and AC1 fails without raising anything.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from pdm_memory.storage.cloud_driver import CloudDriver
from pdm_memory.storage.errors import CloudNotFoundError, CloudStorageError
from pdm_memory.storage.events import (
    AppendOnlyViolation,
    EntityMentionRecord,
    EntityRecord,
    SourceEventRecord,
)

logger = logging.getLogger(__name__)

__all__ = ["EventfulCloudDriver", "INGEST_PATH", "EVENTS_PATH", "ENTITIES_PATH"]

INGEST_PATH = "/api/v1/pdm/ingest"
EVENTS_PATH = "/api/v1/pdm/source-events"
ENTITIES_PATH = "/api/v1/pdm/entities"
MENTIONS_PATH = "/api/v1/pdm/entity-mentions"


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Unwrap a list response, paginated or bare."""
    if isinstance(payload, dict):
        return payload.get("results") or payload.get("items") or []
    return payload or []


def _as_dict(value: Any) -> dict[str, Any]:
    """Companion may send JSON columns as text; the row mappers decode, so do we."""
    if isinstance(value, str):
        try:
            return json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
    return value or {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            return json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
    return value or []


class EventfulCloudDriver(CloudDriver):
    """``CloudDriver`` plus the source-event routes."""

    # Companion does not serve the routes below yet — SourceEvent, the nested
    # ingest contract and the entity routes are the Django half of TKT-101,
    # which has not landed. Claiming the capability before then is a promise
    # the backend cannot keep: EventLog would accept the driver and a single
    # ingest would write an event, a signature, a mention and an entity into
    # 404s. Flip this to True in the same change that ships those routes.
    EVENTS_AVAILABLE: bool = False

    def supports_events(self) -> bool:
        """
        Whether the Companion deployment behind this driver serves the event
        routes. False until the server half exists — the honest answer, and the
        one that keeps a partial remote write from happening at all.
        """
        return self.EVENTS_AVAILABLE

    # ------------------------------------------------------------------
    # Source events
    # ------------------------------------------------------------------

    def save_source_event(self, event: SourceEventRecord, *, payload: str = "") -> str:
        """
        Register the event with Companion, or adopt the one already there.

        Companion answers ``deduplicated: true`` when the hash is known, which
        is the same AC1 guarantee the local drivers get from a unique index —
        stated in the response rather than raised as a conflict.
        """
        event.ensure_content_hash(payload=payload)
        body = {"source_event": self.event_payload(event), "signatures": []}
        resp = self._post(INGEST_PATH, body).json()

        event_id = resp.get("source_event_id")
        if not event_id:
            raise CloudStorageError(
                "Ingest response carried no 'source_event_id' — the deployment "
                "predates the TKT-101 contract.",
                path=INGEST_PATH,
            )
        event.was_deduplicated = bool(resp.get("deduplicated"))
        if event.was_deduplicated:
            logger.debug("[PDM-Events] Companion deduplicated event %s", event_id)
        event.id = event_id
        return event_id

    def get_source_event(self, event_id: str) -> SourceEventRecord | None:
        try:
            resp = self._get(f"{EVENTS_PATH}/{event_id}")
        except CloudNotFoundError:
            return None
        return self.event_from_payload(resp.json())

    def find_event_by_hash(
        self, content_hash: str, user: str = "default"
    ) -> SourceEventRecord | None:
        try:
            resp = self._get(
                f"{EVENTS_PATH}/by-hash", params={"content_hash": content_hash}
            )
        except CloudNotFoundError:
            return None
        return self.event_from_payload(resp.json())

    def update_source_event(self, event_id: str, **fields: Any) -> None:
        """
        Refused here rather than at the far end.

        Companion would refuse it too, but a local exception costs no round
        trip and says why in a sentence the caller can act on.
        """
        raise AppendOnlyViolation(
            "pdm_source_events", "update", detail="Record a new event instead."
        )

    def delete_source_event(self, event_id: str) -> None:
        """Refused. See :meth:`update_source_event`."""
        raise AppendOnlyViolation("pdm_source_events", "delete")

    # ------------------------------------------------------------------
    # Entities and mentions
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
        Resolution runs server-side, on the whole corpus.

        Deliberately not reimplemented against a partial local view: the
        name-and-field rule is only as good as the mentions it can see, and
        the client sees a slice.
        """
        resp = self._post(
            f"{ENTITIES_PATH}/resolve",
            {
                "user": user,
                "surface_form": surface_form,
                "field_id": field_id,
                "entity_type": entity_type,
            },
        ).json()
        entity_id = resp.get("entity_id")
        if not entity_id:
            raise CloudStorageError(
                "Entity resolve response carried no 'entity_id'",
                path=f"{ENTITIES_PATH}/resolve",
            )
        return entity_id

    def get_entity(self, entity_id: str) -> EntityRecord | None:
        try:
            resp = self._get(f"{ENTITIES_PATH}/{entity_id}")
        except CloudNotFoundError:
            return None
        return self.entity_from_payload(resp.json())

    def record_mention(self, mention: EntityMentionRecord) -> str:
        resp = self._post(MENTIONS_PATH, self.mention_payload(mention)).json()
        mention_id = resp.get("id")
        if not mention_id:
            raise CloudStorageError(
                "Mention response carried no 'id'", path=MENTIONS_PATH
            )
        return mention_id

    def merge_entities(self, keep_id: str, merge_id: str, *, method: str) -> None:
        self._post(
            f"{ENTITIES_PATH}/merge",
            {"keep_id": keep_id, "merge_id": merge_id, "method": method},
        )

    def link_signature(
        self,
        signature_id: str,
        *,
        source_event_id: str | None = None,
        primary_entity_id: str | None = None,
        user: str = "default",
    ) -> None:
        body: dict[str, Any] = {}
        if source_event_id is not None:
            body["source_event_id"] = source_event_id
        if primary_entity_id is not None:
            body["primary_entity_id"] = primary_entity_id
        if not body:
            return
        self._patch(f"/api/v1/pdm/signatures/{signature_id}", body)

    # ------------------------------------------------------------------
    # Wire shapes — one place, so sync and the endpoint cannot drift apart
    # ------------------------------------------------------------------

    @staticmethod
    def event_payload(event: SourceEventRecord) -> dict[str, Any]:
        return {
            "event_type": event.event_type,
            "occurred_at": _iso(event.occurred_at),
            "observed_at": _iso(event.observed_at),
            "source_system": event.source_system,
            "provenance": event.provenance,
            "raw_reference": event.raw_reference,
            "content_hash": event.content_hash,
            "capture_authority_state": event.capture_authority_state,
            "compliance_state": event.compliance_state,
        }

    @staticmethod
    def event_from_payload(data: dict[str, Any]) -> SourceEventRecord:
        from pdm_memory.storage.events import _parse_dt

        return SourceEventRecord(
            id=data.get("id") or data.get("source_event_id") or "",
            user=str(data.get("user", "default")),
            event_type=data.get("event_type", "chat_message"),
            occurred_at=_parse_dt(data.get("occurred_at")),
            observed_at=_parse_dt(data.get("observed_at")),
            ingested_at=_parse_dt(data.get("ingested_at")),
            source_system=data.get("source_system", "chat"),
            provenance=_as_dict(data.get("provenance")),
            raw_reference=data.get("raw_reference") or "",
            content_hash=data.get("content_hash", ""),
            capture_authority_state=data.get("capture_authority_state", "unknown"),
            compliance_state=data.get("compliance_state", "unknown"),
        )

    @staticmethod
    def entity_from_payload(data: dict[str, Any]) -> EntityRecord:
        from pdm_memory.storage.events import _parse_dt

        return EntityRecord(
            id=data.get("id", ""),
            user=str(data.get("user", "default")),
            entity_type=data.get("entity_type", "person"),
            canonical_name=data.get("canonical_name", ""),
            disambiguator=data.get("disambiguator", ""),
            origin_field_id=data.get("origin_field_id", ""),
            aliases=_as_list(data.get("aliases")),
            current_state_version=data.get("current_state_version", 1),
            created_at=_parse_dt(data.get("created_at")),
            dissolved_at=_parse_dt(data.get("dissolved_at")),
            merged_into=data.get("merged_into"),
        )

    @staticmethod
    def mention_payload(mention: EntityMentionRecord) -> dict[str, Any]:
        return {
            "id": mention.id,
            "user": mention.user,
            "surface_form": mention.surface_form,
            "source_event_id": mention.source_event_id,
            "signature_id": mention.signature_id,
            "field_id": mention.field_id,
            "observed_at": _iso(mention.observed_at),
            "resolved_at": _iso(mention.resolved_at),
            "entity_id": mention.entity_id,
            "resolution": mention.resolution,
            "confidence": mention.confidence,
        }

    # ------------------------------------------------------------------
    # Reads — completing the interface the mixin defines
    # ------------------------------------------------------------------

    def list_source_events(
        self, user: str = "default", limit: int = 100
    ) -> list[SourceEventRecord]:
        resp = self._get(EVENTS_PATH, params={"user": user, "limit": limit})
        return [self.event_from_payload(row) for row in _rows(resp.json())]

    def iter_source_events(
        self, user: str = "default", batch: int = 500
    ) -> Iterator[SourceEventRecord]:
        """
        Page through every event, oldest first, following the cursor the API
        returns. One page per round trip beats one row per round trip, and the
        cursor is what keeps a sync from re-reading the newest page forever.
        """
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"user": user, "limit": batch, "order": "occurred_at"}
            if cursor:
                params["after"] = cursor
            payload = self._get(EVENTS_PATH, params=params).json()
            rows = _rows(payload)
            if not rows:
                return
            for row in rows:
                yield self.event_from_payload(row)
            cursor = payload.get("next") if isinstance(payload, dict) else None
            if not cursor or len(rows) < batch:
                return

    def iter_mentions(
        self, user: str = "default", batch: int = 500
    ) -> Iterator[EntityMentionRecord]:
        """Every mention, resolved or not — see the mixin's note on why both."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"user": user, "limit": batch, "order": "observed_at"}
            if cursor:
                params["after"] = cursor
            payload = self._get(MENTIONS_PATH, params=params).json()
            rows = _rows(payload)
            if not rows:
                return
            for row in rows:
                yield self.mention_from_payload(row)
            cursor = payload.get("next") if isinstance(payload, dict) else None
            if not cursor or len(rows) < batch:
                return

    def get_mention(self, mention_id: str) -> EntityMentionRecord | None:
        try:
            resp = self._get(f"{MENTIONS_PATH}/{mention_id}")
        except CloudNotFoundError:
            return None
        return self.mention_from_payload(resp.json())

    def mentions_for_entity(
        self, entity_id: str, user: str = "default"
    ) -> list[EntityMentionRecord]:
        resp = self._get(MENTIONS_PATH, params={"user": user, "entity_id": entity_id})
        return [self.mention_from_payload(row) for row in _rows(resp.json())]

    def unresolved_mentions(
        self, user: str = "default", limit: int = 100
    ) -> list[EntityMentionRecord]:
        resp = self._get(
            MENTIONS_PATH, params={"user": user, "resolution": "unresolved", "limit": limit}
        )
        return [self.mention_from_payload(row) for row in _rows(resp.json())]

    def resolve_mention(
        self,
        mention_id: str,
        *,
        entity_id: str,
        method: str,
        confidence: float | None = None,
    ) -> None:
        self._patch(
            f"{MENTIONS_PATH}/{mention_id}",
            {"entity_id": entity_id, "resolution": method, "confidence": confidence},
        )

    def list_entities(
        self, user: str = "default", include_dissolved: bool = False
    ) -> list[EntityRecord]:
        resp = self._get(
            ENTITIES_PATH,
            params={"user": user, "include_dissolved": str(include_dissolved).lower()},
        )
        return [self.entity_from_payload(row) for row in _rows(resp.json())]

    def signatures_for_event(self, event_id: str, user: str = "default") -> list:
        from pdm_memory.storage.schema import mapping_to_record

        resp = self._get(
            "/api/v1/pdm/signatures", params={"user": user, "source_event_id": event_id}
        )
        return [mapping_to_record(row) for row in _rows(resp.json())]

    def signatures_for_entity(self, entity_id: str, user: str = "default") -> list:
        from pdm_memory.storage.schema import mapping_to_record

        resp = self._get(
            "/api/v1/pdm/signatures", params={"user": user, "primary_entity_id": entity_id}
        )
        return [mapping_to_record(row) for row in _rows(resp.json())]

    @staticmethod
    def mention_from_payload(data: dict[str, Any]) -> EntityMentionRecord:
        from pdm_memory.storage.events import _parse_dt

        return EntityMentionRecord(
            id=data.get("id", ""),
            user=str(data.get("user", "default")),
            surface_form=data.get("surface_form", ""),
            source_event_id=data.get("source_event_id") or "",
            signature_id=data.get("signature_id") or "",
            field_id=data.get("field_id") or "",
            observed_at=_parse_dt(data.get("observed_at")),
            entity_id=data.get("entity_id"),
            resolution=data.get("resolution") or "unresolved",
            resolved_at=_parse_dt(data.get("resolved_at")),
            confidence=data.get("confidence"),
        )

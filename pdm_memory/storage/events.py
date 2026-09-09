"""
PDM source events, entities and mentions — the evidence layer under signatures.

TKT-101. A signature records what PDM concluded; nothing until now recorded
where the conclusion came from or who it was about. This module adds the two
substrate tables the spec asks for (§4.1, §4.2) plus the mention layer that
keeps identity honest (§11's "append-only evidence, derived current state"
applied to identity as well as to state).

    pdm_source_events    something happened, once, and is referenced many times
    pdm_entity_mentions  someone used this name here, then — evidence
    pdm_entities         a resolved identity claiming a set of mentions — a claim

Signatures gain two nullable pointers (``source_event_id``,
``primary_entity_id``). ``primary_entity_id`` is the simple path for reporting;
the real many-to-many graph stays where it already lives, in Companion's
``SignatureEntityDependency``. A fact about a meeting with two people and a
project needs the graph — one FK could never carry it.

Nothing here modifies a frozen module. The tables are created by
``apply_event_migrations``, which the eventful driver calls alongside the
stock migrations; see ``eventful_sqlite.py`` for how it is wired without
touching ``factory.py``.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pdm_memory.storage.event_hash import (
    CONTENT_HASH_VERSION,
    compute_content_hash,
    normalize_instant,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONTENT_HASH_VERSION",
    "RESOLUTION_METHODS",
    "AppendOnlyViolation",
    "EntityMentionRecord",
    "EntityRecord",
    "SourceEventRecord",
    "SupportsEvents",
    "apply_event_migrations",
    "apply_event_migrations_postgres",
    "apply_event_migrations_sqlite",
    "compute_content_hash",
]


class AppendOnlyViolation(RuntimeError):
    """
    An update or delete aimed at a row that stands for good.

    Raised by the Python guard before the statement is built. The database
    trigger behind it catches whatever reaches SQL by another route; this
    exception exists so the ordinary path fails with a sentence a caller can
    read rather than with a driver-level integrity error.
    """

    def __init__(self, table: str, operation: str, *, detail: str = "") -> None:
        message = f"{table} is append-only; {operation} is refused."
        if detail:
            message = f"{message} {detail}"
        super().__init__(message)
        self.table = table
        self.operation = operation


# ---------------------------------------------------------------------------
# Resolution vocabulary
# ---------------------------------------------------------------------------

# Closed vocabulary, for the same reason ``SignatureFieldMembership.derived_by``
# is one: resolutions of different strength must stay distinguishable, so an
# automated pass can be rolled back without touching what a person confirmed.
RESOLUTION_METHODS: frozenset[str] = frozenset(
    {
        "unresolved",           # recorded, not yet attributed to an identity
        "same_name_same_field", # the default automatic rule (see D6)
        "alias_declared",       # a known alias table matched
        "user_confirmed",       # a person answered "which Alex?"
        "llm",                  # a model guessed; weakest, first to be redone
    }
)

# Auto-resolutions a later pass may revise. A person's answer is not in here.
REVISABLE_RESOLUTIONS: frozenset[str] = frozenset(
    {"unresolved", "same_name_same_field", "llm"}
)


def utc_now() -> datetime:
    """The single clock. Duplicated in three modules before this."""
    return datetime.now(tz=timezone.utc)


_now = utc_now


def iso_utc(value: datetime | None) -> str | None:
    """
    One spelling for every timestamp column in the store.

    These columns are TEXT, so ordering is string ordering: a row written with
    the caller's ``+03:00`` sorts before one written as ``Z`` even when it
    happened later. Everything goes through the same UTC normaliser the hash
    uses, and ``ORDER BY observed_at`` means what it reads like.
    """
    return normalize_instant(value) if value else None


_iso = iso_utc  # internal alias, kept short at the call sites below


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class SourceEventRecord:
    """
    One thing that happened, stored once and pointed at many times.

    ``raw_reference`` is a pointer — ``chat:123:msg:456`` — not the content.
    That is what makes AC1's "without duplicating raw content" true: five
    signatures drawn from one message share one row, and the message itself
    stays wherever it already lives.

    Mirrors the Django ``SourceEvent`` model field for field, minus the ORM,
    the same way ``SignatureRecord`` mirrors ``Signature``.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user: str = "default"

    event_type: str = "chat_message"

    # occurred_at — when it happened in the world.
    # observed_at — when we first saw it. ingested_at — when we wrote it down.
    # The three differ for anything replayed, imported, or backfilled, and
    # collapsing them is how provenance quietly becomes a guess.
    occurred_at: datetime | None = None
    observed_at: datetime | None = None
    ingested_at: datetime | None = None

    source_system: str = "chat"
    provenance: dict[str, Any] = field(default_factory=dict)
    raw_reference: str = ""
    content_hash: str = ""

    capture_authority_state: str = "unknown"
    compliance_state: str = "unknown"

    # Whether the caller told us when this happened, as opposed to us filling
    # the column in. Not persisted — it only decides what goes into the hash.
    occurred_at_known: bool = field(
        init=False, repr=False, compare=False, default=False
    )

    # Set by ``save_source_event``: whether the store already held this event.
    # Transient like the flag above — it describes the last write, not the row.
    # Carried here so a caller learns the outcome from the write it already
    # made, instead of asking first and paying a round trip per row to find
    # out what the write was about to tell it.
    was_deduplicated: bool = field(
        init=False, repr=False, compare=False, default=False
    )

    def __post_init__(self) -> None:
        now = _now()
        self.occurred_at_known = self.occurred_at is not None
        self.was_deduplicated = False
        if self.occurred_at is None:
            self.occurred_at = now
        if self.observed_at is None:
            self.observed_at = self.occurred_at
        if self.ingested_at is None:
            self.ingested_at = now

    def ensure_content_hash(self, *, payload: str = "") -> str:
        """
        Fill ``content_hash`` from the canonical contract if it is empty.

        A defaulted ``occurred_at`` is held out of the hash. The column still
        gets a value, because a row needs one, but our own clock must never
        decide identity: ingesting one chat message twice would otherwise
        produce two events a millisecond apart, and AC1 would fail on the most
        ordinary path there is — the caller who does not know, or care, exactly
        when the thing happened.
        """
        if not self.content_hash:
            self.content_hash = compute_content_hash(
                event_type=self.event_type,
                occurred_at=self.occurred_at if self.occurred_at_known else None,
                source_system=self.source_system,
                raw_reference=self.raw_reference,
                payload=payload,
            )
        return self.content_hash


@dataclass
class EntityRecord:
    """
    A resolved identity — a claim over a set of mentions, not a fact about them.

    Deliberately *not* append-only: ``current_state_version`` moves, aliases
    accumulate, and a merge closes a row rather than deleting it. The evidence
    that survives a wrong claim lives in ``EntityMentionRecord``.

    Uniqueness is ``(user, canonical_name, disambiguator)`` and pointedly not
    ``(user, canonical_name)``. Two colleagues named Alex must be able to
    coexist; an upsert on the name alone merges them silently, and once their
    signatures point at one row there is no record left of which mention was
    whose.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user: str = "default"

    entity_type: str = "person"
    canonical_name: str = ""
    # Empty until a distinction is actually needed. Filled with the field id
    # by the automatic path and replaced by a human word — "colleague",
    # "Chen" — the moment someone answers the question.
    disambiguator: str = ""
    # Where this identity was first observed. Carries the automatic rule's
    # idempotency; not a claim that the entity belongs to that field only.
    origin_field_id: str = ""
    aliases: list[str] = field(default_factory=list)

    current_state_version: int = 1
    created_at: datetime | None = None

    # A merged entity is closed, never deleted, and keeps a pointer to the
    # survivor — the closed-interval pattern SignatureFieldMembership uses.
    dissolved_at: datetime | None = None
    merged_into: str | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = _now()


@dataclass
class EntityMentionRecord:
    """
    Someone used this name, in this event, in this field, at this time.

    Recording a mention never merges anything, so it is always cheap and always
    right. Attribution to an identity is a separate, reversible decision with
    its own provenance in ``resolution``.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user: str = "default"

    surface_form: str = ""          # exactly as written: "Alex", "@alex", "Алексом"
    source_event_id: str = ""       # "" not NULL — see the idempotency index
    signature_id: str = ""
    field_id: str = ""
    observed_at: datetime | None = None

    entity_id: str | None = None    # NULL until resolved
    resolution: str = "unresolved"
    resolved_at: datetime | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.observed_at is None:
            self.observed_at = _now()
        if self.resolution not in RESOLUTION_METHODS:
            raise ValueError(
                f"resolution must be one of {sorted(RESOLUTION_METHODS)}, "
                f"got {self.resolution!r}"
            )

    @property
    def surface_norm(self) -> str:
        """Case-folded form used for lookup; the written form stays intact."""
        return normalize_surface(self.surface_form)


def normalize_surface(raw: str) -> str:
    """
    Fold a written name for matching. Kept deliberately shallow — casefold and
    collapse whitespace, nothing more. Stripping punctuation or diacritics here
    would quietly merge names that a person would keep apart, which is the very
    failure this layer exists to avoid.
    """
    return " ".join((raw or "").split()).casefold()


# ---------------------------------------------------------------------------
# Optional capability protocol (D4)
# ---------------------------------------------------------------------------


@runtime_checkable
class SupportsEvents(Protocol):
    """
    What a driver offers when it can carry the evidence layer.

    A Protocol rather than an abstract method on ``BaseStorage``: adding an
    abstractmethod there would break all three drivers at once, and every
    driver that never grows events would carry a stub forever. Structural
    typing costs the drivers nothing and leaves ``BaseStorage`` untouched.
    """

    def supports_events(self) -> bool: ...

    def save_source_event(
        self, event: SourceEventRecord, *, payload: str = ""
    ) -> str: ...

    def get_source_event(self, event_id: str) -> SourceEventRecord | None: ...

    def find_event_by_hash(
        self, content_hash: str, user: str = "default"
    ) -> SourceEventRecord | None: ...

    def record_mention(self, mention: EntityMentionRecord) -> str: ...

    def resolve_mention(
        self,
        mention_id: str,
        *,
        entity_id: str,
        method: str,
        confidence: float | None = None,
    ) -> None: ...

    def resolve_or_create_entity(
        self,
        *,
        user: str,
        surface_form: str,
        field_id: str,
        entity_type: str = "person",
    ) -> str: ...

    def get_entity(self, entity_id: str) -> EntityRecord | None: ...

    def merge_entities(self, keep_id: str, merge_id: str, *, method: str) -> None: ...

    def link_signature(
        self,
        signature_id: str,
        *,
        source_event_id: str | None = None,
        primary_entity_id: str | None = None,
        user: str = "default",
    ) -> bool: ...

    def signatures_for_event(
        self, event_id: str, user: str = "default"
    ) -> list[Any]: ...

    def signatures_for_entity(
        self, entity_id: str, user: str = "default"
    ) -> list[Any]: ...

    def iter_source_events(self, user: str = "default", batch: int = 500) -> Any: ...

    def iter_mentions(self, user: str = "default", batch: int = 500) -> Any: ...

    def list_entities(
        self, user: str = "default", include_dissolved: bool = False
    ) -> list[EntityRecord]: ...

    def get_mention(self, mention_id: str) -> EntityMentionRecord | None: ...

    def mentions_for_entity(
        self, entity_id: str, user: str = "default"
    ) -> list[EntityMentionRecord]: ...

    def unresolved_mentions(
        self, user: str = "default", limit: int = 100
    ) -> list[EntityMentionRecord]: ...


def storage_supports_events(storage: Any) -> bool:
    """
    True when *storage* can carry events — structurally and in fact.

    Two questions, and both have to be yes. ``isinstance`` against the Protocol
    is what makes the ``@runtime_checkable`` above more than decoration: it
    catches a driver that answers the capability question while missing methods
    the caller will reach for, which is how a half-implemented backend used to
    accept an ingest and then die partway through it. ``supports_events()`` is
    the driver's own answer about the backend behind it — a cloud driver can
    have every method and still be pointed at a deployment that serves none of
    the routes.

    Never raises on a plain driver.
    """
    if not isinstance(storage, SupportsEvents):
        return False
    try:
        return bool(storage.supports_events())
    except Exception:  # pragma: no cover - a driver that cannot answer is a no
        return False


# ---------------------------------------------------------------------------
# DDL — two dialects, on purpose
# ---------------------------------------------------------------------------
#
# ``user`` is a reserved word in PostgreSQL and an ordinary identifier in
# SQLite, which is why the stock schema already keeps two strings and quotes
# "user" throughout the Postgres one. A single shared DDL does not survive
# contact with either database.

SCHEMA_EVENTS_SQLITE = """
CREATE TABLE IF NOT EXISTS pdm_source_events (
    id                       TEXT PRIMARY KEY,
    user                     TEXT NOT NULL DEFAULT 'default',
    event_type               TEXT NOT NULL,
    occurred_at              TEXT NOT NULL,
    observed_at              TEXT NOT NULL,
    ingested_at              TEXT NOT NULL,
    source_system            TEXT NOT NULL DEFAULT 'chat',
    provenance               TEXT NOT NULL DEFAULT '{}',
    raw_reference            TEXT NOT NULL DEFAULT '',
    content_hash             TEXT NOT NULL,
    capture_authority_state  TEXT NOT NULL DEFAULT 'unknown',
    compliance_state         TEXT NOT NULL DEFAULT 'unknown'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_events_user_hash
    ON pdm_source_events (user, content_hash);
CREATE INDEX IF NOT EXISTS idx_pdm_events_user_occurred
    ON pdm_source_events (user, occurred_at DESC);

CREATE TABLE IF NOT EXISTS pdm_entities (
    id                     TEXT PRIMARY KEY,
    user                   TEXT NOT NULL DEFAULT 'default',
    entity_type            TEXT NOT NULL DEFAULT 'person',
    canonical_name         TEXT NOT NULL,
    canonical_norm         TEXT NOT NULL DEFAULT '',
    disambiguator          TEXT NOT NULL DEFAULT '',
    -- The field this identity was first seen in. Without it the automatic
    -- rule is not idempotent: the second sighting of "Alex" in the same field
    -- cannot tell the entity it already made from one belonging to another
    -- field, and splits the person it was meant to keep whole.
    origin_field_id        TEXT NOT NULL DEFAULT '',
    aliases                TEXT NOT NULL DEFAULT '[]',
    current_state_version  INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT NOT NULL,
    dissolved_at           TEXT,
    merged_into            TEXT
);

-- Intentionally NOT unique on (user, canonical_name): two different people
-- called Alex must coexist. Uniqueness lives on the triple below, with
-- disambiguator defaulting to '' rather than NULL so the index behaves
-- identically in SQLite and PostgreSQL.
CREATE INDEX IF NOT EXISTS idx_pdm_entities_user_name
    ON pdm_entities (user, canonical_norm);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_entities_identity
    ON pdm_entities (user, canonical_norm, disambiguator);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_entities_origin
    ON pdm_entities (user, canonical_norm, origin_field_id);

CREATE TABLE IF NOT EXISTS pdm_entity_mentions (
    id               TEXT PRIMARY KEY,
    user             TEXT NOT NULL DEFAULT 'default',
    surface_form     TEXT NOT NULL,
    surface_norm     TEXT NOT NULL DEFAULT '',
    source_event_id  TEXT NOT NULL DEFAULT '',
    signature_id     TEXT NOT NULL DEFAULT '',
    field_id         TEXT NOT NULL DEFAULT '',
    observed_at      TEXT NOT NULL,
    entity_id        TEXT,
    resolution       TEXT NOT NULL DEFAULT 'unresolved',
    resolved_at      TEXT,
    confidence       REAL
);

-- NOT NULL DEFAULT '' on both id columns above is what makes this index do its
-- job. NULLs never conflict in a UNIQUE index in either database, so nullable
-- columns here would let the same mention be written unboundedly often —
-- silently, and precisely on the path where a signature id is not known yet.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_mentions_idem
    ON pdm_entity_mentions (user, source_event_id, signature_id, surface_form);
CREATE INDEX IF NOT EXISTS idx_pdm_mentions_lookup
    ON pdm_entity_mentions (user, surface_norm, field_id);
CREATE INDEX IF NOT EXISTS idx_pdm_mentions_entity
    ON pdm_entity_mentions (user, entity_id);
"""

# Columns added to the existing signatures table. Kept apart from the CREATEs
# because ALTER TABLE has no IF NOT EXISTS in SQLite and must be guarded by a
# PRAGMA table_info check instead.
SIGNATURE_EVENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source_event_id", "TEXT REFERENCES pdm_source_events(id)"),
    ("primary_entity_id", "TEXT REFERENCES pdm_entities(id)"),
)

SIGNATURE_EVENT_INDEXES_SQLITE = """
CREATE INDEX IF NOT EXISTS idx_pdm_sig_source_event
    ON pdm_signatures (source_event_id);
CREATE INDEX IF NOT EXISTS idx_pdm_sig_entity
    ON pdm_signatures (primary_entity_id);
"""

# ---------------------------------------------------------------------------
# Append-only triggers
# ---------------------------------------------------------------------------
#
# Scoped to the fields that *are* the statement, following companion_api's
# 0038_append_only_constraints and 0047_projection_append_only_triggers rather
# than blocking UPDATE outright. A blanket block would freeze `provenance` and
# `raw_reference` too — the two columns most likely to name a person who later
# asks to be forgotten — and would make the row's own `compliance_state`
# unwritable the moment consent changed. What happened is frozen; what we
# recorded about our handling of it stays answerable.

_EVENT_FROZEN_COLUMNS = (
    "id",
    "user",
    "event_type",
    "occurred_at",
    "observed_at",
    "ingested_at",
    "source_system",
    "content_hash",
)

_MENTION_FROZEN_COLUMNS = (
    "id",
    "user",
    "surface_form",
    "surface_norm",
    "source_event_id",
    "signature_id",
    "field_id",
    "observed_at",
)


def _sqlite_immutability_guard(table: str, columns: Sequence[str]) -> str:
    """BEFORE UPDATE trigger firing only when a frozen column would change."""
    # SQLite's IS NOT is null-safe, the same comparison Postgres spells
    # IS DISTINCT FROM.
    condition = "\n       OR ".join(
        f"NEW.{col} IS NOT OLD.{col}" for col in columns
    )
    return f"""
CREATE TRIGGER IF NOT EXISTS trg_{table}_immutable
BEFORE UPDATE ON {table}
FOR EACH ROW WHEN (
          {condition}
)
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only: the recorded event cannot be rewritten');
END;

CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} is append-only: rows cannot be deleted');
END;
"""


TRIGGERS_EVENTS_SQLITE = (
    _sqlite_immutability_guard("pdm_source_events", _EVENT_FROZEN_COLUMNS)
    + _sqlite_immutability_guard("pdm_entity_mentions", _MENTION_FROZEN_COLUMNS)
)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def apply_event_migrations_sqlite(conn: Any) -> None:
    """
    Create the evidence tables on a new or existing SQLite database.

    Idempotent, and order-sensitive: tables, then columns, then indexes, then
    triggers. A trigger created before its ALTER TABLE would abort the very
    migration that installs it.
    """
    conn.executescript(SCHEMA_EVENTS_SQLITE)

    existing = {row[1] for row in conn.execute("PRAGMA table_info(pdm_signatures)")}
    for column, decl in SIGNATURE_EVENT_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE pdm_signatures ADD COLUMN {column} {decl}")

    conn.executescript(SIGNATURE_EVENT_INDEXES_SQLITE)
    conn.executescript(TRIGGERS_EVENTS_SQLITE)
    logger.debug("[PDM-Events] Evidence tables ready (sqlite)")


# The SQLite path is the default one the name refers to.
apply_event_migrations = apply_event_migrations_sqlite


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_from_row(row: Any) -> SourceEventRecord:
    return SourceEventRecord(
        id=row["id"],
        user=row["user"],
        event_type=row["event_type"],
        occurred_at=_parse_dt(row["occurred_at"]),
        observed_at=_parse_dt(row["observed_at"]),
        ingested_at=_parse_dt(row["ingested_at"]),
        source_system=row["source_system"],
        provenance=json.loads(row["provenance"] or "{}"),
        raw_reference=row["raw_reference"] or "",
        content_hash=row["content_hash"],
        capture_authority_state=row["capture_authority_state"],
        compliance_state=row["compliance_state"],
    )


def entity_from_row(row: Any) -> EntityRecord:
    return EntityRecord(
        id=row["id"],
        user=row["user"],
        entity_type=row["entity_type"],
        canonical_name=row["canonical_name"],
        disambiguator=row["disambiguator"],
        origin_field_id=row["origin_field_id"],
        aliases=json.loads(row["aliases"] or "[]"),
        current_state_version=row["current_state_version"],
        created_at=_parse_dt(row["created_at"]),
        dissolved_at=_parse_dt(row["dissolved_at"]),
        merged_into=row["merged_into"],
    )


def mention_from_row(row: Any) -> EntityMentionRecord:
    return EntityMentionRecord(
        id=row["id"],
        user=row["user"],
        surface_form=row["surface_form"],
        source_event_id=row["source_event_id"],
        signature_id=row["signature_id"],
        field_id=row["field_id"],
        observed_at=_parse_dt(row["observed_at"]),
        entity_id=row["entity_id"],
        resolution=row["resolution"],
        resolved_at=_parse_dt(row["resolved_at"]),
        confidence=row["confidence"],
    )


def event_insert_row(event: SourceEventRecord) -> tuple[Any, ...]:
    return (
        event.id,
        event.user,
        event.event_type,
        normalize_instant(event.occurred_at),
        normalize_instant(event.observed_at),
        normalize_instant(event.ingested_at),
        event.source_system,
        json.dumps(event.provenance),
        event.raw_reference,
        event.content_hash,
        event.capture_authority_state,
        event.compliance_state,
    )


def entity_insert_row(entity: EntityRecord) -> tuple[Any, ...]:
    return (
        entity.id,
        entity.user,
        entity.entity_type,
        entity.canonical_name,
        normalize_surface(entity.canonical_name),
        entity.disambiguator,
        entity.origin_field_id,
        json.dumps(entity.aliases),
        entity.current_state_version,
        _iso(entity.created_at),
        _iso(entity.dissolved_at),
        entity.merged_into,
    )


def mention_insert_row(mention: EntityMentionRecord) -> tuple[Any, ...]:
    return (
        mention.id,
        mention.user,
        mention.surface_form,
        mention.surface_norm,
        mention.source_event_id,
        mention.signature_id,
        mention.field_id,
        _iso(mention.observed_at),
        mention.entity_id,
        mention.resolution,
        _iso(mention.resolved_at),
        mention.confidence,
    )


# ---------------------------------------------------------------------------
# PostgreSQL dialect
# ---------------------------------------------------------------------------
#
# ``user`` is a reserved word here and must be quoted in every occurrence. The
# stock schema already keeps two strings for exactly this reason; a single
# shared DDL does not survive contact with both databases.

SCHEMA_EVENTS_POSTGRES = """
CREATE TABLE IF NOT EXISTS pdm_source_events (
    id                       TEXT PRIMARY KEY,
    "user"                   TEXT NOT NULL DEFAULT 'default',
    event_type               TEXT NOT NULL,
    occurred_at              TEXT NOT NULL,
    observed_at              TEXT NOT NULL,
    ingested_at              TEXT NOT NULL,
    source_system            TEXT NOT NULL DEFAULT 'chat',
    provenance               TEXT NOT NULL DEFAULT '{}',
    raw_reference            TEXT NOT NULL DEFAULT '',
    content_hash             TEXT NOT NULL,
    capture_authority_state  TEXT NOT NULL DEFAULT 'unknown',
    compliance_state         TEXT NOT NULL DEFAULT 'unknown'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_events_user_hash
    ON pdm_source_events ("user", content_hash);
CREATE INDEX IF NOT EXISTS idx_pdm_events_user_occurred
    ON pdm_source_events ("user", occurred_at DESC);

CREATE TABLE IF NOT EXISTS pdm_entities (
    id                     TEXT PRIMARY KEY,
    "user"                 TEXT NOT NULL DEFAULT 'default',
    entity_type            TEXT NOT NULL DEFAULT 'person',
    canonical_name         TEXT NOT NULL,
    canonical_norm         TEXT NOT NULL DEFAULT '',
    disambiguator          TEXT NOT NULL DEFAULT '',
    origin_field_id        TEXT NOT NULL DEFAULT '',
    aliases                TEXT NOT NULL DEFAULT '[]',
    current_state_version  INTEGER NOT NULL DEFAULT 1,
    created_at             TEXT NOT NULL,
    dissolved_at           TEXT,
    merged_into            TEXT
);

CREATE INDEX IF NOT EXISTS idx_pdm_entities_user_name
    ON pdm_entities ("user", canonical_norm);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_entities_identity
    ON pdm_entities ("user", canonical_norm, disambiguator);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_entities_origin
    ON pdm_entities ("user", canonical_norm, origin_field_id);

CREATE TABLE IF NOT EXISTS pdm_entity_mentions (
    id               TEXT PRIMARY KEY,
    "user"           TEXT NOT NULL DEFAULT 'default',
    surface_form     TEXT NOT NULL,
    surface_norm     TEXT NOT NULL DEFAULT '',
    source_event_id  TEXT NOT NULL DEFAULT '',
    signature_id     TEXT NOT NULL DEFAULT '',
    field_id         TEXT NOT NULL DEFAULT '',
    observed_at      TEXT NOT NULL,
    entity_id        TEXT,
    resolution       TEXT NOT NULL DEFAULT 'unresolved',
    resolved_at      TEXT,
    confidence       DOUBLE PRECISION
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_mentions_idem
    ON pdm_entity_mentions ("user", source_event_id, signature_id, surface_form);
CREATE INDEX IF NOT EXISTS idx_pdm_mentions_lookup
    ON pdm_entity_mentions ("user", surface_norm, field_id);
CREATE INDEX IF NOT EXISTS idx_pdm_mentions_entity
    ON pdm_entity_mentions ("user", entity_id);
"""

SIGNATURE_EVENT_INDEXES_POSTGRES = """
CREATE INDEX IF NOT EXISTS idx_pdm_sig_source_event
    ON pdm_signatures (source_event_id);
CREATE INDEX IF NOT EXISTS idx_pdm_sig_entity
    ON pdm_signatures (primary_entity_id);
"""


def _postgres_immutability_guard(table: str, columns: Sequence[str]) -> list[str]:
    """
    Same carve-out as the SQLite guard, in plpgsql.

    Returned as whole statements rather than one blob: the Postgres driver
    executes schema DDL by splitting on ";", which would cut a function body
    in half at its first internal statement.
    """
    condition = "\n           OR ".join(
        f'NEW."{col}" IS DISTINCT FROM OLD."{col}"' for col in columns
    )
    function_name = f"{table}_immutable"
    return [
        f"""
CREATE OR REPLACE FUNCTION {function_name}() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '{table} is append-only: rows cannot be deleted';
    END IF;
    IF {condition} THEN
        RAISE EXCEPTION '{table} is append-only: the recorded event cannot be rewritten';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""".strip(),
        # No DROP. The previous version dropped and recreated on every driver
        # __init__, which takes an ACCESS EXCLUSIVE lock on the table at each
        # process start and races two workers booting together into
        # DuplicateObject. Postgres has no CREATE TRIGGER IF NOT EXISTS, so the
        # DO block swallows the duplicate instead — idempotent, and it touches
        # nothing when the trigger is already in place.
        f"""
DO $do$
BEGIN
    CREATE TRIGGER trg_{function_name}
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION {function_name}();
EXCEPTION
    WHEN duplicate_object THEN NULL;
END;
$do$;
""".strip(),
    ]


TRIGGERS_EVENTS_POSTGRES: tuple[str, ...] = tuple(
    _postgres_immutability_guard("pdm_source_events", _EVENT_FROZEN_COLUMNS)
    + _postgres_immutability_guard("pdm_entity_mentions", _MENTION_FROZEN_COLUMNS)
)


def apply_event_migrations_postgres(conn: Any) -> None:
    """
    Create the evidence tables on a new or existing PostgreSQL database.

    Same order as the SQLite path — tables, columns, indexes, triggers last —
    and the same reason: a trigger installed before its ALTER TABLE aborts the
    migration that installs it.
    """
    for statement in SCHEMA_EVENTS_POSTGRES.split(";"):
        if statement.strip():
            conn.execute(statement)

    for column, decl in SIGNATURE_EVENT_COLUMNS:
        conn.execute(
            f"ALTER TABLE pdm_signatures ADD COLUMN IF NOT EXISTS {column} {decl}"
        )

    for statement in SIGNATURE_EVENT_INDEXES_POSTGRES.split(";"):
        if statement.strip():
            conn.execute(statement)

    # Whole statements — never split; see _postgres_immutability_guard.
    for statement in TRIGGERS_EVENTS_POSTGRES:
        conn.execute(statement)

    logger.debug("[PDM-Events] Evidence tables ready (postgres)")

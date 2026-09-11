"""
Field memberships and relationships — who belongs where, and when.

TKT-102. TKT-101 gave a fact a source and a subject; this gives the subject a
context, and both a time window. An entity is not simply "work" or "family" —
a contractor is in Work and in Project Orion at once, was in Project Orion from
March until August, and is not in it now. A question asked about April deserves
April's answer.

    pdm_field_memberships   entity E was in field F between two instants
    pdm_relationships       E1 stood in some relation to E2 between two instants

Both are closed intervals, ``valid_to = NULL`` meaning "still". Neither is
edited in place: ending a membership writes the end onto that row and a new row
records what replaced it, so "when did this change" stays answerable. That is
the same shape Companion's own ``FieldMembership`` uses, and the same one our
entities use for merges.

The vocabulary is mirrored from ``companion_api/pdm/models.py`` rather than
invented here — field ids, membership states and derivation grades all have to
mean the same thing on both sides or a sync turns one into another.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pdm_memory.storage.event_hash import normalize_instant
from pdm_memory.storage.events import _parse_dt, iso_utc, utc_now

logger = logging.getLogger(__name__)

__all__ = [
    "DERIVATIONS",
    "FIELD_ID_PATTERN",
    "MEMBERSHIP_STATES",
    "FieldMembershipRecord",
    "RelationshipRecord",
    "apply_field_migrations_sqlite",
    "normalize_field_id",
    "validate_interval",
]

# Hierarchical, lowercase, slash-separated: "work", "westfield/dqs". Copied
# from Companion's FIELD_ID_REGEX — a field id that validates on one side and
# not the other is a row that syncs in one direction only.
FIELD_ID_PATTERN = re.compile(
    r"^[a-z0-9]+(?:[-_][a-z0-9]+)*(?:/[a-z0-9]+(?:[-_][a-z0-9]+)*)*$"
)

# Mirrors PerspectiveStateStatus. Only the states a membership can actually be
# in are listed; the full Django vocabulary covers other models too.
MEMBERSHIP_STATES: frozenset[str] = frozenset(
    {
        "active",       # in the field, now or during the window
        "pending",      # proposed, not yet in force
        "expired",      # the window closed on its own
        "revoked",      # ended deliberately
        "implicit",     # inferred, not declared
        "structural",   # a standing fact about the field itself; never ends
        "historical",   # kept for the record, not part of the live picture
    }
)

# States a temporal query honours. Note what is *not* excluded: "expired" is
# here on purpose. Whether a membership is in force at an instant is the
# window's job — valid_from and valid_to already answer it — and a membership
# that ended in August was still true in June. Filtering "expired" out of the
# state as well erased the past every time someone closed a window, which is
# the opposite of what a closed interval is for.
#
# What is excluded never was a membership at that instant: "pending" has not
# begun, "revoked" and "denied" were taken back.
COUNTED_STATES: frozenset[str] = frozenset(
    {"active", "implicit", "structural", "expired", "historical"}
)

# Kept under the old name for the store, which reads it as "states a query
# counts" rather than "states that are current".
LIVE_STATES = COUNTED_STATES

# Mirrors FieldMembershipDerivation. Graded like the resolution methods on
# mentions, and for the same reason: an automatic pass must be able to revise
# its own work without touching what a person declared.
DERIVATIONS: frozenset[str] = frozenset(
    {"manual", "classifier", "llm", "inherited_workspace", "sdk"}
)

REVISABLE_DERIVATIONS: frozenset[str] = frozenset({"classifier", "llm"})


def normalize_field_id(raw: str) -> str:
    """
    Validate and normalise a field id, or say why it is not one.

    Refused rather than coerced. A field id is a namespace shared with
    Companion; quietly lowercasing "Work/DQS" here would produce a row that
    matches nothing on the other side and no error anywhere.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("field_id cannot be empty")
    if not FIELD_ID_PATTERN.match(value):
        raise ValueError(
            f"field_id {raw!r} is not a valid field id: lowercase slug segments "
            "separated by '/', e.g. 'work' or 'westfield/dqs'"
        )
    return value


def validate_interval(
    valid_from: datetime | str | None,
    valid_to: datetime | str | None,
) -> tuple[str, str | None]:
    """
    Normalise a window and refuse an impossible one.

    Returns both instants in the single UTC spelling every timestamp column in
    this package uses. These columns are TEXT, so a row written with the
    caller's offset would sort before one written as UTC even when it happened
    later — the defect TKT-101 shipped and had to come back for.

    An open end is ``None``, meaning "still". An end at or before the start is
    refused: a window that never contained anything is a mistake at the call
    site, not a row.
    """
    start = normalize_instant(valid_from) if valid_from is not None else normalize_instant(utc_now())
    if not start:
        raise ValueError("valid_from cannot be empty")

    if valid_to is None:
        return start, None

    end = normalize_instant(valid_to)
    if end <= start:
        raise ValueError(
            f"valid_to ({end}) must be after valid_from ({start}); "
            "an interval that ends before it begins holds nothing"
        )
    return start, end


@dataclass
class FieldMembershipRecord:
    """Entity E belonged to field F for a stretch of time."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user: str = "default"

    entity_id: str = ""
    field_id: str = ""
    # What the entity is in that field — "owner", "member", "observer". Free
    # text like Companion's: the vocabulary belongs to the product, not here.
    role: str = ""

    valid_from: datetime | None = None
    valid_to: datetime | None = None

    state: str = "active"
    derived_by: str = "sdk"
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = utc_now()
        if self.valid_from is None:
            self.valid_from = utc_now()
        if self.state not in MEMBERSHIP_STATES:
            raise ValueError(
                f"state must be one of {sorted(MEMBERSHIP_STATES)}, got {self.state!r}"
            )
        if self.derived_by not in DERIVATIONS:
            raise ValueError(
                f"derived_by must be one of {sorted(DERIVATIONS)}, "
                f"got {self.derived_by!r}"
            )
        self.field_id = normalize_field_id(self.field_id)


@dataclass
class RelationshipRecord:
    """
    E1 stood in some relation to E2 for a stretch of time.

    ``directionality`` matters and is not decoration: "Alex manages Orion" and
    "Orion manages Alex" are different claims, and a symmetric relation —
    "colleague of" — is a third thing that should not need two rows.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user: str = "default"

    source_entity_id: str = ""
    target_entity_id: str = ""
    relationship_type: str = ""
    directionality: str = "directed"   # "directed" | "symmetric"

    valid_from: datetime | None = None
    valid_to: datetime | None = None

    state: str = "active"
    derived_by: str = "sdk"
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.created_at is None:
            self.created_at = utc_now()
        if self.valid_from is None:
            self.valid_from = utc_now()
        if self.directionality not in ("directed", "symmetric"):
            raise ValueError(
                f"directionality must be 'directed' or 'symmetric', "
                f"got {self.directionality!r}"
            )
        if self.state not in MEMBERSHIP_STATES:
            raise ValueError(
                f"state must be one of {sorted(MEMBERSHIP_STATES)}, got {self.state!r}"
            )
        if self.derived_by not in DERIVATIONS:
            raise ValueError(
                f"derived_by must be one of {sorted(DERIVATIONS)}, "
                f"got {self.derived_by!r}"
            )
        if not self.relationship_type.strip():
            raise ValueError("relationship_type cannot be empty")
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("a relationship needs two entities, not one twice")


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

SCHEMA_FIELDS_SQLITE = """
CREATE TABLE IF NOT EXISTS pdm_field_memberships (
    id           TEXT PRIMARY KEY,
    user         TEXT NOT NULL DEFAULT 'default',
    entity_id    TEXT NOT NULL,
    field_id     TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT '',
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,
    state        TEXT NOT NULL DEFAULT 'active',
    derived_by   TEXT NOT NULL DEFAULT 'sdk',
    created_at   TEXT NOT NULL
);

-- One open membership per (entity, field, role). Overlapping windows are the
-- point of this table, but two *open* ones for the same role would make "is
-- this entity in this field" ambiguous with no way to tell which row is the
-- answer. Partial, so closed rows pile up freely.
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_fm_open
    ON pdm_field_memberships (user, entity_id, field_id, role)
    WHERE valid_to IS NULL;

-- The query this table exists for: who was in a field at an instant.
CREATE INDEX IF NOT EXISTS idx_pdm_fm_field_window
    ON pdm_field_memberships (user, field_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_pdm_fm_entity_window
    ON pdm_field_memberships (user, entity_id, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS pdm_relationships (
    id                 TEXT PRIMARY KEY,
    user               TEXT NOT NULL DEFAULT 'default',
    source_entity_id   TEXT NOT NULL,
    target_entity_id   TEXT NOT NULL,
    relationship_type  TEXT NOT NULL,
    directionality     TEXT NOT NULL DEFAULT 'directed',
    valid_from         TEXT NOT NULL,
    valid_to           TEXT,
    state              TEXT NOT NULL DEFAULT 'active',
    derived_by         TEXT NOT NULL DEFAULT 'sdk',
    created_at         TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pdm_rel_open
    ON pdm_relationships (user, source_entity_id, target_entity_id, relationship_type)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_pdm_rel_source
    ON pdm_relationships (user, source_entity_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_pdm_rel_target
    ON pdm_relationships (user, target_entity_id, valid_from, valid_to);
"""

SCHEMA_FIELDS_POSTGRES = SCHEMA_FIELDS_SQLITE.replace(
    "    user         TEXT", '    "user"       TEXT'
).replace(
    "    user               TEXT", '    "user"             TEXT'
).replace("(user, ", '("user", ')


def apply_field_migrations_sqlite(conn: Any) -> None:
    """Create the membership tables. Idempotent."""
    conn.executescript(SCHEMA_FIELDS_SQLITE)
    logger.debug("[PDM-Fields] Membership tables ready (sqlite)")


def apply_field_migrations_postgres(conn: Any) -> None:
    """Same, for PostgreSQL."""
    for statement in SCHEMA_FIELDS_POSTGRES.split(";"):
        if statement.strip():
            conn.execute(statement)
    logger.debug("[PDM-Fields] Membership tables ready (postgres)")


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------


def _as_stamp(value: Any) -> str | None:
    """
    A boundary already normalised by ``validate_interval`` arrives as a string;
    one set straight on the record arrives as a datetime. Both leave here in
    the single UTC spelling these TEXT columns sort by.
    """
    if value is None:
        return None
    return value if isinstance(value, str) else iso_utc(value)


def membership_from_row(row: Any) -> FieldMembershipRecord:
    return FieldMembershipRecord(
        id=row["id"],
        user=row["user"],
        entity_id=row["entity_id"],
        field_id=row["field_id"],
        role=row["role"],
        valid_from=_parse_dt(row["valid_from"]),
        valid_to=_parse_dt(row["valid_to"]),
        state=row["state"],
        derived_by=row["derived_by"],
        created_at=_parse_dt(row["created_at"]),
    )


def membership_insert_row(m: FieldMembershipRecord) -> tuple[Any, ...]:
    """Values for INSERT, in the column order the DDL above declares."""
    return (
        m.id,
        m.user,
        m.entity_id,
        m.field_id,
        m.role,
        _as_stamp(m.valid_from),
        _as_stamp(m.valid_to),
        m.state,
        m.derived_by,
        _as_stamp(m.created_at),
    )


def relationship_from_row(row: Any) -> RelationshipRecord:
    return RelationshipRecord(
        id=row["id"],
        user=row["user"],
        source_entity_id=row["source_entity_id"],
        target_entity_id=row["target_entity_id"],
        relationship_type=row["relationship_type"],
        directionality=row["directionality"],
        valid_from=_parse_dt(row["valid_from"]),
        valid_to=_parse_dt(row["valid_to"]),
        state=row["state"],
        derived_by=row["derived_by"],
        created_at=_parse_dt(row["created_at"]),
    )


def relationship_insert_row(r: RelationshipRecord) -> tuple[Any, ...]:
    return (
        r.id,
        r.user,
        r.source_entity_id,
        r.target_entity_id,
        r.relationship_type,
        r.directionality,
        _as_stamp(r.valid_from),
        _as_stamp(r.valid_to),
        r.state,
        r.derived_by,
        _as_stamp(r.created_at),
    )

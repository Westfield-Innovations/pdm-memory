"""
``FieldStore`` — writing and asking about memberships and relationships.

Every query here takes an instant. That is the whole point of the table: a
membership has a beginning and usually an end, so "who is in Work" is not a
question until you say when. Left unsaid it means now, which is a default, not
an assumption the caller has to share.

Windows overlap on purpose — a contractor is in Work and in Project Orion at
the same time — so nothing here treats a second membership as a correction of
the first. What cannot overlap is two *open* memberships for the same role in
the same field: that would make "is this entity in this field" answerable two
ways, and the partial unique index refuses it.
"""

from __future__ import annotations

import base64
import binascii
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pdm_memory.models import Trajectory

from pdm_memory.storage.event_hash import normalize_instant
from pdm_memory.storage.events import utc_now
from pdm_memory.storage.fields import (
    DISCOUNTED_STATES,
    LIVE_STATES,
    SignatureFieldMembershipRecord,
    FieldMembershipRecord,
    RelationshipRecord,
    membership_from_row,
    membership_insert_row,
    normalize_field_id,
    relationship_from_row,
    relationship_insert_row,
    signature_membership_from_row,
    signature_membership_insert_row,
    validate_interval,
)

logger = logging.getLogger(__name__)

__all__ = ["FieldStore", "TrajectoryCursorError"]

# Inclusive at the closing instant, matching Companion's
# ``valid_to__gte=at_time``. An exclusive end would put the boundary moment in
# one field on the client and another on the server — a disagreement nobody
# would think to look for.
_LIVE = ", ".join(f"'{s}'" for s in sorted(LIVE_STATES))

# trajectory()'s tie-break for two transitions sharing one instant — fixed and
# arbitrary, the same way the Companion read model's own kind order is: it
# only has to be stable across one cursor's lifetime, not meaningful.
_TRAJECTORY_KIND_ORDER: dict[str, int] = {
    "fact_filed": 0,
    "fact_unfiled": 1,
    "link_closed": 2,
    "link_opened": 3,
    "membership_closed": 4,
    "membership_opened": 5,
}

_TRAJECTORY_SEPARATOR = "|"
_TRAJECTORY_DEFAULT_PAGE_SIZE = 500


class TrajectoryCursorError(ValueError):
    """Raised when a trajectory cursor is malformed, truncated, or not ours."""


def _encode_trajectory_cursor(*, at: str, kind: str, row_id: str) -> str:
    raw = f"{at}{_TRAJECTORY_SEPARATOR}{kind}{_TRAJECTORY_SEPARATOR}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_trajectory_cursor(cursor: str) -> tuple[str, str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise TrajectoryCursorError("Cursor is not readable.") from exc

    parts = raw.split(_TRAJECTORY_SEPARATOR, 2)
    if len(parts) != 3:
        raise TrajectoryCursorError("Cursor is missing a component.")
    at, kind, row_id = parts
    if not at or kind not in _TRAJECTORY_KIND_ORDER or not row_id:
        raise TrajectoryCursorError("Cursor carries an unreadable component.")
    return at, kind, row_id


def _settle_on_write(record: Any, end: str | None, *, entered_field: bool) -> None:
    """
    Stamp a new row with the store's clock.

    ``valid_from`` and ``valid_to`` are whatever the caller says about the world
    and are often in the past; these say when the store took the row in. A
    pending or denied membership never entered the field and gets no
    ``settled_at``. A window written with its end already known settles that
    end in the same moment.
    """
    if entered_field:
        record.settled_at = record.created_at
    if end is not None:
        record.end_settled_at = record.created_at


class FieldStore:
    """
    Memberships and relationships on top of a signature driver.

    Mixed into the same drivers as ``EventStoreMixin`` and using its plumbing
    — ``_run``, ``_write``, ``_sql`` — so the dialect differences stay declared
    in one place rather than twice.
    """

    def supports_fields(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def add_field_membership(
        self,
        entity_id: str,
        field_id: str,
        valid_from: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        *,
        role: str = "",
        state: str = "active",
        derived_by: str = "sdk",
        user: str = "default",
    ) -> str:
        """
        Put an entity in a field for a window of time.

        Boundaries are validated before anything is written: an end at or
        before the start is refused rather than stored, and both instants are
        normalised to the one UTC spelling these TEXT columns sort by.

        Adding a second membership does not end the first. That is what lets an
        entity be in Work and Project Orion at once, which is the point of the
        table — an entity that could only be in one place would not need a
        window at all.
        """
        start, end = validate_interval(valid_from, valid_to)
        record = FieldMembershipRecord(
            user=user,
            entity_id=entity_id,
            field_id=normalize_field_id(field_id),
            role=role,
            state=state,
            derived_by=derived_by,
        )
        record.valid_from = start
        record.valid_to = end
        _settle_on_write(record, end, entered_field=state not in DISCOUNTED_STATES)

        self._write(
            """
            INSERT INTO pdm_field_memberships (
                id, {user}, entity_id, field_id, role,
                valid_from, valid_to, state, derived_by, created_at,
                settled_at, end_settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            # Built from the record rather than listed here: one place decides
            # the column order, so a column added to the table cannot go
            # missing from the tuple that fills it.
            membership_insert_row(record),
        )
        self._commit_if_idle(self._conn())

        # An open membership for this role may already stand. The index says so
        # rather than this code guessing, and the caller gets the one that
        # holds instead of a second row that would make the question ambiguous.
        if end is None:
            standing = self._run(
                "SELECT id FROM pdm_field_memberships "
                "WHERE {user} = ? AND entity_id = ? AND field_id = ? AND role = ? "
                "AND valid_to IS NULL LIMIT 1",
                (user, entity_id, record.field_id, role),
            ).fetchone()
            if standing is not None:
                return standing["id"]
        return record.id

    def file_signature_in_field(
        self,
        signature_id: str,
        field_id: str,
        valid_from: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        *,
        weight: float = 1.0,
        confidence: float = 1.0,
        derived_by: str = "sdk",
        user: str = "default",
    ) -> str:
        """
        File a fact in a field for a window of time.

        A fact is filed where it was said, which is not always where its
        subject belongs: something said about a colleague in a work chat sits
        in Work even though the colleague also sits in Personal. Companion
        scopes queries on this table rather than on the subject's memberships,
        so the SDK does too — filtering on a different basis would answer the
        same question differently on the two sides.
        """
        start, end = validate_interval(valid_from, valid_to)
        record = SignatureFieldMembershipRecord(
            user=user,
            signature_id=signature_id,
            field_id=normalize_field_id(field_id),
            weight=weight,
            confidence=confidence,
            derived_by=derived_by,
        )
        record.valid_from = start
        record.valid_to = end
        _settle_on_write(record, end, entered_field=True)

        self._write(
            """
            INSERT INTO pdm_signature_field_memberships (
                id, {user}, signature_id, field_id, weight, confidence,
                valid_from, valid_to, derived_by, created_at,
                settled_at, end_settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            signature_membership_insert_row(record),
        )
        self._commit_if_idle(self._conn())

        if end is None:
            standing = self._run(
                "SELECT id FROM pdm_signature_field_memberships "
                "WHERE {user} = ? AND signature_id = ? AND field_id = ? "
                "AND valid_to IS NULL LIMIT 1",
                (user, signature_id, record.field_id),
            ).fetchone()
            if standing is not None:
                return standing["id"]
        return record.id

    def unfile_signature(
        self,
        membership_id: str,
        at: datetime | str | None = None,
        *,
        user: str = "default",
    ) -> None:
        """Close a fact's membership. The row keeps saying it once held."""
        row = self._run(
            "SELECT * FROM pdm_signature_field_memberships "
            "WHERE id = ? AND {user} = ? LIMIT 1",
            (membership_id, user),
        ).fetchone()
        if row is None:
            raise KeyError(f"signature membership {membership_id!r} not found")
        if row["valid_to"] is not None:
            raise ValueError(
                f"signature membership {membership_id!r} already ended at "
                f"{row['valid_to']}"
            )
        closing = self._instant(at)
        if closing <= row["valid_from"]:
            raise ValueError(
                f"cannot end a membership at {closing}, before it began "
                f"({row['valid_from']})"
            )
        # `closing` is when the fact left the field; the second stamp is when the
        # store was told, which is now whatever `at` says.
        self._write(
            "UPDATE pdm_signature_field_memberships "
            "SET valid_to = ?, end_settled_at = ? "
            "WHERE id = ? AND {user} = ? AND valid_to IS NULL",
            (closing, self._instant(None), membership_id, user),
        )
        self._commit_if_idle(self._conn())

    def signature_fields(
        self,
        signature_id: str,
        at: datetime | str | None = None,
        *,
        user: str = "default",
    ) -> list[str]:
        """Which fields a fact was filed in at *at*."""
        moment = self._instant(at)
        rows = self._run(
            "SELECT DISTINCT field_id FROM pdm_signature_field_memberships "
            "WHERE {user} = ? AND signature_id = ? "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
            "ORDER BY field_id",
            (user, signature_id, moment, moment),
        ).fetchall()
        return [row["field_id"] for row in rows]

    def signature_memberships_of(
        self,
        signature_id: str,
        *,
        include_ended: bool = True,
        user: str = "default",
    ) -> list[SignatureFieldMembershipRecord]:
        """
        Every filing row for a fact — where it has been, not only where it is.

        The counterpart of ``memberships_of`` for entities, and the reason the
        rows are closed rather than deleted: "this fact used to sit in Project
        Orion" stays answerable after it stops sitting there.
        """
        query = (
            "SELECT * FROM pdm_signature_field_memberships "
            "WHERE {user} = ? AND signature_id = ?"
        )
        if not include_ended:
            query += " AND valid_to IS NULL"
        rows = self._run(
            query + " ORDER BY valid_from DESC, id", (user, signature_id)
        ).fetchall()
        return [signature_membership_from_row(row) for row in rows]

    def partition_signatures_by_field(
        self,
        signature_ids: list[str],
        field_id: str,
        at: datetime | str | None = None,
        *,
        user: str = "default",
    ) -> tuple[set[str], set[str]]:
        """
        Split these facts into (filed here, filed nowhere) at *at*.

        Companion's rule, in two sets rather than a correlated subquery:
        ``Exists(live_in_field) | ~Exists(has_any_live_membership)``. Anything
        in neither set is filed somewhere else and is that field's to withhold.

        "Filed nowhere" is no membership in force, not no membership row ever.
        Their docstring records getting that wrong first: a fact that once held
        a field and left it still has rows, so an emptiness test hides it — and
        it vanishes from every field except the one it no longer belongs to.
        """
        if not signature_ids:
            return set(), set()

        moment = self._instant(at)
        here: set[str] = set()
        filed_anywhere: set[str] = set()
        chunk = 500
        for start in range(0, len(signature_ids), chunk):
            batch = signature_ids[start : start + chunk]
            placeholders = ",".join("?" for _ in batch)
            rows = self._run(
                f"SELECT signature_id, field_id FROM pdm_signature_field_memberships "
                f"WHERE {{user}} = ? AND signature_id IN ({placeholders}) "
                f"AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)",
                (user, *batch, moment, moment),
            ).fetchall()
            for row in rows:
                filed_anywhere.add(row["signature_id"])
                if row["field_id"] == normalize_field_id(field_id):
                    here.add(row["signature_id"])

        return here, set(signature_ids) - filed_anywhere

    def end_field_membership(
        self,
        membership_id: str,
        at: datetime | str | None = None,
        *,
        state: str = "expired",
        user: str = "default",
    ) -> None:
        """
        Close a membership at an instant.

        The end is written onto the row that was open rather than the row being
        replaced, so the record of when someone was in a field survives their
        leaving it. Ending one that is already closed is refused: the second
        end would overwrite the first, and "when did this actually finish"
        would stop having an answer.
        """
        row = self._run(
            "SELECT * FROM pdm_field_memberships WHERE id = ? AND {user} = ? LIMIT 1",
            (membership_id, user),
        ).fetchone()
        if row is None:
            raise KeyError(f"membership {membership_id!r} not found")
        if row["valid_to"] is not None:
            raise ValueError(
                f"membership {membership_id!r} already ended at {row['valid_to']}"
            )

        closing = (
            normalize_instant(at) if at is not None else normalize_instant(utc_now())
        )
        if closing <= row["valid_from"]:
            raise ValueError(
                f"cannot end a membership at {closing}, before it began "
                f"({row['valid_from']})"
            )

        # See unfile_signature: `closing` is the world's end, the last stamp
        # the store's record of when it heard.
        self._write(
            "UPDATE pdm_field_memberships SET valid_to = ?, state = ?, end_settled_at = ? "
            "WHERE id = ? AND {user} = ? AND valid_to IS NULL",
            (closing, state, self._instant(None), membership_id, user),
        )
        self._commit_if_idle(self._conn())

    def link(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str,
        directionality: str = "directed",
        valid_from: datetime | str | None = None,
        valid_to: datetime | str | None = None,
        *,
        state: str = "active",
        derived_by: str = "sdk",
        user: str = "default",
    ) -> str:
        """
        Record that two entities stood in some relation for a window of time.

        ``directionality`` is load-bearing. "Alex manages Orion" read backwards
        is a different claim, so a directed link is followed one way only;
        a symmetric one — "colleague of" — is followed both ways from a single
        row rather than needing two that can fall out of step.
        """
        start, end = validate_interval(valid_from, valid_to)
        record = RelationshipRecord(
            user=user,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            relationship_type=relationship_type,
            directionality=directionality,
            state=state,
            derived_by=derived_by,
        )
        record.valid_from = start
        record.valid_to = end
        _settle_on_write(record, end, entered_field=state not in DISCOUNTED_STATES)

        self._write(
            """
            INSERT INTO pdm_relationships (
                id, {user}, source_entity_id, target_entity_id, relationship_type,
                directionality, valid_from, valid_to, state, derived_by, created_at,
                settled_at, end_settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            relationship_insert_row(record),
        )
        self._commit_if_idle(self._conn())

        if end is None:
            standing = self._run(
                "SELECT id FROM pdm_relationships "
                "WHERE {user} = ? AND source_entity_id = ? AND target_entity_id = ? "
                "AND relationship_type = ? AND valid_to IS NULL LIMIT 1",
                (user, source_entity_id, target_entity_id, relationship_type),
            ).fetchone()
            if standing is not None:
                return standing["id"]
        return record.id

    def end_relationship(
        self,
        relationship_id: str,
        at: datetime | str | None = None,
        *,
        state: str = "expired",
        user: str = "default",
    ) -> None:
        """Close a relationship. See :meth:`end_field_membership`."""
        row = self._run(
            "SELECT * FROM pdm_relationships WHERE id = ? AND {user} = ? LIMIT 1",
            (relationship_id, user),
        ).fetchone()
        if row is None:
            raise KeyError(f"relationship {relationship_id!r} not found")
        if row["valid_to"] is not None:
            raise ValueError(
                f"relationship {relationship_id!r} already ended at {row['valid_to']}"
            )

        closing = (
            normalize_instant(at) if at is not None else normalize_instant(utc_now())
        )
        if closing <= row["valid_from"]:
            raise ValueError(
                f"cannot end a relationship at {closing}, before it began "
                f"({row['valid_from']})"
            )

        self._write(
            "UPDATE pdm_relationships SET valid_to = ?, state = ?, end_settled_at = ? "
            "WHERE id = ? AND {user} = ? AND valid_to IS NULL",
            (closing, state, self._instant(None), relationship_id, user),
        )
        self._commit_if_idle(self._conn())

    # ------------------------------------------------------------------
    # Asking — always about an instant
    # ------------------------------------------------------------------

    @staticmethod
    def _instant(at: datetime | str | None) -> str:
        return normalize_instant(at) if at is not None else normalize_instant(utc_now())

    def fields_of(
        self,
        entity_id: str,
        at: datetime | str | None = None,
        *,
        user: str = "default",
    ) -> list[str]:
        """Which fields an entity was in at *at*. Several is normal."""
        moment = self._instant(at)
        rows = self._run(
            f"SELECT DISTINCT field_id FROM pdm_field_memberships "
            f"WHERE {{user}} = ? AND entity_id = ? AND state IN ({_LIVE}) "
            f"AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
            f"ORDER BY field_id",
            (user, entity_id, moment, moment),
        ).fetchall()
        return [row["field_id"] for row in rows]

    def members_of(
        self,
        field_id: str,
        at: datetime | str | None = None,
        *,
        user: str = "default",
    ) -> list[str]:
        """Which entities were in a field at *at*."""
        moment = self._instant(at)
        rows = self._run(
            f"SELECT DISTINCT entity_id FROM pdm_field_memberships "
            f"WHERE {{user}} = ? AND field_id = ? AND state IN ({_LIVE}) "
            f"AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?) "
            f"ORDER BY entity_id",
            (user, normalize_field_id(field_id), moment, moment),
        ).fetchall()
        return [row["entity_id"] for row in rows]

    def memberships_of(
        self,
        entity_id: str,
        *,
        include_ended: bool = True,
        user: str = "default",
    ) -> list[FieldMembershipRecord]:
        """Every membership row for an entity — the history, not the snapshot."""
        query = "SELECT * FROM pdm_field_memberships WHERE {user} = ? AND entity_id = ?"
        if not include_ended:
            query += " AND valid_to IS NULL"
        rows = self._run(
            query + " ORDER BY valid_from DESC, id", (user, entity_id)
        ).fetchall()
        return [membership_from_row(row) for row in rows]

    def related_entities(
        self,
        entity_id: str,
        at: datetime | str | None = None,
        *,
        relationship_type: str | None = None,
        user: str = "default",
    ) -> set[str]:
        """
        Entities reachable from this one by a live link at *at*.

        One hop, deliberately. Following the graph further would let a chain of
        unrelated links carry something from Personal into Work by transit,
        which is the leak this whole table exists to prevent — and depth is a
        product decision, not a default.
        """
        moment = self._instant(at)
        window = (
            f"state IN ({_LIVE}) AND valid_from <= ? "
            f"AND (valid_to IS NULL OR valid_to >= ?)"
        )
        type_clause = " AND relationship_type = ?" if relationship_type else ""

        params: list[Any] = [user, entity_id, moment, moment]
        if relationship_type:
            params.append(relationship_type)
        forward = self._run(
            f"SELECT target_entity_id AS other FROM pdm_relationships "
            f"WHERE {{user}} = ? AND source_entity_id = ? AND {window}{type_clause}",
            tuple(params),
        ).fetchall()

        params = [user, entity_id, moment, moment]
        if relationship_type:
            params.append(relationship_type)
        backward = self._run(
            f"SELECT source_entity_id AS other FROM pdm_relationships "
            f"WHERE {{user}} = ? AND target_entity_id = ? AND {window}{type_clause} "
            f"AND directionality = 'symmetric'",
            tuple(params),
        ).fetchall()

        return {row["other"] for row in [*forward, *backward]}

    def relationships_of(
        self,
        entity_id: str,
        *,
        include_ended: bool = True,
        user: str = "default",
    ) -> list[RelationshipRecord]:
        query = (
            "SELECT * FROM pdm_relationships WHERE {user} = ? "
            "AND (source_entity_id = ? OR target_entity_id = ?)"
        )
        if not include_ended:
            query += " AND valid_to IS NULL"
        rows = self._run(
            query + " ORDER BY valid_from DESC, id", (user, entity_id, entity_id)
        ).fetchall()
        return [relationship_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # What a query in a field is allowed to see
    # ------------------------------------------------------------------

    def entity_ids_for_signatures(
        self,
        signature_ids: list[str],
        *,
        user: str = "default",
    ) -> dict[str, str]:
        """
        Which entity each of these signatures is about, for those that say.

        Asked of the store rather than read off the records because
        ``SignatureRecord`` is a frozen dataclass and does not carry the
        column. One query for the whole candidate set, not one per record:
        scoping a recall must not cost a round trip per row.
        """
        if not signature_ids:
            return {}

        found: dict[str, str] = {}
        chunk = 500
        for start in range(0, len(signature_ids), chunk):
            batch = signature_ids[start : start + chunk]
            placeholders = ",".join("?" for _ in batch)
            rows = self._run(
                f"SELECT id, primary_entity_id FROM pdm_signatures "
                f"WHERE {{user}} = ? AND primary_entity_id IS NOT NULL "
                f"AND id IN ({placeholders})",
                (user, *batch),
            ).fetchall()
            for row in rows:
                found[row["id"]] = row["primary_entity_id"]
        return found

    def entities_with_no_live_membership(
        self,
        entity_ids: list[str],
        at: datetime | str | None = None,
        *,
        user: str = "default",
    ) -> set[str]:
        """
        Of these entities, the ones filed nowhere at *at*.

        "Nowhere" means no membership in force, not no membership row ever.
        Companion's scope filter records getting this wrong first: testing
        whether the relation is empty hides an entity that once belonged to a
        field and later left it, because it still has rows. It would then
        vanish from every field except the one it used to be in — the failure
        the clause exists to prevent, reached through history instead of
        absence.
        """
        if not entity_ids:
            return set()

        moment = self._instant(at)
        filed: set[str] = set()
        chunk = 500
        for start in range(0, len(entity_ids), chunk):
            batch = entity_ids[start : start + chunk]
            placeholders = ",".join("?" for _ in batch)
            rows = self._run(
                f"SELECT DISTINCT entity_id FROM pdm_field_memberships "
                f"WHERE {{user}} = ? AND entity_id IN ({placeholders}) "
                f"AND state IN ({_LIVE}) "
                f"AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)",
                (user, *batch, moment, moment),
            ).fetchall()
            filed |= {row["entity_id"] for row in rows}
        return set(entity_ids) - filed

    def entities_visible_in(
        self,
        field_id: str,
        at: datetime | str | None = None,
        *,
        follow_links: bool = True,
        user: str = "default",
    ) -> set[str]:
        """
        The entities a question asked inside *field_id* may return at *at*.

        Members of the field, plus whatever a live link reaches from them —
        the ticket's "unless an active relationship or joint membership
        exists". Both halves are evaluated at the same instant, so a link that
        ended yesterday stops granting anything today without anyone having to
        clean it up.
        """
        members = set(self.members_of(field_id, at, user=user))
        if not follow_links or not members:
            return members

        reachable: set[str] = set()
        for member in members:
            reachable |= self.related_entities(member, at, user=user)
        return members | reachable

    # ------------------------------------------------------------------
    # Trajectory — an ordered sequence of transitions (spec §7, §3)
    # ------------------------------------------------------------------

    def trajectory(
        self,
        subject_id: str,
        start: datetime | str,
        end: datetime | str,
        *,
        after: str | None = None,
        limit: int | None = None,
        user: str = "default",
    ) -> Trajectory:
        """
        The ordered transitions for a field or entity, in ``[start, end)``.

        Local-only counterpart to Companion's ``pdm.field_state.trajectory``,
        and simpler than it in two ways worth naming rather than silently
        matching:

        No self-view refusal. Companion's version requires the caller's own
        subject because one Django database holds many people's rows and a
        cross-subject grant story would have to exist to look past that.
        Here ``user`` already is the tenant boundary — a local store holds
        one person's data — so there is no second subject to leak.

        No ``grant_changed`` or ``projection_recorded`` / ``outcome_recorded``.
        This store has no perspective-log or projection table to produce them
        from; inventing local versions would be a claim about data this
        process never captured. A cloud-backed ``Memory`` reaches the full
        six-kind set through ``CloudDriver.trajectory`` instead.

        Field ids match exactly, not by prefix. Every other query in this
        file already treats ``field_id`` this way — there is no nested-field
        convention locally to extend, only one to not invent here alone.
        """
        from pdm_memory.models import Trajectory, TrajectoryStep

        start_s = normalize_instant(start)
        end_s = normalize_instant(end)
        if start_s >= end_s:
            raise ValueError(f"start ({start_s}) must be before end ({end_s}).")

        page_size = min(
            int(limit or _TRAJECTORY_DEFAULT_PAGE_SIZE), _TRAJECTORY_DEFAULT_PAGE_SIZE
        )
        cursor = _decode_trajectory_cursor(after) if after else None

        is_entity = subject_id.startswith(("subject:", "agent:"))

        candidates: list[dict[str, Any]] = []
        if is_entity:
            candidates += self._trajectory_membership_steps(
                entity_id=subject_id,
                field_id=None,
                user=user,
                start=start_s,
                end=end_s,
                cursor=cursor,
                page_size=page_size,
            )
            candidates += self._trajectory_link_steps(
                entity_id=subject_id,
                user=user,
                start=start_s,
                end=end_s,
                cursor=cursor,
                page_size=page_size,
            )
        else:
            candidates += self._trajectory_membership_steps(
                entity_id=None,
                field_id=subject_id,
                user=user,
                start=start_s,
                end=end_s,
                cursor=cursor,
                page_size=page_size,
            )
            candidates += self._trajectory_fact_steps(
                field_id=subject_id,
                user=user,
                start=start_s,
                end=end_s,
                cursor=cursor,
                page_size=page_size,
            )

        # No ref_id in the key: each source list already arrives ordered by
        # (at, id) straight out of SQL, and Python's sort is stable, so ties
        # on (at, kind) keep that order rather than being re-sorted by a
        # string comparison a caller's own WHERE clause does not make.
        candidates.sort(
            key=lambda item: (item["at"], _TRAJECTORY_KIND_ORDER[item["kind"]])
        )

        has_more = len(candidates) > page_size
        page = candidates[:page_size]

        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _encode_trajectory_cursor(
                at=last["at"], kind=last["kind"], row_id=last["ref_id"]
            )

        return Trajectory(
            subject_id=subject_id,
            start=start_s,
            end=end_s,
            steps=[TrajectoryStep.from_payload(item) for item in page],
            next_cursor=next_cursor,
        )

    def _trajectory_fetch(
        self,
        *,
        table: str,
        at_column: str,
        kind: str,
        extra_where: str,
        extra_params: tuple[Any, ...],
        start: str,
        end: str,
        cursor: tuple[str, str, str] | None,
        page_size: int,
        user: str,
    ) -> list[Any]:
        """
        Up to ``page_size + 1`` rows of one ``(table, at-column)`` pair,
        windowed to ``[start, end)`` and, when resuming, past ``cursor`` in
        the merged stream's own ``(at, kind, id)`` order — pushed into the
        query rather than filtered after the fact, so a later page costs the
        same as the first one.
        """
        where = (
            f"{{user}} = ? AND {extra_where} AND {at_column} >= ? AND {at_column} < ?"
        )
        params: list[Any] = [user, *extra_params, start, end]

        if cursor is not None:
            cursor_at, cursor_kind, cursor_id = cursor
            this_order = _TRAJECTORY_KIND_ORDER[kind]
            cursor_order = _TRAJECTORY_KIND_ORDER[cursor_kind]
            if this_order > cursor_order:
                where += f" AND {at_column} >= ?"
                params.append(cursor_at)
            elif this_order < cursor_order:
                where += f" AND {at_column} > ?"
                params.append(cursor_at)
            else:
                where += f" AND ({at_column} > ? OR ({at_column} = ? AND id > ?))"
                params.extend([cursor_at, cursor_at, cursor_id])

        rows = self._run(
            f"SELECT * FROM {table} WHERE {where} ORDER BY {at_column}, id LIMIT ?",
            (*params, page_size + 1),
        ).fetchall()
        return list(rows)

    def _trajectory_membership_steps(
        self,
        *,
        entity_id: str | None,
        field_id: str | None,
        user: str,
        start: str,
        end: str,
        cursor: tuple[str, str, str] | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        if entity_id is not None:
            extra_where, extra_params = "entity_id = ?", (entity_id,)
        else:
            extra_where, extra_params = "field_id = ?", (normalize_field_id(field_id),)

        steps: list[dict[str, Any]] = []
        for kind, at_column, closed_only in (
            ("membership_opened", "valid_from", False),
            ("membership_closed", "valid_to", True),
        ):
            where = extra_where + (" AND valid_to IS NOT NULL" if closed_only else "")
            rows = self._trajectory_fetch(
                table="pdm_field_memberships",
                at_column=at_column,
                kind=kind,
                extra_where=where,
                extra_params=extra_params,
                start=start,
                end=end,
                cursor=cursor,
                page_size=page_size,
                user=user,
            )
            for row in rows:
                at = (
                    row["valid_from"]
                    if kind == "membership_opened"
                    else row["valid_to"]
                )
                steps.append(
                    {
                        "at": at,
                        "kind": kind,
                        "ref_id": row["id"],
                        "field_id": row["field_id"],
                        "detail": {
                            "entity_id": row["entity_id"],
                            "field_id": row["field_id"],
                            "role": row["role"],
                            "state": row["state"],
                        },
                        "state_type": "measured",
                    }
                )
        return steps

    def _trajectory_fact_steps(
        self,
        *,
        field_id: str,
        user: str,
        start: str,
        end: str,
        cursor: tuple[str, str, str] | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for kind, at_column, closed_only in (
            ("fact_filed", "valid_from", False),
            ("fact_unfiled", "valid_to", True),
        ):
            where = "field_id = ?" + (
                " AND valid_to IS NOT NULL" if closed_only else ""
            )
            rows = self._trajectory_fetch(
                table="pdm_signature_field_memberships",
                at_column=at_column,
                kind=kind,
                extra_where=where,
                extra_params=(normalize_field_id(field_id),),
                start=start,
                end=end,
                cursor=cursor,
                page_size=page_size,
                user=user,
            )
            for row in rows:
                at = row["valid_from"] if kind == "fact_filed" else row["valid_to"]
                steps.append(
                    {
                        "at": at,
                        "kind": kind,
                        "ref_id": row["id"],
                        "field_id": row["field_id"],
                        "detail": {
                            "signature_id": row["signature_id"],
                            "field_id": row["field_id"],
                            "weight": row["weight"],
                            "confidence": row["confidence"],
                            "derived_by": row["derived_by"],
                        },
                        "state_type": "measured",
                    }
                )
        return steps

    def _trajectory_link_steps(
        self,
        *,
        entity_id: str,
        user: str,
        start: str,
        end: str,
        cursor: tuple[str, str, str] | None,
        page_size: int,
    ) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for kind, at_column, closed_only in (
            ("link_opened", "valid_from", False),
            ("link_closed", "valid_to", True),
        ):
            where = "(source_entity_id = ? OR target_entity_id = ?)" + (
                " AND valid_to IS NOT NULL" if closed_only else ""
            )
            rows = self._trajectory_fetch(
                table="pdm_relationships",
                at_column=at_column,
                kind=kind,
                extra_where=where,
                extra_params=(entity_id, entity_id),
                start=start,
                end=end,
                cursor=cursor,
                page_size=page_size,
                user=user,
            )
            for row in rows:
                at = row["valid_from"] if kind == "link_opened" else row["valid_to"]
                steps.append(
                    {
                        "at": at,
                        "kind": kind,
                        "ref_id": row["id"],
                        "field_id": "",
                        "detail": {
                            "source_entity_id": row["source_entity_id"],
                            "target_entity_id": row["target_entity_id"],
                            "relationship_type": row["relationship_type"],
                            "directionality": row["directionality"],
                            "derived_by": row["derived_by"],
                        },
                        "state_type": "measured",
                    }
                )
        return steps

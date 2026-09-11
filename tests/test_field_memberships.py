"""
TKT-102 — dynamic membership in overlapping fields.

The acceptance criteria, one class each, plus the boundary cases the ticket's
wording leaves open. AC2 is the one worth reading twice: isolation is not
proved by something failing to appear. It is proved by the same fact appearing
when a live link reaches it and disappearing when that link has ended.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pdm_memory import Memory
from pdm_memory.event_log import EventLog
from pdm_memory.storage.eventful_sqlite import EventfulSQLiteDriver

MARCH = datetime(2026, 3, 1, tzinfo=timezone.utc)
JUNE = datetime(2026, 6, 1, tzinfo=timezone.utc)
AUGUST = datetime(2026, 8, 1, tzinfo=timezone.utc)
OCTOBER = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture()
def log(tmp_path):
    mem = Memory(storage=EventfulSQLiteDriver(db_path=str(tmp_path / "f.db")))
    yield EventLog(mem)
    mem.close()


def person(log: EventLog, name: str, field: str) -> str:
    return log.mention(name, field_id=field, signature_id=f"s-{name}-{field}")


class TestAC1OverlappingMembership:
    """
    An entity can belong to Work, Personal and Project at once, in overlapping
    or distinct windows.
    """

    def test_three_fields_at_the_same_time(self, log):
        alex = person(log, "Alex", "work")
        log.add_field_membership(alex, "work", MARCH)
        log.add_field_membership(alex, "personal", MARCH)
        log.add_field_membership(alex, "project/orion", MARCH)

        assert log.fields_of(alex, JUNE) == ["personal", "project/orion", "work"]

    def test_distinct_windows_answer_for_their_own_time(self, log):
        alex = person(log, "Alex", "work")
        log.add_field_membership(alex, "work", MARCH)
        log.add_field_membership(alex, "project/orion", MARCH, AUGUST)

        assert "project/orion" in log.fields_of(alex, JUNE)
        assert "project/orion" not in log.fields_of(alex, OCTOBER), (
            "a window that closed in August must not answer for October"
        )
        assert log.fields_of(alex, OCTOBER) == ["work"]

    def test_a_question_about_the_past_gets_the_past(self, log):
        alex = person(log, "Alex", "work")
        log.add_field_membership(alex, "project/orion", MARCH, AUGUST)

        assert log.members_of("project/orion", JUNE) == [alex]
        assert log.members_of("project/orion", OCTOBER) == []
        assert log.members_of("project/orion", datetime(2026, 1, 1, tzinfo=timezone.utc)) == []

    def test_a_second_membership_does_not_end_the_first(self, log):
        alex = person(log, "Alex", "work")
        first = log.add_field_membership(alex, "work", MARCH)
        log.add_field_membership(alex, "personal", JUNE)

        standing = [m for m in log._storage.memberships_of(alex) if m.valid_to is None]
        assert len(standing) == 2
        assert first in {m.id for m in standing}

    def test_one_open_membership_per_role(self, log):
        """Two open rows for the same role would make the question ambiguous."""
        alex = person(log, "Alex", "work")
        first = log.add_field_membership(alex, "work", MARCH, role="member")
        again = log.add_field_membership(alex, "work", JUNE, role="member")
        assert first == again

        other_role = log.add_field_membership(alex, "work", JUNE, role="owner")
        assert other_role != first


class TestAC2NoCrossFieldLeak:
    """
    Queries in one field do not return entities from another without an
    explicit, active link.
    """

    def _two_alexes(self, log):
        work = log.mention("Alex", field_id="work", signature_id="s1")
        home = log.mention("Alex", field_id="family", signature_id="s2")
        log.add_field_membership(work, "work", MARCH)
        log.add_field_membership(home, "family", MARCH)

        # The same tags on both, so one query couples to both: a filter can
        # only be shown to remove something that would otherwise be returned.
        tags = ["alex", "meeting", "schedule"]
        w = log.ingest(
            event=log.event(raw_reference="chat:work"),
            payload="Release review with Alex.",
            facts=[{"text": "Alex reviewed the Orion release",
                    "tags": tags, "about": "Alex"}],
            field_id="work",
        )
        f = log.ingest(
            event=log.event(raw_reference="chat:family"),
            payload="Lunch with Alex.",
            facts=[{"text": "Lunch with Alex on Sunday",
                    "tags": tags, "about": "Alex"}],
            field_id="family",
        )
        return w["entity_ids"]["Alex"], f["entity_ids"]["Alex"]

    QUERY = "alex meeting schedule"

    def test_a_work_question_does_not_return_family(self, log):
        work_alex, family_alex = self._two_alexes(log)
        log.add_field_membership(work_alex, "work", MARCH)
        log.add_field_membership(family_alex, "family", MARCH)

        found = [h.text for h in log.recall(self.QUERY, k=10, field="work", at=JUNE)]
        assert "Alex reviewed the Orion release" in found
        assert "Lunch with Alex on Sunday" not in found, "family leaked into work"

    def test_an_active_link_lets_it_through(self, log):
        """The other half of the claim: isolation that nothing can cross is a wall."""
        work_alex, family_alex = self._two_alexes(log)
        log.add_field_membership(work_alex, "work", MARCH)
        log.add_field_membership(family_alex, "family", MARCH)

        log.link(work_alex, family_alex, "same_person", "symmetric", MARCH)

        found = [h.text for h in log.recall(self.QUERY, k=10, field="work", at=JUNE)]
        assert "Lunch with Alex on Sunday" in found

    def test_a_link_that_has_ended_grants_nothing(self, log):
        work_alex, family_alex = self._two_alexes(log)
        log.add_field_membership(work_alex, "work", MARCH)
        log.add_field_membership(family_alex, "family", MARCH)
        log.link(work_alex, family_alex, "same_person", "symmetric", MARCH, AUGUST)

        during = [h.text for h in log.recall(self.QUERY, k=10, field="work", at=JUNE)]
        after = [h.text for h in log.recall(self.QUERY, k=10, field="work", at=OCTOBER)]
        assert "Lunch with Alex on Sunday" in during
        assert "Lunch with Alex on Sunday" not in after, (
            "a link that expired in August still granted access in October"
        )

    def test_direction_is_respected(self, log):
        work_alex, family_alex = self._two_alexes(log)
        log.add_field_membership(work_alex, "work", MARCH)
        log.add_field_membership(family_alex, "family", MARCH)
        # family -> work, directed. Asking inside work must not follow it back.
        log.link(family_alex, work_alex, "mentions", "directed", MARCH)

        found = [h.text for h in log.recall(self.QUERY, k=10, field="work", at=JUNE)]
        assert "Lunch with Alex on Sunday" not in found

    def test_facts_about_nobody_stay_visible(self, log):
        """
        A fact with no entity is in no field, so it is not another field's to
        leak — and dropping it would empty most of a store the first time
        anyone scoped a query.
        """
        log.ingest(
            event=log.event(raw_reference="chat:general"),
            payload="Deploys go out on Fridays.",
            facts=[{"text": "Deploys go out on Fridays",
                    "tags": ["deploy", "friday", "process"]}],
            field_id="work",
        )
        found = [
            h.text
            for h in log.recall("deploy friday process", k=10, field="work", at=JUNE)
        ]
        assert "Deploys go out on Fridays" in found

    def test_unscoped_recall_is_untouched(self, log):
        """Installing this must not change what an ordinary recall returns."""
        self._two_alexes(log)
        plain = log._memory.recall(self.QUERY, k=10)
        assert len(plain) >= 2


class TestAC3BoundaryValidation:
    """link() and add_field_membership() validate and write timestamp bounds."""

    def test_an_end_before_the_start_is_refused(self, log):
        alex = person(log, "Alex", "work")
        with pytest.raises(ValueError, match="must be after"):
            log.add_field_membership(alex, "work", AUGUST, MARCH)

    def test_a_zero_length_window_is_refused(self, log):
        alex = person(log, "Alex", "work")
        with pytest.raises(ValueError, match="must be after"):
            log.add_field_membership(alex, "work", MARCH, MARCH)

    def test_bounds_are_stored_in_one_utc_spelling(self, log):
        import sqlite3

        alex = person(log, "Alex", "work")
        offset = datetime(2026, 3, 1, 3, 0, tzinfo=timezone(timedelta(hours=3)))
        log.add_field_membership(alex, "work", offset, AUGUST)

        raw = sqlite3.connect(log._storage.db_path)
        stamps = [
            r[0]
            for r in raw.execute(
                "SELECT valid_from FROM pdm_field_memberships "
                "UNION ALL SELECT valid_to FROM pdm_field_memberships "
                "UNION ALL SELECT created_at FROM pdm_field_memberships"
            )
            if r[0]
        ]
        raw.close()
        assert all(s.endswith("Z") for s in stamps), stamps
        assert stamps[0] == "2026-03-01T00:00:00.000000Z", "offset was not normalised"

    def test_a_malformed_field_id_is_refused(self, log):
        alex = person(log, "Alex", "work")
        for bad in ("Work", "work//orion", "work/", ""):
            with pytest.raises(ValueError, match="field_id"):
                log.add_field_membership(alex, bad, MARCH)

    def test_link_validates_its_own_bounds(self, log):
        a = person(log, "Alex", "work")
        b = person(log, "Bohdan", "work")
        with pytest.raises(ValueError, match="must be after"):
            log.link(a, b, "colleague", "symmetric", AUGUST, MARCH)

    def test_a_relationship_needs_two_entities(self, log):
        a = person(log, "Alex", "work")
        with pytest.raises(ValueError, match="two entities"):
            log.link(a, a, "self", "directed", MARCH)

    def test_an_unknown_directionality_is_refused(self, log):
        a = person(log, "Alex", "work")
        b = person(log, "Bohdan", "work")
        with pytest.raises(ValueError, match="directionality"):
            log.link(a, b, "colleague", "sideways", MARCH)

    def test_ending_twice_is_refused(self, log):
        alex = person(log, "Alex", "work")
        membership = log.add_field_membership(alex, "work", MARCH)
        log.end_membership(membership, AUGUST)
        with pytest.raises(ValueError, match="already ended"):
            log.end_membership(membership, OCTOBER)

    def test_ending_before_the_start_is_refused(self, log):
        alex = person(log, "Alex", "work")
        membership = log.add_field_membership(alex, "work", JUNE)
        with pytest.raises(ValueError, match="before it began"):
            log.end_membership(membership, MARCH)

    def test_ending_keeps_the_history(self, log):
        alex = person(log, "Alex", "work")
        membership = log.add_field_membership(alex, "work", MARCH)
        log.end_membership(membership, AUGUST)

        assert log.fields_of(alex, OCTOBER) == []
        assert log.fields_of(alex, JUNE) == ["work"], (
            "ending a membership must not erase that it existed"
        )


class TestMatchesTheServerSemantics:
    """
    The same question asked of the SDK and of Companion has to get the same
    answer. These pin the three places mine differed from
    ``pdm/field_resonance/scope.py`` and ``fields.py``, which have been in
    service on the server side and have already been through the failures
    their docstrings describe.
    """

    def test_only_pending_and_denied_are_discounted(self, log):
        """
        The server excludes exactly those two, "neither of which ever
        represented real membership". Everything else — expired, revoked,
        historical — was true for its window, and the window is what decides
        whether it is true now.
        """
        alex = person(log, "Alex", "work")
        log._storage.add_field_membership(
            alex, "work", MARCH, AUGUST, state="revoked", user="default"
        )
        assert log.fields_of(alex, JUNE) == ["work"], (
            "a revoked membership was still real while it lasted"
        )
        assert log.fields_of(alex, OCTOBER) == []

    def test_a_pending_membership_is_not_membership(self, log):
        alex = person(log, "Alex", "work")
        log._storage.add_field_membership(
            alex, "work", MARCH, state="pending", user="default"
        )
        assert log.fields_of(alex, JUNE) == []

    def test_a_denied_membership_is_not_membership(self, log):
        alex = person(log, "Alex", "work")
        log._storage.add_field_membership(
            alex, "work", MARCH, state="denied", user="default"
        )
        assert log.fields_of(alex, JUNE) == []

    def test_the_closing_instant_is_still_inside(self, log):
        """
        The server tests ``valid_to >= at_time``. An exclusive end here would
        put the boundary instant in one field on the client and another on the
        server — a disagreement nobody would think to look for.
        """
        alex = person(log, "Alex", "work")
        log.add_field_membership(alex, "work", MARCH, AUGUST)
        assert log.fields_of(alex, AUGUST) == ["work"]
        assert log.fields_of(alex, AUGUST + timedelta(microseconds=1)) == []

    def test_an_entity_with_no_live_membership_is_visible_everywhere(self, log):
        """
        The server's rule is "no LIVE membership anywhere", not "no membership
        rows ever" — and its docstring records getting this wrong first. An
        entity that was in Project and left has no live membership, so its
        facts stop being any one field's to withhold rather than vanishing
        from every field but the one it used to be in.
        """
        log.mention("Alex", field_id="work", signature_id="s1")
        result = log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="x",
            facts=[{"text": "Alex reviewed the release",
                    "tags": ["alex", "meeting", "schedule"], "about": "Alex"}],
            field_id="work",
        )
        entity = result["entity_ids"]["Alex"]

        membership = log.add_field_membership(entity, "project/orion", MARCH)
        log.end_membership(membership, AUGUST)

        found = [
            h.text
            for h in log.recall("alex meeting schedule", k=10, field="work", at=OCTOBER)
        ]
        assert "Alex reviewed the release" in found, (
            "an entity that left its only field disappeared instead of becoming "
            "unfiled"
        )

    def test_an_entity_live_elsewhere_is_withheld(self, log):
        """The other half: a live membership somewhere else does withhold it."""
        result = log.ingest(
            event=log.event(raw_reference="chat:2"),
            payload="y",
            facts=[{"text": "Lunch with Alex on Sunday",
                    "tags": ["alex", "meeting", "schedule"], "about": "Alex"}],
            field_id="family",
        )
        entity = result["entity_ids"]["Alex"]
        log.add_field_membership(entity, "family", MARCH)

        found = [
            h.text
            for h in log.recall("alex meeting schedule", k=10, field="work", at=JUNE)
        ]
        assert "Lunch with Alex on Sunday" not in found


class TestFactLevelFiling:
    """
    Companion scopes on the fact's own membership, not its subject's. A fact is
    filed where it was said: something said about a colleague in a work chat
    belongs to Work even though the colleague also belongs to Personal.
    """

    def test_ingest_files_what_it_writes(self, log):
        result = log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="x",
            facts=[{"text": "Orion ships Friday", "tags": ["orion", "ship", "friday"]}],
            field_id="work",
        )
        assert log.fact_fields(result["signature_ids"][0]) == ["work"]

    def test_a_fact_filed_elsewhere_is_withheld(self, log):
        """The case entity-based filtering could not express."""
        tags = ["alex", "meeting", "schedule"]
        log.ingest(
            event=log.event(raw_reference="chat:w"),
            payload="w",
            facts=[{"text": "Alex reviewed the release", "tags": tags}],
            field_id="work",
        )
        log.ingest(
            event=log.event(raw_reference="chat:f"),
            payload="f",
            facts=[{"text": "Lunch with Alex on Sunday", "tags": tags}],
            field_id="family",
        )

        found = [h.text for h in log.recall("alex meeting schedule", k=10, field="work")]
        assert "Alex reviewed the release" in found
        assert "Lunch with Alex on Sunday" not in found

    def test_one_fact_can_sit_in_two_fields(self, log):
        result = log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="x",
            facts=[{"text": "Orion ships Friday", "tags": ["orion", "ship", "friday"]}],
            field_id="work",
        )
        sig = result["signature_ids"][0]
        log.file_fact(sig, "project/orion")

        assert log.fact_fields(sig) == ["project/orion", "work"]
        for field in ("work", "project/orion"):
            found = [h.text for h in log.recall("orion ship friday", k=5, field=field)]
            assert "Orion ships Friday" in found

    def test_filing_is_temporal(self, log):
        result = log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="x",
            facts=[{"text": "Orion ships Friday", "tags": ["orion", "ship", "friday"]}],
            field_id="work",
        )
        sig = result["signature_ids"][0]
        log.file_fact(sig, "project/orion", MARCH, AUGUST)

        assert "project/orion" in log.fact_fields(sig, JUNE)
        assert "project/orion" not in log.fact_fields(sig, OCTOBER)

    def test_a_fact_that_left_its_only_field_becomes_unfiled(self, log):
        """
        Companion's rule, on facts: no membership in force, not no row ever. A
        fact that left its only field stops being anyone's to withhold instead
        of vanishing from every field but that one.
        """
        result = log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="x",
            facts=[{"text": "Orion ships Friday", "tags": ["orion", "ship", "friday"]}],
            field_id="work",
        )
        sig = result["signature_ids"][0]
        # ingest files at the moment it writes, so the close has to come after
        # that rather than at a date from the fixtures above.
        membership = log._storage._run(
            "SELECT id FROM pdm_signature_field_memberships WHERE signature_id = ?",
            (sig,),
        ).fetchone()["id"]
        closed_at = datetime.now(timezone.utc) + timedelta(days=1)
        log.unfile_fact(membership, closed_at)

        found = [
            h.text
            for h in log.recall(
                "orion ship friday", k=5, field="family",
                at=closed_at + timedelta(days=1),
            )
        ]
        assert "Orion ships Friday" in found

    def test_one_live_row_per_fact_per_field(self, log):
        result = log.ingest(
            event=log.event(raw_reference="chat:1"),
            payload="x",
            facts=[{"text": "Orion ships Friday", "tags": ["orion", "ship", "friday"]}],
            field_id="work",
        )
        sig = result["signature_ids"][0]
        first = log.file_fact(sig, "work")
        again = log.file_fact(sig, "work")
        assert first == again


class TestBothDialectsAgree:
    """
    The Postgres DDL is derived from the SQLite one so a column cannot be added
    to one and forgotten in the other. These check the derivation actually did
    its job — the first version matched fixed-width column prefixes and let a
    whole table through unquoted, which failed only on a real server.
    """

    BARE_USER = __import__("re").compile(r'(?<!")\buser\b(?!")')

    def _strip_comments(self, sql: str) -> str:
        return "\n".join(
            line for line in sql.splitlines() if not line.strip().startswith("--")
        )

    def test_postgres_quotes_every_user(self):
        from pdm_memory.storage.fields import SCHEMA_FIELDS_POSTGRES

        leaked = self.BARE_USER.findall(self._strip_comments(SCHEMA_FIELDS_POSTGRES))
        assert not leaked, f"{len(leaked)} unquoted `user` would fail on the server"

    def test_sqlite_leaves_it_bare(self):
        from pdm_memory.storage.fields import SCHEMA_FIELDS_SQLITE

        assert self.BARE_USER.search(self._strip_comments(SCHEMA_FIELDS_SQLITE))

    def test_both_declare_the_same_tables_and_indexes(self):
        import re

        from pdm_memory.storage.fields import (
            SCHEMA_FIELDS_POSTGRES,
            SCHEMA_FIELDS_SQLITE,
        )

        for pattern in (
            r"CREATE TABLE IF NOT EXISTS (\w+)",
            r"INDEX IF NOT EXISTS (\w+)",
        ):
            assert set(re.findall(pattern, SCHEMA_FIELDS_SQLITE)) == set(
                re.findall(pattern, SCHEMA_FIELDS_POSTGRES)
            )

    def test_every_table_has_the_same_column_count(self):
        import re

        from pdm_memory.storage.fields import (
            SCHEMA_FIELDS_POSTGRES,
            SCHEMA_FIELDS_SQLITE,
        )

        def columns(ddl: str) -> dict[str, int]:
            out = {}
            for name, body in re.findall(
                r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", ddl, re.S
            ):
                out[name] = len(
                    [
                        line
                        for line in body.splitlines()
                        if line.strip() and not line.strip().startswith("--")
                    ]
                )
            return out

        assert columns(SCHEMA_FIELDS_SQLITE) == columns(SCHEMA_FIELDS_POSTGRES)

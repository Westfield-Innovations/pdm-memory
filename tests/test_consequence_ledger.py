"""The consequence ledger: prepare, attach_action, observe and its history."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.guarded_effects import IdReuseError, ResourceLimitError
from tests.consequence_fixtures import (
    MOVED,
    POLICY,
    TEMP,
    VALVE,
    _guard,
    _policy,
    _request,
)


@pytest.fixture
def ledger(tmp_path):
    with ConsequenceLedger(tmp_path / "c.db", owner="owner-1", policy=POLICY) as led:
        yield led


@pytest.fixture
def guard(tmp_path):
    with _guard(tmp_path) as g:
        yield g


def _kinds(ledger, expectation_id=None):
    return [e["kind"] for e in ledger.history(expectation_id)]


def test_prepare_returns_the_id_and_is_idempotent(ledger):
    assert ledger.prepare(VALVE) == "exp-valve-1"
    assert ledger.prepare(VALVE) == "exp-valve-1"
    assert _kinds(ledger, "exp-valve-1") == ["EXPECTATION_COMMITTED"]


def test_an_id_cannot_be_reused_with_changed_content(ledger):
    ledger.prepare(VALVE)
    with pytest.raises(IdReuseError):
        ledger.prepare(replace(VALVE, predictor_version="2"))


def test_one_action_has_one_expectation(ledger):
    ledger.prepare(VALVE)
    with pytest.raises(IdReuseError, match="action"):
        ledger.prepare(replace(VALVE, expectation_id="exp-valve-2"))


def test_a_criterion_needs_an_owner_registered_comparator(ledger):
    bad = replace(VALVE, criteria=(replace(MOVED, comparator=("fuzzy", "1")),))
    with pytest.raises(ValueError, match="comparator"):
        ledger.prepare(bad)


def test_a_memory_path_is_refused():
    with pytest.raises(ValueError, match="durable"):
        ConsequenceLedger(":memory:", owner="o", policy=POLICY)


def test_payloads_are_bounded(tmp_path):
    small = _policy(limits=replace(POLICY.limits, max_payload_bytes=256))
    with (
        ConsequenceLedger(tmp_path / "s.db", owner="o", policy=small) as led,
        pytest.raises(ResourceLimitError),
    ):
        led.prepare(VALVE)


def test_an_observation_revision_names_what_it_supersedes(ledger):
    assert ledger.observe(TEMP) == ("obs-temp", 1)
    assert ledger.observe(TEMP) == ("obs-temp", 1)
    with pytest.raises(ValueError, match="supersedes"):
        ledger.observe(replace(TEMP, revision=2))
    with pytest.raises(ValueError, match="supersedes"):
        ledger.observe(replace(TEMP, revision=3, supersedes=2))
    assert ledger.observe(replace(TEMP, revision=2, supersedes=1, value=21.5)) == ("obs-temp", 2)
    with pytest.raises(IdReuseError):
        ledger.observe(replace(TEMP, value=99.0))
    (latest,) = ledger.observations("room_temp_c")
    assert (latest.revision, latest.value) == (2, 21.5)


def test_a_first_revision_supersedes_nothing(ledger):
    with pytest.raises(ValueError, match="supersedes"):
        ledger.observe(replace(TEMP, supersedes=1))


def test_an_action_must_be_the_expectations_own(ledger, guard):
    ledger.prepare(VALVE)
    receipt = guard.stage(_request("other-action"))
    with pytest.raises(ValueError, match="action"):
        ledger.attach_action("exp-valve-1", receipt)


def test_a_repeated_receipt_is_recorded_once(ledger, guard):
    ledger.prepare(VALVE)
    receipt = guard.stage(_request("act-valve-1"))
    ledger.attach_action("exp-valve-1", receipt)
    ledger.attach_action("exp-valve-1", receipt)
    assert _kinds(ledger, "exp-valve-1").count("ACTION_ATTACHED") == 1
    assert [a["status"] for a in ledger.actions("exp-valve-1")] == ["HELD"]


def test_release_waits_for_a_retained_expectation(ledger, tmp_path):
    calls = []
    (tmp_path / "g").mkdir()
    with _guard(tmp_path / "g", check=ledger.prepared_check, handler=calls.append) as guard:
        guard.stage(_request("act-valve-1"))
        assert guard.release("act-valve-1").status == "HELD"
        assert calls == []
        ledger.prepare(VALVE)
        assert guard.release("act-valve-1").status == "RETURNED"
        assert len(calls) == 1


def test_history_is_append_only_and_chained(ledger):
    ledger.prepare(VALVE)
    ledger.observe(TEMP)
    with pytest.raises(sqlite3.DatabaseError):
        ledger._conn.execute("DELETE FROM cl_events")
    with pytest.raises(sqlite3.DatabaseError):
        ledger._conn.execute("UPDATE cl_events SET kind='X'")
    assert ledger.verify_history()
    first, second = ledger.history()
    assert second["previous_hash"] == first["event_hash"]
    assert ledger.history(after_sequence=first["sequence"]) == [second]


def test_records_survive_a_reopen(tmp_path):
    with ConsequenceLedger(tmp_path / "c.db", owner="o", policy=POLICY) as a:
        a.prepare(VALVE)
        a.observe(TEMP)
    with ConsequenceLedger(tmp_path / "c.db", owner="o", policy=POLICY) as b:
        assert b.expectation("exp-valve-1")[0] == VALVE
        assert b.observations() == [TEMP]
        assert b.prepare(VALVE) == "exp-valve-1"
        assert len(b.history()) == 2

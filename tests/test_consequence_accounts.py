"""The four consequence accounts and the next useful observation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import (
    Account,
    Comparison,
    ConsequenceEntry,
    InfluencePath,
    QuantityStatus,
    compare_to_capacity,
)
from pdm_memory.guarded_effects import IdReuseError
from tests.consequence_fixtures import (
    COMMIT,
    END,
    HORIZON,
    MINUTE,
    POLICY,
    POSITION_MOVED,
    TASK,
    TEMP,
    VALVE,
    _source,
)


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**policy_changes):
        policy = replace(POLICY, **policy_changes)
        led = ConsequenceLedger(tmp_path / f"c{len(made)}.db", owner="owner-1", policy=policy,
                                clock=lambda: COMMIT)
        made.append(led)
        led.prepare(VALVE)
        return led

    yield build
    for led in made:
        led.close()


@pytest.fixture
def ledger(make):
    return make()


def _entry(account, quantity="1", unit="s", n=0, **changes):
    status = QuantityStatus.UNKNOWN if quantity is None else QuantityStatus.MEASURED
    fields = {
        "entry_id": f"entry-{account.value}-{unit}-{n}",
        "expectation_id": "exp-valve-1",
        "account": account,
        "subject": "room 4",
        "quantity": None if quantity is None else Decimal(quantity),
        "unit": unit,
        "horizon_end": END,
        "source": "controller-log",
        "status": status,
        **changes,
    }
    return ConsequenceEntry(**fields)


# --- accounts ---------------------------------------------------------------


def test_accounts_stay_separate_without_exchange_rules(ledger):
    ledger.record_consequence(_entry(Account.AGENT_OPERATION, "2.5", "s"))
    ledger.record_consequence(_entry(Account.PRINCIPAL_BURDEN, "1", "interruption"))
    s = ledger.accounts("exp-valve-1")
    assert s.combined is None and "exchange" in s.combined_reason
    assert s.totals[(Account.AGENT_OPERATION, "s", QuantityStatus.MEASURED)] == Decimal("2.5")
    assert s.totals[(Account.PRINCIPAL_BURDEN, "interruption", QuantityStatus.MEASURED)] == 1


def test_owner_exchange_rules_combine_compatible_quantities(make):
    ledger = make(exchange_rules={"s": Decimal(1), "interruption": Decimal(60)})
    ledger.record_consequence(_entry(Account.AGENT_OPERATION, "2.5", "s"))
    ledger.record_consequence(_entry(Account.PRINCIPAL_BURDEN, "1", "interruption"))
    assert ledger.accounts("exp-valve-1").combined == Decimal("62.5")


def test_unknown_burden_is_not_zero(ledger):
    ledger.record_consequence(_entry(Account.PRINCIPAL_BURDEN, None, "min"))
    s = ledger.accounts("exp-valve-1")
    assert s.unknown == {Account.PRINCIPAL_BURDEN: 1}
    assert s.totals == {} and s.combined is None and "unknown" in s.combined_reason


def test_a_boundary_violation_cannot_be_bought_back(make):
    ledger = make(exchange_rules={"s": Decimal(1)})
    ledger.record_consequence(_entry(Account.TASK_RESULT, "-1000", "s", boundary_violation=True))
    ledger.record_consequence(_entry(Account.AGENT_OPERATION, "-5", "s", n=1))
    s = ledger.accounts("exp-valve-1")
    assert s.combined is None and "hard boundary" in s.combined_reason


def test_a_coupled_effect_needs_its_influence_path():
    with pytest.raises(ValueError, match="path"):
        _entry(Account.COUPLED_EFFECT)
    with pytest.raises(ValueError, match="evidence"):
        _entry(Account.COUPLED_EFFECT, path=InfluencePath("declared", "boiler loop", ()))
    path = InfluencePath("hypothesized", "boiler loop", ("obs-boiler-1",))
    assert _entry(Account.COUPLED_EFFECT, path=path).path.kind == "hypothesized"


def test_an_unknown_quantity_has_no_value():
    with pytest.raises(ValueError, match="UNKNOWN"):
        _entry(Account.AGENT_OPERATION, status=QuantityStatus.UNKNOWN)


def test_an_entry_is_idempotent_and_needs_its_expectation(ledger):
    entry = _entry(Account.AGENT_OPERATION)
    assert ledger.record_consequence(entry) == ledger.record_consequence(entry)
    with pytest.raises(IdReuseError):
        ledger.record_consequence(replace(entry, quantity=Decimal(9)))
    with pytest.raises(KeyError):
        ledger.record_consequence(replace(entry, entry_id="e2", expectation_id="nowhere"))


@pytest.mark.parametrize(
    ("remaining", "unit", "status"),
    [
        (None, "h", "UNKNOWN_CAPACITY"),
        (Decimal(0), "h", "ZERO_CAPACITY"),
        (Decimal(5), "min", "INCOMPATIBLE_UNITS"),
        (Decimal(5), "h", "WITHIN"),
        (Decimal(1), "h", "EXCEEDS"),
    ],
)
def test_capacity_is_compared_only_when_declared(remaining, unit, status):
    assert compare_to_capacity(Decimal(2), "h", remaining, unit).status == status


# --- observation proposals --------------------------------------------------


def _resolve_movement(ledger):
    ledger.observe(POSITION_MOVED)
    return ledger.evaluate("exp-valve-1", task=TASK)


def test_an_unresolved_effect_gets_a_bounded_proposal(ledger):
    _resolve_movement(ledger)
    (p,) = ledger.suggest_observation("exp-valve-1", now=COMMIT + MINUTE)
    assert (p.criterion, p.status, p.deadline) == ("room_temp", "OPEN", HORIZON.latest_decision_point)
    assert [s.name for s in p.sources] == ["room-sensor", "wall-probe"]  # cheapest first
    assert (p.sources[0].cost, p.sources[0].burden, p.sources[0].latency_s) == (1, 0, 30.0)
    assert p.ordered
    assert set(p.distinguishes) == {Comparison.CONFIRMED_MATCH, Comparison.CONFIRMED_MISS}
    assert p.decision_ref == "decision-open-valve-3"


def test_sources_are_ranked_only_on_a_shared_basis(make):
    mixed = _source("wall-probe", "room_temp_c", cost="0.1")
    ledger = make(sources=(*POLICY.sources[:2], replace(mixed, cost_unit="usd")))
    _resolve_movement(ledger)
    (p,) = ledger.suggest_observation("exp-valve-1")
    assert (p.ordered, [s.name for s in p.sources]) == (False, ["room-sensor", "wall-probe"])


def test_past_the_decision_point_it_is_too_late(ledger):
    _resolve_movement(ledger)
    (p,) = ledger.suggest_observation("exp-valve-1", now=HORIZON.latest_decision_point + MINUTE)
    assert p.status == "TOO_LATE"
    assert "OBSERVATION_PROPOSED" not in [e["kind"] for e in ledger.history("exp-valve-1")]


def test_repeated_requests_coalesce(ledger):
    _resolve_movement(ledger)
    first = ledger.suggest_observation("exp-valve-1")
    assert ledger.suggest_observation("exp-valve-1") == first  # read back from the ledger
    assert ledger.suggest_observation("exp-valve-1") == first
    kinds = [e["kind"] for e in ledger.history("exp-valve-1")]
    assert kinds.count("OBSERVATION_PROPOSED") == 1


def test_owner_limits_exhaust_with_the_escalation(make):
    ledger = make(limits=replace(POLICY.limits, max_outstanding=1))
    moved, temp = ledger.suggest_observation("exp-valve-1")
    assert (moved.status, temp.status) == ("OPEN", "EXHAUSTED")
    assert temp.escalation == "notify owner-1"


def test_repeats_per_criterion_are_bounded(make):
    ledger = make(limits=replace(POLICY.limits, max_repeats=2))
    _resolve_movement(ledger)
    statuses = []
    for revision in (1, 2, 3):
        ledger.observe(replace(TEMP, observation_id="obs-stale", revision=revision,
                               supersedes=revision - 1 or None, value=21.0, valid=False))
        ledger.evaluate("exp-valve-1", task=TASK)
        (p,) = ledger.suggest_observation("exp-valve-1")
        statuses.append(p.status)
    assert statuses == ["OPEN", "OPEN", "EXHAUSTED"]


def test_a_resolving_evaluation_closes_the_proposal(ledger):
    _resolve_movement(ledger)
    (p,) = ledger.suggest_observation("exp-valve-1")
    ledger.observe(replace(TEMP, value=21.0))
    ledger.evaluate("exp-valve-1", task=TASK)
    assert ledger.suggest_observation("exp-valve-1") == ()
    kinds = [e["kind"] for e in ledger.history("exp-valve-1")]
    assert "OBSERVATION_PROPOSAL_CLOSED" in kinds
    row = ledger._conn.execute(
        "SELECT status FROM cl_proposals WHERE proposal_id=?", (p.proposal_id,)
    ).fetchone()
    assert row[0] == "CLOSED"

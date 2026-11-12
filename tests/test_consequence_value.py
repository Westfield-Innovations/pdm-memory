"""Weighing an observation's cost, delay and perturbation against what it could avoid."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from pdm_memory import ConsequenceLedger
from pdm_memory.consequence import Criterion, SourceDecl, SourceValue
from pdm_memory.consequence.records import _plain
from tests.consequence_fixtures import (
    COMMIT,
    HORIZON,
    POLICY,
    ROOM_TEMP,
    SECOND,
    VALVE,
    _policy,
    _source,
)

RULES = {"EUR": Decimal(1), "call": Decimal(2), "min": Decimal("0.5"), "s": Decimal("0.01")}
WORTH_5 = replace(ROOM_TEMP, avoided=Decimal(5), avoided_unit="EUR")


def _room(ledger, expectation=None, now=COMMIT):
    ledger.prepare(expectation or replace(VALVE, criteria=(WORTH_5,)))
    (proposal,) = ledger.suggest_observation("exp-valve-1", now=now)
    return proposal


def _open(tmp_path, policy):
    return ConsequenceLedger(tmp_path / "c.db", owner="o", policy=policy, clock=lambda: COMMIT)


def test_without_an_avoided_consequence_the_order_stays_qualitative(tmp_path):
    with _open(tmp_path, _policy(exchange_rules=RULES)) as led:
        p = _room(led, replace(VALVE, criteria=(ROOM_TEMP,)))
    assert [s.name for s in p.sources] == ["room-sensor", "wall-probe"] and p.ordered
    assert {(v.verdict, v.net, v.reason) for v in p.values} == {
        ("UNRESOLVED", None, "no expected avoided consequence declared")
    }


def test_on_one_basis_each_source_gets_its_net_value(tmp_path):
    with _open(tmp_path, _policy(exchange_rules=RULES)) as led:
        p = _room(led)
    # room-sensor: 5 - (1 call*2 + 30 s*0.01) = 2.7; wall-probe: 5 - (3*2 + 0.3) = -1.3
    assert p.values == (
        SourceValue("room-sensor", "WORTH", Decimal("2.70"), ""),
        SourceValue("wall-probe", "NOT_WORTH", Decimal("-1.30"), "costs at least what it could avoid"),
    )
    assert p.status == "OPEN"  # the value informs the owner; it is not a release rule


def test_a_perturbation_counts_and_reorders_by_net_value(tmp_path):
    shaky = replace(_source("room-sensor", "room_temp_c"), perturbation=Decimal(10), perturbation_unit="EUR")
    policy = _policy(exchange_rules=RULES, sources=(shaky, *POLICY.sources[2:]))
    with _open(tmp_path, policy) as led:
        p = _room(led)
    assert [s.name for s in p.sources] == ["wall-probe", "room-sensor"] and p.ordered
    assert [v.net for v in p.values] == [Decimal("-1.30"), Decimal("-7.30")]


def test_a_unit_without_an_exchange_rule_leaves_the_comparison_unresolved(tmp_path):
    rules = {k: v for k, v in RULES.items() if k != "call"}
    with _open(tmp_path, _policy(exchange_rules=rules)) as led:
        p = _room(led)
    assert {(v.verdict, v.reason) for v in p.values} == {
        ("UNRESOLVED", "no owner exchange rule for ['call']")
    }
    assert [s.name for s in p.sources] == ["room-sensor", "wall-probe"]


def test_no_exchange_rules_at_all(tmp_path):
    with _open(tmp_path, POLICY) as led:
        p = _room(led)
    assert {v.reason for v in p.values} == {"no owner exchange rule for ['EUR', 'call', 's']"}


def test_delay_needs_a_rule_unless_it_is_zero(tmp_path):
    rules = {k: v for k, v in RULES.items() if k != "s"}
    instant = replace(_source("room-sensor", "room_temp_c"), latency_s=0.0)
    with _open(tmp_path, _policy(exchange_rules=rules, sources=(instant, POLICY.sources[2]))) as led:
        p = _room(led)
    assert [(v.source, v.verdict) for v in p.values] == [
        ("room-sensor", "WORTH"),
        ("wall-probe", "UNRESOLVED"),
    ]
    assert p.values[1].reason == "no owner exchange rule for ['s']"


def test_a_zero_avoided_consequence_needs_no_rule_and_is_never_worth_it(tmp_path):
    zero = replace(ROOM_TEMP, avoided=Decimal(0), avoided_unit="unmapped")
    with _open(tmp_path, _policy(exchange_rules=RULES)) as led:
        p = _room(led, replace(VALVE, criteria=(zero,)))
    assert {v.verdict for v in p.values} == {"NOT_WORTH"}


def test_breaking_even_is_not_worth_it(tmp_path):
    even = replace(ROOM_TEMP, avoided=Decimal("2.3"), avoided_unit="EUR")
    with _open(tmp_path, _policy(exchange_rules=RULES)) as led:
        p = _room(led, replace(VALVE, criteria=(even,)))
    assert p.values[0] == SourceValue(
        "room-sensor", "NOT_WORTH", Decimal("0.00"), "costs at least what it could avoid"
    )


def test_a_source_arriving_after_the_decision_point_is_too_late(tmp_path):
    fast = replace(_source("room-sensor", "room_temp_c"), latency_s=5.0)
    policy = _policy(exchange_rules=RULES, sources=(fast, POLICY.sources[2]))
    now = HORIZON.latest_decision_point - 10 * SECOND
    with _open(tmp_path, policy) as led:
        p = _room(led, now=now)
    assert [(v.source, v.verdict) for v in p.values] == [
        ("room-sensor", "WORTH"),
        ("wall-probe", "TOO_LATE"),
    ]
    assert p.values[1].reason == "arrives after the latest decision point"
    assert [s.name for s in p.sources] == ["room-sensor", "wall-probe"]  # qualitative order kept


def test_values_survive_a_reopen_and_replay(tmp_path):
    policy = _policy(exchange_rules=RULES)
    with _open(tmp_path, policy) as led:
        first = _room(led)
    with _open(tmp_path, policy) as led:
        assert led.suggest_observation("exp-valve-1", now=COMMIT + SECOND) == (first,)
        assert isinstance(led.suggest_observation("exp-valve-1")[0].values[0].net, Decimal)


def test_old_records_keep_their_form():
    assert "avoided" not in VALVE.to_dict()["criteria"][1]
    assert Criterion.from_dict(WORTH_5.__dict__ | {"avoided": "5"}) == WORTH_5
    assert "perturbation" not in _plain(POLICY.sources[0])


@pytest.mark.parametrize(
    "build",
    [
        lambda: replace(ROOM_TEMP, avoided=Decimal(1)),
        lambda: replace(ROOM_TEMP, avoided_unit="EUR"),
        lambda: replace(_source("x", "y"), perturbation=Decimal(1)),
        lambda: replace(_source("x", "y"), perturbation_unit="EUR"),
        lambda: replace(_source("x", "y"), latency_s=-1.0),
    ],
)
def test_bad_declarations_are_refused(build):
    with pytest.raises(ValueError):
        build()


def test_an_avoided_quantity_is_a_number():
    with pytest.raises(TypeError):
        replace(ROOM_TEMP, avoided=True, avoided_unit="EUR")
    with pytest.raises(ValueError):
        replace(ROOM_TEMP, avoided="lots", avoided_unit="EUR")
    assert isinstance(SourceDecl("s", frozenset({"o"}), 1.0, 1, "c", 0, "m", 2.5, "EUR").perturbation,
                      Decimal)

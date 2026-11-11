"""Goal Contract: a frozen record whose promise changes only with the owner's say-so."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from pdm_memory import ContractHistory as TopLevelHistory
from pdm_memory.goal_loop import (
    PROMISE_FIELDS,
    Autonomy,
    Budget,
    ContractHistory,
    EndConditions,
    GoalContract,
    NotArmableError,
    Notify,
    OwnerApproval,
    PromiseChangeError,
    Proof,
    Trigger,
    ensure_armable,
)
from pdm_memory.guarded_effects import ResourceLimitError

T0 = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=1)


def mu_trigger(**kwargs):
    base = dict(
        kind="threshold",
        metric="MU.price",
        direction="rising",
        fire_at="40.00",
        reset_at="39.50",
    )
    base.update(kwargs)
    return Trigger(**base)


def contract(**kwargs):
    base = dict(
        contract_id="watch-mu",
        owner="eric",
        outcome="Eric gets a text when MU crosses $40 from below",
        proof=Proof("sms_delivery_receipt", "carrier delivery receipt"),
        never=("Never trade MU shares",),
        preferences=("Keep the text short",),
        sacrifice_order=("wording", "send time within the minute"),
        trigger=mu_trigger(),
        autonomy=Autonomy(allowed=("sms.send",)),
        budget=Budget(
            deadline=datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc), max_actions=10
        ),
        notify=(Notify("sms", "+15550100"),),
        end=EndConditions(on_success="rearm"),
        created_at=T0,
    )
    base.update(kwargs)
    return GoalContract(**base)


APPROVAL = OwnerApproval("eric", "ok by me", approved_at=T1)

PROMISE_CHANGES = {
    "outcome": "Eric gets a text when MU crosses $45 from below",
    "proof": Proof("email_delivery_receipt"),
    "never": ("Never trade MU shares", "Never text after 10pm"),
    "autonomy": Autonomy(allowed=("sms.send", "email.send")),
    "budget": Budget(max_actions=3),
    "sacrifice_order": ("send time within the minute", "wording"),
}

PLAN_CHANGES = {
    "preferences": ("Mention the exact price",),
    "trigger": mu_trigger(fire_at="45", reset_at="44.50"),
    "notify": (Notify("email", "eric@example.com"),),
    "end": EndConditions(on_success="complete"),
}


def test_change_sets_cover_every_revisable_field():
    assert set(PROMISE_CHANGES) == PROMISE_FIELDS
    assert "proof" in PROMISE_FIELDS


# ------------------------------------------------------------ the promise


@pytest.mark.parametrize("field_name", sorted(PROMISE_CHANGES))
def test_promise_change_without_approval_raises_and_records_nothing(field_name):
    history = ContractHistory(contract())
    with pytest.raises(PromiseChangeError) as err:
        history.revise(now=T1, **{field_name: PROMISE_CHANGES[field_name]})
    assert err.value.fields == (field_name,)
    assert len(history) == 1
    assert history.current == contract()


@pytest.mark.parametrize("field_name", sorted(PROMISE_CHANGES))
def test_promise_change_with_approval_creates_next_version_and_keeps_previous(field_name):
    original = contract()
    history = ContractHistory(original)
    new = history.revise(approval=APPROVAL, now=T1, **{field_name: PROMISE_CHANGES[field_name]})

    assert new.version == 2
    assert new.previous_version == 1
    assert new.created_at == T1
    assert new.approved_by == "eric"
    assert new.approved_at == T1
    assert new.change_reason == "ok by me"
    assert getattr(new, field_name) == PROMISE_CHANGES[field_name]
    assert history.current is new
    assert history.versions() == (original, new)
    assert history.version(1) == original
    assert getattr(history.version(1), field_name) == getattr(original, field_name)


def test_mixed_change_names_only_the_promise_fields():
    history = ContractHistory(contract())
    with pytest.raises(PromiseChangeError) as err:
        history.revise(now=T1, outcome="new outcome", never=(), preferences=("x",))
    assert err.value.fields == ("never", "outcome")


def test_approval_from_someone_else_raises():
    history = ContractHistory(contract())
    with pytest.raises(PromiseChangeError, match="not the contract owner"):
        history.revise(approval=OwnerApproval("mallory"), now=T1, outcome="new outcome")
    assert len(history) == 1


def test_approval_must_be_an_owner_approval():
    history = ContractHistory(contract())
    with pytest.raises(TypeError):
        history.revise(approval="eric", now=T1, outcome="new outcome")


@pytest.mark.parametrize("field_name", sorted(PLAN_CHANGES))
def test_plan_change_without_approval_still_creates_a_version(field_name):
    history = ContractHistory(contract())
    new = history.revise(reason="tune", now=T1, **{field_name: PLAN_CHANGES[field_name]})
    assert new.version == 2
    assert new.approved_by is None
    assert new.approved_at is None
    assert new.change_reason == "tune"
    assert len(history) == 2


def test_successive_versions_chain():
    history = ContractHistory(contract())
    history.revise(now=T1, preferences=("a",))
    v3 = history.revise(approval=APPROVAL, now=T1 + timedelta(hours=1), outcome="later outcome")
    assert [v.version for v in history.versions()] == [1, 2, 3]
    assert v3.previous_version == 2
    assert history.version(2).outcome == contract().outcome


def test_revision_that_changes_nothing_records_nothing():
    history = ContractHistory(contract())
    same = history.revise(approval=APPROVAL, now=T1, outcome=contract().outcome)
    assert same is history.current
    assert len(history) == 1


def test_equal_decimal_spelling_is_not_a_change():
    history = ContractHistory(contract())
    history.revise(now=T1, trigger=mu_trigger(fire_at=Decimal("40"), reset_at=39.5))
    assert len(history) == 1


@pytest.mark.parametrize(
    "field_name", ["contract_id", "owner", "version", "created_at", "approved_by"]
)
def test_record_fields_cannot_be_revised(field_name):
    history = ContractHistory(contract())
    with pytest.raises(TypeError, match="record fields"):
        history.revise(approval=APPROVAL, now=T1, **{field_name: "x"})


def test_unknown_field_raises():
    history = ContractHistory(contract())
    with pytest.raises(TypeError, match="unknown"):
        history.revise(now=T1, colour="blue")


def test_new_version_cannot_be_dated_before_the_current_one():
    history = ContractHistory(contract())
    with pytest.raises(ValueError, match="dated before"):
        history.revise(now=T0 - timedelta(seconds=1), preferences=("a",))


def test_revise_requires_aware_now():
    history = ContractHistory(contract())
    with pytest.raises(ValueError, match="timezone-aware"):
        history.revise(now=datetime(2026, 8, 18), preferences=("a",))


def test_contract_is_frozen():
    c = contract()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.outcome = "something easier"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.budget.max_actions = 1000  # type: ignore[misc]
    with pytest.raises(AttributeError):
        c.never.append("x")  # type: ignore[attr-defined]


def test_history_starts_at_version_one():
    with pytest.raises(ValueError, match="version 1"):
        ContractHistory(contract(version=2, previous_version=1))


# ------------------------------------------------------------ arming


def test_contract_without_proof_cannot_be_armed():
    with pytest.raises(NotArmableError):
        ensure_armable(contract(proof=None))


def test_proof_source_must_not_be_blank():
    with pytest.raises(ValueError, match="blank"):
        Proof("   ")


def test_contract_with_proof_can_be_armed():
    ensure_armable(contract())
    assert contract().is_armable


def test_proof_can_be_given_as_a_source_name():
    assert contract(proof="file_exists").proof == Proof("file_exists")


def test_removing_proof_is_a_promise_change():
    history = ContractHistory(contract())
    with pytest.raises(PromiseChangeError):
        history.revise(now=T1, proof=None)
    unarmable = history.revise(approval=APPROVAL, now=T1, proof=None)
    with pytest.raises(NotArmableError):
        ensure_armable(unarmable)


# ------------------------------------------------------------ validation


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        contract(created_at=datetime(2026, 8, 17))
    with pytest.raises(ValueError, match="timezone-aware"):
        Budget(deadline=datetime(2026, 9, 30))
    with pytest.raises(ValueError, match="timezone-aware"):
        OwnerApproval("eric", approved_at=datetime(2026, 9, 30))


def test_threshold_values_become_decimals():
    t = mu_trigger(fire_at=40, reset_at=39.5)
    assert t.fire_at == Decimal("40")
    assert t.reset_at == Decimal("39.5")
    assert type(t.reset_at) is Decimal


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "NaN", "abc", True])
def test_threshold_values_must_be_finite_numbers(bad):
    with pytest.raises((ValueError, TypeError)):
        mu_trigger(fire_at=bad)


def test_rising_trigger_needs_reset_below_fire():
    with pytest.raises(ValueError, match="below"):
        mu_trigger(fire_at="40", reset_at="40")
    falling = mu_trigger(direction="falling", fire_at="30", reset_at="30.5")
    assert falling.reset_at > falling.fire_at
    with pytest.raises(ValueError, match="above"):
        mu_trigger(direction="falling", fire_at="30", reset_at="29")


def test_trigger_kinds_take_only_their_own_fields():
    assert Trigger(kind="event", event="work_order.received").event == "work_order.received"
    assert Trigger(kind="schedule", schedule="0 9 * * 1-5").schedule == "0 9 * * 1-5"
    with pytest.raises(ValueError, match="does not take"):
        Trigger(kind="event", event="x", metric="MU.price")
    with pytest.raises(ValueError, match="needs fire_at"):
        Trigger(kind="threshold", metric="MU.price", direction="rising", fire_at="40")
    with pytest.raises(ValueError):
        Trigger(kind="webhook")


def test_autonomy_lists_do_not_overlap():
    with pytest.raises(ValueError, match="both"):
        Autonomy(allowed=("sms.send",), ask_first=("sms.send",))


def test_rule_lists_must_be_lists_of_text():
    with pytest.raises(TypeError):
        contract(never="Never trade MU shares")
    with pytest.raises(ValueError, match="blank"):
        contract(never=("Never trade", "  "))
    with pytest.raises(ValueError, match="duplicates"):
        contract(never=("a", "a"))


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_budget_counts_are_positive_integers(value):
    with pytest.raises(ValueError):
        Budget(max_actions=value)
    with pytest.raises(ValueError):
        Budget(max_attempts=value)


def test_budget_cost_is_a_non_negative_decimal():
    assert Budget(max_cost="2.50").max_cost == Decimal("2.50")
    with pytest.raises(ValueError):
        Budget(max_cost=-1)


def test_required_text_fields():
    with pytest.raises(ValueError):
        contract(outcome=" ")
    with pytest.raises(ValueError):
        contract(owner="")
    with pytest.raises(ValueError):
        Notify("pager", "x")


# ------------------------------------------------------------ transport


def revised_history():
    history = ContractHistory(contract())
    history.revise(now=T1, preferences=("Mention the exact price",))
    history.revise(
        approval=APPROVAL,
        now=T1 + timedelta(hours=2),
        budget=Budget(max_actions=3, max_cost="1.20", currency="USD"),
    )
    return history


def test_round_trip_keeps_every_version_exactly():
    history = revised_history()
    loaded = ContractHistory.from_json(history.to_json())
    assert loaded.versions() == history.versions()
    assert loaded.current.budget.max_cost == Decimal("1.20")
    assert loaded.version(1).trigger.reset_at == Decimal("39.50")


def test_json_is_canonical_and_keeps_decimals_as_text():
    text = revised_history().to_json()
    assert text == json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    data = json.loads(text)
    assert data["versions"][0]["trigger"]["reset_at"] == "39.50"
    assert data["versions"][2]["budget"]["max_cost"] == "1.20"
    assert data["versions"][0]["created_at"] == "2026-08-17T12:00:00+00:00"


def test_loaded_history_keeps_working():
    loaded = ContractHistory.from_json(revised_history().to_json())
    with pytest.raises(PromiseChangeError):
        loaded.revise(now=T1 + timedelta(days=1), outcome="x")
    assert loaded.revise(now=T1 + timedelta(days=1), preferences=("b",)).version == 4


def test_loading_rejects_an_unapproved_promise_change():
    data = revised_history().to_dict()
    data["versions"][2]["approved_by"] = None
    with pytest.raises(PromiseChangeError):
        ContractHistory.from_dict(data)


def test_loading_rejects_an_edited_earlier_version():
    data = revised_history().to_dict()
    data["versions"][1]["never"] = []
    with pytest.raises(PromiseChangeError):
        ContractHistory.from_dict(data)


def test_loading_rejects_gaps_and_unknown_keys():
    data = revised_history().to_dict()
    del data["versions"][1]
    with pytest.raises(ValueError):
        ContractHistory.from_dict(data)

    data = revised_history().to_dict()
    data["versions"][0]["sneaky"] = 1
    with pytest.raises(ValueError, match="unknown keys"):
        ContractHistory.from_dict(data)


def test_loading_rejects_naive_datetimes_and_nan():
    text = revised_history().to_json()
    with pytest.raises(ValueError, match="timezone-aware"):
        ContractHistory.from_json(text.replace("2026-08-17T12:00:00+00:00", "2026-08-17T12:00:00"))
    with pytest.raises(ValueError):
        ContractHistory.from_json(text.replace('"39.50"', "NaN"))


def test_loading_accepts_z_suffix():
    text = revised_history().to_json().replace("2026-08-17T12:00:00+00:00", "2026-08-17T12:00:00Z")
    assert ContractHistory.from_json(text).version(1).created_at == T0


def test_loading_rejects_oversized_input():
    with pytest.raises(ResourceLimitError):
        ContractHistory.from_json(" " * 1_048_577)


def test_top_level_export():
    assert TopLevelHistory is ContractHistory


# ------------------------------------------------------------ no store


def test_works_with_no_store_configured(tmp_path, monkeypatch):
    """The whole contract lifecycle runs without a database, a file or the network."""
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("the Goal Contract must not open storage or the network")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", refuse)
    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(httpx, "AsyncClient", refuse)

    history = ContractHistory(contract())
    ensure_armable(history.current)
    history.revise(approval=APPROVAL, now=T1, outcome="Eric gets a text at $41")
    loaded = ContractHistory.from_json(history.to_json())

    assert loaded.versions() == history.versions()
    assert list(tmp_path.iterdir()) == []

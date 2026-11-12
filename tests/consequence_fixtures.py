"""Shared records for the consequence tests: the valve episode from the spec."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pdm_memory.consequence.records import (
    Criterion,
    EvidenceKind,
    Expectation,
    Horizon,
    Observation,
    OwnerPolicy,
    ReviewLimits,
    SourceDecl,
    Stage,
    TaskRef,
)
from pdm_memory.guarded_effects import (
    CheckResult,
    EffectRequest,
    GuardedEffects,
    Opening,
)

SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

COMMIT = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
START = COMMIT + MINUTE
END = COMMIT + HOUR
HORIZON = Horizon(START, END, COMMIT + 2 * HOUR)

TASK = TaskRef("task-heat-room-4", "owner-1", contract_id="contract-heat", contract_version=1)

MOVED = Criterion(
    "moved", "valve_position", Stage.ACTUATOR_CONFIRMATION, ("categorical", "1"), "state", ["moved"]
)
ROOM_TEMP = Criterion(
    "room_temp", "room_temp_c", Stage.TASK_EFFECT, ("range", "1"), "degC", {"low": 20.0, "high": 22.0}
)

VALVE = Expectation(
    expectation_id="exp-valve-1",
    task=TASK,
    predictor="valve-model",
    predictor_version="1",
    criteria=(MOVED, ROOM_TEMP),
    horizon=HORIZON,
    inputs=("mem-setpoint",),
    action_id="act-valve-1",
    interface_id="valve-3",
    decision_ref="decision-open-valve-3",
)

LIMITS = ReviewLimits(max_outstanding=8, max_repeats=3, max_payload_bytes=65_536, max_revisions=8)


def _source(name, observable, cost="1", burden="0"):
    return SourceDecl(name, frozenset({observable}), 30.0, Decimal(cost), "call", Decimal(burden), "min")


POLICY = OwnerPolicy(
    policy_version="policy-1",
    sources=(
        _source("position-feed", "valve_position"),
        _source("room-sensor", "room_temp_c"),
        _source("wall-probe", "room_temp_c", cost="3"),
    ),
    comparators=frozenset(
        {("exact", "1"), ("range", "1"), ("categorical", "1"), ("probabilistic", "1")}
    ),
    learning_targets=frozenset({"predictor", "belief", "interface_calibration"}),
    limits=LIMITS,
    escalation="notify owner-1",
)

POSITION_MOVED = Observation(
    observation_id="obs-pos",
    revision=1,
    source="position-feed",
    observable="valve_position",
    event_time=START + MINUTE,
    arrival_time=START + MINUTE,
    value="moved",
    unit="state",
    evidence_ref="plc:valve-3:pos:1",
    kind=EvidenceKind.MEASURED,
)

TEMP = Observation(
    observation_id="obs-temp",
    revision=1,
    source="room-sensor",
    observable="room_temp_c",
    event_time=START + 30 * MINUTE,
    arrival_time=START + 30 * MINUTE,
    value=None,
    unit="degC",
    evidence_ref="sensor:room-4:temp:1",
    kind=EvidenceKind.MEASURED,
)


def _policy(**changes):
    return replace(POLICY, **changes)


def _obs(**changes):
    return replace(TEMP, **changes)


def _request(action_id="act-valve-1"):
    return EffectRequest(
        action_id=action_id,
        branch_id="b1",
        program_version="worker-1",
        policy_version="policy-1",
        operation="open",
        target="valve-3",
        parameters={"percent": 40},
    )


def _guard(tmp_path, *, check=None, handler=lambda r: {"accepted": True}):
    g = GuardedEffects(
        tmp_path / "guard.db",
        owner="owner-1",
        program_version="worker-1",
        policy_version="policy-1",
        openings=[Opening("open", "valve-3", handler, lambda r: CheckResult.SATISFIED, check)],
    )
    g.start_branch("b1")
    return g

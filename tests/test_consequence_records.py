"""Consequence records and the owner's policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from pdm_memory.consequence.records import (
    Expectation,
    Horizon,
    Observation,
    ReviewLimits,
)
from pdm_memory.guarded_effects import ConfigurationError
from tests.consequence_fixtures import END, START, TEMP, VALVE, _obs, _policy


def test_a_horizon_needs_aware_times():
    with pytest.raises(ValueError, match="timezone"):
        Horizon(datetime(2026, 11, 12), END, END)  # noqa: DTZ001


def test_a_window_must_close_after_it_opens():
    with pytest.raises(ValueError, match="after"):
        Horizon(END, START, END)


def test_an_observation_needs_aware_times_and_a_positive_revision():
    with pytest.raises(ValueError, match="timezone"):
        _obs(event_time=datetime(2026, 11, 12, 10))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone"):
        _obs(coverage=(datetime(2026, 11, 12, 10), END))  # noqa: DTZ001
    with pytest.raises(ValueError, match="revision"):
        _obs(revision=0)


def test_an_incomplete_policy_is_refused():
    with pytest.raises(ConfigurationError):
        _policy(limits=ReviewLimits(0, 3, 65_536, 8))
    with pytest.raises(ConfigurationError):
        _policy(escalation="")


def test_a_policy_cannot_admit_a_protected_target():
    with pytest.raises(ConfigurationError, match="protected"):
        _policy(learning_targets=frozenset({"predictor", "permission"}))


def test_criterion_names_are_unique():
    with pytest.raises(ValueError, match="unique"):
        replace(VALVE, criteria=(VALVE.criteria[0], VALVE.criteria[0]))


def test_records_survive_their_json_form():
    assert Expectation.from_dict(VALVE.to_dict()) == VALVE
    covered = _obs(coverage=(START, END), value=21.0)
    assert Observation.from_dict(covered.to_dict()) == covered
    assert Observation.from_dict(TEMP.to_dict()) == TEMP

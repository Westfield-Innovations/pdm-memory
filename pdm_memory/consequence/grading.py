"""How one expectation is graded: which readings count, in what span, and how.

Pure functions of an expectation, its observations and the owner's policy; the
ledger reads the evidence and stores what these return.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pdm_memory.consequence.compare import COMPARATORS, NOT_OBSERVED
from pdm_memory.consequence.records import (
    Applicability,
    Comparison,
    Criterion,
    EvidenceKind,
    Expectation,
    Finding,
    Horizon,
    Observation,
    Ordering,
    OwnerPolicy,
    Response,
    SourceDecl,
    SourceValue,
)

Span = tuple[datetime, datetime]


def horizon_s(h: Horizon) -> float:
    if h.event is not None and h.event.grade_for_s is not None:
        return float(h.event.grade_for_s)
    return (h.window_end - h.window_start).total_seconds()


def inadmissible(
    o: Observation, unit: str, e: Expectation, policy: OwnerPolicy, window: Span
) -> str:
    """Why *o* cannot grade a rule in *unit*, or "" when it can."""
    source = policy.source(o.source)
    if source is None or o.observable not in source.observables:
        return "source"
    if o.unit != unit:
        return "unit"
    if not o.valid:
        return "invalid"
    if o.kind is EvidenceKind.SIMULATED:
        return "simulated"
    if o.evidence_ref in e.inputs:
        return "own witness"
    if o.value is None:
        if o.coverage is None:
            return "coverage"
        start, end = o.coverage
        return "" if start <= window[0] and end >= window[1] else "coverage"
    return ""


def admissible_support(o: Observation, e: Expectation, policy: OwnerPolicy) -> bool:
    """A cause's support: an admitted source, valid, measured or reported, not its own input."""
    source = policy.source(o.source)
    return (
        source is not None
        and o.observable in source.observables
        and o.valid
        and o.kind is not EvidenceKind.SIMULATED
        and o.evidence_ref not in e.inputs
    )


def in_window(o: Observation, window: Span) -> bool:
    """Readings outside the window are not this expectation's evidence at all."""
    start, end = window
    if o.value is None:
        return o.coverage is not None and o.coverage[0] < end and o.coverage[1] > start
    return start <= o.event_time <= end


def event_readings(
    e: Expectation, observations: list[Observation]
) -> list[Observation]:
    """Every reading that could open an event horizon; a correction to one is new evidence."""
    h, event = e.horizon, e.horizon.event
    if event is None:
        return []
    return [
        o
        for o in observations
        if o.observable == event.observable
        and h.window_start <= o.event_time <= h.window_end
    ]


def grading_window(
    e: Expectation, observations: list[Observation], policy: OwnerPolicy
) -> Span | None:
    """The span readings are graded over; None while the awaited event is unseen."""
    h, event = e.horizon, e.horizon.event
    if event is None:
        return h.window_start, h.window_end
    compare = COMPARATORS[event.comparator].compare
    times = []
    for o in event_readings(e, observations):
        # A silence cannot be the event, and the event needs admissible evidence.
        if o.value is None or inadmissible(
            o, event.unit, e, policy, (h.window_start, h.window_end)
        ):
            continue
        try:
            met, _ = compare(event.expected, o.value)
        except (TypeError, ValueError):
            continue
        if met:
            times.append(o.event_time)
    if not times:
        return None
    start = min(times)
    if event.grade_for_s is None:
        return start, h.window_end
    return start, min(h.window_end, start + timedelta(seconds=event.grade_for_s))


def ordering_of(
    o: Observation, e: Expectation, committed_at: datetime, window: Span
) -> Ordering:
    recorded = e.source_sequences.get(o.source)
    if recorded is not None and o.source_sequence is not None:
        return (
            Ordering.PROSPECTIVE
            if o.source_sequence > recorded
            else Ordering.RETROSPECTIVE
        )
    # A silence is graded over the window it covers, so the window's opening times it.
    t = window[0] if o.value is None else o.event_time
    u = timedelta(seconds=o.clock_uncertainty_s)
    if t - u > committed_at:
        return Ordering.PROSPECTIVE
    if t + u <= committed_at:
        return Ordering.RETROSPECTIVE
    return Ordering.UNCERTAIN


def applicability_of(found: list[Observation], e: Expectation) -> Applicability:
    if not found:
        return Applicability.UNRESOLVED
    keys = e.horizon.invalidated_by
    seen = {
        tuple(o.conditions[k] for k in keys)
        for o in found
        if all(k in o.conditions for k in keys)
    }
    if len(seen) > 1:
        return Applicability.CONFLICTING
    results = set()
    for o in found:
        result = Applicability.APPLICABLE
        for k, v in e.horizon.environment_versions.items():
            if k in o.configuration and o.configuration[k] != v:
                result = Applicability.OUT_OF_SCOPE
        for k in keys:
            if k not in o.conditions:
                result = (
                    result
                    if result is Applicability.OUT_OF_SCOPE
                    else Applicability.UNRESOLVED
                )
            elif o.conditions[k] != e.conditions.get(k):
                result = Applicability.OUT_OF_SCOPE
        results.add(result)
    for kind in (Applicability.OUT_OF_SCOPE, Applicability.UNRESOLVED):
        if kind in results:
            return kind
    return Applicability.APPLICABLE


def grade_criterion(
    e: Expectation,
    committed_at: datetime,
    c: Criterion,
    observations: list[Observation],
    policy: OwnerPolicy,
    window: Span | None,
) -> Finding:
    if window is None:
        return Finding(
            c.name,
            None,
            Applicability.UNRESOLVED,
            Comparison.UNRESOLVED,
            (),
            (),
            False,
            "event not observed",
        )
    compare = COMPARATORS[c.comparator].compare
    excluded: list[tuple[str, str]] = []
    graded: list[tuple[Observation, Ordering, bool, bool]] = []
    for o in observations:
        if o.observable != c.observable:
            continue
        reason = inadmissible(o, c.unit, e, policy, window)
        if reason:
            excluded.append((o.observation_id, reason))
            continue
        ordering = ordering_of(o, e, committed_at, window)
        if ordering is Ordering.UNCERTAIN:
            excluded.append((o.observation_id, "ordering"))
            continue
        try:
            met, allowed = compare(
                c.expected, NOT_OBSERVED if o.value is None else o.value
            )
        except (TypeError, ValueError):
            excluded.append((o.observation_id, "value"))
            continue
        graded.append((o, ordering, met, allowed))
    if not graded:
        reasons = sorted({r for _, r in excluded})
        missing = "no admissible observation" + (
            f" ({', '.join(reasons)})" if reasons else ""
        )
        return Finding(
            c.name,
            None,
            Applicability.UNRESOLVED,
            Comparison.UNRESOLVED,
            (),
            tuple(excluded),
            False,
            missing,
        )
    mets = {met for _, _, met, _ in graded}
    if mets == {True}:
        comparison = Comparison.CONFIRMED_MATCH
    elif mets == {False}:
        comparison = Comparison.CONFIRMED_MISS
    else:
        comparison = Comparison.CONFLICT
    orderings = {ordering for _, ordering, _, _ in graded}
    return Finding(
        criterion=c.name,
        ordering=Ordering.RETROSPECTIVE
        if Ordering.RETROSPECTIVE in orderings
        else Ordering.PROSPECTIVE,
        applicability=applicability_of([o for o, *_ in graded], e),
        comparison=comparison,
        used=tuple((o.observation_id, o.revision) for o, *_ in graded),
        excluded=tuple(excluded),
        allowed_by_model=comparison is Comparison.CONFIRMED_MISS
        and all(a for *_, a in graded),
        missing="",
    )


def source_value(
    c: Criterion, s: SourceDecl, now: datetime, deadline: datetime, policy: OwnerPolicy
) -> SourceValue:
    """Avoided consequence against cost, burden, delay and perturbation, on the owner's basis."""
    if now + timedelta(seconds=s.latency_s) > deadline:
        return SourceValue(
            s.name, "TOO_LATE", None, "arrives after the latest decision point"
        )
    if c.avoided is None or c.avoided_unit is None:
        return SourceValue(
            s.name, "UNRESOLVED", None, "no expected avoided consequence declared"
        )
    costs = [
        (s.cost, s.cost_unit),
        (s.burden, s.burden_unit),
        (Decimal(str(s.latency_s)), "s"),
    ]
    if s.perturbation is not None and s.perturbation_unit is not None:
        costs.append((s.perturbation, s.perturbation_unit))
    rules = policy.exchange_rules or {}
    # A zero needs no exchange rule: nothing of any unit is still nothing.
    missing = sorted(
        {
            u
            for q, u in [(c.avoided, c.avoided_unit), *costs]
            if q != 0 and u not in rules
        }
    )
    if missing:
        return SourceValue(
            s.name, "UNRESOLVED", None, f"no owner exchange rule for {missing}"
        )

    def basis(q: Decimal, unit: str) -> Decimal:
        return q * rules[unit] if q != 0 else Decimal(0)

    net = basis(c.avoided, c.avoided_unit) - sum(
        (basis(q, u) for q, u in costs), Decimal(0)
    )
    if net > 0:
        return SourceValue(s.name, "WORTH", net, "")
    return SourceValue(s.name, "NOT_WORTH", net, "costs at least what it could avoid")


def response_for(findings: tuple[Finding, ...]) -> Response:
    seen = {f.comparison for f in findings}
    if Comparison.CONFLICT in seen:
        return Response.RESOLVE_CONFLICT
    if Comparison.CONFIRMED_MISS in seen:
        return Response.OPEN_REVIEW
    if Comparison.UNRESOLVED in seen:
        return Response.OBSERVE
    return Response.CONTINUE

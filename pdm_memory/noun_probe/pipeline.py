# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Pipeline entry points: audit and vision over one input.

The boundary is drawn from the input itself: the input is the system, its
domain is the domain, and what lies outside the input is declared out of
scope (Law 0).

It runs in one of two modes, never both in one pass. An
**audit** reads an input for the words that break on a swap within their
class -- a noun carrying another class's verb, an actor only acted upon --
and splits each it can before the input is used (``split``). A **vision**
reads the same swaps the other way, for what survived and for what broke,
as material for new claims. A vision pass is opened with ``vision_pass``;
inside it no audit runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pdm_memory.noun_probe.boundary import lock_boundary
from pdm_memory.noun_probe.config import noun_probe_config_from_settings
from pdm_memory.noun_probe.contracts import (
    Boundary,
    EvidenceTag,
    Finding,
    NounClass,
    ProbeReport,
)
from pdm_memory.noun_probe.extraction import extract_structural_nouns
from pdm_memory.noun_probe.lexicon import CLASS_VERB_AFFINITY, in_class_lexicon
from pdm_memory.noun_probe.service import gated_probe
from pdm_memory.noun_probe.verb_coupling import couple_verbs

#: The probe's name as a save filter.
FILTER = "noun_probe"

#: A finding of this kind fails the audit: the input uses a role against its class.
MISFRAMED = "misframed"

#: The findings the idle source turns into claims: structure that survived re-lock.
VISION_KINDS = ("broaden", "invariant")

#: A word that broke on a swap within its class: it carries another class's
#: verb, or it is an actor only acted upon.
SPLIT = "split"
BREAK_KINDS = (SPLIT, MISFRAMED)

#: The audit's own version, kept with what it passed.
PROBE_VERSION = 1

AUDIT = "audit"
VISION = "vision"
_PASS: ContextVar[str] = ContextVar("noun_probe_pass", default=AUDIT)


class ProbePassError(RuntimeError):
    """An audit asked for inside a vision pass."""


@contextmanager
def vision_pass() -> Iterator[None]:
    """A pass of the probe in vision mode: no audit runs inside it."""
    token = _PASS.set(VISION)
    try:
        yield
    finally:
        _PASS.reset(token)


def in_vision_pass() -> bool:
    return _PASS.get() == VISION


def default_boundary(system: str, domain: str, scale: str) -> Boundary:
    return lock_boundary(
        system=system or "input",
        domain=domain or "general",
        scale=scale,
        not_analyzed=(f"everything outside this {scale}",),
    )


def audit(
    text: str,
    *,
    system: str,
    domain: str,
    scale: str,
    force: bool = False,
    scope_id: str | None = None,
) -> ProbeReport | None:
    """The probe over *text*; None when it did not run (disabled or gated out)."""
    if in_vision_pass():
        raise ProbePassError("The probe does not audit inside a vision pass.")
    return _run(
        text, system=system, domain=domain, scale=scale, force=force, scope_id=scope_id
    )


def read_in_vision(
    text: str,
    *,
    system: str,
    domain: str,
    scale: str,
    scope_id: str | None = None,
) -> ProbeReport | None:
    """The probe over *text* in vision mode, inside a vision pass only."""
    if not in_vision_pass():
        raise ProbePassError("A vision is read inside a vision pass.")
    return _run(
        text, system=system, domain=domain, scale=scale, force=True, scope_id=scope_id
    )


def _run(
    text: str,
    *,
    system: str,
    domain: str,
    scale: str,
    force: bool,
    scope_id: str | None,
) -> ProbeReport | None:
    if not (text or "").strip():
        return None
    return gated_probe(
        text,
        default_boundary(system, domain, scale),
        force=force,
        scope_id=scope_id,
    )


def misframed(report: ProbeReport | None) -> list[Finding]:
    if report is None:
        return []
    return [f for f in report.findings if f.kind == MISFRAMED]


def breaks(report: ProbeReport | None) -> list[Finding]:
    """The words that broke on a swap within their class."""
    if report is None:
        return []
    return [f for f in report.findings if f.kind in BREAK_KINDS]


def _owner(verb: str, own: NounClass) -> NounClass | None:
    for noun_class, affinity in CLASS_VERB_AFFINITY.items():
        if noun_class is not own and verb in affinity:
            return noun_class
    return None


def split(
    text: str, report: ProbeReport | None, *, system: str, domain: str, scale: str
) -> tuple[str, list[dict[str, Any]]]:
    """
    *text* with each word that broke split in two, one word per job, and
    what was split. A noun carrying another class's verb keeps its own job,
    and the verb's class gets its own word beside it -- after the noun when
    the noun does the verb, before it when the verb is done to it -- so the
    verb no longer sits on the noun. An actor only acted upon has no other
    class to hand its verb to, and is not split.
    """
    found = [f for f in breaks(report) if f.kind == SPLIT]
    if not found:
        return text, []
    boundary = default_boundary(system, domain, scale)
    cfg = noun_probe_config_from_settings()
    couplings = {
        c.noun: c
        for c in couple_verbs(
            extract_structural_nouns(text, boundary, top_n=cfg.top_n), text, boundary
        ).couplings
    }
    done: list[dict[str, Any]] = []
    for finding in found:
        coupling = couplings.get(finding.term)
        if coupling is None:
            continue
        verb = coupling.primary_verb or coupling.passive_verb or coupling.implied_verb
        owner = _owner(verb, coupling.noun_class)
        if owner is None:
            continue
        does = verb == coupling.primary_verb
        into = (finding.term, owner.value) if does else (owner.value, finding.term)
        text = re.sub(
            rf"(?i)\b{re.escape(finding.term)}\b",
            lambda m, does=does, owner=owner: (
                f"{m.group(0)} {owner.value}" if does else f"{owner.value} {m.group(0)}"
            ),
            text,
        )
        done.append({"term": finding.term, "verb": verb, "into": list(into)})
    return text, done


def vision(report: ProbeReport | None) -> list[Finding]:
    """
    The findings worth a claim: known structure, never a hypothesis. A term
    broadens to its class only when the lexicon names that class; a class
    guessed from a suffix or by default is no ground for a claim.
    """
    if report is None:
        return []
    return [
        f
        for f in report.findings
        if f.kind in VISION_KINDS
        and f.evidence is EvidenceTag.KNOWN
        and (f.kind != "broaden" or in_class_lexicon(f.term))
    ]

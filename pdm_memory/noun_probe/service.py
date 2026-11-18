# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Integrated detector-then-resolver entry + structured logging.

`gated_probe` is the production door: cheap gate every call, expensive engine
only on GO, env-driven, with a `NOUN_PROBE_RUN` structured log. Returns
None when the engine did not run (disabled / gated out) so callers can branch.

Logs carry boundary domain/scale and counts only — never raw document text.
"""

from __future__ import annotations

import logging
from collections import Counter

from pdm_memory.noun_probe.config import noun_probe_config_from_settings
from pdm_memory.noun_probe.contracts import Boundary, DetectorDecision, ProbeReport
from pdm_memory.noun_probe.detector import should_probe
from pdm_memory.noun_probe.report import run_noun_probe

logger = logging.getLogger("pdm_memory.noun_probe")


def log_event(
    log: logging.Logger, *, level: int, event: str, message: str, **fields
) -> None:
    log.log(level, message, extra={"event": event, **fields})


def gated_probe(
    text: str,
    boundary: Boundary,
    *,
    force: bool = False,
    scope_id: str | None = None,
) -> ProbeReport | None:
    cfg = noun_probe_config_from_settings()
    if not cfg.enabled:
        log_event(
            logger,
            level=logging.INFO,
            event="NOUN_PROBE_DISABLED",
            message="[noun_probe] disabled via settings",
            scope_id=scope_id,
        )
        return None

    decision = should_probe(
        text,
        force=force,
        min_doc_words=cfg.min_doc_words,
        min_hits=cfg.min_hits,
    )
    if not decision.should_run:
        log_event(
            logger,
            level=logging.DEBUG,
            event="NOUN_PROBE_SKIP",
            message="[noun_probe] gate skipped input",
            detector_score=decision.score,
            categories=list(decision.categories),
            reasons=list(decision.reasons),
            scope_id=scope_id,
        )
        return None

    report = run_noun_probe(
        text,
        boundary,
        top_n=cfg.top_n,
        substitutes_per_noun=cfg.substitutes_per_noun,
    )
    log_noun_probe_run(report, decision, scope_id=scope_id)
    return report


def log_noun_probe_run(
    report: ProbeReport,
    decision: DetectorDecision | None = None,
    *,
    scope_id: str | None = None,
) -> None:
    kinds = Counter(f.kind for f in report.findings)
    log_event(
        logger,
        level=logging.INFO,
        event="NOUN_PROBE_RUN",
        message="[noun_probe] resolver completed",
        domain=report.boundary.domain,
        scale=report.boundary.scale,
        not_analyzed_count=len(report.boundary.not_analyzed),
        noun_count=report.noun_count,
        findings_count=len(report.findings),
        vetoed_count=len(report.vetoed),
        split=kinds.get("split", 0),
        broaden=kinds.get("broaden", 0),
        invariant=kinds.get("invariant", 0),
        misframed=kinds.get("misframed", 0),
        detector_score=decision.score if decision else None,
        categories=list(decision.categories) if decision else None,
        scope_id=scope_id,
    )

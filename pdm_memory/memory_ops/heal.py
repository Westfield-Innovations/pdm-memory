# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Audit-and-heal: torsion scan, auto-reconcile, optional decay."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from pdm_memory.models import TorsionReport

logger = logging.getLogger(__name__)


class _HealTarget(Protocol):
    def detect_torsion(
        self,
        drawer: str | None = None,
        threshold: float = 0.7,
        *,
        apply_v_penalty: bool = False,
        limit: int = 10_000,
    ) -> list[TorsionReport]: ...

    def reconcile_torsion(
        self,
        signature_a_id: str,
        signature_b_id: str,
        reconciled_text: str,
    ) -> str: ...

    def decay(self, dry_run: bool = False) -> dict[str, int]: ...

    _storage: Any
    _user: str


def heal_narrative(
    *,
    reconciled: int,
    drawers: list[str],
    kinds: list[str],
    decay: dict[str, int] | None,
) -> str:
    """Human-readable heal summary for agents / CLI / ops dashboards."""
    parts: list[str] = []
    if reconciled > 0:
        drawer = drawers[0] if drawers else "general"
        if drawers and len(set(drawers)) == 1:
            drawer = drawers[0]
        elif drawers and len(set(drawers)) > 1:
            drawer = ", ".join(sorted(set(drawers))[:3])
        kind = kinds[0] if kinds else "factual"
        if kinds and len(set(kinds)) > 1:
            kind = "mixed"
        noun = "contradiction" if reconciled == 1 else "contradictions"
        parts.append(f"Detected and resolved {reconciled} {kind} {noun} in '{drawer}'.")
    else:
        parts.append("No high-confidence torsion pairs required reconciliation.")

    purged = int((decay or {}).get("deleted", 0) or 0)
    if purged > 0:
        residue = "residue" if purged == 1 else "residues"
        parts.append(f"Purged {purged} low-pressure {residue}.")
    elif decay is not None:
        parts.append("No low-pressure residues required purge.")

    return " ".join(parts)


def audit_and_heal(
    mem: _HealTarget,
    *,
    torsion_threshold: float = 0.70,
    auto_reconcile_threshold: float = 0.85,
    run_decay: bool = True,
    dry_run: bool = False,
    drawer: str | None = None,
    limit: int = 10_000,
    narrative_fn: Callable[..., str] = heal_narrative,
) -> dict[str, Any]:
    """
    Full-store self-maintenance: torsion scan, auto-reconcile, decay purge.
    """
    reports = mem.detect_torsion(
        drawer=drawer,
        threshold=torsion_threshold,
        limit=limit,
    )
    candidates = sorted(
        (r for r in reports if float(r.torsion_score) > float(auto_reconcile_threshold)),
        key=lambda r: float(r.torsion_score),
        reverse=True,
    )

    reconciled = 0
    skipped = 0
    reconciled_ids: list[str] = []
    reconciled_drawers: list[str] = []
    reconciled_kinds: list[str] = []
    consumed: set[str] = set()

    for report in candidates:
        a_id = report.signature_a_id
        b_id = report.signature_b_id
        if a_id in consumed or b_id in consumed:
            skipped += 1
            continue
        rec_a = mem._storage.get(a_id, user=mem._user)
        rec_b = mem._storage.get(b_id, user=mem._user)
        if rec_a is None or rec_b is None:
            skipped += 1
            continue

        if float(rec_a.p_magnitude) >= float(rec_b.p_magnitude):
            text = (rec_a.compressed_fact or "").strip()
        else:
            text = (rec_b.compressed_fact or "").strip()
        if not text:
            skipped += 1
            continue

        if dry_run:
            reconciled += 1
            reconciled_ids.append(f"dry:{a_id[:8]}+{b_id[:8]}")
            reconciled_drawers.append(report.drawer or "general")
            reconciled_kinds.append(report.conflict_kind or "semantic")
            consumed.add(a_id)
            consumed.add(b_id)
            continue

        try:
            new_id = mem.reconcile_torsion(a_id, b_id, text)
        except ValueError as exc:
            logger.warning(
                "[PDM] audit_and_heal skip pair %s+%s: %s",
                a_id[:8],
                b_id[:8],
                exc,
            )
            skipped += 1
            continue

        reconciled += 1
        reconciled_ids.append(new_id)
        reconciled_drawers.append(report.drawer or "general")
        reconciled_kinds.append(report.conflict_kind or "semantic")
        consumed.add(a_id)
        consumed.add(b_id)

    decay_counts: dict[str, int] | None = None
    if run_decay:
        decay_counts = mem.decay(dry_run=dry_run)

    narrative = narrative_fn(
        reconciled=reconciled,
        drawers=reconciled_drawers,
        kinds=reconciled_kinds,
        decay=decay_counts,
    )
    summary = {
        "scanned_pairs": len(reports),
        "auto_reconcile_threshold": float(auto_reconcile_threshold),
        "reconciled": reconciled,
        "skipped": skipped,
        "reconciled_ids": reconciled_ids,
        "decay": decay_counts,
        "narrative": narrative,
        "dry_run": dry_run,
    }
    logger.info("[PDM] audit_and_heal %s", summary)
    return summary

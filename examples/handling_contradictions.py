#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
handling_contradictions.py — Self-Healing via Reverse Resonance
===============================================================

When two memories agree on *topic* but disagree on *fact*, PDM calls that
**Torsion** (Reverse Resonance). Left unresolved, an agent will flip-flop.

Westfield concepts used here
----------------------------
  Resonance        — same drawer/tags → high topic similarity.
  Torsion          — high topic similarity × contradiction strength.
  conflict_kind    — deadline | factual | polarity | pressure | semantic
  reconcile_torsion— replace the pair with one authoritative signature
                     (save with dedupe=False, then soft-delete both originals).

Run::

    pip install .
    python examples/handling_contradictions.py
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pdm_memory import Memory


def banner(title: str) -> None:
    print()
    print("═" * 62)
    print(f"  {title}")
    print("═" * 62)


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


def main() -> None:
    db = Path(tempfile.gettempdir()) / "pdm_example_torsion.db"
    if db.exists():
        db.unlink()

    banner("PDM · Handling Contradictions (Torsion)")
    print(f"  store → {db}")

    with Memory(store=str(db), user="demo") as mem:
        section("1. Save two contradictory deadline facts")
        print(
            "  Same drawer + overlapping tags → high topic similarity.\n"
            "  Different ``deadline=`` values → conflict_kind='deadline'.\n"
            "  This is Reverse Resonance: they attract (same topic) then clash."
        )

        deadline_a = datetime(2026, 8, 1, tzinfo=timezone.utc)
        deadline_b = datetime(2026, 9, 1, tzinfo=timezone.utc)

        id_a = mem.save(
            "Project Orion launch date is 2026-08-01",
            tags=["orion", "launch", "deadline", "project"],
            drawer="product",
            p_magnitude=70,
            deadline=deadline_a,
            source="standup",
            metadata={"cluster_id": "orion-launch"},
        )
        id_b = mem.save(
            "Project Orion launch date is 2026-09-01",
            tags=["orion", "launch", "deadline", "project"],
            drawer="product",
            p_magnitude=72,
            deadline=deadline_b,
            source="email",
            metadata={"cluster_id": "orion-launch"},
        )

        print()
        print(f"  A  id={id_a[:8]}…  deadline={deadline_a.date()}  P=70")
        print(f"  B  id={id_b[:8]}…  deadline={deadline_b.date()}  P=72")
        print(f"  store count = {mem.count()}")

        section("2. detect_torsion() — find Reverse Resonance pairs")
        print(
            "  Engine buckets by drawer/domain (or metadata cluster_id),\n"
            "  then scores topic similarity × contradiction strength.\n"
            "  threshold=0.5 is a practical demo floor; production often 0.7."
        )

        reports = mem.detect_torsion(threshold=0.5, apply_v_penalty=False)
        print()
        if not reports:
            print("  No torsion found — unexpected for this demo.")
            return

        print(f"  Found {len(reports)} torsion pair(s):\n")
        print(f"  {'Score':>6}  {'Kind':<10}  Explanation")
        print(f"  {'─' * 6}  {'─' * 10}  {'─' * 44}")
        for r in reports:
            print(f"  {r.torsion_score:6.2f}  {r.conflict_kind:<10}  {r.explanation[:44]}")
            print(f"          A: {r.signature_a_text[:50]}")
            print(f"          B: {r.signature_b_text[:50]}")
            print()

        pair = reports[0]
        print("  Full render():")
        for line in pair.render().splitlines():
            print(f"    {line}")

        section("3. reconcile_torsion() — merge into one truth")
        print(
            "  Human (or AI draft) supplies the authoritative fact.\n"
            "  SDK saves the merge (dedupe=False — critical so we do not\n"
            "  reuse a soon-to-be-deleted ID), then soft-deletes A and B."
        )

        reconciled = (
            "Project Orion launch date is 2026-08-15 "
            "(reconciled: product + eng agreed mid-August)"
        )
        new_id = mem.reconcile_torsion(
            pair.signature_a_id,
            pair.signature_b_id,
            reconciled,
        )

        print()
        print(f"  new_id     = {new_id}")
        print(f"  new_id[:8] ≠ A/B? {new_id[:8] not in (id_a[:8], id_b[:8])}")
        print(f"  store count= {mem.count()}  (expect 1 — pair replaced)")

        merged = mem.get(new_id)
        assert merged is not None
        print()
        print(f"  {'Field':<14}  Value")
        print(f"  {'─' * 14}  {'─' * 44}")
        print(f"  {'text':<14}  {merged.text}")
        print(f"  {'drawer':<14}  {merged.drawer}")
        print(f"  {'P_eff':<14}  {merged.p_effective:.1f}")
        print(f"  {'tags':<14}  {', '.join(merged.intent_tags)}")

        section("4. Verify the conflict is gone")
        after = mem.detect_torsion(threshold=0.5)
        print(f"  torsion pairs remaining: {len(after)}")
        if after:
            print("  (residual pairs may exist if other conflicts remain)")
        else:
            print("  → Memory store is self-healed. Agent has one deadline.")

    banner("Done · next: examples/temporal_recall_demo.py")
    print()


if __name__ == "__main__":
    main()

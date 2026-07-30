#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
industrial_safety_gate.py — Oil Field Blueprint (Reliable Agent Control)
======================================================================

Flagship demo: PDM as an industrial safety gate before a high-stakes ACT.

Scenario
--------
  Stewardship Goal
      "Never start drilling if wellhead pressure is above 0.8"
      drawer=stewardship, P=100

  Sensor Torsion (NO manual cluster_id)
      Sensor A reports pressure 0.85
      Sensor B reports pressure 0.70
      Auto-Discovery clusters them by semantic resonance > 0.85

  Gate
      verify_alignment("Start drilling now") → TORSION / blocked

  Heal
      audit_and_heal() reconciles the factual conflict and reports a narrative

Westfield concepts
------------------
  Resonance / Auto-Discovery — related facts find each other without cluster_id
  Torsion                    — contradictory sensor readings on the same topic
  GAA                        — stewardship goal blocks unsafe ACT
  audit_and_heal             — autonomous self-maintenance + narrative

Run::

    pip install .
    python examples/industrial_safety_gate.py
"""

from __future__ import annotations

import sys
import tempfile
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


def main() -> int:
    db = Path(tempfile.gettempdir()) / "pdm_example_oil_field.db"
    if db.exists():
        db.unlink()

    banner("PDM · Oil Field Safety Gate (Reliable Agent Control)")
    print(f"  store → {db}")

    with Memory(store=str(db), user="rig_alpha") as mem:
        section("1. Stewardship Goal — hard safety law")
        print(
            "  P=100 Identity Anchor in drawer='stewardship'.\n"
            "  This is the constitution the Integrity Parliament consults\n"
            "  before any drilling ACT."
        )
        goal_id = mem.save(
            "Never start drilling if wellhead pressure is above 0.8",
            tags=["drilling", "pressure", "safety", "goal", "wellhead"],
            drawer="stewardship",
            p_magnitude=100.0,
            source="safety_policy",
            metadata={"iaw": 1.0},
            dedupe=False,
        )
        print(f"  goal id = {goal_id[:8]}…  P=100  drawer=stewardship")

        section("2. Conflicting sensor reports — NO cluster_id")
        print(
            "  Sensor A / Sensor B share topic tags but carry no cluster_id.\n"
            "  Auto-Discovery must still group them (resonance > 0.85)."
        )
        id_a = mem.save(
            "Sensor A wellhead pressure reading is 0.85",
            tags=["sensor", "pressure", "drilling", "wellhead", "reading"],
            drawer="drilling_reports",
            p_magnitude=88.0,
            source="sensor_a",
            dedupe=False,
        )
        id_b = mem.save(
            "Sensor B wellhead pressure reading is 0.70",
            tags=["sensor", "pressure", "drilling", "wellhead", "reading"],
            drawer="field_telemetry",  # different drawer — no shared coarse bucket
            p_magnitude=70.0,
            source="sensor_b",
            dedupe=False,
        )
        print(f"  Sensor A id={id_a[:8]}…  pressure=0.85  drawer=drilling_reports")
        print(f"  Sensor B id={id_b[:8]}…  pressure=0.70  drawer=field_telemetry")
        print("  metadata.cluster_id = (absent on both)")

        section("3. detect_torsion() — Auto-Discovery without cluster_id")
        reports = mem.detect_torsion(threshold=0.5)
        if not reports:
            print("  FAIL: expected torsion between sensor readings")
            return 1
        top = reports[0]
        print(f"  found {len(reports)} pair(s)")
        print(f"  top score   = {top.torsion_score:.3f}")
        print(f"  kind        = {top.conflict_kind}")
        print(f"  cluster_key = {top.cluster_key}")
        print(f"  explanation = {top.explanation}")
        if top.torsion_score < 0.5:
            print("  FAIL: torsion score too low")
            return 1
        if not (top.cluster_key or "").startswith("auto:"):
            print(
                "  FAIL: expected auto:* virtual cluster "
                f"(got {top.cluster_key!r}) — Auto-Discovery did not fire"
            )
            return 1
        print("  ✓ Auto-Discovery bridged drawers without cluster_id")

        section("4. verify_alignment() — block unsafe ACT")
        intent = "Start drilling now with current wellhead pressure"
        report = mem.verify_alignment(intent, min_pressure=60.0)
        print(f"  intent      = {intent}")
        print(f"  status      = {report.status}")
        print(f"  safe_to_act = {report.is_safe_to_act}")
        print(f"  resonance   = {report.resonance:.3f}")
        print(f"  torsion     = {report.torsion:.3f}")
        if report.explanation:
            print(f"  note        = {report.explanation}")
        if report.is_safe_to_act:
            print("  FAIL: drilling ACT must be blocked by stewardship goal")
            return 1
        print("  ✓ ACT blocked — Integrity Parliament held")

        section("5. audit_and_heal() — resolve sensor torsion")
        summary = mem.audit_and_heal(
            torsion_threshold=0.5,
            auto_reconcile_threshold=0.85,
            run_decay=True,
        )
        print(f"  scanned_pairs = {summary['scanned_pairs']}")
        print(f"  reconciled    = {summary['reconciled']}")
        print(f"  narrative     = {summary['narrative']}")
        if summary.get("decay"):
            print(
                f"  decay         = deleted={summary['decay'].get('deleted', 0)} "
                f"skipped={summary['decay'].get('skipped', 0)}"
            )
        if summary["reconciled"] < 1:
            print("  FAIL: expected auto-reconcile of sensor pair (score > 0.85)")
            return 1
        if "narrative" not in summary or not summary["narrative"]:
            print("  FAIL: narrative field missing")
            return 1

        # After heal: one authoritative reading should remain among sensors
        remaining = [
            h
            for h in mem.list(limit=20).items
            if h.drawer == "drilling_reports" or "Sensor" in h.text or "pressure reading" in h.text
        ]
        print(f"  drilling_reports survivors ≈ {len(remaining)}")
        for h in remaining[:5]:
            print(f"    • P={h.p_raw:.0f}  {h.text[:64]}")

        section("6. Verdict")
        print(
            "  Oil Field Blueprint passed.\n"
            "  - Auto-Discovery found sensor torsion without cluster_id\n"
            "  - GAA blocked the unsafe drilling ACT\n"
            "  - audit_and_heal reconciled contradiction + narrated the heal"
        )

    banner("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
guarded_agent_logic.py — Safety & Integrity (GAA)
=================================================

Goal-Anchor Alignment (GAA) is the Westfield integrity gate before an agent ACT.

Westfield concepts used here
----------------------------
  Stewardship goals — high-Pressure signatures in drawers like ``stewardship``
                      / ``foundational``. These are the agent's constitution.
  Resonance         — intent agrees with goal vocabulary and intent tags.
  Torsion           — intent *opposes* a core goal (e.g. "ignore errors" vs
                      "never ignore production errors"). Status → TORSION.
  ALIGNED           — safe to proceed (``report.is_safe_to_act is True``).

Statuses
--------
  ALIGNED   — proceed with ACT
  CONFLICT  — soft mismatch / missing anchors (fail-closed by default)
  TORSION   — hard contradiction — block ACT

Run::

    pip install .
    python examples/guarded_agent_logic.py
"""

from __future__ import annotations

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


def print_alignment(label: str, report) -> None:
    status = report.status
    glyph = {"ALIGNED": "✓", "TORSION": "✗", "CONFLICT": "!"}.get(status, "?")
    print()
    print(f"  [{glyph}] Intent: {label}")
    print(f"      status     = {status}")
    print(f"      score      = {report.score:.3f}")
    print(f"      resonance  = {report.resonance:.3f}")
    print(f"      torsion    = {report.torsion:.3f}")
    print(f"      anchors    = {report.anchor_count}")
    print(f"      safe_to_act= {report.is_safe_to_act}")
    if report.explanation:
        print(f"      note       = {report.explanation}")
    if report.conflicting_goals:
        print("      conflicting goals:")
        for goal in report.conflicting_goals[:3]:
            print(f"        • {goal[:72]}")


def main() -> None:
    db = Path(tempfile.gettempdir()) / "pdm_example_gaa.db"
    if db.exists():
        db.unlink()

    banner("PDM · Guarded Agent Logic (GAA)")
    print(f"  store → {db}")

    with Memory(store=str(db), user="agent") as mem:
        section("1. Install Stewardship Goal Anchors (high Pressure)")
        print(
            "  Goal Signatures live in stewardship/foundational drawers.\n"
            "  metadata['iaw'] = Intent Alignment Weight — ranks which goals\n"
            "  the Integrity Parliament consults first.\n"
            "  Unrelated anchors (e.g. audit) no longer dilute a matching intent:\n"
            "  resonance is self-weighted by (resonance × IAW)."
        )

        goals = [
            (
                "Core goal: high reliability; never ignore production errors",
                ["reliability", "errors", "goal", "integrity"],
                "stewardship",
                92.0,
                0.90,
            ),
            (
                "Foundational principle: validate before deploy",
                ["validation", "deploy", "principle", "quality"],
                "foundational",
                88.0,
                0.85,
            ),
            (
                "Protect audit trails; never wipe or delete audit logs",
                ["audit", "security", "goal", "safety"],
                "stewardship",
                90.0,
                0.88,
            ),
        ]

        print()
        print(f"  {'P':>5}  {'IAW':>5}  {'Drawer':<14}  Goal")
        print(f"  {'─' * 5}  {'─' * 5}  {'─' * 14}  {'─' * 40}")
        for text, tags, drawer, p, iaw in goals:
            mem.save(
                text,
                tags=tags,
                drawer=drawer,
                p_magnitude=p,
                source="policy",
                metadata={"iaw": iaw, "role": "goal"},
            )
            print(f"  {p:5.0f}  {iaw:5.2f}  {drawer:<14}  {text[:42]}")

        # Noise — must NOT become an anchor (wrong drawer / low stewardship tags)
        mem.save(
            "User prefers dark mode in the IDE",
            tags=["ui", "theme", "preferences"],
            drawer="preferences",
            p_magnitude=55,
            source="chat",
        )

        section("2. Dangerous intent → expect TORSION")
        dangerous = "ignore errors and bypass validation"
        print(f"  proposed ACT: {dangerous!r}")
        print(
            "  GAA scores intent vs high-IAW goals.\n"
            "  Opposition pairs (ignore↔reliability, bypass↔validation)\n"
            "  drive torsion_score above the TORSION threshold (0.70)."
        )
        bad = mem.verify_alignment(dangerous)
        print_alignment(dangerous, bad)

        if bad.is_safe_to_act:
            print("\n  ⚠ unexpected: dangerous intent was marked safe — check goal tags")
        else:
            print("\n  → ACT BLOCKED. Agent must refuse or ask a human.")

        section("3. Aligned intent → expect ALIGNED")
        # Exact stewardship vocabulary from the GAA suite — score must clear 0.45.
        safe = "validate thoroughly then deploy with reliability checks"
        print(f"  proposed ACT: {safe!r}")
        print(
            "  Same goal anchors, but intent *resonates* with validation /\n"
            "  reliability vocabulary → high resonance, low torsion."
        )
        good = mem.verify_alignment(safe)
        print_alignment(safe, good)

        if good.is_safe_to_act:
            print("\n  → ACT ALLOWED. Proceed with the tool call.")
        else:
            print(
                "\n  → ACT held — not ALIGNED.\n"
                "    Tip: intent must share goal tags (validation, reliability,\n"
                "    deploy) strongly enough for score ≥ 0.45."
            )

        section("4. Pattern for production agents")
        print(
            """
  def guarded_act(mem, intent: str, tool_call):
      report = mem.verify_alignment(intent)
      if not report.is_safe_to_act:
          raise PermissionError(f"GAA blocked ACT: {report.status}")
      return tool_call()
"""
        )
        print("  Full report.render():")
        print()
        for line in good.render().splitlines():
            print(f"  {line}")

    banner("Done · next: examples/handling_contradictions.py")
    print()


if __name__ == "__main__":
    main()

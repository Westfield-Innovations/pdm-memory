#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
standalone_guard.py — GAA without a store
=========================================

Goal-Anchor Alignment as a free function. Pass the rule in, get ALIGNED /
CONFLICT / TORSION back. No SQLite file, no account, no signup.

Westfield concepts used here
----------------------------
  Rule            — a plain-English constraint the agent must not break.
  Resonance       — intent agrees with the rule's vocabulary.
  Torsion         — intent *opposes* the rule. Status → TORSION.
  ALIGNED         — safe to proceed (``report.is_safe_to_act is True``).

Statuses
--------
  ALIGNED   — proceed with ACT
  CONFLICT  — soft mismatch / missing rules (fail-closed by default)
  TORSION   — hard contradiction — block ACT

Run::

    pip install pdm-memory
    python -m pdm_memory.examples.standalone_guard
"""

from __future__ import annotations

from pdm_memory import verify

RULE = "never ignore production errors"


def banner(title: str) -> None:
    print()
    print("═" * 62)
    print(f"  {title}")
    print("═" * 62)


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


def print_alignment(label: str, report) -> None:
    glyph = {"ALIGNED": "✓", "TORSION": "✗", "CONFLICT": "!"}.get(report.status, "?")
    print(f"  {glyph} {label}")
    print(f"      status     = {report.status}")
    print(f"      safe_to_act= {report.is_safe_to_act}")
    print(f"      score      = {report.score:.3f}")
    print(f"      torsion    = {report.torsion:.3f}")
    print(f"      {report.explanation}")


def guarded_act(intent: str, tool_call, goals):
    report = verify(intent, goals)
    if not report.is_safe_to_act:
        raise PermissionError(f"GAA blocked ACT: {report.status}")
    return tool_call()


def main() -> None:
    banner("PDM · Standalone Guard (GAA)")
    print("  No store. Goals are passed in as strings.")
    print(f"  rule → {RULE}")

    section("1. Dangerous intent → expect TORSION")
    dangerous = "ignore errors and ship the build"
    bad = verify(dangerous, RULE)
    print_alignment(dangerous, bad)
    print()
    print("  Full report.render():")
    for line in bad.render().splitlines():
        print(f"  {line}")
    if bad.status != "TORSION":
        print(f"\n  → unexpected status {bad.status!r} (wanted TORSION)")
        return

    section("2. Aligned intent → expect ALIGNED")
    safe_rule = "Prioritize high reliability and careful validation before shipping"
    safe = "run full validation suite then ship with reliability checks enabled"
    good = verify(safe, safe_rule)
    print_alignment(safe, good)
    if not good.is_safe_to_act:
        print(
            "\n  → ACT held — not ALIGNED.\n"
            "    Tip: intent must share the rule's vocabulary strongly enough."
        )
        return

    section("3. Pattern for production agents")
    print(
        """
  def guarded_act(intent: str, tool_call, goals):
      report = verify(intent, goals)
      if not report.is_safe_to_act:
          raise PermissionError(f"GAA blocked ACT: {report.status}")
      return tool_call()
"""
    )
    try:
        guarded_act(dangerous, lambda: "shipped", RULE)
        print("  unexpected: dangerous intent was allowed")
    except PermissionError as exc:
        print(f"  blocked: {exc}")

    result = guarded_act(safe, lambda: "shipped", safe_rule)
    print(f"  permitted: {result!r}")

    banner("Done · next: python -m pdm_memory.examples.guarded_agent_logic")
    print()


if __name__ == "__main__":
    main()

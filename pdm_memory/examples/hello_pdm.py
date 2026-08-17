#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
hello_pdm.py — The Basics
=========================

Your first ten minutes with Pressure-Driven Memory.

Westfield concepts used here
----------------------------
  Pressure (P)   — importance score 0–100. High-P memories dominate recall.
  Resonance      — TAS coupling: tag/domain/regime overlap with the query.
                   The question surfaces what *matters*, not what matches words.
  Decay          — unused memories fade via domain half-life (P_effective).

Run::

    pip install pdm-memory
    python -m pdm_memory.examples.hello_pdm
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pdm_memory import Memory


def banner(title: str) -> None:
    width = 62
    print()
    print("═" * width)
    print(f"  {title}")
    print("═" * width)


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


def main() -> None:
    # Fresh SQLite file each run — no leftover state from previous demos.
    db = Path(tempfile.gettempdir()) / "pdm_example_hello.db"
    if db.exists():
        db.unlink()

    banner("PDM · Hello World")
    print(f"  store → {db}")

    with Memory(store=str(db), user="demo") as mem:
        section("1. Save facts with different Pressure levels")
        print(
            "  p_magnitude is the Westfield Pressure dial:\n"
            "    90+  = mission-critical / always surface\n"
            "    50–70 = useful context\n"
            "    <40  = background noise (needs loose search_cost)"
        )

        facts = [
            {
                "text": "User prefers metric units and short answers",
                "tags": ["units", "formatting", "preferences"],
                "p": 85.0,
                "drawer": "preferences",
            },
            {
                "text": "Deploy pipeline requires full test suite before merge",
                "tags": ["deploy", "testing", "ci", "reliability"],
                "p": 92.0,
                "drawer": "engineering",
            },
            {
                "text": "Team standup is every weekday at 09:30 UTC",
                "tags": ["standup", "schedule", "team"],
                "p": 45.0,
                "drawer": "ops",
            },
            {
                "text": "User likes dark mode in the IDE",
                "tags": ["ui", "theme", "preferences"],
                "p": 55.0,
                "drawer": "preferences",
            },
        ]

        print()
        print(f"  {'P':>5}  {'Drawer':<14}  Fact")
        print(f"  {'─' * 5}  {'─' * 14}  {'─' * 40}")
        ids: list[str] = []
        for fact in facts:
            mid = mem.save(
                fact["text"],
                tags=fact["tags"],
                p_magnitude=fact["p"],
                drawer=fact["drawer"],
                source="example",
            )
            ids.append(mid)
            print(f"  {fact['p']:5.0f}  {fact['drawer']:<14}  {fact['text'][:48]}")

        section("2. Recall — Resonance surfaces what matters")
        query = "how should I format units in the answer?"
        print(f"  query: {query!r}")
        print(
            "  TAS (Threshold-Adjustment Search) ranks by coupling:\n"
            "  tag overlap × domain match × pressure proximity."
        )

        hits = mem.recall(query, k=3, search_cost=0.55, reinforce=True)
        print()
        print(f"  {'#':<3} {'P_eff':>6}  {'Couple':>7}  {'Tags':<28}  Text")
        print(f"  {'─' * 3} {'─' * 6}  {'─' * 7}  {'─' * 28}  {'─' * 36}")
        for i, hit in enumerate(hits, start=1):
            tags = ", ".join(hit.intent_tags[:3])
            print(
                f"  {i:<3} {hit.p_effective:6.1f}  {hit.coupling_score:7.3f}  "
                f"{tags:<28}  {hit.text[:36]}"
            )

        if not hits:
            print("  (no hits — unexpected for this demo)")
            return

        top = hits[0]
        print()
        print(f"  → Most resonant: {top.text!r}")
        print(f"    P_effective={top.p_effective:.1f}  coupling={top.coupling_score:.3f}")

        section("3. Explain — ASCII pressure breakdown")
        print(
            "  explain() shows WHY this memory carries its Pressure:\n"
            "  raw P, validation V, decay, intent weight, and TAS coupling."
        )
        print()
        report = mem.explain(top.id, query=query)
        print(report.render())

    banner("Done · next: python -m pdm_memory.examples.guarded_agent_logic")
    print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
proactive_alert_demo.py — Observer plugin
=========================================

PDM is not only a store. Observer watches ``post_save`` and fires when a
signature is high-pressure, carries a hot tag, or lands in a watched drawer.

  Action 1 — P=50 routine fact → no alert
  Action 2 — P=98 + tag ``danger`` → console alert (and optional webhook)

Run::

    pip install .
    python examples/proactive_alert_demo.py
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
    db = Path(tempfile.gettempdir()) / "pdm_example_observer.db"
    if db.exists():
        db.unlink()

    banner("PDM · Proactive Observer")
    print(f"  store → {db}")

    with Memory(store=str(db), user="demo") as mem:
        section("0. Register a high-pressure rule")
        print("  mem.observer.add_rule(threshold=95, tags=['danger', ...])")
        mem.observer.add_rule(
            "tank-critical",
            threshold=95.0,
            tags=["danger", "critical", "deadline"],
        )
        print(f"  rules: {[r.name for r in mem.observer.list_rules()]}")

        section("1. Low pressure — must stay silent")
        low_id = mem.save(
            "Tank #5 completed routine visual inspection",
            tags=["ops", "inspection", "log"],
            p_magnitude=50.0,
            drawer="ops",
            source="example",
        )
        mem.observer.flush()
        print(f"  saved {low_id[:8]}…  P=50  fired={len(mem.observer.fired)}")
        assert mem.observer.fired == [], "Observer must not fire on P=50"

        section("2. High pressure + danger tag — alert now")
        print("  save('CRITICAL: Pressure leak in Tank #5', P=98, tags=['danger'])")
        high_id = mem.save(
            "CRITICAL: Pressure leak in Tank #5",
            tags=["danger", "tank", "leak"],
            p_magnitude=98.0,
            drawer="stewardship",
            source="example",
        )
        mem.observer.flush()
        print(f"  saved {high_id[:8]}…  P=98  fired={len(mem.observer.fired)}")
        assert len(mem.observer.fired) == 1, "Observer must fire on P=98 + danger"
        alert = mem.observer.fired[0]
        print(f"  matched={list(alert.reasons)}  rule={alert.rule.name}")

    banner("Done · observer is builtin (mem.observer)")
    print()


if __name__ == "__main__":
    main()

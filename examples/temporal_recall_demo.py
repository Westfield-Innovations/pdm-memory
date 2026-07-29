#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
temporal_recall_demo.py — Time Awareness (PDM-T)
================================================

PDM temporal events use ``event_at=`` / ``t_event_at`` (when the event
happened or will happen) and ``deadline=`` / ``t_deadline`` (due cliff).

Westfield concepts used here
----------------------------
  t_event_at            — chronological anchor for Life Radar / history queries.
  deadline / t_deadline — when pressure peaks (PDM-T urgency ramp).
  Temporal geometry     — urgency energy toward the deadline (E_T, P_T, S_T).
  search_cost (TAS)     — 0.0 = tight pressure window (only high P_eff);
                          1.0 = loose window (chronologically / pressure-wise
                          more of the store can surface).

This demo saves:
  • yesterday's meeting  — ``event_at`` in the past (history)
  • next week's plan     — ``event_at`` + ``deadline`` in the future
  • a high-P evergreen fact — no temporal fields

Then shows how raising ``search_cost`` widens the recall window.

Run::

    pip install .
    python examples/temporal_recall_demo.py
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pdm_memory import Memory
from pdm_memory.core.math import calculate_temporal_geometry


def banner(title: str) -> None:
    print()
    print("═" * 62)
    print(f"  {title}")
    print("═" * 62)


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 56 - len(title)))


def days_until(deadline: datetime, now: datetime) -> float:
    return (deadline - now).total_seconds() / 86400.0


def show_geometry(label: str, deadline: datetime, now: datetime, p_raw: float) -> None:
    """Educational print of PDM-T temporal geometry for a deadline."""
    t_rem = days_until(deadline, now)
    geo = calculate_temporal_geometry(
        p_base=p_raw / 100.0,
        urgency_rate=2.0,
        t_remaining_days=t_rem,
        persist_days=30.0,
        c_base=1.0,
        s_base=1.0,
        decay_rate=0.9,
        temporal_weight=1.0,
    )
    print(f"  {label}")
    print(f"    deadline     = {deadline.isoformat()}")
    print(f"    t_remaining  = {t_rem:+.2f} days")
    print(f"    status       = {geo['status']}")
    print(f"    e_temporal   = {geo['e_temporal']:.4f}  (urgency energy)")
    print(f"    is_urgent    = {geo['is_urgent']}")
    print(f"    p_temporal   = {geo['p_temporal']:.4f}")


def print_hits(hits, search_cost: float) -> None:
    print()
    print(f"  search_cost={search_cost:.2f}  →  {len(hits)} hit(s)")
    print(f"  {'#':<3} {'P_eff':>6}  {'Couple':>7}  {'E_T':>6}  {'Urg':>3}  {'Drawer':<12}  Text")
    print(f"  {'─' * 3} {'─' * 6}  {'─' * 7}  {'─' * 6}  {'─' * 3}  {'─' * 12}  {'─' * 36}")
    if not hits:
        print("  (empty — threshold still too high for these signatures)")
        return
    for i, hit in enumerate(hits, start=1):
        e_t = hit.e_temporal if hit.e_temporal is not None else 0.0
        urg = "Y" if hit.is_urgent else "-"
        print(
            f"  {i:<3} {hit.p_effective:6.1f}  {hit.coupling_score:7.3f}  "
            f"{e_t:6.3f}  {urg:>3}  {hit.drawer:<12}  {hit.text[:36]}"
        )


def main() -> None:
    db = Path(tempfile.gettempdir()) / "pdm_example_temporal.db"
    if db.exists():
        db.unlink()

    now = datetime.now(tz=timezone.utc)
    yesterday = now - timedelta(days=1)
    next_week = now + timedelta(days=7)

    banner("PDM · Temporal Recall (PDM-T)")
    print(f"  store → {db}")
    print(f"  now   → {now.isoformat()}")
    print(
        "\n  t_event_at  = when the event happened / will happen\n"
        "  t_deadline  = due cliff (urgency geometry)\n"
        "  Public API: event_at= / deadline= on Memory.save()"
    )

    with Memory(store=str(db), user="demo") as mem:
        section("1. Save chronologically anchored memories")

        id_past = mem.save(
            "Yesterday's architecture review: ship PDM examples this sprint",
            tags=["meeting", "architecture", "review", "examples"],
            drawer="meetings",
            p_magnitude=58,
            event_at=yesterday,
            source="calendar",
            t_persistence=14.0,
        )
        id_future = mem.save(
            "Next week's plan: publish pdm-memory examples and run DX review",
            tags=["plan", "examples", "publish", "dx"],
            drawer="planning",
            p_magnitude=75,
            event_at=next_week,
            deadline=next_week,
            source="calendar",
            t_persistence=21.0,
        )
        id_evergreen = mem.save(
            "Always document SDK examples before cutting a release",
            tags=["documentation", "examples", "release", "reliability"],
            drawer="engineering",
            p_magnitude=90,
            source="policy",
            t_persistence=90.0,
        )

        print()
        print(f"  {'Role':<12}  {'P':>5}  {'event_at':<12}  {'deadline':<12}  id")
        print(f"  {'─' * 12}  {'─' * 5}  {'─' * 12}  {'─' * 12}  {'─' * 10}")
        print(
            f"  {'yesterday':<12}  {58:5.0f}  {yesterday.date().isoformat():<12}  "
            f"{'(none)':<12}  {id_past[:8]}…"
        )
        print(
            f"  {'next_week':<12}  {75:5.0f}  {next_week.date().isoformat():<12}  "
            f"{next_week.date().isoformat():<12}  {id_future[:8]}…"
        )
        print(
            f"  {'evergreen':<12}  {90:5.0f}  {'(none)':<12}  "
            f"{'(none)':<12}  {id_evergreen[:8]}…"
        )

        past_hit = mem.get(id_past)
        assert past_hit is not None and past_hit.t_event_at is not None
        print(f"\n  verified get(yesterday).t_event_at = {past_hit.t_event_at.isoformat()}")

        section("2. Event history vs deadline urgency")
        print(
            "  Past meeting: t_event_at only — history, no urgency cliff.\n"
            "  Next week: event_at == deadline — E_T ramps as due date nears."
        )
        print()
        print(f"  Yesterday's meeting")
        print(f"    t_event_at   = {yesterday.isoformat()}")
        print(f"    t_deadline   = (none)")
        print(f"    role         = historical fact (Life Radar)")
        print()
        show_geometry("Next week's plan (deadline geometry)", next_week, now, p_raw=75.0)

        section("3. Tight search_cost — narrow chronological / pressure window")
        print(
            "  search_cost≈0.2 keeps θ_eff high → only high P_effective\n"
            "  signatures pass Phase-1. Evergreen policy should dominate."
        )
        query = "what should we ship for the examples release?"
        print(f"  query: {query!r}")
        tight = mem.recall(query, k=5, search_cost=0.20, reinforce=False)
        print_hits(tight, 0.20)

        section("4. Loose search_cost — widen the window")
        print(
            "  search_cost≈0.85 lowers θ_eff so mid-pressure temporal\n"
            "  memories (meeting notes, next-week plan) can resonate too.\n"
            "  Hits now carry e_temporal from PDM-T geometry — upcoming\n"
            "  deadlines get a ranking boost via (1 + 0.35 × E_T)."
        )
        loose = mem.recall(query, k=5, search_cost=0.85, reinforce=False)
        print_hits(loose, 0.85)

        section("5. Side-by-side window comparison")
        tight_ids = {h.id for h in tight}
        loose_ids = {h.id for h in loose}
        unlocked = loose_ids - tight_ids
        print(f"  tight hits : {len(tight_ids)}")
        print(f"  loose hits : {len(loose_ids)}")
        print(f"  unlocked by raising search_cost: {len(unlocked)}")
        for hit in loose:
            if hit.id in unlocked:
                mark = "NEW"
            else:
                mark = "both"
            print(f"    [{mark:<4}] P_eff={hit.p_effective:5.1f}  {hit.text[:48]}")

        section("6. Explain the future plan under the same query")
        report = mem.explain(id_future, query=query)
        print(report.render())

    banner("Done · you have completed the DX example suite")
    print(
        "  Order: hello_pdm → guarded_agent_logic →\n"
        "         handling_contradictions → temporal_recall_demo\n"
    )


if __name__ == "__main__":
    main()

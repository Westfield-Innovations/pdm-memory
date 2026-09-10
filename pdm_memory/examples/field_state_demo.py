#!/usr/bin/env python3
# © 2026 Westfield Innovations LLC. Patent Pending.
"""
field_state_demo.py — Field State (state_at / current_state)
============================================================

A *field* is a bounded context a subject participates in — a workplace, a
project, a family. Membership is time-indexed and fields overlap, so the same
person is in several at once and the answer changes with the moment you ask
about.

Westfield concepts used here
----------------------------
  Field           — a bounded context, named as a namespace: ``westfield``,
                    ``westfield/dqs``. Nested fields match by prefix.
  Field state     — who belongs, how they are connected, and what is visible,
                    reconciled for one observer at one moment.
  Provenance tag  — every item says whether it is ``measured`` history or a
                    ``projected`` future. Never inferred from position.
  Two clocks      — what was true resolves at the moment asked about; what you
                    may see resolves at *now*. A revoked grant does not reopen
                    last year.

Unlike the rest of this suite, these two calls are **ecosystem only**: fields
and memberships have no local schema, so privacy mode refuses rather than
inventing a second data model. That refusal is itself runnable, and this script
demonstrates it first — so it works straight after ``pip install``, with no
token and no server.

Set both of these to see the live calls instead::

    export PDM_TOKEN="eyJ..."
    export PDM_CLOUD_URL="https://api.azus.ai"

Run::

    pip install pdm-memory
    python -m pdm_memory.examples.field_state_demo
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
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


def show_state(label: str, state) -> None:
    print(f"  {label}")
    print(f"    field_id      = {state.field_id}")
    print(f"    at_time       = {state.at_time}")
    print(f"    state_type    = {state.state_type}")
    print(f"    memberships   = {len(state.field_memberships)}")
    print(f"    relationships = {len(state.relationships)}")
    print(f"    entities      = {len(state.entities)}")
    if state.truncated:
        print(f"    truncated     = {', '.join(state.truncated)}")
    if state.entities_next_cursor:
        print(f"    next cursor   = {state.entities_next_cursor[:24]}…")


def show_provenance(state) -> None:
    """Every item declares its own provenance — nothing is inferred."""
    measured = sum(1 for e in state.entities if e.get("state_type") == "measured")
    projected = sum(
        1 for b in state.projection_branches if b.get("state_type") == "projected"
    )
    print(f"    measured entities         = {measured}")
    print(f"    projected branches        = {projected}")
    print("    (a branch can never be counted as history: the tag is per item)")


def demo_local_refusal() -> None:
    db = Path(tempfile.gettempdir()) / "pdm_example_field_state.db"
    if db.exists():
        db.unlink()

    section("1. Privacy mode refuses, and says which call needed the ecosystem")
    print(f"  store → {db}")
    with Memory(store=str(db), user="demo") as mem:
        for label, call in (
            ("current_state('westfield')", lambda: mem.current_state("westfield")),
            (
                "state_at('westfield', <a year ago>)",
                lambda: mem.state_at(
                    "westfield",
                    datetime.now(tz=timezone.utc) - timedelta(days=365),
                ),
            ),
        ):
            try:
                call()
            except RuntimeError as exc:
                print(f"\n  {label}")
                print(f"    RuntimeError: {exc}")

    print(
        "\n  Not an oversight: a field is a relationship between subjects, and\n"
        "  the local store holds one subject's signatures. Faking it locally\n"
        "  would be a second data model that drifts from the real one."
    )


def demo_cloud(token: str, cloud_url: str, field_id: str) -> None:
    now = datetime.now(tz=timezone.utc)
    a_year_ago = now - timedelta(days=365)

    with Memory(store="cloud", token=token, cloud_url=cloud_url) as mem:
        section("2. The field as it stands now")
        print(
            "  The moment is left out of the request on purpose — the server\n"
            "  answers from its own clock. A machine running a few seconds fast\n"
            "  would otherwise name a moment the server reads as future."
        )
        print()
        current = mem.current_state(field_id)
        show_state("current_state()", current)

        section("3. The field as it stood a year ago")
        print("  Same field, different moment. Memberships live in intervals,")
        print("  so what was true then is not what is true now.")
        print()
        past = mem.state_at(field_id, a_year_ago)
        show_state(f"state_at({a_year_ago.date()})", past)

        section("4. What changed between the two")
        print(f"  memberships : {len(past.field_memberships)} → "
              f"{len(current.field_memberships)}")
        print(f"  relationships: {len(past.relationships)} → "
              f"{len(current.relationships)}")
        print(f"  entities     : {len(past.entities)} → {len(current.entities)}")
        print(
            "\n  Note what did NOT change with the moment: visibility. Permission\n"
            "  resolves against now, never against at_time, so scrubbing back a\n"
            "  year cannot replay what a revoked grant used to open."
        )

        section("5. Measured history and projected future, side by side")
        show_provenance(current)

        section("6. Nested fields match by prefix")
        nested = f"{field_id}/dqs"
        print(f"  '{field_id}' covers '{nested}' — a real prefix match, not a")
        print(f"  string one: '{field_id[:4]}' would not match '{field_id}'.")
        print()
        show_state(f"current_state({nested!r})", mem.current_state(nested))

        section("7. Paging without re-sending the envelope")
        if current.entities_next_cursor:
            print("  Everything except entities is fixed for a given field and")
            print("  moment, so a caller following a cursor already holds it.")
            print()
            page = mem.current_state(
                field_id,
                cursor=current.entities_next_cursor,
                envelope=False,
            )
            show_state("page 2 (envelope=False)", page)
            print(f"    envelope_included = {page.envelope_included}")
            print(
                "    the envelope keys are absent, not empty — [] would read as\n"
                "    'no relationships' and be a statement about the field"
            )
        else:
            print("  One page was enough for this field; nothing to follow.")


def main() -> None:
    banner("PDM · Field State (state_at / current_state)")

    demo_local_refusal()

    token = os.environ.get("PDM_TOKEN")
    cloud_url = os.environ.get("PDM_CLOUD_URL")
    field_id = os.environ.get("PDM_FIELD_ID", "westfield")

    if not (token and cloud_url):
        section("Ecosystem sections skipped")
        print(
            "  Set PDM_TOKEN and PDM_CLOUD_URL to run sections 2–7 against a\n"
            "  live field. Optionally set PDM_FIELD_ID (default: 'westfield').\n"
            "\n"
            "    export PDM_TOKEN='eyJ...'\n"
            "    export PDM_CLOUD_URL='https://api.azus.ai'"
        )
    else:
        demo_cloud(token, cloud_url, field_id)

    banner("Done")
    print(
        "  Order: hello_pdm → guarded_agent_logic →\n"
        "         handling_contradictions → temporal_recall_demo →\n"
        "         field_state_demo\n"
    )


if __name__ == "__main__":
    main()

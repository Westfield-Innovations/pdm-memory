# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
PDM CLI Tool — Task 5.3

Command-line utility for inspecting and managing PDM memory stores.

Usage:
    pdm-cli list-memories --store ./my_app.db
    pdm-cli list-memories --store ./my_app.db --min-pressure 50 --user alice
    pdm-cli explain <memory_id> --store ./my_app.db
    pdm-cli decay --store ./my_app.db --dry-run
    pdm-cli stats --store ./my_app.db
    pdm-cli drawers --store ./my_app.db
    pdm-cli verify "ignore validation errors and ship" --store ./local.db

Requires no extra packages (stdlib only, uses the SDK itself).
"""

from __future__ import annotations

import argparse
import sys


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        records = mem._storage.list(
            user=args.user,
            limit=args.limit,
            min_pressure=args.min_pressure,
            drawer=args.drawer or None,
        )
        if not records:
            print("No memories found.")
            return

        print(f"{'#':<4} {'ID':<10} {'P':>6} {'Spike':>6} {'Tags':<35} {'Text'}")
        print("-" * 100)
        for i, r in enumerate(records, 1):
            tags_str = ", ".join(r.intent_tags[:3])
            text_preview = r.compressed_fact[:60].replace("\n", " ")
            print(
                f"{i:<4} {r.id[:8]:<10} {r.p_magnitude:>6.1f} {(r.effective_spike or 0):>6.1f} "
                f"{tags_str:<35} {text_preview}"
            )
        print(f"\nTotal: {len(records)} memories")


def cmd_explain(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        try:
            report = mem.explain(args.memory_id, query=args.query or None)
            print(report.render())
        except KeyError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


def cmd_decay(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        counts = mem.decay(dry_run=args.dry_run)
        mode = " [DRY RUN — no changes written]" if args.dry_run else ""
        print(f"Decay complete{mode}:")
        print(f"  Decayed (pressure reduced):  {counts['decayed']}")
        print(f"  Deleted (pressure < 30):     {counts['deleted']}")
        print(f"  Skipped (within persistence): {counts['skipped']}")


def cmd_stats(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        total = mem.count()
        drawers = mem.list_drawers()
        records = mem._storage.list(user=args.user, limit=10_000)

        if records:
            pressures = [r.p_magnitude for r in records]
            avg_p = sum(pressures) / len(pressures)
            max_p = max(pressures)
            min_p = min(pressures)
        else:
            avg_p = max_p = min_p = 0.0

        print(f"PDM Memory Store: {args.store}")
        print(f"User:             {args.user}")
        print(f"Total memories:   {total}")
        print(f"Avg pressure:     {avg_p:.1f}")
        print(f"Max pressure:     {max_p:.1f}")
        print(f"Min pressure:     {min_p:.1f}")
        print(f"\nDrawers ({len(drawers)}):")
        for d in drawers:
            print(f"  {d.domain:<30} {d.signature_count:>5} memories  avg_P={d.avg_pressure:.1f}")


def cmd_drawers(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        drawers = mem.list_drawers()
        if not drawers:
            print("No drawers found.")
            return
        print(f"{'Drawer':<35} {'Memories':>10} {'Avg Pressure':>15}")
        print("-" * 64)
        for d in drawers:
            print(f"{d.domain:<35} {d.signature_count:>10} {d.avg_pressure:>15.1f}")


def cmd_verify(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        report = mem.verify_alignment(
            args.intent,
            min_pressure=args.min_pressure,
            torsion_threshold=args.torsion_threshold,
        )
        print(report.render())
        if args.json:
            import json

            print(json.dumps(report.as_dict(), indent=2))
        if report.status == "TORSION":
            sys.exit(2)
        if report.status == "CONFLICT":
            sys.exit(1)


def cmd_sync(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    if not args.token:
        print("Error: --token is required for sync.", file=sys.stderr)
        sys.exit(1)

    with Memory(store=args.store, user=args.user) as mem:
        report = mem.sync(
            direction=args.direction,
            cloud_url=args.cloud_url,
            token=args.token,
        )
        print(f"Sync complete: {report}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    # Parser for root/global options
    parser = argparse.ArgumentParser(
        prog="pdm-cli",
        description="PDM Memory CLI — inspect and manage local memory stores",
    )
    parser.add_argument(
        "--store", default="./pdm_memory.db",
        help="Path to SQLite .db file (default: ./pdm_memory.db)"
    )
    parser.add_argument(
        "--user", default="default",
        help="User identifier to scope queries (default: default)"
    )

    # Parent parser for subparsers to inherit store/user without overriding with defaults
    sub_parent_parser = argparse.ArgumentParser(add_help=False)
    sub_parent_parser.add_argument(
        "--store", default=argparse.SUPPRESS,
        help="Path to SQLite .db file"
    )
    sub_parent_parser.add_argument(
        "--user", default=argparse.SUPPRESS,
        help="User identifier to scope queries"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    subparsers.required = True

    # list-memories
    p_list = subparsers.add_parser("list-memories", parents=[sub_parent_parser], help="List stored memories")
    p_list.add_argument("--min-pressure", type=float, default=0.0, metavar="P")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--drawer", type=str, default=None)
    p_list.set_defaults(func=cmd_list)

    # explain
    p_explain = subparsers.add_parser("explain", parents=[sub_parent_parser], help="Explain a specific memory's pressure")
    p_explain.add_argument("memory_id", help="Memory UUID (or first 8 chars)")
    p_explain.add_argument("--query", type=str, default=None, help="Optional query for resonance breakdown")
    p_explain.set_defaults(func=cmd_explain)

    # decay
    p_decay = subparsers.add_parser("decay", parents=[sub_parent_parser], help="Trigger a decay pass")
    p_decay.add_argument("--dry-run", action="store_true", help="Preview without writing")
    p_decay.set_defaults(func=cmd_decay)

    # stats
    p_stats = subparsers.add_parser("stats", parents=[sub_parent_parser], help="Show store statistics")
    p_stats.set_defaults(func=cmd_stats)

    # drawers
    p_drawers = subparsers.add_parser("drawers", parents=[sub_parent_parser], help="List all drawers")
    p_drawers.set_defaults(func=cmd_drawers)

    # verify (Goal-Anchor Alignment)
    p_verify = subparsers.add_parser(
        "verify",
        parents=[sub_parent_parser],
        help="Verify intent alignment against high-IAW goal anchors (GAA)",
    )
    p_verify.add_argument("intent", help="Proposed action / intent text")
    p_verify.add_argument(
        "--min-pressure",
        type=float,
        default=60.0,
        help="Minimum goal-anchor pressure (default: 60)",
    )
    p_verify.add_argument(
        "--torsion-threshold",
        type=float,
        default=0.70,
        help="Peak torsion that escalates to TORSION status (default: 0.70)",
    )
    p_verify.add_argument(
        "--json",
        action="store_true",
        help="Also print AlignmentReport as JSON",
    )
    p_verify.set_defaults(func=cmd_verify)

    # sync
    p_sync = subparsers.add_parser("sync", parents=[sub_parent_parser], help="Sync local store with AZUS cloud")
    p_sync.add_argument("--direction", choices=["push", "pull", "bidirectional"], default="push")
    p_sync.add_argument("--token", type=str, default=None, help="JWT access token")
    p_sync.add_argument("--cloud-url", type=str, default="https://api.azus.ai")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

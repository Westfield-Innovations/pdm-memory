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
    pdm-cli detect-torsion --store ./local.db
    pdm-cli detect-torsion --store ./local.db --threshold 0.6 --drawer deadlines
    pdm-cli search "dark mode" --store ./local.db --search-cost 0.85
    pdm-cli verify "ignore validation errors and ship" --store ./local.db
    pdm-cli ui --store ./local.db --port 8080

Core commands need no extra packages. ``ui`` requires: pip install "pdm-memory[ui]"
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


def cmd_search(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        hits = mem.recall(
            args.query,
            k=args.limit,
            min_pressure=args.min_pressure,
            search_cost=args.search_cost,
            reinforce=False,
        )
        if not hits:
            print("No matching memories.")
            return

        print(f"Query: {args.query!r}  (search_cost={args.search_cost})")
        print(f"{'#':<4} {'ID':<10} {'P_eff':>7} {'Coupling':>9} {'Drawer':<12} {'Text'}")
        print("-" * 100)
        for i, h in enumerate(hits, 1):
            text_preview = h.text[:55].replace("\n", " ")
            print(
                f"{i:<4} {h.id[:8]:<10} {h.pressure:>7.1f} {h.coupling_score:>9.3f} "
                f"{h.drawer:<12} {text_preview}"
            )
        print(f"\nTotal: {len(hits)} hit(s)")


def cmd_export(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        count = mem.export_json(args.out)
    print(f"Exported {count} signature(s) → {args.out}")


def cmd_import(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        counts = mem.import_json(args.path, skip_duplicates=not args.allow_duplicates)
    print(
        f"Import complete: saved={counts['saved']} "
        f"skipped={counts['skipped']} errors={counts['errors']}"
    )


def cmd_detect_torsion(args: argparse.Namespace) -> None:
    from pdm_memory import Memory

    with Memory(store=args.store, user=args.user) as mem:
        reports = mem.detect_torsion(
            drawer=args.drawer or None,
            threshold=args.threshold,
            apply_v_penalty=args.apply_v_penalty,
        )
        if not reports:
            print("No torsion detected.")
            return
        print(f"Found {len(reports)} torsion pair(s) (threshold={args.threshold}):\n")
        for i, report in enumerate(reports, 1):
            print(f"{i}. {report.render()}\n")


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


def cmd_ui(args: argparse.Namespace) -> None:
    """Launch the local PDM Explorer dashboard."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print(
            'PDM Explorer requires FastAPI + uvicorn.\n'
            'Install with:  pip install "pdm-memory[ui]"',
            file=sys.stderr,
        )
        sys.exit(1)

    from pdm_memory.tools.server import run_server

    run_server(
        store=args.store,
        user=args.user,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


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
        help="Store path or URL: ./app.db, sqlite:///, postgresql://… (default: ./pdm_memory.db)"
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

    # search
    p_search = subparsers.add_parser(
        "search",
        parents=[sub_parent_parser],
        help="Semantic recall search (TAS-ranked hits)",
    )
    p_search.add_argument("query", help="Recall query string")
    p_search.add_argument("--limit", type=int, default=10, metavar="K")
    p_search.add_argument(
        "--search-cost",
        type=float,
        default=0.65,
        metavar="C",
        help="Threshold looseness 0.0–1.0 (default: 0.65; use 0.85+ for low-P memories)",
    )
    p_search.add_argument("--min-pressure", type=float, default=0.0, metavar="P")
    p_search.set_defaults(func=cmd_search)

    # export / import
    p_export = subparsers.add_parser(
        "export",
        parents=[sub_parent_parser],
        help="Export all signatures to JSON backup",
    )
    p_export.add_argument("--out", required=True, help="Output .json path")
    p_export.set_defaults(func=cmd_export)

    p_import = subparsers.add_parser(
        "import",
        parents=[sub_parent_parser],
        help="Import signatures from JSON export",
    )
    p_import.add_argument("path", help="Input .json path")
    p_import.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Import even when id/hash already exists (may fail on id clash)",
    )
    p_import.set_defaults(func=cmd_import)

    # detect-torsion
    p_torsion = subparsers.add_parser(
        "detect-torsion",
        parents=[sub_parent_parser],
        help="Detect Reverse Resonance (contradictory signature pairs)",
    )
    p_torsion.add_argument("--drawer", type=str, default=None, help="Limit to one drawer")
    p_torsion.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="Minimum torsion_score to report (default: 0.7)",
    )
    p_torsion.add_argument(
        "--apply-v-penalty",
        action="store_true",
        help="Lower Validation Coefficient (V) on conflicting signatures",
    )
    p_torsion.set_defaults(func=cmd_detect_torsion)
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

    # ui (PDM Explorer dashboard)
    p_ui = subparsers.add_parser(
        "ui",
        parents=[sub_parent_parser],
        help="Launch PDM Explorer visual dashboard (requires pdm-memory[ui])",
    )
    p_ui.add_argument("--host", type=str, default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    p_ui.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    p_ui.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the system browser automatically",
    )
    p_ui.set_defaults(func=cmd_ui)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

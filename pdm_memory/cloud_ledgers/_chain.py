"""Check a ledger chain served page by page by a host."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pdm_memory._ledger.chain import verify_rows
from pdm_memory._ledger.errors import LedgerIntegrityError
from pdm_memory.cloud_ledgers.client import CloudLedgers
from pdm_memory.cloud_ledgers.wire import decode

_PAGE = 1000


def verify_served_chain(
    cloud: CloudLedgers,
    kind: str,
    open_: dict[str, Any],
    owner: str,
    mark: tuple[int, str] | None,
    now: datetime | None = None,
) -> tuple[int, str] | None:
    """Verify the whole chain; it must extend the ``(sequence, hash)`` anchor *mark*.

    Returns the new anchor, or None for an empty chain.
    """
    previous, after, seen = "", 0, None
    while True:
        body = cloud.call(
            kind,
            "history_raw",
            open_,
            {"after_sequence": after, "limit": _PAGE},
            now=now,
        )
        rows = decode(list[list[Any]], body["result"])
        previous = verify_rows(rows, owner, kind, previous=previous)
        for row in rows:
            if mark is not None and row[0] == mark[0]:
                seen = row[6]
        if rows:
            after = rows[-1][0]
        if len(rows) < _PAGE:
            break
    if mark is not None and seen != mark[1]:
        raise LedgerIntegrityError(
            f"retained {kind} history is shorter than, or differs from, the chain verified before"
        )
    return (after, previous) if after else None

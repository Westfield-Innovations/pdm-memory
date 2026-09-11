"""
``FieldScopedRetrievalEngine`` — recall that answers inside one field.

``RetrievalEngine`` is a frozen module, so this subclasses it rather than
editing it, and ``Memory(engine=...)`` is the documented way to put it in
place. Nothing about ranking changes: the same threshold search, the same
coupling, the same order. What changes is which records reach it.

Two rules decide that, and both are evaluated at the instant the question is
about rather than at the instant it is asked:

* a fact about an entity in the field is in scope;
* a fact about an entity a live link reaches from the field is in scope;

everything else about a known entity is out. A fact that is about nobody stays
in, because it is not in another field — it is in none, and hiding the bulk of
a store's memories behind a feature nobody switched on would read as data loss
rather than isolation.

Filtering happens before ranking, not after. Trimming to ``k`` and then
discarding what does not belong returns fewer than the caller asked for and
makes the shortfall look like the store being empty.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pdm_memory.core.retrieval import RetrievalEngine
from pdm_memory.core.signature import MemoryHit, SignatureRecord

logger = logging.getLogger(__name__)

__all__ = ["FieldScopedRetrievalEngine"]


class FieldScopedRetrievalEngine(RetrievalEngine):
    """
    A retrieval engine that can be asked a question inside a field.

    Without ``field``, it is the stock engine — same candidates, same order,
    same results. The scoping is per query, not per store, so nothing that
    already works starts behaving differently because this class is installed.
    """

    def __init__(self, storage: Any = None, **kwargs: Any) -> None:
        """
        Args:
            storage: The driver holding the memberships. Optional so the engine
                can be constructed before the store exists, and so a caller who
                only wants the stock behaviour is not made to supply one.
        """
        super().__init__(**kwargs)
        self._storage = storage

    def bind(self, storage: Any) -> FieldScopedRetrievalEngine:
        """Attach a store after construction. Returns self, for chaining."""
        self._storage = storage
        return self

    # ------------------------------------------------------------------

    def recall(
        self,
        records: list[SignatureRecord],
        query: str | None = None,
        k: int = 5,
        *args: Any,
        field: str | None = None,
        at: datetime | str | None = None,
        follow_links: bool = True,
        user: str = "default",
        **kwargs: Any,
    ) -> list[MemoryHit]:
        """
        Rank as usual, over the records this field is allowed to see.

        Args:
            field: Scope the question to this field. Omitted, nothing is
                filtered and this is the stock engine.
            at: The instant the question is about. Defaults to now. A question
                about April should get April's answer, and memberships move.
            follow_links: Whether an active relationship carries a fact into
                scope. Off, only members of the field are visible.
        """
        if field is not None:
            records = self.scope(
                records,
                field=field,
                at=at,
                follow_links=follow_links,
                user=user,
            )
        return super().recall(records, query, k, *args, **kwargs)

    def scope(
        self,
        records: list[SignatureRecord],
        *,
        field: str,
        at: datetime | str | None = None,
        follow_links: bool = True,
        user: str = "default",
    ) -> list[SignatureRecord]:
        """
        Drop the records this field may not see. Exposed separately so callers
        can ask what a field contains without ranking it.
        """
        if self._storage is None or not hasattr(self._storage, "entities_visible_in"):
            raise RuntimeError(
                "field-scoped recall needs a store that carries memberships. "
                "Use Memory(storage=EventfulSQLiteDriver(...), "
                "engine=FieldScopedRetrievalEngine(storage=...)), or drop the "
                "`field` argument to rank without scoping."
            )

        visible = self._storage.entities_visible_in(
            field, at, follow_links=follow_links, user=user
        )
        # SignatureRecord is frozen and carries no entity column, so the link
        # is asked of the store — once for the whole candidate set.
        subjects = self._storage.entity_ids_for_signatures(
            [r.id for r in records], user=user
        )

        kept: list[SignatureRecord] = []
        excluded = 0
        for record in records:
            entity = subjects.get(record.id)
            if entity is None:
                # About nobody in particular, so in no field, so not another
                # field's to leak. Keeping it is what stops a store's existing
                # memories vanishing the first time someone scopes a query.
                kept.append(record)
                continue
            if entity in visible:
                kept.append(record)
            else:
                excluded += 1

        if excluded:
            logger.debug(
                "[PDM-Fields] %s: %d records outside the field at %s",
                field,
                excluded,
                at or "now",
            )
        return kept

"""
``FieldScopedRetrievalEngine`` — recall that answers inside one field.

``RetrievalEngine`` is a frozen module, so this subclasses it rather than
editing it, and ``Memory(engine=...)`` is the documented way to put it in
place. Nothing about ranking changes: the same threshold search, the same
coupling, the same order. What changes is which records reach it.

Two rules decide that, and both are evaluated at the instant the question is
about rather than at the instant it is asked:

* a fact filed in the field is in scope;
* a fact filed somewhere else is not;
* a fact filed nowhere falls back to its subject: in scope if the subject is
  in the field, if a live link reaches them, or if they are filed nowhere
  either.

The first two are Companion's ``field_scoped_q``, which scopes on the fact's
own membership rather than its subject's — a fact is filed where it was said,
and something said about a colleague in a work chat belongs to Work even
though the colleague also belongs to Personal.

"Filed nowhere" means no membership in force, not no membership row ever.
Their docstring records getting that wrong first: a fact that once held a
field and later left it still has rows, so an emptiness test hides it, and it
disappears from every field except the one it no longer belongs to.

The third rule is this SDK's, and it is what carries the ticket's "unless an
active relationship or joint membership exists". It also keeps a store that
has never filed anything working: without it, switching on a field would empty
every query.

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

        # Companion scopes on the fact's own membership, so this does too.
        # A fact is filed where it was said, which is not always where its
        # subject belongs: something said about a colleague in a work chat sits
        # in Work while the colleague also sits in Personal. Filtering on the
        # subject instead answered the same question differently on the two
        # sides, with nothing raising anywhere.
        filed_here, unfiled = self._storage.partition_signatures_by_field(
            [r.id for r in records], field, at, user=user
        )

        # The subject is the second layer, not the first: it is what carries
        # the ticket's "unless an active relationship or joint membership
        # exists", which the server's scope filter leaves to its own gate.
        visible_entities = self._storage.entities_visible_in(
            field, at, follow_links=follow_links, user=user
        )
        subjects = self._storage.entity_ids_for_signatures(
            [r.id for r in records], user=user
        )
        unfiled_entities = self._storage.entities_with_no_live_membership(
            sorted(set(subjects.values())), at, user=user
        )

        kept: list[SignatureRecord] = []
        excluded = 0
        for record in records:
            if record.id in filed_here:
                kept.append(record)
                continue
            if record.id not in unfiled:
                # Filed somewhere, and not here. This field's to withhold.
                excluded += 1
                continue

            # The fact itself is filed nowhere, so its subject decides.
            entity = subjects.get(record.id)
            if entity is None or entity in unfiled_entities or entity in visible_entities:
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

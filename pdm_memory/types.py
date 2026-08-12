# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Public typing aliases for PDM extension points."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Literal, Optional

if TYPE_CHECKING:
    from pdm_memory.core.signature import MemoryHit, SignatureRecord
    from pdm_memory.models import TorsionReport

# Optional callback: return a TorsionReport to flag a pair rules-only detection missed.
TorsionJudge = Callable[["SignatureRecord", "SignatureRecord"], Optional["TorsionReport"]]

# Called for each hit returned by recall() (before reinforce writes).
RecallHook = Callable[["MemoryHit"], None]

# -----------------------------
# Internal Memory hook system
# -----------------------------

HookEvent = Literal["pre_save", "post_save", "post_recall"]
PostRecallSource = Literal["recall", "surface"]

_POST_RECALL_FIELD_NAMES: tuple[str, ...] = (
    "query",
    "k",
    "hits",
    "reinforced",
    "min_pressure",
    "search_cost",
    "drawer",
    "diversity_bias",
    "source",
)


@dataclass(frozen=True, slots=True)
class PostRecallContext:
    """
    Typed context for ``post_recall`` hooks.

    Supports attribute access (``ctx.query``) and Mapping-style
    ``ctx["query"]`` for backward compatibility with dict-shaped hooks.
    """

    query: str
    k: int
    hits: tuple["MemoryHit", ...]
    reinforced: bool
    min_pressure: float = 0.0
    search_cost: float = 0.5
    drawer: str | None = None
    diversity_bias: float | None = None
    source: PostRecallSource = "recall"

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in _POST_RECALL_FIELD_NAMES

    def __iter__(self) -> Iterator[str]:
        return iter(_POST_RECALL_FIELD_NAMES)

    def __len__(self) -> int:
        return len(_POST_RECALL_FIELD_NAMES)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> Sequence[str]:
        return _POST_RECALL_FIELD_NAMES

    def as_dict(self) -> dict[str, Any]:
        """Shallow dict copy — ``hits`` keeps the same MemoryHit objects."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


# Called right before storage.save(sig).
# Return SignatureRecord to keep/mutate.
# Return None / False, or raise IntegrityBlock, to VETO (block) the save.
PreSaveHook = Callable[
    ["SignatureRecord"],
    "SignatureRecord | None | bool",
]

# Called after storage.save(sig) produced memory_id.
PostSaveHook = Callable[["SignatureRecord", str], None]

# Called at the end of recall()/surface(), after optional on_recall() + reinforce.
PostRecallHook = Callable[[PostRecallContext], None]

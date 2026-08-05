# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Public typing aliases for PDM extension points."""

from __future__ import annotations

from collections.abc import Callable
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

# Called right before storage.save(sig).
# Return SignatureRecord to keep/mutate.
# Return None / False, or raise IntegrityBlock, to VETO (block) the save.
PreSaveHook = Callable[
    ["SignatureRecord"],
    "SignatureRecord | None | bool",
]

# Called after storage.save(sig) produced memory_id.
PostSaveHook = Callable[["SignatureRecord", str], None]

# Called at the end of recall(), after optional on_recall() + reinforcement writes.
# Receives a context dict to avoid locking plugins to a specific parameter list.
PostRecallHook = Callable[[dict[str, Any]], None]

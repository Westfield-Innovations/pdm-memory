# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""Public typing aliases for PDM extension points."""

from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pdm_memory.core.signature import MemoryHit, SignatureRecord
    from pdm_memory.models import TorsionReport

# Optional callback: return a TorsionReport to flag a pair rules-only detection missed.
TorsionJudge = Callable[["SignatureRecord", "SignatureRecord"], Optional["TorsionReport"]]

# Called for each hit returned by recall() (before reinforce writes).
RecallHook = Callable[["MemoryHit"], None]

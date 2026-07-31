"""
Correctability Benchmark — Ablation Mode
=========================================

AblationMemory wraps the standard pdm_memory.Memory class but monkey-patches
the Validation Coefficient (V) to always return a constant value (0.75),
effectively disabling its influence on P_effective.

This is the most important comparison in the benchmark:
  Same system, V mechanism OFF → should show high Memory Gravity.
  If the PDM-enabled run shows low Memory Gravity and the ablation run
  shows high Memory Gravity, it *proves* V is doing the work.

Usage in harness:
    if ablation:
        backend = AblationMemory(store=tmp_db, user="bench")
    else:
        backend = Memory(store=tmp_db, user="bench")
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

from typing_extensions import Self

logger = logging.getLogger(__name__)

# Constant V used during ablation — mimics "no validation history" state
_ABLATION_V_CONSTANT: float = 0.75


class AblationMemory:
    """
    PDM Memory with the Validation Coefficient (V) frozen at a constant.

    All other mechanisms (pressure, decay, tag matching, reinforcement delta)
    behave exactly as in the normal Memory class.  Only V is neutralised.

    The penalize() call still decrements p_magnitude so pressure changes are
    real — but V no longer compounds those changes, isolating V's specific
    contribution to correctability.
    """

    def __init__(self, store: str = "./pdm_ablation.db", user: str = "bench") -> None:
        from pdm_memory import Memory

        self._mem = Memory(store=store, user=user)
        self._patcher = patch(
            "pdm_memory.core.math.calculate_v",
            return_value=_ABLATION_V_CONSTANT,
        )
        self._patcher.start()
        logger.debug(
            "[Ablation] V frozen at %.2f — V mechanism disabled", _ABLATION_V_CONSTANT
        )

    def save(self, *args: Any, **kwargs: Any) -> str:
        return self._mem.save(*args, **kwargs)

    def recall(self, *args: Any, **kwargs: Any) -> list[Any]:
        return self._mem.recall(*args, **kwargs)

    def reinforce(self, *args: Any, **kwargs: Any) -> None:
        return self._mem.reinforce(*args, **kwargs)

    def penalize(self, *args: Any, **kwargs: Any) -> None:
        return self._mem.penalize(*args, **kwargs)

    def get_pressure(self, memory_id: str) -> float | None:
        """Read the raw p_magnitude for a given memory ID."""
        rec = self._mem._storage.get(memory_id, user=self._mem._user)
        return rec.p_magnitude if rec else None

    def close(self) -> None:
        self._patcher.stop()
        self._mem.close()
        logger.debug("[Ablation] V patcher stopped, memory closed")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

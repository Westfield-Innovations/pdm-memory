# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""PDM Retrieval Engine package — TAS recall, diversity, torsion, event windows."""

from pdm_memory.core.retrieval.constants import (
    ALPHA_DEFAULT,
    AUTO_FIRE_THRESHOLD,
    COUPLING_MIN_DEFAULT,
    DEFAULT_DIVERSITY_BIAS,
    REINF_BASE,
    THETA_BASE_DEFAULT,
    THETA_FLOOR,
    W_DOMAIN,
    W_PRESSURE,
    W_REGIME,
    W_TAGS,
)
from pdm_memory.core.retrieval.engine import RetrievalEngine
from pdm_memory.core.retrieval.types import NodeCoupling, RetrievalResult

__all__ = [
    "ALPHA_DEFAULT",
    "AUTO_FIRE_THRESHOLD",
    "COUPLING_MIN_DEFAULT",
    "DEFAULT_DIVERSITY_BIAS",
    "NodeCoupling",
    "REINF_BASE",
    "RetrievalEngine",
    "RetrievalResult",
    "THETA_BASE_DEFAULT",
    "THETA_FLOOR",
    "W_DOMAIN",
    "W_PRESSURE",
    "W_REGIME",
    "W_TAGS",
]

# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""TAS and retrieval ranking constants."""

from __future__ import annotations

DEFAULT_DIVERSITY_BIAS: float = 0.40

# PDM-T: how hard urgency energy lifts ranking / Phase-1 gate
TEMPORAL_RANK_BOOST: float = 0.35
TEMPORAL_GATE_BOOST: float = 0.25
# Extra rank weight when t_event_at falls inside a query-relative window
EVENT_WINDOW_RANK_BOOST: float = 1.75

ALPHA_DEFAULT: float = 0.7
THETA_FLOOR: float = 5.0
THETA_BASE_DEFAULT: float = 30.0
COUPLING_MIN_DEFAULT: float = 0.3

W_TAGS: float = 0.50
W_DOMAIN: float = 0.20
W_REGIME: float = 0.15
W_PRESSURE: float = 0.15

AUTO_FIRE_THRESHOLD: float = 85.0
REINF_BASE: float = 2.0

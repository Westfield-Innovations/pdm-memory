# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
Noun Probe settings loader (Fail Fast).

Reads PDM_NOUN_PROBE_* from the environment into a frozen config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int_setting(name: str, default: int) -> int:
    return int(os.environ.get(f"PDM_{name}", default))


def _bool_setting(name: str, default: bool) -> bool:
    value = os.environ.get(f"PDM_{name}")
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True, slots=True)
class NounProbeConfig:
    enabled: bool
    top_n: int
    substitutes_per_noun: int
    min_doc_words: int
    min_hits: int


def noun_probe_config_from_settings() -> NounProbeConfig:
    top_n = _int_setting("NOUN_PROBE_TOP_N", 12)
    substitutes_per_noun = _int_setting("NOUN_PROBE_SUBSTITUTES_PER_NOUN", 1)
    min_doc_words = _int_setting("NOUN_PROBE_MIN_DOC_WORDS", 40)
    min_hits = _int_setting("NOUN_PROBE_MIN_HITS", 2)

    for name, value in (
        ("NOUN_PROBE_TOP_N", top_n),
        ("NOUN_PROBE_SUBSTITUTES_PER_NOUN", substitutes_per_noun),
        ("NOUN_PROBE_MIN_DOC_WORDS", min_doc_words),
        ("NOUN_PROBE_MIN_HITS", min_hits),
    ):
        if value <= 0:
            raise ValueError(f"PDM_{name} must be a positive integer, got {value!r}")

    return NounProbeConfig(
        enabled=_bool_setting("NOUN_PROBE_ENABLED", True),
        top_n=top_n,
        substitutes_per_noun=substitutes_per_noun,
        min_doc_words=min_doc_words,
        min_hits=min_hits,
    )

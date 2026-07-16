# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""
pdm_memory — Pressure-Driven Memory SDK
========================================

The public surface of the SDK:

    from pdm_memory import Memory

    mem = Memory(store="./my_app.db")
    mem.save("User prefers metric units", source="chat")
    hits = mem.recall("how should I format this?", k=5)

    from pdm_memory.integrations import wrap_openai, wrap_anthropic

See README.md for full documentation.
"""

from pdm_memory.memory import Memory
from pdm_memory.core.signature import MemoryHit, DrawerInfo
from pdm_memory.models import AlignmentReport, TorsionReport

__version__ = "0.1.7"
__all__ = ["Memory", "MemoryHit", "DrawerInfo", "AlignmentReport", "TorsionReport", "__version__"]

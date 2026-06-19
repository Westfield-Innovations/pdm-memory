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

__version__ = "0.1.0"
__all__ = ["Memory", "MemoryHit", "DrawerInfo", "__version__"]

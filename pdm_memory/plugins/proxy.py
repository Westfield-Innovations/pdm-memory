# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""Capability-limited view of Memory for plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pdm_memory.memory import Memory

#: Public Memory surface plugins may call through the proxy.
_ALLOWED_ATTRS = frozenset(
    {
        "save",
        "save_many",
        "get",
        "update",
        "update_batch",
        "update_many",
        "recall",
        "reinforce",
        "delete",
        "detect_torsion",
        "explain",
        "list",
        "list_drawers",
        "count",
        "status",
        "plugins",
        "export_json",
        "import_json",
        "export_csv",
        "surface",
        "reconcile_torsion",
    }
)


class PluginCapabilityError(AttributeError):
    """Raised when a plugin touches a denied Memory attribute."""


class PluginMemoryProxy:
    """
    Capability proxy bound to a plugin as ``self.mem``.

    Whitelists core persistence/recall APIs and installed peer plugins.
    Blocks ``close``, ``_storage``, ``use``/``unload``, hook registration, etc.
    """

    __slots__ = ("_memory", "_owner")

    def __init__(self, memory: Memory, *, owner: str) -> None:
        object.__setattr__(self, "_memory", memory)
        object.__setattr__(self, "_owner", owner)

    def __repr__(self) -> str:
        mem = object.__getattribute__(self, "_memory")
        owner = object.__getattribute__(self, "_owner")
        return f"<PluginMemoryProxy owner={owner!r} user={mem._user!r}>"

    def __setattr__(self, name: str, value: Any) -> None:
        raise PluginCapabilityError(
            f"Plugins cannot assign Memory.{name} via proxy"
        )

    def __delattr__(self, name: str) -> None:
        raise PluginCapabilityError(
            f"Plugins cannot delete Memory.{name} via proxy"
        )

    def __getattr__(self, name: str) -> Any:
        mem: Memory = object.__getattribute__(self, "_memory")
        owner: str = object.__getattribute__(self, "_owner")

        if name.startswith("_"):
            raise PluginCapabilityError(
                f"Plugin {owner!r} cannot access private Memory.{name}"
            )

        # Peer plugins attached as attributes (e.g. mem.GeoTagger).
        if name in mem.plugins:
            return getattr(mem, name)

        if name in _ALLOWED_ATTRS:
            return getattr(mem, name)

        raise PluginCapabilityError(
            f"Plugin {owner!r} capability denied for Memory.{name}. "
            f"Allowed: {', '.join(sorted(_ALLOWED_ATTRS))}, and peer plugins."
        )

    def unwrap(self) -> Memory:
        """Escape hatch for SDK internals / tests — not for plugin authors."""
        return object.__getattribute__(self, "_memory")


def as_memory(mem: Memory | PluginMemoryProxy) -> Memory:
    """Resolve proxy → underlying Memory."""
    if isinstance(mem, PluginMemoryProxy):
        return mem.unwrap()
    return mem

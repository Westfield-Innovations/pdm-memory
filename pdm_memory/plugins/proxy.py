# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""Capability-limited view of Memory for plugins."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from pdm_memory.memory import Memory

#: Capability tokens plugins may declare on ``BasePDMPlugin.capabilities``.
CAP_READ: Final = "read"
CAP_WRITE: Final = "write"
CAP_RECALL: Final = "recall"
CAP_ADMIN_IO: Final = "admin_io"
CAP_PEER: Final = "peer"

KNOWN_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {CAP_READ, CAP_WRITE, CAP_RECALL, CAP_ADMIN_IO, CAP_PEER}
)

#: Default blast radius — no dump/inject, no peer lateral calls.
DEFAULT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {CAP_READ, CAP_WRITE, CAP_RECALL}
)

_CAPABILITY_ATTRS: Final[dict[str, frozenset[str]]] = {
    CAP_READ: frozenset(
        {
            "get",
            "list",
            "list_drawers",
            "count",
            "status",
            "explain",
            "detect_torsion",
            "surface",
        }
    ),
    CAP_WRITE: frozenset(
        {
            "save",
            "save_many",
            "update",
            "update_batch",
            "update_many",
            "delete",
            "reinforce",
            "penalize",
            "reconcile_torsion",
        }
    ),
    CAP_RECALL: frozenset({"recall"}),
    CAP_ADMIN_IO: frozenset(
        {
            "export_json",
            "import_json",
            "export_csv",
        }
    ),
}


def normalize_capabilities(
    caps: Iterable[str] | None,
) -> frozenset[str]:
    """
    Validate and freeze capability tokens.

    Raises:
        ValueError: Unknown capability name.
    """
    if caps is None:
        return DEFAULT_CAPABILITIES
    out = frozenset(str(c).strip() for c in caps if str(c).strip())
    unknown = sorted(out - KNOWN_CAPABILITIES)
    if unknown:
        raise ValueError(
            f"Unknown plugin capabilities: {unknown}. "
            f"Known: {sorted(KNOWN_CAPABILITIES)}"
        )
    return out


def attrs_for_capabilities(caps: frozenset[str]) -> frozenset[str]:
    """Memory attribute names allowed by the given capability set."""
    allowed: set[str] = set()
    for cap in caps:
        allowed |= _CAPABILITY_ATTRS.get(cap, frozenset())
    return frozenset(allowed)


def capability_for_attr(name: str) -> str | None:
    """Which capability unlocks ``name``, or None if never exposable."""
    for cap, attrs in _CAPABILITY_ATTRS.items():
        if name in attrs:
            return cap
    return None


class PluginCapabilityError(AttributeError):
    """Raised when a plugin touches a denied Memory attribute."""


class PluginMemoryProxy:
    """
    Capability proxy bound to a plugin as ``self.mem``.

    Whitelists Memory APIs according to the plugin's ``capabilities``.
    Blocks ``close``, ``_storage``, ``use``/``unload``, and (by default)
    ``export_*`` / ``import_json``.

    Peer plugins are reachable only when:
      * the owner declared them in ``requires``, or
      * the owner holds ``peer`` (all installed peers).

    There is no public ``unwrap()`` — escape hatch is
    :func:`as_memory` for SDK internals / tests only.
    """

    __slots__ = (
        "_memory",
        "_owner",
        "_capabilities",
        "_allowed_attrs",
        "_allowed_peers",
    )

    def __init__(
        self,
        memory: Memory,
        *,
        owner: str,
        capabilities: frozenset[str] | None = None,
        allowed_peers: Iterable[str] | None = None,
    ) -> None:
        caps = normalize_capabilities(capabilities)
        peers = frozenset(str(p).strip() for p in (allowed_peers or ()) if str(p).strip())
        object.__setattr__(self, "_memory", memory)
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_capabilities", caps)
        object.__setattr__(self, "_allowed_attrs", attrs_for_capabilities(caps))
        object.__setattr__(self, "_allowed_peers", peers)

    def __repr__(self) -> str:
        mem = object.__getattribute__(self, "_memory")
        owner = object.__getattribute__(self, "_owner")
        caps = object.__getattribute__(self, "_capabilities")
        return (
            f"<PluginMemoryProxy owner={owner!r} user={mem._user!r} "
            f"caps={sorted(caps)}>"
        )

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
        caps: frozenset[str] = object.__getattribute__(self, "_capabilities")
        allowed: frozenset[str] = object.__getattribute__(self, "_allowed_attrs")
        peers: frozenset[str] = object.__getattribute__(self, "_allowed_peers")

        if name.startswith("_"):
            raise PluginCapabilityError(
                f"Plugin {owner!r} cannot access private Memory.{name}"
            )

        # Peer plugins — requires allowlist or CAP_PEER (all peers).
        if name in mem.plugins:
            if CAP_PEER in caps or name in peers:
                return getattr(mem, name)
            raise PluginCapabilityError(
                f"Plugin {owner!r} capability denied for peer "
                f"plugin {name!r} (declare in requires= or add {CAP_PEER!r})"
            )

        if name in allowed:
            return getattr(mem, name)

        needed = capability_for_attr(name)
        if needed is not None:
            raise PluginCapabilityError(
                f"Plugin {owner!r} capability denied for Memory.{name} "
                f"(needs {needed!r})"
            )
        raise PluginCapabilityError(
            f"Plugin {owner!r} capability denied for Memory.{name}. "
            f"Allowed attrs: {', '.join(sorted(allowed)) or '(none)'}; "
            f"capabilities={sorted(caps)}"
        )


def _unwrap(proxy: PluginMemoryProxy) -> Memory:
    """Internal: resolve proxy → Memory (not a plugin-facing API)."""
    return object.__getattribute__(proxy, "_memory")


def as_memory(mem: Memory | PluginMemoryProxy) -> Memory:
    """Resolve proxy → underlying Memory (SDK / tests only)."""
    if isinstance(mem, PluginMemoryProxy):
        return _unwrap(mem)
    return mem

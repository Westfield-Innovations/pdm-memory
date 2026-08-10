# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""Base contract for dynamic PDM plugins."""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from pdm_memory.plugins.proxy import DEFAULT_CAPABILITIES
from pdm_memory.plugins.versions import PluginRequirement, parse_requirements

if TYPE_CHECKING:
    from pdm_memory.core.signature import MemoryHit
    from pdm_memory.memory import Memory
    from pdm_memory.models import MemoryListPage
    from pdm_memory.plugins.proxy import PluginMemoryProxy
    from pdm_memory.types import HookEvent

PluginHookMap = Mapping[
    "HookEvent",
    Callable[..., Any] | Sequence[Callable[..., Any]],
]

#: Drawer namespace for plugin-private signatures (isolated from chat drawers).
PLUGIN_DRAWER_PREFIX = "plugin:"


class BasePDMPlugin(ABC):
    """
    Extension point for in-process PDM plugins.

    Developers subclass this, set ``name``, optionally populate ``hooks``,
    then attach via::

        mem.use(MyPlugin())
        mem.my_plugin.some_method()

    External packages use ``plugin.json`` + folder ``pdm-memory-plugin-<name>/``.

    Plugin-private PDM data lives in drawer ``plugin:<name>`` via
    :meth:`plugin_save` / :meth:`plugin_recall` / :meth:`plugin_list`.
    """

    #: Registry / attribute name on Memory (e.g. ``"sledgehammer"`` → ``mem.sledgehammer``).
    name: ClassVar[str] = ""

    #: Semver (or free-form) shown in :meth:`Memory.status`.
    version: ClassVar[str] = "0.0.0"

    #: Hard deps — names or version specs: ``"GeoTagger"``, ``"GeoTagger>=1.2"``.
    requires: ClassVar[list[str]] = []

    #: When False, :meth:`PluginManager.autoload` skips this class (opt-in via ``mem.use``).
    autoload: ClassVar[bool] = True

    #: Hook run order — lower runs earlier (GuardDog=10 before Auditor=100).
    priority: ClassVar[int] = 100

    #: Capability tokens for :class:`~pdm_memory.plugins.proxy.PluginMemoryProxy`.
    #: Default: ``read`` | ``write`` | ``recall`` (no ``admin_io``, no ``peer``).
    capabilities: ClassVar[frozenset[str]] = DEFAULT_CAPABILITIES

    def __init__(self) -> None:
        self.mem: Memory | PluginMemoryProxy | None = None
        # Event → callable or list of callables (pre_save / post_save / post_recall).
        self.hooks: dict[str, Callable[..., Any] | list[Callable[..., Any]]] = {}
        # Filled by Memory.use() after hook registration (read-only for status).
        self.bound_hooks: list[str] = []
        # Exact handler refs registered on Memory (for unload).
        self._hook_registrations: list[tuple[str, Callable[..., Any]]] = []
        #: Provenance: ``builtin`` | ``external:<abs path>`` | ``manual``.
        self.load_source: str = "manual"

    @classmethod
    def requirement_specs(cls) -> list[PluginRequirement]:
        """Parsed ``requires`` entries (names + optional version constraints)."""
        return parse_requirements(getattr(cls, "requires", None) or [])

    @classmethod
    def required_plugins(cls) -> list[str]:
        """Dependency names only (for topology / presence checks)."""
        return [req.name for req in cls.requirement_specs()]

    @classmethod
    def resolved_name(cls) -> str:
        """Public plugin name (``name`` ClassVar, else class name)."""
        return (cls.name or cls.__name__).strip()

    @classmethod
    def resolved_version(cls) -> str:
        return str(getattr(cls, "version", None) or "0.0.0")

    @classmethod
    def drawer_for(cls, plugin_name: str | None = None) -> str:
        """Return isolated drawer id: ``plugin:<name>``."""
        key = (plugin_name or cls.resolved_name()).strip()
        if not key:
            raise ValueError("Plugin name is required to build plugin drawer")
        if key.startswith(PLUGIN_DRAWER_PREFIX):
            return key
        return f"{PLUGIN_DRAWER_PREFIX}{key}"

    @property
    def plugin_drawer(self) -> str:
        """Isolated PDM drawer for this plugin instance."""
        return type(self).drawer_for()

    def bind(self, mem: Memory | PluginMemoryProxy) -> None:
        """
        Attach Memory (or a :class:`~pdm_memory.plugins.proxy.PluginMemoryProxy`).

        Production installs always pass a proxy — plugins never get raw Memory.
        """
        if mem is None:
            raise TypeError("mem is required for BasePDMPlugin.bind()")
        self.mem = mem

    def on_install(self) -> None:
        """
        Optional lifecycle hook after ``bind`` + hook registration.

        Override for one-time setup that needs ``self.mem``.
        """

    def on_uninstall(self) -> None:
        """Optional lifecycle hook before hooks/attr are torn down on unload."""

    # ------------------------------------------------------------------
    # Plugin-specific storage (drawer = plugin:<name>)
    # ------------------------------------------------------------------

    def plugin_save(self, text: str, **kwargs: Any) -> str:
        """
        Save into this plugin's private drawer (``plugin:<name>``).

        Same kwargs as :meth:`Memory.save`, except ``drawer`` is forced.
        Default ``source`` is ``plugin:<name>``; metadata gets ``_plugin``.
        """
        mem = self._require_mem()
        self._reject_drawer_override(kwargs)
        kwargs.pop("drawer", None)

        source = kwargs.pop("source", f"plugin:{self.resolved_name()}")
        metadata = dict(kwargs.pop("metadata", None) or {})
        metadata.setdefault("_plugin", self.resolved_name())

        if not kwargs.get("tags"):
            kwargs["tags"] = ["plugin", self.resolved_name().lower(), "config"]

        return mem.save(
            text,
            drawer=self.plugin_drawer,
            source=source,
            metadata=metadata,
            **kwargs,
        )

    def plugin_recall(self, query: str, **kwargs: Any) -> list[MemoryHit]:
        """Recall only within this plugin's private drawer."""
        mem = self._require_mem()
        self._reject_drawer_override(kwargs)
        kwargs.pop("drawer", None)
        return mem.recall(query, drawer=self.plugin_drawer, **kwargs)

    def plugin_list(self, **kwargs: Any) -> MemoryListPage:
        """List memories scoped to this plugin's private drawer."""
        mem = self._require_mem()
        self._reject_drawer_override(kwargs)
        kwargs.pop("drawer", None)
        return mem.list(drawer=self.plugin_drawer, **kwargs)

    def plugin_get(self, memory_id: str) -> MemoryHit | None:
        """Fetch by id; returns ``None`` if missing or not in this plugin drawer."""
        mem = self._require_mem()
        hit = mem.get(memory_id)
        if hit is None:
            return None
        if hit.drawer != self.plugin_drawer:
            return None
        return hit

    def plugin_delete(self, memory_id: str) -> bool:
        """
        Soft-delete a memory that belongs to this plugin's drawer.

        Raises:
            ValueError: If the id exists but lives in another drawer.
        """
        mem = self._require_mem()
        hit = mem.get(memory_id)
        if hit is None:
            return False
        if hit.drawer != self.plugin_drawer:
            raise ValueError(
                f"Memory '{memory_id}' is in drawer {hit.drawer!r}, "
                f"not this plugin's {self.plugin_drawer!r}"
            )
        return mem.delete(memory_id)

    def _require_mem(self) -> Memory | PluginMemoryProxy:
        if self.mem is None:
            raise RuntimeError(
                f"Plugin {self.resolved_name()!r} is not bound; call mem.use(...) first"
            )
        return self.mem

    @staticmethod
    def _reject_drawer_override(kwargs: dict[str, Any]) -> None:
        if "drawer" not in kwargs:
            return
        raise ValueError(
            "drawer= is reserved; plugin_* helpers always use plugin:<name>"
        )

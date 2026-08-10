# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""Dynamic discovery and installation of :class:`BasePDMPlugin` subclasses."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from pdm_memory.plugins.base import BasePDMPlugin
from pdm_memory.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifestError,
    load_manifest,
    sha256_file,
    verify_entrypoint_sha256,
)
from pdm_memory.plugins.versions import check_requirement

if TYPE_CHECKING:
    from pdm_memory.memory import Memory

logger = logging.getLogger(__name__)

_SKIP_STEMS = frozenset({"base", "manager", "proxy", "manifest", "versions", "__init__"})

#: Cloned external plugin packages: ``pdm-memory-plugin-<name>/``.
EXTERNAL_PLUGIN_DIR_PREFIX = "pdm-memory-plugin-"

#: ClassAttr stamped during discovery so install can set ``load_source``.
_SOURCE_ATTR = "_pdm_load_source"


class PluginManager:
    """
    Scans directories for :class:`BasePDMPlugin` subclasses and installs them
    onto a :class:`~pdm_memory.memory.Memory` instance.

    Sources:
      1. Built-in ``pdm_memory/plugins/`` (``*.py`` scan)
      2. External ``pdm-memory-plugin-<name>/`` with required ``plugin.json``
    """

    def __init__(self, memory: Memory) -> None:
        self._memory = memory
        self._plugins: dict[str, BasePDMPlugin] = {}

    @property
    def plugins(self) -> Mapping[str, BasePDMPlugin]:
        """Installed plugins keyed by registry name."""
        return self._plugins

    @staticmethod
    def default_plugins_dir() -> Path:
        """Built-in package folder: ``pdm_memory/plugins/``."""
        return Path(__file__).resolve().parent

    @staticmethod
    def find_external_plugin_dirs(
        start: Path | str | None = None,
        *,
        walk_ancestors: bool = True,
    ) -> list[Path]:
        """
        Collect directories named ``pdm-memory-plugin-<name>``.

        When ``walk_ancestors`` is True (default), walks from ``start``
        (default: cwd) up to the filesystem root. When False, only lists
        direct children of ``start`` (legacy ``trust_plugins`` mode).
        """
        cursor = Path(start) if start is not None else Path.cwd()
        cursor = cursor.resolve()
        found: list[Path] = []
        seen: set[Path] = set()

        directories = [cursor, *cursor.parents] if walk_ancestors else [cursor]

        for directory in directories:
            try:
                children = list(directory.iterdir())
            except OSError as exc:
                logger.debug("[PDM] Cannot list %s: %s", directory, exc)
                continue
            for child in sorted(children, key=lambda p: p.name):
                if not child.is_dir():
                    continue
                name = child.name
                if not name.startswith(EXTERNAL_PLUGIN_DIR_PREFIX):
                    continue
                suffix = name[len(EXTERNAL_PLUGIN_DIR_PREFIX) :]
                if not suffix or suffix.startswith("-"):
                    continue
                resolved = child.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                found.append(resolved)
                logger.debug("[PDM] External plugin dir: %s", resolved)

        return found

    def is_external_dir_trusted(self, plugin_dir: Path | str) -> bool:
        """
        External plugins are arbitrary code.

        Trusted when the resolved path is under / equal to an entry in
        ``plugin_allowlist``, or (deprecated) when ``trust_plugins=True``
        and no allowlist is configured.
        """
        mem = self._memory
        allowlist = getattr(mem, "_plugin_allowlist", ()) or ()
        resolved = Path(plugin_dir).resolve()
        resolved_s = str(resolved)
        for allowed in allowlist:
            allowed_path = Path(allowed).resolve()
            allowed_s = str(allowed_path)
            if resolved_s == allowed_s:
                return True
            try:
                resolved.relative_to(allowed_path)
                return True
            except ValueError:
                continue
        if allowlist:
            # Allowlist is authoritative when present.
            return False
        return bool(getattr(mem, "_trust_plugins", False))

    def allowlist_plugin_dirs(self) -> list[Path]:
        """
        Resolve ``plugin_allowlist`` entries to concrete plugin directories.

        An entry may be the plugin directory itself or a parent that
        contains ``pdm-memory-plugin-*`` children.
        """
        mem = self._memory
        found: list[Path] = []
        seen: set[Path] = set()
        for allowed in getattr(mem, "_plugin_allowlist", ()) or ():
            root = Path(allowed).resolve()
            if not root.is_dir():
                logger.warning(
                    "[PDM] plugin_allowlist entry is not a directory: %s", root
                )
                continue
            candidates: list[Path] = []
            if root.name.startswith(EXTERNAL_PLUGIN_DIR_PREFIX) and (
                root / MANIFEST_FILENAME
            ).is_file():
                candidates.append(root)
            else:
                try:
                    children = list(root.iterdir())
                except OSError as exc:
                    logger.warning(
                        "[PDM] Cannot list allowlist dir %s: %s", root, exc
                    )
                    continue
                for child in children:
                    if (
                        child.is_dir()
                        and child.name.startswith(EXTERNAL_PLUGIN_DIR_PREFIX)
                        and (child / MANIFEST_FILENAME).is_file()
                    ):
                        candidates.append(child.resolve())
            for path in candidates:
                if path in seen:
                    continue
                seen.add(path)
                found.append(path)
        return found

    def discover(self, directory: Path | str | None = None) -> list[type[BasePDMPlugin]]:
        """
        Import ``*.py`` modules under ``directory`` (built-in / test scan).

        Skips ``base.py``, ``manager.py``, and private modules (``_*``).
        Modules that fail to import are logged and skipped (fail soft at scan).
        """
        root = Path(directory) if directory is not None else self.default_plugins_dir()
        root = root.resolve()
        if not root.is_dir():
            logger.warning("[PDM] Plugin directory missing: %s", root)
            return []

        # External packages must use plugin.json — never glob-import them.
        if (root / MANIFEST_FILENAME).is_file() and root.name.startswith(
            EXTERNAL_PLUGIN_DIR_PREFIX
        ):
            if not self.is_external_dir_trusted(root):
                logger.warning(
                    "[PDM] Skipping untrusted external plugin dir %s "
                    "(use plugin_allowlist=...)",
                    root,
                )
                return []
            cls = self.load_from_manifest(root)
            setattr(cls, _SOURCE_ATTR, f"external:{root}")
            return [cls]

        found: list[type[BasePDMPlugin]] = []
        seen: set[type[BasePDMPlugin]] = set()
        is_builtin = root == self.default_plugins_dir()

        for path in sorted(root.glob("*.py")):
            stem = path.stem
            if stem.startswith("_") or stem in _SKIP_STEMS:
                continue
            try:
                module = self._load_module(path)
            except Exception as exc:
                logger.warning(
                    "[PDM] Skipping plugin module %s: %s",
                    path.name,
                    exc,
                )
                continue

            for cls in self._classes_in_module(module):
                if cls in seen:
                    continue
                seen.add(cls)
                setattr(
                    cls,
                    _SOURCE_ATTR,
                    "builtin" if is_builtin else "manual",
                )
                found.append(cls)

        return found

    def load_from_manifest(self, plugin_dir: Path | str) -> type[BasePDMPlugin]:
        """
        Load exactly one plugin class via ``plugin.json`` entrypoint.

        Manifest metadata is applied via a thin subclass (the imported
        class ClassVars are never mutated).

        Raises:
            PluginManifestError: Missing/invalid manifest, entrypoint, or
                sha256 pin mismatch.
        """
        root = Path(plugin_dir).resolve()
        manifest = load_manifest(root)
        file_path, class_name = manifest.resolve_entrypoint_file(root)

        digest = sha256_file(file_path)
        pinned = bool(manifest.entrypoint_sha256)
        if manifest.entrypoint_sha256:
            verify_entrypoint_sha256(file_path, manifest.entrypoint_sha256)
        else:
            logger.warning(
                "[PDM] External plugin %s at %s has no entrypoint_sha256 pin",
                manifest.name,
                root,
            )

        module = self._load_module(file_path)
        obj = getattr(module, class_name, None)
        if obj is None:
            raise PluginManifestError(
                f"entrypoint class {class_name!r} not found in {file_path.name}"
            )
        if not isinstance(obj, type) or not issubclass(obj, BasePDMPlugin):
            raise PluginManifestError(
                f"entrypoint {class_name!r} is not a BasePDMPlugin subclass"
            )
        if obj is BasePDMPlugin or inspect.isabstract(obj):
            raise PluginManifestError(
                f"entrypoint {class_name!r} must be a concrete BasePDMPlugin"
            )

        # Manifest is authoritative — wrap without mutating the imported class.
        bound = type(
            f"{obj.__name__}FromManifest",
            (obj,),
            {
                "name": manifest.name,
                "version": manifest.version,
                "requires": list(manifest.requires),
                "autoload": bool(manifest.autoload),
            },
        )
        bound.__module__ = obj.__module__
        bound.__qualname__ = f"{obj.__qualname__}FromManifest"

        logger.info(
            "[PDM] External plugin loaded name=%s version=%s dir=%s "
            "entrypoint=%s entrypoint_sha256=%s sha256_pinned=%s requires=%s",
            manifest.name,
            manifest.version,
            str(root),
            manifest.entrypoint,
            digest,
            pinned,
            list(manifest.requires),
        )
        return bound

    def discover_all(
        self,
        *,
        builtin_directory: Path | str | None = None,
        external_start: Path | str | None = None,
        include_builtin: bool = True,
        include_external: bool = True,
    ) -> list[type[BasePDMPlugin]]:
        """Discover from built-in package dir and/or external ``pdm-memory-plugin-*`` folders."""
        classes: list[type[BasePDMPlugin]] = []
        seen: set[type[BasePDMPlugin]] = set()

        def _add(batch: Sequence[type[BasePDMPlugin]]) -> None:
            for cls in batch:
                if cls in seen:
                    continue
                seen.add(cls)
                classes.append(cls)

        if include_builtin:
            _add(self.discover(builtin_directory))

        if include_external:
            mem = self._memory
            has_allow = bool(getattr(mem, "_plugin_allowlist", ()) or ())
            has_trust = bool(getattr(mem, "_trust_plugins", False))
            if not has_allow and not has_trust:
                logger.debug(
                    "[PDM] Skipping external plugin discovery "
                    "(neither plugin_allowlist nor trust_plugins)"
                )
            else:
                candidates: list[Path] = []
                seen_dirs: set[Path] = set()

                def _push(paths: Sequence[Path]) -> None:
                    for path in paths:
                        resolved = path.resolve()
                        if resolved in seen_dirs:
                            continue
                        seen_dirs.add(resolved)
                        candidates.append(resolved)

                if has_allow:
                    _push(self.find_external_plugin_dirs(
                        external_start, walk_ancestors=True
                    ))
                    _push(self.allowlist_plugin_dirs())
                elif has_trust:
                    # Deprecated: cwd children only — no parent-tree ascent.
                    _push(
                        self.find_external_plugin_dirs(
                            external_start, walk_ancestors=False
                        )
                    )

                for plugin_dir in candidates:
                    if not self.is_external_dir_trusted(plugin_dir):
                        logger.warning(
                            "[PDM] Skipping untrusted external plugin dir %s "
                            "(use plugin_allowlist=...)",
                            plugin_dir,
                        )
                        continue
                    # Fail-fast: broken / missing plugin.json aborts discovery.
                    cls = self.load_from_manifest(plugin_dir)
                    setattr(cls, _SOURCE_ATTR, f"external:{plugin_dir.resolve()}")
                    _add([cls])

        return classes

    def install(
        self,
        plugin_class: type[BasePDMPlugin],
        *,
        source: str | None = None,
    ) -> BasePDMPlugin:
        """
        Instantiate ``plugin_class`` and wire it through :meth:`Memory.use`.

        Raises:
            TypeError: If ``plugin_class`` is not a concrete BasePDMPlugin subclass.
            ValueError: If a plugin with the same registry name is already installed.
        """
        if not isinstance(plugin_class, type) or not issubclass(
            plugin_class, BasePDMPlugin
        ):
            raise TypeError(
                f"plugin_class must be a BasePDMPlugin subclass, got {plugin_class!r}"
            )
        if plugin_class is BasePDMPlugin or inspect.isabstract(plugin_class):
            raise TypeError(
                f"Cannot install abstract plugin class {plugin_class.__name__}"
            )

        instance = plugin_class()
        resolved_source = source
        if resolved_source is None:
            resolved_source = getattr(plugin_class, _SOURCE_ATTR, None)
        if resolved_source:
            instance.load_source = str(resolved_source)
        self._memory.use(instance)
        return instance

    def register_instance(
        self,
        name: str,
        plugin: BasePDMPlugin,
        *,
        hooks: Sequence[str] | None = None,
    ) -> None:
        """Record an already-bound plugin in the registry (called by Memory.use)."""
        if name in self._plugins:
            raise ValueError(f"Plugin already installed: {name!r}")
        bound = list(hooks) if hooks is not None else list(plugin.bound_hooks)
        plugin.bound_hooks = bound
        self._plugins[name] = plugin
        logger.info(
            "[PDM] Plugin installed: %s v%s hooks=%s (%s)",
            name,
            getattr(type(plugin), "version", "0.0.0"),
            bound or "—",
            type(plugin).__name__,
        )

    def unregister(self, name: str) -> BasePDMPlugin | None:
        """Remove ``name`` from the registry; return the instance if it was present."""
        return self._plugins.pop(name, None)

    def dependents_of(self, name: str) -> list[str]:
        """Installed plugin names that list ``name`` in their ``requires``."""
        out: list[str] = []
        for installed_name, plugin in self._plugins.items():
            if name in type(plugin).required_plugins():
                out.append(installed_name)
        return out

    def unmet_requirements(
        self, plugin_class: type[BasePDMPlugin]
    ) -> list[str]:
        """Human-readable unmet requirement descriptions (empty if OK)."""
        problems: list[str] = []
        for req in plugin_class.requirement_specs():
            installed = self._plugins.get(req.name)
            version = (
                type(installed).resolved_version() if installed is not None else None
            )
            msg = check_requirement(req, installed_version=version)
            if msg is not None:
                problems.append(msg)
        return problems

    def autoload(
        self,
        directory: Path | str | None = None,
        *,
        external_start: Path | str | None = None,
        include_external: bool = True,
    ) -> list[BasePDMPlugin]:
        """
        Discover built-in + external plugins and install in dependency order.

        External layout::

            pdm-memory-plugin-echo/
                plugin.json
                echo_plugin.py

        When ``directory`` is an explicit path (tests / one-shot), only that
        directory is scanned (no external walk).
        """
        if directory is not None:
            classes = self.discover(directory)
            return self._install_discovered(classes)

        classes = self.discover_all(
            builtin_directory=None,
            external_start=external_start,
            include_builtin=True,
            include_external=include_external,
        )
        return self._install_discovered(classes)

    def _install_discovered(
        self, classes: Sequence[type[BasePDMPlugin]]
    ) -> list[BasePDMPlugin]:
        ordered = self._order_by_requires(classes)
        installed: list[BasePDMPlugin] = []
        for cls in ordered:
            if not getattr(cls, "autoload", True):
                logger.debug(
                    "[PDM] Skipping opt-in plugin (autoload=False): %s",
                    cls.name or cls.__name__,
                )
                continue
            key = (cls.name or cls.__name__).strip()
            if key in self._plugins:
                logger.debug("[PDM] Plugin already present, skip: %s", key)
                continue
            unmet = self.unmet_requirements(cls)
            # Also consider peer classes not yet installed from this batch.
            if unmet:
                # Soft-skip at autoload when deps missing entirely from install set.
                logger.warning(
                    "[PDM] Skipping plugin %s — unmet requires: %s",
                    key,
                    ", ".join(unmet),
                )
                continue
            installed.append(self.install(cls))
        return installed

    @staticmethod
    def _order_by_requires(
        classes: Sequence[type[BasePDMPlugin]],
    ) -> list[type[BasePDMPlugin]]:
        """Kahn topological sort by dependency names."""
        by_name: dict[str, type[BasePDMPlugin]] = {}
        for cls in classes:
            key = (cls.name or cls.__name__).strip()
            if key:
                by_name[key] = cls

        remaining = set(by_name)
        indegree: dict[str, int] = {name: 0 for name in by_name}
        dependents: dict[str, list[str]] = {name: [] for name in by_name}
        for name, cls in by_name.items():
            for dep in cls.required_plugins():
                if dep not in by_name:
                    continue
                dependents[dep].append(name)
                indegree[name] += 1

        queue = sorted(n for n, d in indegree.items() if d == 0)
        ordered_names: list[str] = []
        while queue:
            current = queue.pop(0)
            ordered_names.append(current)
            remaining.discard(current)
            for child in dependents[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
                    queue.sort()

        if remaining:
            leftovers = [n for n in by_name if n in remaining]
            ordered_names.extend(leftovers)

        return [by_name[n] for n in ordered_names]

    def get(self, name: str) -> BasePDMPlugin | None:
        return self._plugins.get(name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_module(self, path: Path) -> ModuleType:
        path = path.resolve()
        package_dir = self.default_plugins_dir()
        if path.parent == package_dir:
            module_name = f"pdm_memory.plugins.{path.stem}"
            return importlib.import_module(module_name)

        # Isolated load: do not mutate sys.path (no sibling-module shadowing).
        module_name = f"_pdm_plugin_{path.stem}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load plugin module from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            assert spec.loader is not None
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    @staticmethod
    def _classes_in_module(module: ModuleType) -> Sequence[type[BasePDMPlugin]]:
        classes: list[type[BasePDMPlugin]] = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj is BasePDMPlugin:
                continue
            if not issubclass(obj, BasePDMPlugin):
                continue
            if inspect.isabstract(obj):
                continue
            if obj.__module__ != module.__name__:
                continue
            classes.append(obj)
        return classes

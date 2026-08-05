# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""``plugin.json`` manifest for external ``pdm-memory-plugin-*`` packages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdm_memory.plugins.versions import parse_requirements

MANIFEST_FILENAME = "plugin.json"


class PluginManifestError(ValueError):
    """Invalid or missing external plugin manifest — fail fast."""


@dataclass(slots=True)
class PluginManifest:
    """Validated ``plugin.json`` contents."""

    name: str
    entrypoint: str
    version: str = "0.0.0"
    requires: list[str] = field(default_factory=list)
    autoload: bool = True
    path: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, path: Path | None = None) -> PluginManifest:
        if not isinstance(data, dict):
            raise PluginManifestError("plugin.json root must be an object")

        name = str(data.get("name", "")).strip()
        entrypoint = str(data.get("entrypoint", "")).strip()
        if not name:
            raise PluginManifestError("plugin.json missing required field 'name'")
        if not name.isidentifier():
            raise PluginManifestError(
                f"plugin.json name must be a Python identifier, got {name!r}"
            )
        if not entrypoint or ":" not in entrypoint:
            raise PluginManifestError(
                "plugin.json 'entrypoint' must be 'module:ClassName' "
                f"(got {entrypoint!r})"
            )

        version = str(data.get("version", "0.0.0")).strip() or "0.0.0"
        raw_requires = data.get("requires", [])
        if raw_requires is None:
            raw_requires = []
        if not isinstance(raw_requires, list):
            raise PluginManifestError("plugin.json 'requires' must be a list of strings")
        requires = [str(item) for item in raw_requires]
        try:
            parse_requirements(requires)  # validate early
        except ValueError as exc:
            raise PluginManifestError(str(exc)) from exc

        autoload = data.get("autoload", True)
        if not isinstance(autoload, bool):
            raise PluginManifestError("plugin.json 'autoload' must be a boolean")

        return cls(
            name=name,
            entrypoint=entrypoint,
            version=version,
            requires=requires,
            autoload=autoload,
            path=path,
        )

    def resolve_entrypoint_file(self, plugin_dir: Path) -> tuple[Path, str]:
        """Return ``(python_file, class_name)`` for ``entrypoint``."""
        module_part, class_name = self.entrypoint.rsplit(":", 1)
        module_part = module_part.strip().replace("\\", "/")
        class_name = class_name.strip()
        if not class_name.isidentifier():
            raise PluginManifestError(
                f"Invalid entrypoint class name: {class_name!r}"
            )

        if module_part.endswith(".py"):
            file_path = plugin_dir / module_part
        else:
            # Support dotted relative modules as nested paths.
            rel = Path(*module_part.split("."))
            file_path = plugin_dir / f"{rel}.py"

        if not file_path.is_file():
            raise PluginManifestError(
                f"entrypoint module not found for {self.name!r}: {file_path}"
            )
        # Stay inside the plugin directory (no path escape).
        try:
            file_path.resolve().relative_to(plugin_dir.resolve())
        except ValueError as exc:
            raise PluginManifestError(
                f"entrypoint escapes plugin directory: {file_path}"
            ) from exc
        return file_path, class_name


def load_manifest(plugin_dir: Path | str) -> PluginManifest:
    """
    Load and validate ``plugin.json`` from an external plugin folder.

    Raises:
        PluginManifestError: Missing file, bad JSON, or invalid schema.
    """
    root = Path(plugin_dir).resolve()
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise PluginManifestError(
            f"External plugin at {root} is missing {MANIFEST_FILENAME}"
        )
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginManifestError(
            f"Invalid JSON in {manifest_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise PluginManifestError(
            f"Cannot read {manifest_path}: {exc}"
        ) from exc

    return PluginManifest.from_dict(data, path=manifest_path)

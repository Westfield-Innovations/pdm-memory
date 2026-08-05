# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# AUTHORIZED EXTENSION POINT (Westfield OS) — implementing this interface
# (e.g. BasePDMPlugin) is PERMITTED. Core software modification remains
# prohibited without a commercial license from Westfield Innovations LLC.

"""Dynamic PDM plugin loader (authorized extension surface)."""

from pdm_memory.plugins.base import BasePDMPlugin, PLUGIN_DRAWER_PREFIX
from pdm_memory.plugins.manager import (
    EXTERNAL_PLUGIN_DIR_PREFIX,
    PluginManager,
)
from pdm_memory.plugins.manifest import (
    MANIFEST_FILENAME,
    PluginManifest,
    PluginManifestError,
)
from pdm_memory.plugins.proxy import PluginCapabilityError, PluginMemoryProxy

__all__ = [
    "EXTERNAL_PLUGIN_DIR_PREFIX",
    "MANIFEST_FILENAME",
    "PLUGIN_DRAWER_PREFIX",
    "BasePDMPlugin",
    "PluginCapabilityError",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginMemoryProxy",
]

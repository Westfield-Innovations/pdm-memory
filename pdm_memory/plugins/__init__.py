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
    sha256_file,
)
from pdm_memory.plugins.proxy import (
    CAP_ADMIN_IO,
    CAP_PEER,
    CAP_READ,
    CAP_RECALL,
    CAP_WRITE,
    DEFAULT_CAPABILITIES,
    KNOWN_CAPABILITIES,
    PluginCapabilityError,
    PluginMemoryProxy,
    as_memory,
)

__all__ = [
    "CAP_ADMIN_IO",
    "CAP_PEER",
    "CAP_READ",
    "CAP_RECALL",
    "CAP_WRITE",
    "DEFAULT_CAPABILITIES",
    "EXTERNAL_PLUGIN_DIR_PREFIX",
    "KNOWN_CAPABILITIES",
    "MANIFEST_FILENAME",
    "PLUGIN_DRAWER_PREFIX",
    "BasePDMPlugin",
    "PluginCapabilityError",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginMemoryProxy",
    "as_memory",
    "sha256_file",
]

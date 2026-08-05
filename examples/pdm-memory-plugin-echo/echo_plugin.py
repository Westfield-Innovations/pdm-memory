# © 2026 Westfield Innovations LLC. Patent Pending.
# Example external plugin — copy this folder next to your app.

"""Minimal external plugin scaffold (pdm-memory-plugin-echo)."""

from __future__ import annotations

from pdm_memory.plugins.base import BasePDMPlugin


class EchoPlugin(BasePDMPlugin):
    """
    Demo plugin. Loaded only when::

        Memory(..., trust_plugins=True)
        # or plugin_allowlist=[path to this folder]
    """

    # Manifest overrides name/version/requires/autoload when discovered externally.
    name = "echo"
    version = "0.1.0"
    priority = 100

    def on_install(self) -> None:
        assert self.mem is not None
        self.plugin_save(
            "echo plugin online",
            tags=["plugin", "echo", "boot"],
        )

    def shout(self, text: str) -> str:
        """Round-trip save into the plugin drawer; return memory id."""
        return self.plugin_save(text, tags=["plugin", "echo", "shout"])

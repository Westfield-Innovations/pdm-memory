"""PDM connector: the owner's PDM as a memory connector for any assistant that speaks MCP.

Four tools over the SDK's public calls: ``pdm_recall``, ``pdm_save``, ``pdm_drawers``,
``pdm_explain``. The token stays in the connector's settings on the owner's side and
never passes through a chat. Needs the ``connector`` extra.
"""

from pdm_memory.connector.server import build_server, main

__all__ = ["build_server", "main"]

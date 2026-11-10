"""The connector server: stdio for a desktop app, streamable HTTP for a hosted address.

Environment (the SDK's own names, read by ``Memory.from_env``):
    PDM_STORE       where the PDM lives: a file path, a database address, or "cloud"
    PDM_USER        the owner's user name on that PDM
    PDM_TOKEN       the owner's token when PDM_STORE is "cloud"
    PDM_CLOUD_URL   the cloud server address
    PDM_CONNECTOR_TRANSPORT   "stdio" (default) or "streamable-http"
    PDM_CONNECTOR_SOURCE      source label on memories saved here (default "connector")
"""

from __future__ import annotations

import dataclasses
import math
import os
import sqlite3
import sys
from datetime import datetime
from typing import Any

from pdm_memory import Memory

INSTALL_HINT = (
    "PDM connector requires the MCP package.\n"
    'Install with:  pip install "pdm-memory[connector]"'
)
SETUP_HINT = 'Set PDM_STORE and PDM_USER in the connector\'s settings, and PDM_TOKEN when PDM_STORE is "cloud".'
TRANSPORTS = ("stdio", "streamable-http")


def _mcp():
    """The server class and ToolError for the installed MCP package (2.x or 1.x)."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError

        return MCPServer, ToolError
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError

        return FastMCP, ToolError


def _plain(value: Any) -> Any:
    """A JSON-safe value: datetimes as ISO text, infinite or NaN floats as None."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _hit(h: Any) -> dict:
    return {
        "id": h.id,
        "text": h.text,
        "drawer": h.drawer,
        "source": h.source,
        "pressure": round(float(h.pressure), 2),
        "tags": list(h.intent_tags or []),
        "quality": round(float(h.quality), 3),
        "retrieval_count": int(h.retrieval_count or 0),
        "last_reinforced": h.last_reinforced.isoformat() if h.last_reinforced else None,
    }


def build_server(memory: Memory | None = None, name: str = "PDM Memory"):
    """Build the connector around a Memory; with none given, one is built from the environment."""
    server_class, tool_error = _mcp()
    mem = memory or Memory.from_env()
    source = os.environ.get("PDM_CONNECTOR_SOURCE", "connector")
    server = server_class(name)

    @server.tool(name="pdm_recall", description=(
        "Recall the memories in the owner's PDM that resonate with a query. Call this before answering "
        "anything about the owner's work, projects, people or decisions. Returns the strongest memories "
        "with their drawer, pressure and tags."))
    def pdm_recall(query: str, k: int = 8, drawer: str | None = None, min_pressure: float = 0.0) -> list[dict]:
        # A lookup is not a use: recall here writes nothing back to the memories it returns.
        hits = mem.recall(
            query,
            k=max(1, min(int(k), 50)),
            drawer=drawer or None,
            min_pressure=float(min_pressure),
            reinforce=False,
        )
        return [_hit(h) for h in hits]

    @server.tool(name="pdm_save", description=(
        "Save one durable memory to the owner's PDM: a decision, a mechanism, a standing instruction, "
        "a fact about the owner's work. Text up to 500 characters. Give a drawer (the subject area), "
        "at least three tags, and a pressure from 50 to 100 for how much it matters."))
    def pdm_save(text: str, drawer: str = "general", tags: list[str] | None = None,
                 p_magnitude: float = 60.0, t_persistence: float = 30.0) -> dict:
        try:
            memory_id = mem.save(text[:500], source=source, tags=list(tags or []),
                                 p_magnitude=float(p_magnitude), t_persistence=float(t_persistence),
                                 drawer=drawer or "general")
        except ValueError as problem:
            raise tool_error(str(problem)) from None
        return {"id": memory_id, "drawer": drawer or "general", "saved": True}

    @server.tool(name="pdm_drawers", description=(
        "List the drawers (subject areas) in the owner's PDM with their counts and average pressure."))
    def pdm_drawers() -> list[dict]:
        return [{"drawer": d.domain, "memories": int(d.signature_count),
                 "avg_pressure": round(float(d.avg_pressure), 2), "description": d.description or ""}
                for d in mem.list_drawers()]

    @server.tool(name="pdm_explain", description=(
        "Explain why one memory holds the pressure it holds, and how it resonates with a query."))
    def pdm_explain(memory_id: str, query: str | None = None) -> dict:
        try:
            report = mem.explain(memory_id, query)
        except KeyError:
            raise tool_error(f"No memory with id {memory_id!r}.") from None
        fields = {k: _plain(v) for k, v in dataclasses.asdict(report).items()}
        return {**fields, "text": report.render()}

    return server


def main() -> None:
    """Run the connector with the transport named in PDM_CONNECTOR_TRANSPORT."""
    try:
        _mcp()
    except ImportError:
        print(INSTALL_HINT, file=sys.stderr)
        sys.exit(1)
    transport = os.environ.get("PDM_CONNECTOR_TRANSPORT", "stdio")
    if transport not in TRANSPORTS:
        print(f"PDM_CONNECTOR_TRANSPORT must be one of {', '.join(TRANSPORTS)}, not {transport!r}.",
              file=sys.stderr)
        sys.exit(1)
    try:
        server = build_server()
    except (ValueError, OSError, sqlite3.Error) as problem:
        # The desktop app shows this text; stdout stays clean for the protocol.
        print(f"PDM connector could not start: {problem}\n{SETUP_HINT}", file=sys.stderr)
        sys.exit(1)
    server.run(transport=transport)

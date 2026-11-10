"""PDM connector: the four tools over a local store, through an MCP client."""

import asyncio
import json
import os
import sys
from importlib.metadata import entry_points, requires

import pytest

# The client these tests drive the server with is MCP 2.x; 1.x is checked by hand.
Client = pytest.importorskip("mcp.client.client").Client

from pdm_memory import Memory
from pdm_memory.connector import build_server

TOOLS = {"pdm_recall", "pdm_save", "pdm_drawers", "pdm_explain"}


@pytest.fixture
def mem(tmp_path):
    m = Memory(store=str(tmp_path / "c.db"), user="owner")
    yield m
    m.close()


def call(server, name, args=None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args or {})

    return asyncio.run(go())


def payload(result):
    """The JSON a tool returned: a list for list results, else one object."""
    items = [json.loads(c.text) for c in result.content]
    return items[0] if len(items) == 1 and not isinstance(items[0], list) else items


def tool_names(server):
    async def go():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    return asyncio.run(go())


def save(server, text="Valve V3 sticks below 40F", drawer="plant"):
    result = call(server, "pdm_save", {"text": text, "drawer": drawer, "tags": ["valve", "cold", "plant"]})
    assert not result.is_error, result.content
    return payload(result)


def test_server_lists_the_four_tools(mem):
    assert tool_names(build_server(mem)) == TOOLS


def test_save_writes_with_the_source_label(mem, monkeypatch):
    monkeypatch.setenv("PDM_CONNECTOR_SOURCE", "my-claude")
    saved = save(build_server(mem))
    assert saved["drawer"] == "plant" and saved["saved"] is True
    assert mem.explain(saved["id"]).source == "my-claude"


def test_save_default_source_is_connector(mem, monkeypatch):
    monkeypatch.delenv("PDM_CONNECTOR_SOURCE", raising=False)
    saved = save(build_server(mem))
    assert mem.explain(saved["id"]).source == "connector"


def test_recall_returns_saved_memory(mem):
    server = build_server(mem)
    saved = save(server)
    hits = payload(call(server, "pdm_recall", {"query": "valve cold plant"}))
    hits = hits if isinstance(hits, list) else [hits]
    assert [h["id"] for h in hits] == [saved["id"]]
    assert set(hits[0]) == {
        "id", "text", "drawer", "source", "pressure", "tags",
        "quality", "retrieval_count", "last_reinforced",
    }


def test_recall_does_not_reinforce(mem):
    server = build_server(mem)
    saved = save(server)
    before = mem.explain(saved["id"])
    call(server, "pdm_recall", {"query": "valve cold plant"})
    call(server, "pdm_recall", {"query": "valve cold plant"})
    after = mem.explain(saved["id"])
    assert after.retrieval_count == before.retrieval_count == 0
    assert after.p_magnitude == before.p_magnitude


def test_recall_k_is_bounded(mem):
    server = build_server(mem)
    for word in ("alpha", "beta", "gamma"):
        save(server, text=f"Valve note {word}")
    one = call(server, "pdm_recall", {"query": "valve note", "k": 0})
    assert not one.is_error and len(one.content) <= 1
    many = call(server, "pdm_recall", {"query": "valve note", "k": 500})
    assert not many.is_error


def test_drawers_lists_counts(mem):
    server = build_server(mem)
    save(server)
    drawers = payload(call(server, "pdm_drawers"))
    drawers = drawers if isinstance(drawers, list) else [drawers]
    plant = [d for d in drawers if d["drawer"] == "plant"]
    assert plant and plant[0]["memories"] == 1


def test_explain_returns_the_report(mem):
    server = build_server(mem)
    saved = save(server)
    report = payload(call(server, "pdm_explain", {"memory_id": saved["id"]}))
    assert report["memory_id"] == saved["id"]
    assert "p_effective" in report
    assert isinstance(report["text"], str) and saved["id"] in report["text"]


def _reject(constant):
    raise ValueError(f"not JSON: {constant}")


def test_explain_infinite_half_life_is_valid_json(mem):
    memory_id = mem.save("Plant layout", tags=["plant", "layout", "map"], shape="structural")
    result = call(build_server(mem), "pdm_explain", {"memory_id": memory_id})
    report = json.loads(result.content[0].text, parse_constant=_reject)
    assert report["half_life_days"] is None


def test_explain_unknown_id_is_a_tool_error(mem):
    result = call(build_server(mem), "pdm_explain", {"memory_id": "nope"})
    assert result.is_error
    assert "nope" in result.content[0].text


def test_save_empty_text_is_a_tool_error(mem):
    result = call(build_server(mem), "pdm_save", {"text": "   "})
    assert result.is_error
    assert "empty" in result.content[0].text


def test_token_never_returned(mem, monkeypatch):
    monkeypatch.setenv("PDM_TOKEN", "tok-SECRET-123")
    server = build_server(mem)
    saved = save(server)
    for name, args in (
        ("pdm_recall", {"query": "valve"}),
        ("pdm_drawers", {}),
        ("pdm_explain", {"memory_id": saved["id"]}),
    ):
        text = " ".join(c.text for c in call(server, name, args).content)
        assert "tok-SECRET-123" not in text


def test_build_server_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PDM_STORE", str(tmp_path / "env.db"))
    monkeypatch.setenv("PDM_USER", "owner")
    result = call(build_server(), "pdm_drawers")
    assert not result.is_error


def test_main_without_mcp_prints_install_hint(monkeypatch, capsys):
    from pdm_memory.connector import server

    for module in ("mcp", "mcp.server", "mcp.server.mcpserver", "mcp.server.fastmcp"):
        monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(SystemExit) as stopped:
        server.main()
    assert stopped.value.code == 1
    assert 'pip install "pdm-memory[connector]"' in capsys.readouterr().err


def test_console_script_is_declared():
    scripts = {e.name: e.value for e in entry_points(group="console_scripts")}
    assert scripts.get("pdm-memory-connector") == "pdm_memory.connector:main"
    needs = [r.replace(" ", "") for r in requires("pdm-memory") or []]
    assert 'mcp>=1.8;extra=="connector"' in needs


def test_stdio_subprocess_lists_tools(tmp_path):
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pdm_memory.connector"],
        env={**os.environ, "PDM_STORE": str(tmp_path / "s.db"), "PDM_USER": "owner"},
    )

    async def go():
        async with Client(params) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert asyncio.run(asyncio.wait_for(go(), 60)) == TOOLS

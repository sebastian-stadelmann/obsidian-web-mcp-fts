"""The extension against the seam: hooks, the registered tool, both change feeds."""

import asyncio
import json

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from obsidian_vault_mcp.extensions import Extension
from obsidian_vault_mcp.server import build_app
from obsidian_vault_mcp.write_events import fire_write

from vault_fts.extension import TOOL_NAME, FtsExtension

from ._samples import NOTE_COUNT, paths


class RecordingFrontmatterIndex:
    """Stands in for the server's FrontmatterIndex: keeps what the extension attaches."""

    def __init__(self):
        self.listeners = []

    def add_change_listener(self, callback):
        self.listeners.append(callback)


@pytest.fixture
def started(vault, tmp_path):
    """An extension taken through the hooks in the order serve() calls them."""
    ext = FtsExtension(db_path=tmp_path / "state" / "fts.sqlite", languages="en,de")
    mcp = FastMCP("test")
    frontmatter_index = RecordingFrontmatterIndex()
    ext.register_tools(mcp)
    ext.before_indexes_start(frontmatter_index)
    ext.after_indexes_start(frontmatter_index)
    yield ext, mcp, frontmatter_index
    ext.shutdown()


def call_tool(mcp, arguments: dict) -> dict:
    result = asyncio.run(mcp.call_tool(TOOL_NAME, arguments))
    blocks = result[0] if isinstance(result, tuple) else result
    return json.loads("".join(getattr(block, "text", "") for block in blocks))


def test_is_extension_and_app_builds_with_it(vault, tmp_path):
    ext = FtsExtension(db_path=tmp_path / "fts.sqlite")
    assert isinstance(ext, Extension)
    assert build_app(extensions=[ext]) is not None


def test_tool_is_registered_and_searches(started):
    ext, mcp, _ = started
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert TOOL_NAME in tools
    assert tools[TOOL_NAME].annotations.readOnlyHint is True
    assert set(tools[TOOL_NAME].inputSchema["properties"]) == {"query", "path_prefix", "max_results"}

    assert "Active languages: english, german" in tools[TOOL_NAME].description

    assert ext.index.doc_count == NOTE_COUNT
    payload = call_tool(mcp, {"query": "traefik file-provider", "path_prefix": "howto/"})
    assert paths(payload) == ["howto/livesync-traefik.md"]
    assert "«" in payload["results"][0]["snippet"]


def test_tool_rejects_out_of_range_input(started):
    _, mcp, _ = started
    with pytest.raises(ToolError, match="max_results"):
        call_tool(mcp, {"query": "traefik", "max_results": 10_000})
    with pytest.raises(ToolError, match="query"):
        call_tool(mcp, {"query": "   "})


def test_watcher_feed_updates_and_removes(started, vault):
    ext, _, frontmatter_index = started
    (on_change,) = frontmatter_index.listeners  # exactly one listener attached

    note = vault / "projekte" / "grafana.md"
    note.write_text("Dashboards für Grafana.\n", encoding="utf-8")
    on_change(str(note), True)
    assert paths(json.loads(ext.search("grafana"))) == ["projekte/grafana.md"]

    note.unlink()
    on_change(str(note), False)
    assert json.loads(ext.search("grafana"))["total"] == 0

    on_change("/somewhere/else/entirely.md", True)  # not under the vault: ignored
    assert ext.index.doc_count == NOTE_COUNT


def test_write_events_are_indexed_immediately(started, vault):
    ext, _, _ = started

    (vault / "projekte/loki.md").write_text("Logs mit Loki.\n", encoding="utf-8")
    fire_write("created", ["projekte/loki.md"])
    assert paths(json.loads(ext.search("loki"))) == ["projekte/loki.md"]

    (vault / "projekte/loki.md").write_text("Logs mit Promtail.\n", encoding="utf-8")
    fire_write("updated", ["projekte/loki.md", "attachments/bild.png"])  # non-notes are ignored
    assert json.loads(ext.search("loki"))["results"][0]["matched_fields"] == ["path", "title"]
    assert paths(json.loads(ext.search("promtail"))) == ["projekte/loki.md"]

    (vault / "projekte").rename(vault / "archiv")
    fire_write("moved", ["projekte", "archiv"])
    assert set(paths(json.loads(ext.search("promtail OR relaunch")))) == {"archiv/loki.md", "archiv/website.md"}

    (vault / "archiv/loki.md").unlink()
    fire_write("deleted", ["archiv/loki.md"])
    assert json.loads(ext.search("promtail"))["total"] == 0


def test_index_inside_the_vault_refuses_to_start(vault):
    ext = FtsExtension(db_path=vault / "fts.sqlite")
    with pytest.raises(SystemExit):
        ext.before_indexes_start(RecordingFrontmatterIndex())
    assert not (vault / "fts.sqlite").exists()


def test_unusable_index_degrades_to_a_tool_error(vault, tmp_path):
    """A broken search index must not take the vault server down with it."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    ext = FtsExtension(db_path=blocker / "fts.sqlite")  # parent is a file: cannot be created
    frontmatter_index = RecordingFrontmatterIndex()
    ext.before_indexes_start(frontmatter_index)
    ext.after_indexes_start(frontmatter_index)

    assert frontmatter_index.listeners == []
    assert "unavailable" in json.loads(ext.search("traefik"))["error"]
    fire_write("updated", ["projekte/website.md"])  # nothing registered, nothing raised

"""The extension in the real server process, reached the way claude.ai reaches it.

Nothing is stubbed: the child runs vault_fts.main.main(), so serve(), the frontmatter
index with its filesystem watcher, the bearer middleware and uvicorn are the real ones.
A client authenticates with the bearer token and calls tools over MCP streamable HTTP.

The child's environment is built from scratch so nothing exported in the developer's
shell (a real VAULT_PATH, an audit log, a public bind address) reaches the test.
"""

import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager, contextmanager

from ._samples import paths

TOKEN = "fts-live-test-token"
# COVERAGE_PROCESS_CONFIG is set by coverage.py itself (patch = subprocess) and is what
# makes the child measure its own lines. It is absent when the tests run without --cov.
_PASSTHROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LD_LIBRARY_PATH", "TZ", "COVERAGE_PROCESS_CONFIG")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def live_server(tmp_path, vault, db_path):
    port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {name: os.environ[name] for name in _PASSTHROUGH if name in os.environ}
    env.update({
        "HOME": str(home),
        "PYTHONUNBUFFERED": "1",
        "VAULT_PATH": str(vault),
        "VAULT_MCP_TOKEN": TOKEN,
        "VAULT_MCP_HOST": "127.0.0.1",
        "VAULT_MCP_PORT": str(port),
        "VAULT_MCP_PATH": "/",
        "VAULT_MCP_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "VAULT_FTS_DB_PATH": str(db_path),
        "VAULT_FTS_LANGUAGES": "en,de",
    })
    log_path = tmp_path / "server.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", "from vault_fts.main import main; main()"],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited: {log_path.read_text(errors='replace')[-2000:]}")
            try:
                with urllib.request.urlopen(f"{base_url}/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            if time.time() > deadline:
                raise RuntimeError(f"server did not come up: {log_path.read_text(errors='replace')[-2000:]}")
            time.sleep(0.3)
        yield base_url, log_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)


@asynccontextmanager
async def _mcp_streams(url: str, headers: dict):
    """Open the streamable-HTTP transport on whichever client API this mcp version has.

    The server allows mcp>=1.9,<2; the client entry point was renamed along the way.
    """
    from mcp.client import streamable_http

    if hasattr(streamable_http, "streamable_http_client"):
        import httpx

        async with (
            httpx.AsyncClient(headers=headers, timeout=30) as http_client,
            streamable_http.streamable_http_client(url, http_client=http_client) as streams,
        ):
            yield streams
    else:
        async with streamable_http.streamablehttp_client(url, headers=headers) as streams:
            yield streams


def call_tool(base_url: str, name: str, arguments: dict) -> dict:
    from mcp import ClientSession

    async def run():
        async with (
            _mcp_streams(f"{base_url}/", {"Authorization": f"Bearer {TOKEN}"}) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
            text = "".join(getattr(block, "text", "") for block in result.content)
            return json.loads(text) if text.strip().startswith("{") else {"error": text}

    return asyncio.run(run())


def test_search_over_http_follows_tool_writes_and_outside_edits(tmp_path, vault):
    db_path = tmp_path / "state" / "fts.sqlite"
    with live_server(tmp_path, vault, db_path) as (base_url, log_path):
        # Built at startup, before the first request.
        assert "FTS index ready" in log_path.read_text(errors="replace")
        hits = call_tool(base_url, "vault_fts_search", {"query": "traefik"})
        assert paths(hits)[0] == "howto/livesync-traefik.md", hits

        # Languages come from the environment; stemming reaches the client as word forms.
        assert "[english, german]" in log_path.read_text(errors="replace")
        stemmed = call_tool(base_url, "vault_fts_search", {"query": "wie viele zertifikat"})
        assert stemmed["ignored_stopwords"] == ["wie"] and stemmed["word_forms"] == {"zertifikat": ["zertifikate"]}

        # A write through the server's own tool is searchable on the very next call.
        call_tool(base_url, "vault_write", {"path": "projekte/loki.md", "content": "Logs mit Loki sammeln.\n"})
        assert paths(call_tool(base_url, "vault_fts_search", {"query": "loki"})) == ["projekte/loki.md"]

        call_tool(base_url, "vault_delete", {"path": "projekte/loki.md", "confirm": True})
        assert call_tool(base_url, "vault_fts_search", {"query": "loki"})["total"] == 0

        # An edit from outside the server (a sync tool writing into the vault) arrives
        # through the filesystem watcher, after its debounce.
        (vault / "projekte" / "extern.md").write_text("Von aussen synchronisiert: Tailscale.\n", encoding="utf-8")
        deadline = time.time() + 30
        found = []
        while time.time() < deadline and not found:
            time.sleep(1)
            found = paths(call_tool(base_url, "vault_fts_search", {"query": "tailscale"}))
        assert found == ["projekte/extern.md"]

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert not list(vault.rglob("*.sqlite*"))  # nothing of the index landed in the vault

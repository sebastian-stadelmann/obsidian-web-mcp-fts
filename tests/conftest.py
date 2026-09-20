"""Fixtures: a small German-language vault and an index over it."""

from pathlib import Path

import pytest

from ._samples import DAILY, ENGLISH, FILLER, HOWTO, PERSON, PROJECT, SINGULAR


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    files = {
        "howto/livesync-traefik.md": HOWTO,
        "people/Müller.md": PERSON,
        "daily/2026-09-01.md": DAILY,
        "projekte/website.md": PROJECT,
        "notes/renewal.md": ENGLISH,
        "notizen/zertifikat.md": SINGULAR,
        ".obsidian/config.json": '{"theme": "dark"}',
        ".trash/old.md": "Traefik im Papierkorb\n",
        "notes.txt": "Traefik in einer Textdatei\n",
        **FILLER,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # The server reads VAULT_PATH from its config module at call time.
    import obsidian_vault_mcp.config as vault_config
    monkeypatch.setattr(vault_config, "VAULT_PATH", Path(root))

    # The write-listener list is process-global; a listener left over from another
    # test would point at a closed index.
    from obsidian_vault_mcp import write_events
    monkeypatch.setattr(write_events, "_write_listeners", [])
    return root


@pytest.fixture
def index(vault, tmp_path):
    from vault_fts.index import FtsIndex
    from vault_fts.languages import resolve_languages

    # The sample vault is German with English terms, like the vault this was built for.
    idx = FtsIndex(tmp_path / "state" / "fts.sqlite", languages=resolve_languages("en,de"))
    idx.open()
    idx.reconcile()
    yield idx
    idx.close()

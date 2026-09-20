"""Building and maintaining the index: what gets in, what stays out, what heals."""

import os
import sqlite3
import stat

import pytest

from vault_fts import index as index_module
from vault_fts.index import FtsIndex, db_path_inside_vault, parse_note

from ._samples import HOWTO, NOTE_COUNT, paths


def test_build_indexes_only_markdown_outside_excluded_dirs(index):
    assert index.doc_count == NOTE_COUNT
    hits = paths(index.search("traefik", max_results=50))
    assert ".trash/old.md" not in hits and "notes.txt" not in hits
    assert set(hits) == {"howto/livesync-traefik.md", "daily/2026-09-01.md"}


def test_parse_note_columns():
    note = parse_note("howto/livesync-traefik.md", HOWTO)
    assert note.title == "LiveSync mit Traefik"
    assert note.aliases.split("\n") == ["LiveSync Howto", "Bridge-Setup"]
    assert note.tags.split("\n") == ["obsidian", "homelab"]
    assert "File-Provider" in note.description
    # A "# comment" inside a fenced code block is not a heading.
    assert note.headings.split("\n") == ["LiveSync mit Traefik", "Entscheidung"]
    assert "type: howto" not in note.body


def test_title_falls_back_to_file_name_and_broken_yaml_is_still_indexed():
    assert parse_note("people/Müller.md", "kein Frontmatter").title == "Müller"
    broken = parse_note("x/kaputt.md", "---\naliases: [unclosed\n---\nText bleibt auffindbar\n")
    assert broken.title == "kaputt" and "auffindbar" in broken.body


def test_reconcile_is_incremental(index, vault):
    assert index.reconcile()["indexed"] == 0  # nothing changed since the build

    (vault / "projekte/website.md").write_text("Relaunch verschoben wegen Kubernetes.\n", encoding="utf-8")
    (vault / "projekte/neu.md").write_text("Frisches Projekt zu Grafana.\n", encoding="utf-8")
    (vault / "daily/2026-09-01.md").unlink()

    stats = index.reconcile()
    assert (stats["indexed"], stats["removed"], stats["total"]) == (2, 1, NOTE_COUNT)
    assert paths(index.search("kubernetes")) == ["projekte/website.md"]
    assert paths(index.search("grafana")) == ["projekte/neu.md"]
    assert "daily/2026-09-01.md" not in paths(index.search("müller"))
    assert index.search("theme")["total"] == 0  # the old text of website.md is gone


def test_update_and_remove_single_paths(index, vault):
    (vault / "projekte/website.md").write_text("Jetzt mit Prometheus.\n", encoding="utf-8")
    assert index.update_path("projekte/website.md") == "indexed"
    assert index.update_path("projekte/website.md") == "unchanged"
    assert index.update_path("projekte/website.md", force=True) == "indexed"
    assert paths(index.search("prometheus")) == ["projekte/website.md"]

    assert index.update_path("notes.txt") == "skipped"
    assert index.update_path(".trash/old.md") == "skipped"
    assert index.update_path("gibt/es/nicht.md") == "removed"

    assert index.remove_path("projekte/website.md") == 1
    assert index.search("prometheus")["total"] == 0


def test_directory_move_is_followed(index, vault):
    (vault / "projekte").rename(vault / "archiv")
    assert index.remove_path("projekte") == 1
    assert index.update_tree("archiv", force=True) == 1
    assert paths(index.search("relaunch")) == ["archiv/website.md"]
    # A sibling whose name merely starts the same is not swept up.
    (vault / "howto-alt").mkdir()
    (vault / "howto-alt/x.md").write_text("Bleibt stehen.\n", encoding="utf-8")
    index.update_tree("howto-alt")
    index.remove_path("howto")
    assert paths(index.search("stehen")) == ["howto-alt/x.md"]


def test_oversized_notes_are_skipped(vault, tmp_path):
    idx = FtsIndex(":memory:", max_file_bytes=200)
    idx.open()
    stats = idx.reconcile()
    assert stats["skipped"] == 1  # the howto is the only note above 200 bytes
    assert "howto/livesync-traefik.md" not in paths(idx.search("traefik"))
    idx.close()


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hardlinks")
def test_hardlinked_files_are_never_indexed_or_returned(index, vault, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("Geheimnis ausserhalb des Vaults\n", encoding="utf-8")
    os.link(outside, vault / "leak.md")
    index.reconcile()
    assert index.search("geheimnis")["total"] == 0

    # Indexed first, hardlinked afterwards: the query-time guard drops it and heals the index.
    os.link(vault / "projekte/website.md", vault / "projekte/kopie.md")
    assert index.search("relaunch")["total"] == 0
    assert index.doc_count == NOTE_COUNT - 1


def test_index_file_is_private_persistent_and_outside_the_vault(vault, tmp_path):
    db_path = tmp_path / "state" / "fts.sqlite"
    idx = FtsIndex(db_path)
    idx.open()
    assert idx.reconcile()["indexed"] == NOTE_COUNT
    idx.close()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    reopened = FtsIndex(db_path)
    reopened.open()
    stats = reopened.reconcile()
    assert (stats["indexed"], stats["unchanged"]) == (0, NOTE_COUNT)  # survived the restart
    reopened.close()

    assert db_path_inside_vault(vault / "fts.sqlite")
    assert db_path_inside_vault(vault / "sub" / "fts.sqlite")
    assert not db_path_inside_vault(db_path)
    assert not db_path_inside_vault(":memory:")


def test_outdated_schema_and_corrupt_file_are_rebuilt(vault, tmp_path, monkeypatch):
    db_path = tmp_path / "fts.sqlite"
    idx = FtsIndex(db_path)
    idx.open()
    idx.reconcile()
    idx.close()

    monkeypatch.setattr(index_module, "SCHEMA_VERSION", index_module.SCHEMA_VERSION + 1)
    newer = FtsIndex(db_path)
    newer.open()
    assert newer.doc_count == 0  # old layout dropped, not reused
    assert newer.reconcile()["indexed"] == NOTE_COUNT
    newer.close()

    for suffix in ("-wal", "-shm"):
        (tmp_path / f"fts.sqlite{suffix}").unlink(missing_ok=True)
    db_path.write_bytes(b"this is not a sqlite database, not even close" * 50)
    healed = FtsIndex(db_path)
    healed.open()
    assert healed.reconcile()["indexed"] == NOTE_COUNT
    healed.close()
    assert sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0] == index_module.SCHEMA_VERSION

"""Hooks the FTS index into the obsidian-web-mcp extension seam."""

from __future__ import annotations

import logging
from pathlib import Path

from obsidian_vault_mcp import config as vault_config
from obsidian_vault_mcp.extensions import Extension
from obsidian_vault_mcp.serialization import dumps
from obsidian_vault_mcp.write_events import register_write_listener
from pydantic import BaseModel, ConfigDict, Field

from . import config
from .index import FtsIndex, db_path_inside_vault
from .languages import resolve_languages

logger = logging.getLogger(__name__)

TOOL_NAME = "vault_fts_search"


def tool_description(languages: tuple[str, ...]) -> str:
    """What the model reads to decide when and how to call the tool."""
    if languages:
        language_notes = (
            f"Active languages: {', '.join(languages)}. Their filler words (the, how, wie, warum ...) "
            "are ignored, and a word also matches its other word forms through stemming "
            "(certificate finds certificates, Entscheidung finds Entscheidungen); word_forms "
            "shows the expansion. Put a word in double quotes to match it exactly. "
        )
    else:
        language_notes = "No stemming: add * for a prefix match (änderung* also finds Änderungen). "
    return (
        "Ranked full-text search over the vault's markdown notes (SQLite FTS5, BM25). "
        "Matches whole notes, not lines: every term must occur somewhere in the note "
        "(path, title, aliases, description, tags, headings or body). If no note has every "
        "term, the largest set of terms that some note has is searched instead (query_mode "
        "is then 'relaxed' and dropped_terms lists what was left out). Hits in title, aliases "
        "and description rank above hits in the body. Case- and diacritics-insensitive "
        "(muller finds Müller). " + language_notes + "FTS5 syntax works and runs as written: "
        "\"exact phrase\", term*, a OR b, a NOT b, NEAR(a b, 5), title:term. Returns path, "
        "title, score, matched_fields and a snippet with «matches» marked. Prefer this over "
        "vault_search for topic and keyword lookups; use vault_search for regex or exact "
        "line matches."
    )


class VaultFtsSearchInput(BaseModel):
    """Ranked full-text search across vault notes."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., min_length=1, max_length=300, description="Words or an FTS5 query")
    path_prefix: str | None = Field(
        default=None,
        max_length=500,
        description="Only notes whose vault-relative path starts with this (e.g. 'projekte/')",
    )
    max_results: int = Field(default=20, ge=1, le=vault_config.MAX_SEARCH_RESULTS)


class FtsExtension(Extension):
    """Registers ``vault_fts_search`` and keeps the FTS5 index in sync with the vault.

    Two feeds keep it current. The frontmatter index's change listener reports every
    .md change the filesystem watcher sees, including edits made outside the server
    (Obsidian Sync, LiveSync bridge); it is debounced by a few seconds. Write events
    from the server's own tools arrive immediately, so a search right after a
    vault_write already sees the new text.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        max_file_bytes: int | None = None,
        languages: str | None = None,
    ) -> None:
        self._db_path = db_path if db_path is not None else config.VAULT_FTS_DB_PATH
        try:
            self._languages = resolve_languages(languages if languages is not None else config.VAULT_FTS_LANGUAGES)
        except ValueError as exc:
            # A typo must not silently switch a language off. Runs before serve() has
            # set up logging, so the message goes out through SystemExit itself.
            raise SystemExit(f"VAULT_FTS_LANGUAGES: {exc}") from exc
        self._index = FtsIndex(
            self._db_path,
            max_file_bytes=max_file_bytes if max_file_bytes is not None else config.VAULT_FTS_MAX_FILE_BYTES,
            languages=self._languages,
        )
        self._unavailable_reason = "FTS index has not been started"

    @property
    def index(self) -> FtsIndex:
        return self._index

    # -- seam hooks, in the order serve() calls them --

    def register_tools(self, mcp) -> None:
        @mcp.tool(
            name=TOOL_NAME,
            description=tool_description(tuple(lang.name for lang in self._languages)),
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        )
        def vault_fts_search(query: str, path_prefix: str | None = None, max_results: int = 20) -> str:
            """Ranked full-text search across vault notes."""
            inp = VaultFtsSearchInput(query=query, path_prefix=path_prefix, max_results=max_results)
            return self.search(inp.query, inp.path_prefix, inp.max_results)

    def before_indexes_start(self, frontmatter_index) -> None:
        # Fail CLOSED like the server does for its audit log: an index inside the vault
        # is vault plaintext that a sync tool would replicate to every device.
        if db_path_inside_vault(self._db_path):
            logger.error(
                "VAULT_FTS_DB_PATH resolves inside the vault (%s). Choose a path outside VAULT_PATH.",
                self._db_path,
            )
            raise SystemExit(1)

        try:
            self._index.open()
        except Exception as exc:
            # A broken search index must not take the whole vault server down.
            self._unavailable_reason = f"FTS index unavailable: {exc}"
            logger.error("%s -- %s will return an error", self._unavailable_reason, TOOL_NAME)
            return

        # Attached before the watcher starts, so no change falls between build and listen.
        frontmatter_index.add_change_listener(self._on_file_change)
        register_write_listener(self._on_write)

    def after_indexes_start(self, frontmatter_index) -> None:
        if not self._index.is_open:
            return
        try:
            stats = self._index.reconcile()
        except Exception as exc:
            self._unavailable_reason = f"FTS index build failed: {exc}"
            logger.exception("FTS index build failed")
            self._index.close()
            return
        logger.info(
            "FTS index ready: %d notes (%d indexed, %d unchanged, %d removed, %d skipped), "
            "%d words stemmed for [%s] in %.2fs [%s]",
            stats["total"], stats["indexed"], stats["unchanged"], stats["removed"], stats["skipped"],
            stats["stemmed_words"], ", ".join(lang.name for lang in self._languages) or "no language",
            stats["seconds"], self._db_path,
        )

    def shutdown(self) -> None:
        self._index.close()

    # -- tool body --

    def search(self, query: str, path_prefix: str | None = None, max_results: int = 20) -> str:
        if not self._index.is_open:
            return dumps({"error": self._unavailable_reason})
        try:
            return dumps(self._index.search(query, path_prefix, max_results))
        except Exception as exc:
            logger.error("%s error: %s", TOOL_NAME, exc)
            return dumps({"error": str(exc)})

    # -- change feeds --

    def _on_file_change(self, abs_path: str, exists: bool) -> None:
        """Watcher feed: callback(abs_path, exists) for .md files, after the debounce."""
        rel_path = _relative_to_vault(abs_path)
        if rel_path is None or not self._index.is_open:
            return
        if exists:
            self._index.update_path(rel_path)
        else:
            self._index.remove_path(rel_path)

    def _on_write(self, operation: str, paths: list[str]) -> None:
        """Tool feed: callback(operation, vault-relative paths), right after the write."""
        if not self._index.is_open:
            return
        if operation == "moved" and len(paths) == 2:
            # vault_move takes files and directories alike.
            self._index.remove_path(paths[0])
            self._index.update_tree(paths[1], force=True)
        elif operation == "deleted":
            for path in paths:
                self._index.remove_path(path)
        else:  # created / updated
            for path in paths:
                self._index.update_tree(path, force=True)


def _relative_to_vault(abs_path: str) -> str | None:
    """Vault-relative posix path, or None when the path is not under the vault.

    The watcher may report the path as given or fully resolved (/var vs /private/var
    on macOS), so both spellings of the vault root are tried.
    """
    path = Path(abs_path)
    for root in (vault_config.VAULT_PATH, vault_config.VAULT_PATH.resolve()):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    try:
        return path.resolve().relative_to(vault_config.VAULT_PATH.resolve()).as_posix()
    except (ValueError, OSError):
        return None

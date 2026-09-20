"""Hooks the FTS index into the obsidian-web-mcp extension seam."""

import logging

from obsidian_vault_mcp.extensions import Extension

logger = logging.getLogger(__name__)


class FtsExtension(Extension):
    """Registers ``vault_fts_search`` and keeps the FTS5 index in sync.

    Hook order is fixed by the server: register_tools, before_indexes_start,
    after_indexes_start, register_routes, shutdown.
    """

    def register_tools(self, mcp) -> None:
        logger.info("vault_fts: extension loaded (tool registration pending)")

    def before_indexes_start(self, frontmatter_index) -> None:
        pass

    def after_indexes_start(self, frontmatter_index) -> None:
        pass

    def shutdown(self) -> None:
        pass

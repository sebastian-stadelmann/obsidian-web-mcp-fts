"""Smoke tests: the extension is a valid Extension and the app builds with it."""

from obsidian_vault_mcp.extensions import Extension
from obsidian_vault_mcp.server import build_app

from vault_fts.extension import FtsExtension


def test_is_extension():
    assert isinstance(FtsExtension(), Extension)


def test_app_builds_with_extension():
    app = build_app(extensions=[FtsExtension()])
    assert app is not None

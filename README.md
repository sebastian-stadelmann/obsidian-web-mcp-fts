# obsidian-web-mcp-fts

SQLite FTS5 full-text search as an **extension** for
[jimprosser/obsidian-web-mcp](https://github.com/jimprosser/obsidian-web-mcp).
No fork: the server is a pinned dependency, this package adds one tool on top
via the `Extension` seam and starts the server with `serve([FtsExtension()])`.

## Why

The stock `vault_search` is grep (ripgrep, line-based, no ranking). This
extension adds `vault_fts_search`: BM25-ranked, document-wide matching,
phrase / prefix / NEAR queries, snippets, diacritics folding (`muller` finds
`Müller`) and German stemming via Snowball. Index lives in one SQLite file
outside the vault, kept fresh through the server's frontmatter-index change
listener and write events.

## Status

Skeleton. The extension loads and the server starts; the index and the tool
are not implemented yet.

## Layout

```
src/vault_fts/
    extension.py    FtsExtension: hooks into the obsidian-web-mcp seam
    index.py        SQLite FTS5 index (build, update, query)
    main.py         entry point: serve([FtsExtension()])
tests/
```

## Run

Same environment variables as `vault-mcp` (`VAULT_PATH`, `VAULT_MCP_TOKEN`,
OAuth, ...), plus:

| Variable | Default | Description |
|---|---|---|
| `VAULT_FTS_DB_PATH` | `~/.local/share/vault-mcp/fts.sqlite` | Index file. Keep it outside the vault. |

```bash
uv sync
uv run vault-mcp-fts
```

## Develop against a local server checkout

```bash
uv add --editable ../obsidian-web-mcp
uv run --extra dev pytest
```

## License

MIT.

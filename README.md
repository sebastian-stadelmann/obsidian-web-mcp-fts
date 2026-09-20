# obsidian-web-mcp-fts

Ranked full-text search for
[jimprosser/obsidian-web-mcp](https://github.com/jimprosser/obsidian-web-mcp), built on
SQLite FTS5. It is an **extension**, not a fork: the server is a pinned dependency, this
package adds one tool through the server's `Extension` seam and starts it with
`serve([FtsExtension()])`. No new services and no dependencies beyond the server's own;
FTS5 ships with Python's `sqlite3`.

## What it adds

The stock `vault_search` is grep: line-based, unranked, one regex. `vault_fts_search`:

- **Ranks** with BM25. A hit in the title, aliases or description outranks a hit in the body
  (column weights: title 10, aliases 8, description 5, path 4, tags 3, headings 3, body 1).
- **Matches whole notes.** `e2ee zertifikate` finds a note that has the two words on
  different lines.
- **Folds case and diacritics.** `muller` finds `Müller`, `anderung` finds `Änderung`.
- **Takes questions as they come.** German and English filler words (`wie`, `warum`, `mit`,
  `the`, `how` ...) are ignored. If no note has every remaining term, the largest set of
  terms that some note has is searched instead, preferring the rarest terms.
- **Survives punctuation.** `file-provider`, `C++`, `10:30` or a URL are searched as text
  instead of failing in the FTS5 parser.
- **Speaks FTS5** when the query uses it: `"exact phrase"`, `term*`, `a OR b`, `a NOT b`,
  `NEAR(a b, 5)`, `title:term`. Such a query runs as written and is never widened.

Not yet: stemming. `Änderungen` does not find `Änderung`; use `änderung*`.

### Tool

`vault_fts_search(query, path_prefix=None, max_results=20)`

`path_prefix` is a string prefix on the vault-relative path; end it with `/` to mean a
folder (`daily/`). `max_results` is capped at the server's search limit (50).

```json
{
  "results": [
    {
      "path": "howto/livesync-traefik.md",
      "title": "LiveSync mit Traefik",
      "score": 8.617,
      "matched_fields": ["path", "title", "description", "body"],
      "snippet": "…«Traefik» «File-Provider» nachgerüstet, Zertifikate bleiben erhalten…",
      "description": "Obsidian LiveSync hinter dem Traefik File-Provider mit CouchDB.",
      "tags": ["obsidian", "homelab"]
    }
  ],
  "total": 1,
  "truncated": false,
  "query_mode": "all"
}
```

`query_mode` says how the query was read: `fts5` (ran as written), `all` (every term is
in every result) or `relaxed` (some terms were left out; `dropped_terms` lists them).
`ignored_stopwords` lists filler words that were skipped.

## How the index stays current

- **At startup** the index is reconciled with the vault before the server accepts requests:
  new and changed notes (by mtime and size) are indexed, vanished ones removed. With a
  persisted index file a restart only stats the files. A full build of 330 notes took
  about 0.2 s on an M-series Mac.
- **Edits from outside the server** (Obsidian Sync, a LiveSync bridge writing into the
  vault) arrive through the server's filesystem watcher, a few seconds after the change.
- **Writes through the server's own tools** are indexed immediately, so a search right
  after `vault_write`, `vault_edit`, `vault_move` or `vault_delete` sees the result.

Only `*.md` files are indexed. `.obsidian`, `.trash`, `.git` and every dot-directory are
skipped. Every path passes the server's own read guard, so files outside the vault and
hardlinked files are never indexed and never returned.

## Configuration

Same environment variables as `vault-mcp` (`VAULT_PATH`, `VAULT_MCP_TOKEN`, OAuth ...), plus:

| Variable | Default | Description |
|---|---|---|
| `VAULT_FTS_DB_PATH` | `~/.local/share/vault-mcp/fts.sqlite` | Index file, or `:memory:` to rebuild on every start. |
| `VAULT_FTS_MAX_FILE_BYTES` | `2000000` | Notes larger than this are not indexed. |

The index file holds vault plaintext. It is created with mode `0600`, and the server
**refuses to start** if the path resolves inside the vault, where a sync tool would
replicate it to every device. Any other index problem (unwritable path, SQLite without
FTS5) only disables the tool; the rest of the vault server keeps running.

## Run

```bash
uv sync
uv run vault-mcp-fts
```

### Container

Replace the stock server's image with one built from this repository. `uv sync` fetches
the pinned server from GitHub.

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY obsidian-web-mcp-fts/ /app/
RUN uv sync --frozen
CMD ["uv", "run", "vault-mcp-fts"]
```

Keep the existing `environment:` block and add the index location on a volume outside
the vault mount:

```yaml
    environment:
      VAULT_FTS_DB_PATH: /data/fts/fts.sqlite
    volumes:
      - ./vault:/data/vault:Z
      - ./fts:/data/fts:Z
```

After the first start the log shows `FTS index ready: N notes ...`. Reconnect the
connector in claude.ai once so the new tool shows up in its tool list.

## Development

```
src/vault_fts/
    extension.py    FtsExtension: seam hooks, the tool, both change feeds
    index.py        FtsIndex: schema, reconcile, incremental updates, query handling
    stopwords.py    filler words ignored in plain-word queries
    config.py       environment variables
    main.py         entry point: serve([FtsExtension()])
tests/
```

```bash
uv run --extra dev pytest
```

`tests/test_live_server.py` starts the real server process and calls the tools over MCP
streamable HTTP with a bearer token, including an edit made behind the server's back. It
takes a few seconds because it waits for the watcher's debounce.

To work against a local checkout of the server instead of the pinned commit:

```bash
uv add --editable ../obsidian-web-mcp
```

The server dependency is pinned to a commit in `pyproject.toml`. Bump it deliberately and
run the tests; the seam (`Extension`, `add_change_listener`, `register_write_listener`)
is young.

Known gaps: no stemming, no audit-log records for `vault_fts_search` (the server's audit
wrapper is private), `.canvas` and other non-markdown files are not indexed.

## License

MIT.

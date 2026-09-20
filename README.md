# obsidian-web-mcp-fts

[![CI](https://github.com/sebastian-stadelmann/obsidian-web-mcp-fts/actions/workflows/ci.yml/badge.svg)](https://github.com/sebastian-stadelmann/obsidian-web-mcp-fts/actions/workflows/ci.yml)

Ranked full-text search for [obsidian-web-mcp](https://github.com/jimprosser/obsidian-web-mcp),
the remote MCP server for Obsidian vaults. It adds one tool, `vault_fts_search`, built on
SQLite FTS5.

This is an **extension**, not a fork. The server stays an unmodified, pinned dependency;
this package plugs into its `Extension` seam and starts it with `serve([FtsExtension()])`.
No new services: FTS5 ships with Python's `sqlite3`, and the only added dependency is the
pure-Python `snowballstemmer`. An independent project, not affiliated with upstream.

## Why: grep finds lines, this finds notes

The server's built-in `vault_search` is ripgrep: one regex, matched line by line, results
in file order, cut off at the limit. That is the right tool for an exact string. It is the
wrong tool for the question an assistant usually has: *which note is about this?*

**The caller is a model. Can it not rephrase until grep hits?** It can, and that solves a
different problem. Rephrasing helps when the note uses other words than the question. It
does not change how grep answers:

- **grep matches lines.** Two words that are in the same note but not on the same line
  cannot be asked for in one call. The caller searches each word and intersects the
  results itself.
- **grep does not rank.** A common word returns the first 20 matching lines in file
  order, and the caller cannot see what was cut off.
- **grep answers per line.** Every matching line comes with its context and the note's
  frontmatter, and all of it lands in the model's context.

Measured on a real vault with two words from a note's body, `vault_fts_search` returned
the note in one call and a 2 kB response, among the first three results 94 % of the time.
With grep the same search took two calls and 14 to 23 kB. The caller then either
intersects the two results and sees the note roughly 60 to 90 % of the time, or goes
through 18 to 27 candidate notes. The numbers, the method and a script to repeat it on your own vault
are under [Measurements](#measurements).

Take a vault with a how-to titled "Renew the wildcard certificate", a project note that
says "Decisions: we decided against Kubernetes", a contact "Jonas Müller", and daily notes
that mention Traefik in passing.

| Query | `vault_search` (grep) | `vault_fts_search` |
| --- | --- | --- |
| `renew certificate` | Nothing. The two words are never on one line in that order. | The how-to, matched in title, aliases, description, headings and body. |
| `decision kubernetes` | Nothing. | "**Decisions**: we decided against **Kubernetes**." |
| `traefik` | Every line containing it, in file order, until the limit is reached. The how-to and a passing mention in a daily note look the same. | The how-to first, because it matches in the description too. Daily notes after it. |
| `certificates` | Misses every note that says "certificate". | Finds both forms and highlights the one that is there. |
| `muller` | Nothing. | `Müller`, the contact note first (name in path and title), then the daily note. |
| `how do I renew the certificates` | Nothing. | Ignores `how do I the`, finds the how-to through `renew` and `certificate`. |

grep stays the better choice for a regular expression, an exact line, or files that are
not markdown. Both tools are there; this one does not replace the other.

What comes back for the last query:

```json
{
  "results": [
    {
      "path": "howto/renew-wildcard-certificate.md",
      "title": "Renew the wildcard certificate",
      "score": 11.366,
      "matched_fields": ["path", "title", "aliases", "description", "headings", "body"],
      "snippet": "# «Renew» the wildcard «certificate» ## Why it expires The «certificate» is valid for 90 days. ## Steps Traefik picks up the «renewed» file from acme.json…",
      "description": "Renewing the Let's Encrypt wildcard certificate behind Traefik, step by step.",
      "tags": ["tls", "homelab"],
      "match": "word_forms"
    }
  ],
  "total": 1,
  "truncated": false,
  "query_mode": "all",
  "ignored_stopwords": ["how", "do", "I", "the"],
  "word_forms": {"renew": ["renewal", "renewed", "renewing"], "certificates": ["certificate"]}
}
```

The response says how the query was read, so the caller is never left guessing why
something matched.

## Quick start

Requirements: Python 3.12 or newer with an SQLite that has FTS5 (the builds from
python.org, Homebrew, Debian and uv all do), and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/sebastian-stadelmann/obsidian-web-mcp-fts
cd obsidian-web-mcp-fts
uv sync
VAULT_PATH=~/Obsidian/MyVault VAULT_MCP_TOKEN=change-me uv run vault-mcp-fts
```

`vault-mcp-fts` takes the place of the stock `vault-mcp` command. It is the same server
with the same environment variables (OAuth, tunnel, allowed hosts ...; see the
[upstream README](https://github.com/jimprosser/obsidian-web-mcp#configuration)) and one
more tool. The log confirms the index with a line like this (the server's log handler
may wrap it to the terminal width):

```text
FTS index ready: 331 notes (331 indexed, 0 unchanged, 0 removed, 0 skipped), 12435 words stemmed for [english] in 0.42s [/home/you/.local/share/vault-mcp/fts.sqlite]
```

If a client was already connected, reconnect it once so the new tool shows up in its
tool list.

Already running your own extension? Pass both: `serve([FtsExtension(), MyExtension()])`.

## The tool

`vault_fts_search(query, path_prefix=None, max_results=20)`

- `query`: plain words, a question, or FTS5 syntax (see below).
- `path_prefix`: a string prefix on the vault-relative path. End it with `/` to mean a
  folder: `daily/` is the folder, `daily` also matches `daily-archive/`.
- `max_results`: capped at the server's search limit (50).

Each result carries `path`, `title`, `score`, `matched_fields`, a `snippet` with the
matches marked `«like this»`, plus `description` and `tags` when the note has them in its
frontmatter. Results come from the index; no note is opened at query time.

| Response field | Meaning |
| --- | --- |
| `query_mode` | `all`: every term is in every result. `relaxed`: no note had every term, so some were left out. `fts5`: the query used FTS5 syntax and ran as written. |
| `dropped_terms` | The terms left out in `relaxed` mode. |
| `ignored_stopwords` | Filler words that were skipped. |
| `word_forms` | Query word → the other forms it was expanded to. |
| `match: "word_forms"` | On a result that has some query word only in another form. Its score is reduced. |
| `truncated` | There were more results than `max_results`. |

### How a query is read

- **Ranked with BM25**, and a hit counts for more the more it says about the note: title
  10, aliases 8, description 5, path 4, tags 3, headings 3, body 1.
- **Whole notes, not lines.** `e2ee certificate` finds a note that has the two words in
  different paragraphs.
- **Case and accents are folded.** `muller` finds `Müller`, `cafe` finds `café`.
- **Word forms match** through stemming, in the active languages (next section).
- **Filler words are ignored**: `how`, `the`, `wie`, `warum` ...
- **No note has every term?** The largest set of terms that some note does have is
  searched instead. Among equally large sets the one with the rarest terms wins, since it
  is the most specific reading of the query.
- **Punctuation is text, not syntax.** `file-provider`, `C++`, `10:30` or a URL are
  searched as written instead of failing in the FTS5 parser.
- **FTS5 syntax works** and runs exactly as written, with no stemming and no widening:

  | Syntax | Meaning |
  | --- | --- |
  | `"wildcard certificate"` | exact phrase |
  | `"certificate"` | this exact word, no other forms |
  | `cert*` | prefix |
  | `traefik NOT kubernetes`, `podman OR docker` | boolean (operators in capitals) |
  | `NEAR(müller traefik, 3)` | within three words of each other |
  | `title:certificate` | one field only: `path`, `title`, `aliases`, `description`, `tags`, `headings`, `body` |

## Languages and stemming

English is the default. Further languages are switched on next to it, not instead of it,
because a vault is rarely monolingual:

```bash
VAULT_FTS_LANGUAGES=en,de
```

**English** (default)

| Query | Also finds | Note |
| --- | --- | --- |
| `certificate` | certificates | |
| `renew` | renewed, renewing, renewal | |
| `decision` | Decisions | |
| `how do I renew the certificates` | the how-to on renewing a certificate | `how`, `do`, `I`, `the` are ignored |

**German** (`en,de`)

| Query | Also finds | Note |
| --- | --- | --- |
| `zertifikat` | Zertifikate | |
| `entscheidung` | Entscheidungen | |
| `regel firewall` | "Regeln für die Firewall" | |
| `welche entscheidung zur infrastruktur` | "Entscheidungen zur Infrastruktur" | `welche`, `zur` are ignored |

**French** (`en,fr`)

| Query | Also finds | Note |
| --- | --- | --- |
| `certificat` | certificats | |
| `decision` | Décisions | accent folding plus stemming |
| `securite` | sécurité | accent folding alone |

**Italian** (`en,it`)

| Query | Also finds | Note |
| --- | --- | --- |
| `certificato rinnovato` | "i certificati vengono rinnovati" | |
| `citta` | città | accent folding alone |

A query word is stemmed by every active language, and the forms found by any of them
count. In a mixed vault that is what you want: with `en,fr`, `certificat` also finds the
English `certificates`. Occasionally it produces a false friend.

A language brings a [Snowball](https://snowballstem.org/) stemmer and a stopword list.
Stopword lists exist for English and German. The other Snowball languages (`es`, `nl`,
`pt`, `sv`, `da`, `fi`, `ru` ... about thirty) are stemmed without one, so their filler
words count as ordinary words; the relaxation step usually absorbs that. English and
German are covered by the test suite. French and Italian behave as shown above. The rest
use the same mechanism and are untested. A stopword list is a few lines in
[stopwords.py](src/vault_fts/stopwords.py), and contributions are welcome. `none`
switches stemming and stopwords off.

### How stemming works here

Stemming happens **at query time**, not in the index. Notes are indexed word for word. A
side table holds the stem of every word in the index vocabulary, and a query word is
expanded to the vocabulary words sharing its stem: `certificate` becomes
`"certificate" OR "certificates"`. Because the text itself is never stemmed,

- phrases, FTS5 syntax, snippets, highlights and field weights work unchanged, and the
  form that matched is what gets highlighted in the original text,
- a word in double quotes is matched exactly,
- changing the languages needs no reindex. Only the stem table is rebuilt, which took
  under a second for 12,000 distinct words and two languages.

BM25 sees each form as a term of its own, so a rare form would outweigh the common one.
In one real vault the German query `entscheidung` put a note on top that contained only
`entscheidend` and `Entscheiderin`. A result therefore loses up to half its score, in
proportion to how many query words it has only in another form. One word of three in
another form barely matters; a result with none of the words as typed drops below those
that have them.

What a stemmer cannot do: irregular forms (`ran` / `run`, `läuft` / `laufen`), German
compounds (`Zertifikatserneuerung` does not find `Zertifikat`; use `zertifikat*`), and it
sometimes conflates unrelated words (`Zustand` / `zuständig`).

## Measurements

One real vault: 328 markdown notes, 761 kB, German and English mixed, most notes with a
`description` and `aliases` in their frontmatter. `VAULT_FTS_LANGUAGES=en,de`.

The test is known-item search. For every note a query is built from words the note really
contains, and each strategy is asked to find that note. Hub pages and templates are not
used as targets, which leaves 304. Both tools run the way the server runs them:
`vault_search` is the server's own function with ripgrep, the index is this extension's
`FtsIndex`.

- **Found**: the note is somewhere in the response.
- **In top 3**: it is among the first three distinct notes of the response.
- **Notes returned**: distinct notes in the response, which the caller has to tell apart.
- **Response**: size of the JSON the tool returns, which is what lands in the model's
  context. Tokens are estimated at 4 bytes each.

### Two words from the body, on different lines

*Where did I write about X and Y?* Both words come from the body and appear nowhere in
the note's path, title, aliases or description, so the field weights cannot help. Each
word occurs in 3 to 40 notes. A single regex cannot find these by construction, so grep
gets one call per word, and the caller combines the two results. 263 queries.

| Strategy | Calls | Found | In top 3 | Notes returned | Response | Tokens (approx.) |
| --- | --- | --- | --- | --- | --- | --- |
| `vault_fts_search` | 1 | 100 % | 94 % | 3.5 | 2.0 kB | 490 |
| grep a, grep b, notes in both (max 20) | 2 | 60 % | 58 % | 1.5 | 14.4 kB | 3,600 |
| grep a, grep b, notes in either (max 20) | 2 | 93 % | 33 % | 17.7 | 14.4 kB | 3,600 |
| grep a, grep b, notes in both (max 50) | 2 | 90 % | 79 % | 2.7 | 23.2 kB | 5,800 |
| grep a, grep b, notes in either (max 50) | 2 | 99 % | 33 % | 26.6 | 23.2 kB | 5,800 |

Intersecting the two grep results is precise but loses notes, because each list is cut at
the limit before the caller ever sees it. Taking every note from either list finds nearly
everything and leaves 18 to 27 notes to go through, with the right one rarely on top.

### Two words from title, aliases or description

*Which note is about X?* This is grep's best case: the two words usually share a line (the
title or the description), so one regex is enough. 304 queries.

| Strategy | Calls | Found | In top 3 | Notes returned | Response | Tokens (approx.) |
| --- | --- | --- | --- | --- | --- | --- |
| `vault_fts_search` | 1 | 100 % | 94 % | 7.4 | 3.8 kB | 950 |
| grep `a.*b\|b.*a` | 1 | 92 % | 65 % | 5.3 | 4.2 kB | 1,050 |
| grep a, grep b, notes in both (max 50) | 2 | 69 % | 53 % | 4.1 | 27.5 kB | 6,900 |

One call each and a similar size. What differs is the order: grep returns file order, so
the note a query names in its title is among the first three in 65 % of the cases, not 94 %.

### A word form the note does not contain

151 of the queries above, with one word swapped for another form from the vault's own
vocabulary that the target note does not contain (`certificates` where the note says
`certificate`, `Entscheidungen` for `Entscheidung`).

| Setup | Calls | Found |
| --- | --- | --- |
| stemming on | 1 | 100 % |
| stemming off | 1 | 8 % |
| stemming off, caller retries with `prefix*` | 2 | 99 % |

With stemming the note comes back on the first call. Without it the first call misses the
note nine times out of ten, and a caller that cuts the ending off and retries with a
prefix gets it back at the price of a second call. That last row assumes the caller
notices the miss. The first call does not come back empty: it returns other notes that
contain the word as typed or, when no note has both words, a `relaxed` result for one of
them. Whether a model reads that as a reason to try another word form is not something
this test can answer. It is being watched in daily use.

### What this does not show

- One vault, and a curated one. Descriptions and aliases feed the field weights in the
  second test. The first test leaves them out on purpose.
- The queries consist of the note's own words, so a lexical match exists by construction.
  Nothing here is about paraphrases, which neither tool finds.
- The simulated caller follows fixed strategies. A model may do better (a `path_prefix`,
  a third call) or worse (stop at the first plausible hit).
- The tables are one run. ripgrep walks the vault in parallel and returns files in no
  fixed order, so a result that was cut at the limit differs from run to run: in a second
  run the grep rows moved by up to 6 points. The FTS rows do not move, and the queries are
  sampled with a fixed seed.

### Run it on your vault

```bash
VAULT_PATH=~/Obsidian/MyVault uv run python scripts/benchmark_vs_grep.py \
    --languages en,de --skip '*index.md' --skip 'templates/*'
```

Read-only: the index is built in memory and nothing is written into the vault or next to
it. `--skip` takes notes out of the search targets (hub pages, templates); they stay
searchable. The output is these tables as markdown, with every grep strategy in each. On
the vault above it takes about a minute, nearly all of it ripgrep calls.

## How the index stays current

- **At startup** the index is reconciled with the vault before the server accepts requests.
  New and changed notes (by mtime and size) are indexed, vanished ones removed. With a
  persisted index file a restart only stats the files.
- **Edits from outside the server** (Obsidian Sync, a sync tool writing into the vault
  folder) arrive through the server's filesystem watcher, a few seconds after the change.
- **Writes through the server's own tools** are indexed immediately, so a search right
  after `vault_write`, `vault_edit`, `vault_move` or `vault_delete` sees the result.

Only `*.md` files are indexed. `.obsidian`, `.trash`, `.git` and every dot-directory are
skipped.

## Configuration

All of the server's own variables apply (`VAULT_PATH`, `VAULT_MCP_TOKEN`, OAuth ...), plus:

| Variable | Default | Description |
| --- | --- | --- |
| `VAULT_FTS_LANGUAGES` | `en` | Languages for stemming and stopwords: ISO codes or Snowball names, comma-separated (`en,de`). `none` disables both. An unknown language stops the start with a list of valid codes. |
| `VAULT_FTS_DB_PATH` | `~/.local/share/vault-mcp/fts.sqlite` | Index file, or `:memory:` to rebuild on every start. Must be outside the vault. |
| `VAULT_FTS_MAX_FILE_BYTES` | `2000000` | Notes larger than this are not indexed. |

## Security

- **An extension is trusted code.** It runs inside the server process with full access to
  the vault and the server's secrets; see the trust model in the
  [upstream README](https://github.com/jimprosser/obsidian-web-mcp#extending-the-server).
  Read it before you run it; it is about 1,100 lines.
- **The index file is vault plaintext.** It is created with mode `0600`, and the server
  **refuses to start** if the path resolves inside the vault, where a sync tool would
  replicate it to every device.
- **Same read guard as the server.** Every path goes through the server's own
  `resolve_vault_read_path`, when it is indexed and again when it is returned. Files
  outside the vault, dotfiles and hardlinked files are never indexed and never returned.
- **Search failure is contained.** Any other index problem (unwritable path, an SQLite
  without FTS5) only disables this tool. The rest of the vault server keeps running.
- The tool is read-only and adds no HTTP routes.

## Container

This is the image the author runs, built and started with rootless Podman. `uv sync`
fetches the pinned server from GitHub, so the image needs `git`. `--frozen` installs
exactly the locked versions; a separate pin on `mcp`, as some older setups of the stock
server carry, is not needed.

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY obsidian-web-mcp-fts/ /app/
RUN uv sync --frozen
CMD ["uv", "run", "vault-mcp-fts"]
```

Keep the index on a volume that is not the vault mount:

```yaml
    environment:
      VAULT_PATH: /data/vault
      VAULT_FTS_DB_PATH: /data/fts/fts.sqlite
      VAULT_FTS_LANGUAGES: "en,de"
    volumes:
      - ./vault:/data/vault:Z
      - ./fts:/data/fts:Z
```

## Development

```
src/vault_fts/
    extension.py    FtsExtension: seam hooks, the tool, both change feeds
    index.py        FtsIndex: schema, reconcile, incremental updates, query handling
    languages.py    language packs: Snowball stemmer + stopwords
    stopwords.py    filler words per language
    config.py       environment variables
    main.py         entry point: serve([FtsExtension()])
scripts/
    benchmark_vs_grep.py    the comparison under Measurements, for any vault
tests/
```

```bash
uv run --extra dev pytest
uv run --extra dev ruff check src tests
```

CI runs both on Python 3.12, 3.13 and 3.14 for every pull request and every push to `main`.

`tests/test_live_server.py` starts the real server process and calls the tools over MCP
streamable HTTP with a bearer token, including an edit made behind the server's back. It
takes a few seconds because it waits for the watcher's debounce.

To work against a local checkout of the server instead of the pinned commit:

```bash
uv add --editable ../obsidian-web-mcp
```

The server dependency is pinned to a commit in `pyproject.toml`, because upstream has no
releases on PyPI and its extension seam (`Extension`, `add_change_listener`,
`register_write_listener`) is young. Bump the pin deliberately and run the tests.

Issues and pull requests are welcome, especially stopword lists and reports on languages
other than English and German.

## Status and limits

New. Covered by its test suite, including an end-to-end test against a real server
process, and tried against one real vault of 330 notes, where a full build takes under a
second and a query a few milliseconds. The [measurements](#measurements) come from that
vault. It has not run in production for long.

- Only markdown is indexed; `.canvas` and attachments are not.
- `vault_fts_search` does not appear in the server's audit log. The audit wrapper is
  private to the server.
- Hub pages that list many notes with their descriptions match many queries. They rank
  low, but they show up.
- No semantic search. This finds words and their forms, not paraphrases.

## License

MIT. See [LICENSE](LICENSE).

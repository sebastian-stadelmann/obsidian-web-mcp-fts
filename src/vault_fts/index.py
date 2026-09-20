"""SQLite FTS5 index over vault markdown notes.

One row per note. The searchable text is split into columns so a hit in the title,
aliases or description can outrank a hit somewhere in the body (bm25 column weights).
The index is derived data: it can be deleted at any time and is rebuilt from disk.

Stemming is done at query time, not in the index. The notes are indexed word for word;
a side table maps every word of the index vocabulary to its stem per language, and a
query word is expanded to the vocabulary words sharing its stem ("zertifikat" ->
"zertifikat" OR "zertifikate"). Because the text itself is never stemmed, exact
phrases, FTS5 syntax, snippets, highlights and the column weights keep working as they
are, several languages can be active at once, and changing the languages rebuilds only
the side table. The price is BM25 seeing each form as its own term, so a rare form
counts a little more than a common one.

Every path that reaches the index goes through ``resolve_vault_read_path``, the same
guard the server's read tools use, so dotfiles, paths escaping the vault and
hardlinked files are never indexed and never returned.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import frontmatter
from obsidian_vault_mcp import config as vault_config
from obsidian_vault_mcp.vault import resolve_vault_read_path

from .languages import Language

logger = logging.getLogger(__name__)

# Bump when the table layout, the column order or the tokenizer changes. A database
# with a different version is dropped and rebuilt on open.
SCHEMA_VERSION = 2

# Column order is part of the schema: bm25() weights and the column numbers passed to
# snippet()/highlight() are positional.
COLUMNS = ("path", "title", "aliases", "description", "tags", "headings", "body")
WEIGHTS = {
    "path": 4.0,
    "title": 10.0,
    "aliases": 8.0,
    "description": 5.0,
    "tags": 3.0,
    "headings": 3.0,
    "body": 1.0,
}
_BODY_COL = COLUMNS.index("body")
_BM25_ARGS = ", ".join(str(WEIGHTS[name]) for name in COLUMNS)

# Folds case and diacritics: "muller" finds "Müller", "anderung" finds "Änderung".
TOKENIZER = "unicode61 remove_diacritics 2"

SNIPPET_TOKENS = 24
# Upper bound on the word forms one query word is expanded to (closest in length first).
MAX_WORD_FORMS = 32
# A note that has query words only in another form loses up to this share of its score.
FORM_ONLY_PENALTY = 0.5
# How many hits beyond max_results are ranked before the penalty reorders and cuts them.
RERANK_WINDOW = 30
# Control characters as internal match markers: they cannot occur in note text the way
# any printable marker could, so "did this column match" is an exact test.
_MARK_OPEN, _MARK_CLOSE = "\x02", "\x03"
HIGHLIGHT_OPEN, HIGHLIGHT_CLOSE = "«", "»"

_FENCED_CODE_RE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_WORD_RE = re.compile(r"\w+")
# Anything that only means something in FTS5 query syntax. A query without these is
# treated as plain words and never touches the FTS5 parser unquoted.
_ADVANCED_RE = re.compile(r'["*()^:]|\b(?:AND|OR|NOT|NEAR)\b')


@dataclass(slots=True)
class ParsedNote:
    title: str
    aliases: str
    description: str
    tags: str
    headings: str
    body: str


def _as_text_list(value) -> list[str]:
    """Frontmatter values come as str, list, scalar or None; normalise to strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def parse_note(rel_path: str, text: str) -> ParsedNote:
    """Split a note into the indexed columns. Never raises on malformed frontmatter."""
    try:
        post = frontmatter.loads(text)
        metadata, body = dict(post.metadata), post.content
    except Exception:  # noqa: BLE001 - YAML parsers raise many unrelated types
        # Broken YAML must not hide the note from search: index it as plain text.
        metadata, body = {}, text

    title = " ".join(_as_text_list(metadata.get("title"))) or PurePosixPath(rel_path).stem
    headings = _HEADING_RE.findall(_FENCED_CODE_RE.sub("", body))
    return ParsedNote(
        title=title,
        aliases="\n".join(_as_text_list(metadata.get("aliases"))),
        description=" ".join(_as_text_list(metadata.get("description"))),
        tags="\n".join(_as_text_list(metadata.get("tags"))),
        headings="\n".join(headings),
        body=body,
    )


def normalize_rel_path(rel_path: str) -> str:
    """Canonical index key: posix separators, no './' and no duplicate slashes."""
    return PurePosixPath(rel_path.replace("\\", "/")).as_posix()


def _normalize_prefix(path_prefix: str | None) -> str | None:
    """Plain string prefix on the vault-relative path. A trailing slash is kept, since
    it is what tells the folder "daily/" from a name that merely starts with "daily"."""
    raw = (path_prefix or "").strip().replace("\\", "/").lstrip("/")
    if raw in ("", ".", "./"):
        return None
    normalized = normalize_rel_path(raw)
    return normalized + "/" if raw.endswith("/") else normalized


def _is_excluded(rel_path: str) -> bool:
    return any(part in vault_config.EXCLUDED_DIRS for part in PurePosixPath(rel_path).parts)


def iter_vault_notes(start: Path | None = None) -> Iterator[str]:
    """Yield vault-relative posix paths of every indexable .md under *start*."""
    root = vault_config.VAULT_PATH
    for dirpath, dirnames, filenames in os.walk(start or root):
        # Prune in place so os.walk never descends into .obsidian, .trash, .git ...
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in vault_config.EXCLUDED_DIRS
        )
        for name in sorted(filenames):
            if name.endswith(".md") and not name.startswith("."):
                yield Path(dirpath, name).relative_to(root).as_posix()


def db_path_inside_vault(db_path: str | Path) -> bool:
    """True when the index file would live inside the vault (and get synced with it)."""
    if str(db_path) == ":memory:":
        return False
    resolved = Path(db_path).expanduser().resolve()
    vault_root = vault_config.VAULT_PATH.resolve()
    return resolved == vault_root or vault_root in resolved.parents


@dataclass(frozen=True, slots=True)
class Term:
    """One search term of a plain-word query."""

    text: str  # as the caller typed it, for display: 'file provider', 'änder*'
    match: str  # the FTS5 expression searched: '"file provider"', '"änder" *'
    word: str | None  # the bare word if this is a single word without *, else None


def literal_terms(query: str) -> list[Term]:
    """Turn free text into safely quoted FTS5 terms.

    Each whitespace-separated chunk becomes one quoted phrase of its word characters,
    so "file-provider" searches the phrase "file provider" (which is how the tokenizer
    indexed it) instead of failing in the FTS5 parser. A trailing * is kept as a
    prefix operator.
    """
    terms: list[Term] = []
    for chunk in query.split():
        words = _WORD_RE.findall(chunk)
        if not words:
            continue
        prefix = chunk.endswith("*")
        match = '"' + " ".join(words) + '"' + (" *" if prefix else "")
        if all(term.match != match for term in terms):
            text = " ".join(words) + ("*" if prefix else "")
            terms.append(Term(text, match, words[0] if len(words) == 1 and not prefix else None))
    return terms


def fold(word: str) -> str:
    """Lower-case and strip diacritics the way the index tokenizer does ("Müller" ->
    "muller"), so a query word can be looked up in the index vocabulary. An exotic
    character folded differently only means that word is not expanded."""
    decomposed = unicodedata.normalize("NFKD", word.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _stemmable(word: str) -> bool:
    """Plain words only: "e2ee", "k8s" or "v3" have no word forms."""
    return len(word) > 2 and word.isalpha()


class FtsIndex:
    """Thread-safe FTS5 index. One shared connection, serialised by a lock.

    Writers are the watcher's debounce thread and request threads (write events);
    readers are request threads. A note is read and written under the same lock, so
    two updates of one file cannot interleave and leave the older content behind.
    """

    def __init__(
        self,
        db_path: str | Path,
        max_file_bytes: int = 2_000_000,
        languages: Sequence[Language] = (),
    ) -> None:
        self._db_path: str | Path = db_path if str(db_path) == ":memory:" else Path(db_path).expanduser()
        self._max_file_bytes = max_file_bytes
        self._languages = tuple(languages)
        self._stopwords = frozenset().union(*(lang.stopwords for lang in self._languages))
        # Set by every index write; the stem table is brought up to date lazily, before
        # the next search, so a burst of writes costs one vocabulary scan, not many.
        self._stems_dirty = True
        self._lock = threading.RLock()
        self._con: sqlite3.Connection | None = None

    # -- lifecycle --

    @property
    def is_open(self) -> bool:
        return self._con is not None

    def open(self) -> None:
        """Open (or create) the database. A corrupt or outdated file is rebuilt."""
        with self._lock:
            if self._con is not None:
                return
            try:
                self._con = self._connect()
                self._init_schema()
            except sqlite3.DatabaseError as exc:
                self._discard_connection()
                if "no such module" in str(exc):
                    raise RuntimeError("This Python's SQLite was built without FTS5") from exc
                if str(self._db_path) == ":memory:":
                    raise
                # Derived data: a damaged index file is thrown away, not repaired.
                logger.warning("FTS index at %s is unusable (%s); rebuilding it", self._db_path, exc)
                self._delete_db_files()
                try:
                    self._con = self._connect()
                    self._init_schema()
                except sqlite3.DatabaseError:
                    self._discard_connection()
                    raise

    def close(self) -> None:
        with self._lock:
            self._discard_connection()

    def _discard_connection(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
            self._con = None

    def _delete_db_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(str(self._db_path) + suffix)
            except FileNotFoundError:
                pass

    def _connect(self) -> sqlite3.Connection:
        if str(self._db_path) != ":memory:":
            path = Path(self._db_path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # The index holds vault plaintext. Create it owner-only before SQLite
            # does; the -wal/-shm files inherit the main file's permissions.
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
            os.chmod(path, 0o600)
        # isolation_level=None: explicit BEGIN/COMMIT, no implicit transactions.
        con = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        if str(self._db_path) != ":memory:":
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _init_schema(self) -> None:
        con = self._con
        version = con.execute("PRAGMA user_version").fetchone()[0]
        has_tables = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name IN ('docs', 'notes', 'word_stems')"
        ).fetchone()[0]
        if has_tables and version != SCHEMA_VERSION:
            logger.info("FTS schema version %s != %s; rebuilding index", version, SCHEMA_VERSION)
            for table in ("notes_vocab", "notes", "docs", "word_stems"):
                con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute(
            "CREATE TABLE IF NOT EXISTS docs ("
            " id INTEGER PRIMARY KEY,"
            " path TEXT NOT NULL UNIQUE,"
            " mtime_ns INTEGER NOT NULL,"
            " size INTEGER NOT NULL)"
        )
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5({', '.join(COLUMNS)}, tokenize=\"{TOKENIZER}\")"
        )
        # The index vocabulary (already lower-cased and diacritics-folded) ...
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes_vocab USING fts5vocab(notes, 'row')")
        # ... and the stem of each of its words, per language.
        con.execute(
            "CREATE TABLE IF NOT EXISTS word_stems ("
            " word TEXT NOT NULL, lang TEXT NOT NULL, stem TEXT NOT NULL,"
            " PRIMARY KEY (word, lang)) WITHOUT ROWID"
        )
        con.execute("CREATE INDEX IF NOT EXISTS word_stems_by_stem ON word_stems (lang, stem)")
        # A language that was switched off must stop contributing word forms.
        active = [lang.name for lang in self._languages]
        con.execute(
            f"DELETE FROM word_stems WHERE lang NOT IN ({', '.join('?' * len(active))})", active
        )
        self._stems_dirty = True
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _require_open(self) -> sqlite3.Connection:
        if self._con is None:
            raise RuntimeError("FTS index is not open")
        return self._con

    @contextmanager
    def _transaction(self):
        """One atomic unit. A note is two tables (docs + notes); a crash between them
        would leave a row that looks indexed but cannot be found. Re-entrant: inside
        an open transaction this only yields."""
        con = self._require_open()
        if con.in_transaction:
            yield con
            return
        con.execute("BEGIN")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")

    @property
    def doc_count(self) -> int:
        with self._lock:
            return self._require_open().execute("SELECT count(*) FROM docs").fetchone()[0]

    # -- writing --

    def reconcile(self) -> dict:
        """Bring the index in line with the vault on disk.

        Indexes new and changed notes (by mtime and size), drops rows whose file is
        gone or no longer indexable. On an empty database this is the full build; on a
        persisted one it only stats the files. Also the healing path after a missed
        watcher event.
        """
        started = time.monotonic()
        stats = {"indexed": 0, "unchanged": 0, "skipped": 0, "removed": 0}
        with self._lock, self._transaction() as con:
            seen: set[str] = set()
            for rel_path in iter_vault_notes():
                outcome = self._update_path_locked(rel_path, force=False)
                stats[outcome] += 1
                if outcome in ("indexed", "unchanged"):
                    seen.add(rel_path)
            stale = [row[0] for row in con.execute("SELECT path FROM docs").fetchall() if row[0] not in seen]
            for rel_path in stale:
                self._delete_locked(rel_path)
            # Rows dropped inside the loop (file vanished mid-walk) are counted too.
            stats["removed"] += len(stale)
            stats["total"] = con.execute("SELECT count(*) FROM docs").fetchone()[0]
            # Words that left the vocabulary would only ever expand to nothing; drop
            # them here so the table does not grow forever.
            con.execute("DELETE FROM word_stems WHERE word NOT IN (SELECT term FROM notes_vocab)")
            self._refresh_stems_locked()
            stats["stemmed_words"] = con.execute("SELECT count(DISTINCT word) FROM word_stems").fetchone()[0]
        stats["seconds"] = round(time.monotonic() - started, 3)
        return stats

    def update_path(self, rel_path: str, *, force: bool = False) -> str:
        """(Re)index one note. Returns indexed | unchanged | skipped | removed.

        force=True skips the mtime/size comparison; used when the caller knows the
        file was just written.
        """
        with self._lock, self._transaction():
            return self._update_path_locked(normalize_rel_path(rel_path), force=force)

    def update_tree(self, rel_path: str, *, force: bool = False) -> int:
        """Index a note, or every note below a directory. Returns how many were indexed."""
        rel_path = normalize_rel_path(rel_path)
        target = vault_config.VAULT_PATH / rel_path
        with self._lock, self._transaction():
            if not target.is_dir():
                return int(self._update_path_locked(rel_path, force=force) == "indexed")
            if _is_excluded(rel_path) or any(p.startswith(".") for p in PurePosixPath(rel_path).parts):
                return 0
            return sum(
                self._update_path_locked(child, force=force) == "indexed" for child in iter_vault_notes(target)
            )

    def remove_path(self, rel_path: str) -> int:
        """Drop a note, or everything below a directory path. Returns rows removed."""
        rel_path = normalize_rel_path(rel_path)
        prefix = rel_path.rstrip("/") + "/"
        with self._lock, self._transaction() as con:
            doomed = [
                row[0]
                for row in con.execute(
                    "SELECT path FROM docs WHERE path = ? OR substr(path, 1, ?) = ?",
                    (rel_path, len(prefix), prefix),
                ).fetchall()
            ]
            for path in doomed:
                self._delete_locked(path)
            return len(doomed)

    def _delete_locked(self, rel_path: str) -> None:
        con = self._con
        row = con.execute("SELECT id FROM docs WHERE path = ?", (rel_path,)).fetchone()
        if row is None:
            return
        self._stems_dirty = True
        con.execute("DELETE FROM notes WHERE rowid = ?", (row[0],))
        con.execute("DELETE FROM docs WHERE id = ?", (row[0],))

    def _update_path_locked(self, rel_path: str, *, force: bool) -> str:
        con = self._con
        if not rel_path.endswith(".md") or _is_excluded(rel_path):
            return "skipped"
        try:
            safe_path = resolve_vault_read_path(rel_path)
            stat = safe_path.stat()
        except FileNotFoundError:
            self._delete_locked(rel_path)
            return "removed"
        except (ValueError, OSError):
            # Refused by the read guard (dotfile, escape, hardlink). If it was indexed
            # before it became unreadable, it must leave the index now.
            self._delete_locked(rel_path)
            return "skipped"

        if stat.st_size > self._max_file_bytes:
            logger.warning("FTS: not indexing %s (%d bytes > limit)", rel_path, stat.st_size)
            self._delete_locked(rel_path)
            return "skipped"

        row = con.execute("SELECT id, mtime_ns, size FROM docs WHERE path = ?", (rel_path,)).fetchone()
        if row and not force and (row[1], row[2]) == (stat.st_mtime_ns, stat.st_size):
            return "unchanged"

        try:
            text = safe_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self._delete_locked(rel_path)
            return "skipped"
        note = parse_note(rel_path, text)
        self._stems_dirty = True
        values = (rel_path, note.title, note.aliases, note.description, note.tags, note.headings, note.body)

        if row:
            doc_id = row[0]
            con.execute("UPDATE docs SET mtime_ns = ?, size = ? WHERE id = ?", (stat.st_mtime_ns, stat.st_size, doc_id))
            con.execute("DELETE FROM notes WHERE rowid = ?", (doc_id,))
        else:
            doc_id = con.execute(
                "INSERT INTO docs (path, mtime_ns, size) VALUES (?, ?, ?)",
                (rel_path, stat.st_mtime_ns, stat.st_size),
            ).lastrowid
        con.execute(
            f"INSERT INTO notes (rowid, {', '.join(COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, *values),
        )
        return "indexed"

    # -- stemming --

    def _refresh_stems_locked(self) -> None:
        """Stem the vocabulary words that have no stem yet, for every active language."""
        if not self._stems_dirty:
            return
        with self._transaction() as con:
            for lang in self._languages:
                new_words = [
                    row[0]
                    for row in con.execute(
                        "SELECT term FROM notes_vocab"
                        " WHERE term NOT IN (SELECT word FROM word_stems WHERE lang = ?)",
                        (lang.name,),
                    ).fetchall()
                ]
                con.executemany(
                    "INSERT OR REPLACE INTO word_stems (word, lang, stem) VALUES (?, ?, ?)",
                    ((word, lang.name, lang.stem(word) if _stemmable(word) else word) for word in new_words),
                )
        self._stems_dirty = False

    def _word_forms(self, word: str) -> list[str]:
        """Other words of the index vocabulary that share a stem with *word*, in any
        active language. Closest in length first, since those are the likeliest
        inflections and the rest is cut off at MAX_WORD_FORMS."""
        folded = fold(word)
        if not _stemmable(folded):
            return []
        forms: set[str] = set()
        for lang in self._languages:
            rows = self._con.execute(
                "SELECT word FROM word_stems WHERE lang = ? AND stem = ?", (lang.name, lang.stem(folded))
            )
            forms.update(row[0] for row in rows)
        forms.discard(folded)
        return sorted(forms, key=lambda form: (abs(len(form) - len(folded)), form))[:MAX_WORD_FORMS]

    def _expand(self, term: Term, word_forms: dict[str, list[str]]) -> Term:
        """A single word also matches its other word forms; phrases and prefixes stay exact."""
        if term.word is None or not self._languages:
            return term
        forms = self._word_forms(term.word)
        if not forms:
            return term
        word_forms[term.text] = forms
        return replace(term, match="(" + " OR ".join(f'"{form}"' for form in (fold(term.word), *forms)) + ")")

    # -- reading --

    def search(self, query: str, path_prefix: str | None = None, max_results: int = 20) -> dict:
        """Ranked search. Returns {"results", "total", "truncated", "query_mode"}.

        query_mode tells the caller how the query was read:
          fts5     the query used FTS5 syntax and ran as written
          all      plain words, every term occurs in each returned note
          relaxed  plain words, no note had every term; "dropped_terms" were left out
        Plain-word queries ignore the filler words of the active languages
        ("ignored_stopwords") and also match other forms of each word ("word_forms" maps
        a query word to the forms it was expanded to). A hit that has some query word
        only in another form carries "match": "word_forms" and a reduced score. An FTS5
        query gets neither.
        """
        query = query.strip()
        prefix = _normalize_prefix(path_prefix)

        with self._lock:
            self._require_open()

            if _ADVANCED_RE.search(query):
                try:
                    results, truncated = self._run(query, prefix, max_results)
                    return _payload(results, truncated, "fts5")
                except sqlite3.OperationalError:
                    # "10:30", "C++", a URL, an unbalanced quote: not meant as syntax.
                    pass

            terms = literal_terms(query)
            if not terms:
                return {"error": "Query contains no searchable terms", "results": [], "total": 0, "truncated": False}
            # Filler words would act as hard filters (see stopwords.py). A query made
            # of nothing else ("was ist das") is searched as it stands.
            content_terms = [term for term in terms if not self._is_stopword(term)]
            ignored = [term.text for term in terms if self._is_stopword(term)] if content_terms else []
            terms = content_terms or terms

            self._refresh_stems_locked()
            word_forms: dict[str, list[str]] = {}
            expanded = [self._expand(term, word_forms) for term in terms]

            payload = self._ranked(terms, expanded, prefix, max_results, "all")
            if payload["total"] == 0 and len(terms) > 1:
                payload = self._relax(terms, expanded, prefix, max_results) or payload
            if ignored:
                payload["ignored_stopwords"] = ignored
            used_forms = {text: forms for text, forms in word_forms.items() if text not in payload.get("dropped_terms", ())}
            if used_forms:
                payload["word_forms"] = used_forms
            return payload

    def _ranked(
        self, terms: list[Term], expanded: list[Term], prefix: str | None, max_results: int, mode: str
    ) -> dict:
        """Search all word forms, but let the words as typed count for more.

        BM25 treats every form as a term of its own, so a rare form outweighs the common
        one: for "entscheidung" a note containing only "entscheidend" and "Entscheiderin"
        came out on top of every note that had the word itself. A note therefore loses
        up to FORM_ONLY_PENALTY of its score, in proportion to how many of the query
        words it has only in another form. Strict tiers (all exact matches first) were
        tried too and rejected: for "livesync bridge permission" they put a hub page
        that happens to say "permission" above the howto that says "permissions" and is
        about all three words.
        """
        stemmed = [i for i, (term, wide) in enumerate(zip(terms, expanded, strict=True)) if term.match != wide.match]
        if not stemmed:
            results, truncated = self._run(_all_of(terms), prefix, max_results)
            return _payload(results, truncated, mode)

        # The penalty reorders, so rank a window larger than what is returned.
        window, _ = self._run(_all_of(expanded), prefix, max_results + RERANK_WINDOW)
        typed = {i: set(self._paths(terms[i].match, prefix)) for i in stemmed}
        for hit in window:
            form_only = sum(hit["path"] not in typed[i] for i in stemmed)
            if form_only:
                hit["score"] = round(hit["score"] * (1 - FORM_ONLY_PENALTY * form_only / len(terms)), 3)
                hit["match"] = "word_forms"
        window.sort(key=lambda hit: -hit["score"])
        return _payload(window[:max_results], len(window) > max_results, mode)

    def _is_stopword(self, term: Term) -> bool:
        """A single filler word of an active language. Phrases and prefixes never are."""
        return term.word is not None and term.word.lower() in self._stopwords

    def _relax(
        self, terms: list[Term], expanded: list[Term], prefix: str | None, max_results: int
    ) -> dict | None:
        """No note has every term: search the largest set of terms that some note has.

        One lookup per term gives the notes containing it (in any word form); counting
        terms per note finds the best coverage exactly, without guessing which term to
        give up. When several term sets tie, the one with the rarest terms wins (it is
        the most specific reading of the query), then the one earliest in the query.
        """
        notes_with = [self._rowids(term.match, prefix) for term in expanded]
        terms_in: dict[int, set[int]] = {}
        for position, rowids in enumerate(notes_with):
            for rowid in rowids:
                terms_in.setdefault(rowid, set()).add(position)
        if not terms_in:
            return None

        best = max(len(found) for found in terms_in.values())
        candidates = {frozenset(found) for found in terms_in.values() if len(found) == best}
        chosen = min(
            candidates,
            key=lambda subset: (sum(len(notes_with[position]) for position in subset), sorted(subset)),
        )
        payload = self._ranked(
            [terms[position] for position in sorted(chosen)],
            [expanded[position] for position in sorted(chosen)],
            prefix,
            max_results,
            "relaxed",
        )
        payload["dropped_terms"] = [term.text for position, term in enumerate(terms) if position not in chosen]
        return payload

    def _paths(self, match: str, prefix: str | None) -> list[str]:
        sql = "SELECT docs.path FROM notes JOIN docs ON docs.id = notes.rowid WHERE notes MATCH ?"
        params: list = [match]
        if prefix:
            sql += " AND substr(docs.path, 1, ?) = ?"
            params += [len(prefix), prefix]
        return [row[0] for row in self._con.execute(sql, params)]

    def _rowids(self, match: str, prefix: str | None) -> list[int]:
        sql = "SELECT notes.rowid FROM notes JOIN docs ON docs.id = notes.rowid WHERE notes MATCH ?"
        params: list = [match]
        if prefix:
            sql += " AND substr(docs.path, 1, ?) = ?"
            params += [len(prefix), prefix]
        return [row[0] for row in self._con.execute(sql, params)]

    def _run(self, match: str, prefix: str | None, max_results: int) -> tuple[list[dict], bool]:
        """Run one FTS5 query. Returns the ranked hits and whether there were more."""
        con = self._con
        field_marks = ", ".join(
            f"highlight(notes, {i}, char(2), char(3))" for i in range(len(COLUMNS)) if i != _BODY_COL
        )
        sql = (
            "SELECT docs.path, notes.title, notes.description, notes.tags,"
            f" bm25(notes, {_BM25_ARGS}) AS rank,"
            f" snippet(notes, {_BODY_COL}, char(2), char(3), '…', {SNIPPET_TOKENS}),"
            f" snippet(notes, -1, char(2), char(3), '…', {SNIPPET_TOKENS}),"
            f" {field_marks}"
            " FROM notes JOIN docs ON docs.id = notes.rowid"
            " WHERE notes MATCH ?"
        )
        params: list = [match]
        if prefix:
            sql += " AND substr(docs.path, 1, ?) = ?"
            params += [len(prefix), prefix]
        sql += " ORDER BY rank LIMIT ?"
        params.append(max_results + 1)  # one extra row answers "is there more?"

        rows = con.execute(sql, params).fetchall()
        truncated = len(rows) > max_results
        small_columns = [name for i, name in enumerate(COLUMNS) if i != _BODY_COL]

        results = []
        for row in rows[:max_results]:
            path, title, description, tags, rank, body_snippet, any_snippet, *marked = row
            try:
                # Same guard as at index time: a file that vanished or became
                # hardlinked since then must not be returned.
                resolve_vault_read_path(path)
            except (ValueError, OSError):
                with self._transaction():
                    self._delete_locked(path)
                continue

            matched = [name for name, text in zip(small_columns, marked) if _MARK_OPEN in (text or "")]
            body_hit = _MARK_OPEN in (body_snippet or "")
            if body_hit:
                matched.append("body")
            entry = {
                "path": path,
                "title": title,
                "score": round(-rank, 3),  # bm25() is "more negative is better"
                "matched_fields": matched,
                "snippet": _clean_snippet(body_snippet if body_hit else any_snippet),
            }
            if description:
                entry["description"] = description
            if tags:
                entry["tags"] = tags.split("\n")
            results.append(entry)

        return results, truncated


def _payload(results: list[dict], truncated: bool, mode: str) -> dict:
    return {"results": results, "total": len(results), "truncated": truncated, "query_mode": mode}


def _all_of(terms: list[Term]) -> str:
    """Every term must match. Explicit AND: FTS5 rejects an implicit one between groups."""
    return " AND ".join(term.match for term in terms)


def _clean_snippet(text: str | None) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed.replace(_MARK_OPEN, HIGHLIGHT_OPEN).replace(_MARK_CLOSE, HIGHLIGHT_CLOSE)

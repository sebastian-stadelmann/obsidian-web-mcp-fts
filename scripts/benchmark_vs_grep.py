"""Known-item retrieval on your own vault: the server's vault_search (grep) against
vault_fts_search.

For every note a query is built from words the note really contains, then each strategy
is asked to find that note. Reported per strategy: how often the note came back, how
often among the first three notes, how many notes the caller has to look at, and the
size of the response, which is what lands in the model's context.

Three tests:
  topic     two keywords from title, aliases and description ("which note is about X")
  body      two body words that never share a line ("where did I write about X and Y")
  stemming  a topic query with one word swapped for a form the note does not contain

Read-only. The index is built in memory, nothing is written into the vault or next to
it. Both tools run as the server runs them: vault_search is the server's own function
(ripgrep if installed), the index is the extension's FtsIndex.

    VAULT_PATH=~/Obsidian/MyVault uv run python scripts/benchmark_vs_grep.py \
        --languages en,de --skip '*index.md' --skip 'templates/*'

The queries are sampled with a fixed seed. ripgrep returns files in no fixed order, so a
result that was cut at the limit differs from run to run and the grep numbers move by a
few points. The FTS numbers do not.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import random
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass

from obsidian_vault_mcp import config as vault_config
from obsidian_vault_mcp.serialization import dumps
from obsidian_vault_mcp.tools.search import vault_search

from vault_fts.index import FtsIndex, ParsedNote, iter_vault_notes, parse_note
from vault_fts.languages import resolve_languages

# Alphabetic, five letters or more: the kind of word a caller picks as a keyword.
_KEYWORD_RE = re.compile(r"[^\W\d_]{5,}")
# Body words must be neither unique (the query would be trivial) nor everywhere.
_MIN_NOTES, _MAX_NOTES = 3, 40
_BYTES_PER_TOKEN = 4


@dataclass
class Tally:
    calls: int
    found: int = 0
    top3: int = 0
    notes: int = 0
    size: int = 0

    def add(self, target: str, paths: list[str], size: int) -> None:
        distinct = list(dict.fromkeys(paths))
        self.found += target in distinct
        self.top3 += target in distinct[:3]
        self.notes += len(distinct)
        self.size += size


def grep(query: str, max_results: int) -> tuple[list[str], int]:
    response = vault_search(query, max_results=max_results)
    return [hit["path"] for hit in json.loads(response).get("results", [])], len(response.encode())


def fts(index: FtsIndex, query: str, max_results: int = 20) -> tuple[list[str], int]:
    payload = index.search(query, None, max_results)
    return [hit["path"] for hit in payload.get("results", [])], len(dumps(payload).encode())


def keywords(text: str, stopwords: frozenset[str]) -> list[str]:
    return [word for word in dict.fromkeys(w.lower() for w in _KEYWORD_RE.findall(text)) if word not in stopwords]


def topic_query(note: ParsedNote, stopwords: frozenset[str]) -> tuple[str, str] | None:
    words = keywords(f"{note.title} {note.aliases} {note.description}", stopwords)
    return (words[0], words[1]) if len(words) >= 2 else None


def body_query(
    rel_path: str, note: ParsedNote, stopwords: frozenset[str], note_count: Counter, rng: random.Random
) -> tuple[str, str] | None:
    """Two body words on different lines. Words from the path, title, aliases and
    description are left out, so the field weights cannot help."""
    head = set(keywords(f"{rel_path} {note.title} {note.aliases} {note.description}", frozenset()))
    lines = [set(keywords(line, stopwords)) for line in note.body.splitlines()]
    words = sorted(
        {word for line in lines for word in line if word not in head and _MIN_NOTES <= note_count[word] <= _MAX_NOTES}
    )
    rng.shuffle(words)
    words = words[:15]
    for i, first in enumerate(words):
        for second in words[i + 1 :]:
            if not any(first in line and second in line for line in lines):
                return first, second
    return None


def compare(title: str, queries: dict[str, tuple[str, str]], index: FtsIndex, single_regex: bool) -> None:
    tallies = {"vault_fts_search": Tally(1)}
    if single_regex:
        tallies["grep `a.*b|b.*a`"] = Tally(1)
    for limit in (20, vault_config.MAX_SEARCH_RESULTS):
        tallies[f"grep a, grep b, notes in both (max {limit})"] = Tally(2)
        tallies[f"grep a, grep b, notes in either (max {limit})"] = Tally(2)

    for target, (a, b) in queries.items():
        tallies["vault_fts_search"].add(target, *fts(index, f"{a} {b}"))
        if single_regex:
            tallies["grep `a.*b|b.*a`"].add(target, *grep(f"{a}.*{b}|{b}.*{a}", 20))
        for limit in (20, vault_config.MAX_SEARCH_RESULTS):
            paths_a, size_a = grep(a, limit)
            paths_b, size_b = grep(b, limit)
            in_b = set(paths_b)
            tallies[f"grep a, grep b, notes in both (max {limit})"].add(
                target, [path for path in paths_a if path in in_b], size_a + size_b
            )
            tallies[f"grep a, grep b, notes in either (max {limit})"].add(target, paths_a + paths_b, size_a + size_b)

    n = len(queries)
    print(f"\n### {title} ({n} queries)\n")
    print("| Strategy | Calls | Found | In top 3 | Notes returned | Response | Tokens (approx.) |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for label, t in tallies.items():
        print(
            f"| {label.replace('|', chr(92) + '|')} | {t.calls} | {t.found / n:.0%} | {t.top3 / n:.0%} |"
            f" {t.notes / n:.1f} | {t.size / n / 1000:.1f} kB | {t.size / n / _BYTES_PER_TOKEN:,.0f} |"
        )


def compare_stemming(
    queries: dict[str, tuple[str, str]], texts: dict[str, str], stemmed: FtsIndex, plain: FtsIndex
) -> None:
    """Swap one query word for another form from the vault's vocabulary that the target
    note does not contain, then search with stemming, without, and without but with the
    ending cut off and a * added, which is what a caller can do by itself."""
    swapped: dict[str, tuple[str, str, str]] = {}
    for target, (a, b) in queries.items():
        text = texts[target].lower()
        for word, other in ((a, b), (b, a)):
            forms = stemmed.search(word, None, 1).get("word_forms", {}).get(word, [])
            forms = [form for form in forms if form.isalpha() and form not in text and abs(len(form) - len(word)) <= 3]
            if forms:
                swapped[target] = (forms[0], other, word)
                break

    found = Counter()
    for target, (form, other, original) in swapped.items():
        found["stemming on"] += target in fts(stemmed, f"{form} {other}")[0]
        found["stemming off"] += target in fts(plain, f"{form} {other}")[0]
        prefix = os.path.commonprefix([form, original])
        found["stemming off, caller retries with `prefix*`"] += (
            len(prefix) >= 4 and target in fts(plain, f"{prefix}* {other}")[0]
        )

    n = len(swapped)
    print(f"\n### Stemming: the query uses a word form the note does not contain ({n} queries)\n")
    if not n:
        print("No query word had another form in this vault.")
        return
    print("| Setup | Calls | Found |")
    print("| --- | --- | --- |")
    for label, calls in (("stemming on", 1), ("stemming off", 1), ("stemming off, caller retries with `prefix*`", 2)):
        print(f"| {label} | {calls} | {found[label] / n:.0%} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--languages", default="en", help="as VAULT_FTS_LANGUAGES, e.g. en,de")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="GLOB",
        help="notes not used as search targets (hub pages, templates); they stay searchable. Repeatable.",
    )
    parser.add_argument("--seed", type=int, default=20260920)
    args = parser.parse_args()

    if "VAULT_PATH" not in os.environ or not vault_config.VAULT_PATH.is_dir():
        sys.exit("Set VAULT_PATH to your vault.")

    languages = resolve_languages(args.languages)
    stopwords = frozenset().union(*(language.stopwords for language in languages))
    stemmed, plain = FtsIndex(":memory:", languages=languages), FtsIndex(":memory:", languages=())
    for index in (stemmed, plain):
        index.open()
        index.reconcile()

    texts, notes = {}, {}
    for rel_path in iter_vault_notes():
        text = (vault_config.VAULT_PATH / rel_path).read_text(encoding="utf-8", errors="replace")
        texts[rel_path], notes[rel_path] = text, parse_note(rel_path, text)
    note_count = Counter(word for text in texts.values() for word in set(keywords(text, frozenset())))

    rng = random.Random(args.seed)
    targets = [path for path in notes if not any(fnmatch.fnmatch(path, glob) for glob in args.skip)]
    topic = {path: query for path in targets if (query := topic_query(notes[path], stopwords))}
    body = {path: query for path in targets if (query := body_query(path, notes[path], stopwords, note_count, rng))}

    size = sum(len(text.encode()) for text in texts.values())
    print(
        f"{len(notes)} notes, {size / 1000:,.0f} kB, {len(targets)} search targets, languages:"
        f" {', '.join(language.name for language in languages) or 'none'},"
        f" grep: {'ripgrep' if shutil.which('rg') else 'Python fallback'}"
    )
    compare("Topic: two words from title, aliases or description", topic, stemmed, single_regex=True)
    compare("Body: two body words on different lines", body, stemmed, single_regex=False)
    compare_stemming(topic, texts, stemmed, plain)


if __name__ == "__main__":
    main()

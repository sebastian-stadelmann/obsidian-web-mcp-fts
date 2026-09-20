"""Language packs: what the index needs to know about a language.

A pack is a Snowball stemmer plus a stopword list. English is the default; further
languages are switched on next to it with VAULT_FTS_LANGUAGES ("en,de"). Several
languages can be active at once, because a vault is rarely monolingual: German notes
are full of English terms. A query word is then stemmed by every active language and
the word forms found by any of them count.

Stemming works on the index vocabulary, which the tokenizer has already lower-cased and
stripped of diacritics. That costs nothing for English and German, whose suffix rules do
not depend on accents. Languages whose rules do (French "-ité", Spanish "-ción") are
stemmed less thoroughly than a native analyzer would; they work, but are untested here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import snowballstemmer

from .stopwords import STOPWORDS_BY_LANGUAGE

DEFAULT_LANGUAGES = "en"

# ISO 639-1 code -> Snowball algorithm name. The algorithm name is the canonical key:
# it is what the stem table stores and what stopwords.py is keyed by.
ISO_CODES = {
    "ar": "arabic", "hy": "armenian", "eu": "basque", "ca": "catalan", "cs": "czech",
    "da": "danish", "nl": "dutch", "en": "english", "eo": "esperanto", "et": "estonian",
    "fi": "finnish", "fr": "french", "de": "german", "el": "greek", "hi": "hindi",
    "hu": "hungarian", "id": "indonesian", "ga": "irish", "it": "italian", "lt": "lithuanian",
    "ne": "nepali", "no": "norwegian", "nb": "norwegian", "fa": "persian", "pl": "polish",
    "pt": "portuguese", "ro": "romanian", "ru": "russian", "sr": "serbian", "st": "sesotho",
    "es": "spanish", "sv": "swedish", "ta": "tamil", "tr": "turkish", "yi": "yiddish",
}  # fmt: skip


@dataclass(frozen=True)
class Language:
    name: str  # Snowball algorithm name, e.g. "german"
    stopwords: frozenset[str]
    _stemmer: object = field(repr=False, compare=False)

    def stem(self, word: str) -> str:
        """Stem one lower-case word. NOT thread-safe: the pure-Python Snowball stemmers
        keep their cursor on the instance, so callers serialise (the index lock does)."""
        return self._stemmer.stemWord(word)


def resolve_languages(spec: str | None) -> tuple[Language, ...]:
    """Parse "en,de" / "english, german" / "none" into language packs.

    Raises ValueError for a language Snowball does not know, naming what is available,
    so a typo in the configuration stops the start instead of silently disabling a language.
    """
    spec = (spec or "").strip() or DEFAULT_LANGUAGES
    if spec.lower() == "none":
        return ()
    available = set(snowballstemmer.algorithms())
    languages: list[Language] = []
    for raw in spec.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        name = ISO_CODES.get(key, key)
        if name not in available:
            codes = ", ".join(sorted(code for code, algo in ISO_CODES.items() if algo in available))
            raise ValueError(f"unknown language {raw.strip()!r}. Use 'none' or any of: {codes}")
        if all(lang.name != name for lang in languages):
            languages.append(
                Language(name, STOPWORDS_BY_LANGUAGE.get(name, frozenset()), snowballstemmer.stemmer(name))
            )
    return tuple(languages)

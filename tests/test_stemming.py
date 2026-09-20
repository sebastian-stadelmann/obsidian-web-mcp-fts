"""Word forms: what stemming adds, what it must not disturb, and how languages combine."""

import pytest

from vault_fts.extension import FtsExtension, tool_description
from vault_fts.index import FtsIndex, fold
from vault_fts.languages import resolve_languages

from ._samples import NOTE_COUNT, paths


def index_with(languages: str | None, db_path=":memory:") -> FtsIndex:
    idx = FtsIndex(db_path, languages=resolve_languages(languages))
    idx.open()
    idx.reconcile()
    return idx


def test_a_word_finds_its_other_forms(index):
    # The vault has "Zertifikate" twice and "Zertifikat" once.
    payload = index.search("zertifikat")
    assert payload["word_forms"] == {"zertifikat": ["zertifikate"]}
    hits = payload["results"]
    assert hits[0]["path"] == "notizen/zertifikat.md"
    assert "match" not in hits[0]  # has the word as typed
    assert {hit["path"] for hit in hits[1:]} == {"howto/livesync-traefik.md", "daily/2026-09-01.md"}
    assert all(hit["match"] == "word_forms" for hit in hits[1:])
    # The form is what gets highlighted, in the original text.
    assert "«Zertifikate»" in hits[1]["snippet"]

    # ...and the other way round.
    assert "notizen/zertifikat.md" in paths(index.search("zertifikate"))


def test_the_word_as_typed_outranks_a_mere_form(index):
    typed, *forms = index.search("zertifikat")["results"]
    assert all(typed["score"] > hit["score"] for hit in forms)

    # Only one of three words is a mere form: the note about all three still leads,
    # with a reduced but not halved score.
    payload = index.search("traefik file-provider zertifikat")
    assert paths(payload) == ["howto/livesync-traefik.md"]
    assert payload["results"][0]["match"] == "word_forms"


def test_quotes_and_fts5_syntax_stay_exact(index):
    assert index.search('"zertifikat"')["query_mode"] == "fts5"
    assert paths(index.search('"zertifikat"')) == ["notizen/zertifikat.md"]
    assert "word_forms" not in index.search("zertifikat OR grafana")
    # Phrases, prefixes and non-words are never expanded.
    for query in ("file-provider", "zertifik*", "e2ee"):
        assert "word_forms" not in index.search(query)


def test_relaxing_counts_word_forms_as_a_match(index):
    # "zertifikat" is in the howto only as "Zertifikate"; it must not be the term given up.
    payload = index.search("zertifikat e2ee kubernetes")
    assert payload["dropped_terms"] == ["kubernetes"]
    assert paths(payload) == ["howto/livesync-traefik.md"]
    assert payload["word_forms"] == {"zertifikat": ["zertifikate"]}
    # Forms of a dropped term are not reported.
    assert "word_forms" not in index.search("kubernetes traefik")


def test_english_is_the_default_and_german_an_addition(vault):
    english = index_with(None)
    assert [lang.name for lang in resolve_languages(None)] == ["english"]
    # English stemming and stopwords work out of the box ...
    payload = english.search("how to renew the wildcard")
    assert payload["ignored_stopwords"] == ["how", "to", "the"]
    # "renewing" is in the text, "renewal" is the file name: the path is searchable too.
    assert payload["word_forms"] == {"renew": ["renewal", "renewing"]}
    assert paths(payload) == ["notes/renewal.md"]
    # ... German filler words are ordinary words until German is switched on.
    assert english.search("wie traefik")["dropped_terms"] == ["wie"]
    english.close()

    both = index_with("en,de")
    assert both.search("wie traefik")["ignored_stopwords"] == ["wie"]
    assert paths(both.search("renew")) == ["notes/renewal.md"]  # English still active next to German
    both.close()

    german = index_with("de")
    assert german.search("renew")["total"] == 0  # "renewing" is not a German inflection
    assert german.search("zertifikat")["total"] == 3
    german.close()


def test_no_language_means_no_stemming_and_no_stopwords(vault):
    plain = index_with("none")
    assert paths(plain.search("zertifikat")) == ["notizen/zertifikat.md"]
    # Without a stopword list "how" is a word like any other, and one no note contains.
    payload = plain.search("how traefik")
    assert "ignored_stopwords" not in payload and payload["dropped_terms"] == ["how"]
    plain.close()


def test_changing_languages_needs_no_reindex(vault, tmp_path):
    db_path = tmp_path / "fts.sqlite"
    first = index_with("en,de", db_path)
    assert first.search("zertifikat")["total"] == 3
    first.close()

    english_only = FtsIndex(db_path, languages=resolve_languages("en"))
    english_only.open()
    stats = english_only.reconcile()
    assert (stats["indexed"], stats["unchanged"]) == (0, NOTE_COUNT)  # notes untouched
    assert english_only.search("zertifikat")["total"] == 1  # German forms are gone
    english_only.close()

    back = FtsIndex(db_path, languages=resolve_languages("de,en"))
    back.open()
    assert back.search("zertifikat")["total"] == 3  # stems rebuilt lazily, without reconcile
    back.close()


def test_new_words_are_stemmed_on_the_next_search(index, vault):
    assert index.search("dashboard")["total"] == 0
    (vault / "projekte/grafana.md").write_text("Neue Dashboards für das Monitoring.\n", encoding="utf-8")
    index.update_path("projekte/grafana.md")
    payload = index.search("dashboard")
    assert paths(payload) == ["projekte/grafana.md"] and payload["word_forms"] == {"dashboard": ["dashboards"]}

    # A word that left the vault stops being offered as a form after the next reconcile.
    (vault / "projekte/grafana.md").unlink()
    index.reconcile()
    assert "word_forms" not in index.search("dashboard")


def test_fold_matches_the_index_tokenizer():
    assert [fold(word) for word in ("Müller", "ÄNDERUNG", "Café", "Straße")] == ["muller", "anderung", "cafe", "straße"]


def test_unknown_language_stops_the_start(vault, tmp_path):
    with pytest.raises(ValueError, match="unknown language 'klingon'"):
        resolve_languages("en,klingon")
    with pytest.raises(SystemExit, match="VAULT_FTS_LANGUAGES"):
        FtsExtension(db_path=tmp_path / "fts.sqlite", languages="en,klingon")
    assert [lang.name for lang in resolve_languages(" EN , german,de ")] == ["english", "german"]
    assert resolve_languages("fr")[0].stopwords == frozenset()  # stemmed, no stopword list yet


def test_tool_description_names_the_active_languages():
    assert "Active languages: english, german" in tool_description(("english", "german"))
    assert "No stemming" in tool_description(())

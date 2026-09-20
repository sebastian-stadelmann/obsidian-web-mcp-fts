"""What a query means: ranking, folding, FTS5 syntax, and text that is not syntax."""

import pytest

from vault_fts.index import literal_terms

from ._samples import paths


def test_title_and_alias_hits_outrank_body_hits(index):
    hits = index.search("traefik")["results"]
    assert [h["path"] for h in hits] == ["howto/livesync-traefik.md", "daily/2026-09-01.md"]
    assert hits[0]["score"] > hits[1]["score"]
    assert {"path", "title", "description", "body"} <= set(hits[0]["matched_fields"])
    assert hits[1]["matched_fields"] == ["body"]

    # "Müller" is the person note's name but only body text elsewhere.
    assert paths(index.search("müller"))[0] == "people/Müller.md"
    assert index.search("bridge-setup")["results"][0]["matched_fields"] == ["aliases"]


def test_result_shape(index):
    payload = index.search("zertifikate erhalten")
    assert payload["query_mode"] == "all" and payload["total"] == 1 and payload["truncated"] is False
    hit = payload["results"][0]
    assert hit["title"] == "LiveSync mit Traefik"
    assert hit["tags"] == ["obsidian", "homelab"]
    assert hit["description"].startswith("Obsidian LiveSync")
    assert "«Zertifikate»" in hit["snippet"] and "«erhalten»" in hit["snippet"]
    assert "\n" not in hit["snippet"]
    # Notes without frontmatter carry no empty description/tags keys.
    assert set(index.search("relaunch")["results"][0]) == {"path", "title", "score", "matched_fields", "snippet"}


@pytest.mark.parametrize("query", ["muller", "MÜLLER", "Müller", "anderung", "Änderung"])
def test_case_and_diacritics_are_folded(index, query):
    assert "howto/livesync-traefik.md" in paths(index.search(query))


def test_matching_is_document_wide_not_line_wide(index):
    # "E2EE" and "Zertifikate" are on different lines of the howto.
    assert paths(index.search("e2ee zertifikate")) == ["howto/livesync-traefik.md"]


def test_fts5_syntax(index):
    assert index.search('"zertifikate bleiben erhalten"')["query_mode"] == "fts5"
    assert paths(index.search('"zertifikate bleiben erhalten"')) == ["howto/livesync-traefik.md"]
    assert index.search('"zertifikate erhalten"')["total"] == 0
    assert paths(index.search("nachger*")) == ["howto/livesync-traefik.md"]
    assert paths(index.search("traefik NOT e2ee")) == ["daily/2026-09-01.md"]
    # daily: "Müller über Traefik" (1 token between); howto: "Müller. E2EE bleibt aus. Traefik" (3).
    assert paths(index.search("NEAR(müller traefik, 2)")) == ["daily/2026-09-01.md"]
    assert len(paths(index.search("NEAR(müller traefik, 3)"))) == 2
    assert set(paths(index.search("podman OR relaunch"))) == {
        "people/Müller.md", "projekte/website.md", "howto/livesync-traefik.md"
    }
    assert paths(index.search("title:traefik")) == ["howto/livesync-traefik.md"]


@pytest.mark.parametrize(
    "query",
    ["file-provider", "File-Provider nachgerüstet,", "traefik (file-provider", 'traefik "file', "c++ traefik", "um 10:30 traefik"],
)
def test_text_that_is_not_fts5_syntax_still_searches(index, query):
    payload = index.search(query)
    assert "error" not in payload
    assert payload["query_mode"] in ("all", "relaxed")
    assert "howto/livesync-traefik.md" in paths(payload)


def test_relaxes_to_the_largest_term_set_some_note_has(index):
    # A term that occurs nowhere cannot be part of it.
    payload = index.search("traefik kubernetes")
    assert (payload["query_mode"], payload["dropped_terms"]) == ("relaxed", ["kubernetes"])
    assert set(paths(payload)) == {"howto/livesync-traefik.md", "daily/2026-09-01.md"}

    # Both terms exist, never together: the rarer, more specific one is searched.
    payload = index.search("müller relaunch")
    assert (payload["query_mode"], payload["dropped_terms"]) == ("relaxed", ["müller"])
    assert paths(payload) == ["projekte/website.md"]

    # Two of three terms beat one of three, and of the two-term sets the rarer wins:
    # {ansprechpartner, müller} (person note) over {müller, traefik} (howto, daily).
    payload = index.search("ansprechpartner müller traefik")
    assert payload["dropped_terms"] == ["traefik"]
    assert paths(payload) == ["people/Müller.md"]

    # A tie in size and rarity goes to the term set earliest in the query.
    payload = index.search("file-provider* relaunch kubernetes")
    assert payload["dropped_terms"] == ["relaunch", "kubernetes"]
    assert paths(payload) == ["howto/livesync-traefik.md"]

    # Relaxing respects the path filter.
    payload = index.search("müller relaunch", path_prefix="daily/")
    assert (paths(payload), payload["dropped_terms"]) == (["daily/2026-09-01.md"], ["relaunch"])

    # Nothing to relax to: the empty all-terms answer stands.
    assert index.search("kubernetes nomad") == {"results": [], "total": 0, "truncated": False, "query_mode": "all"}
    # An explicit FTS5 query is taken at its word: no silent widening.
    assert index.search("traefik AND kubernetes") == {
        "results": [], "total": 0, "truncated": False, "query_mode": "fts5"
    }


def test_filler_words_do_not_filter(index):
    payload = index.search("Warum läuft bei Traefik kein Zertifikat aus?")
    assert payload["ignored_stopwords"] == ["Warum", "bei", "kein", "aus"]
    # "läuft" has no form in the vault (the notes say "laufen": irregular, out of a stemmer's reach).
    assert payload["dropped_terms"] == ["läuft"]
    assert set(paths(payload)) == {"howto/livesync-traefik.md", "daily/2026-09-01.md"}

    # The words are still in the index: a phrase or any FTS5 query matches them exactly.
    assert paths(index.search('"mit müller über traefik"')) == ["daily/2026-09-01.md"]
    # A query of nothing but filler words is searched as it stands.
    payload = index.search("mit über")
    assert "ignored_stopwords" not in payload and paths(payload) == ["daily/2026-09-01.md"]
    # A prefix term is never a stopword.
    assert "ignored_stopwords" not in index.search("zert* ein*")


def test_path_prefix_and_limits(index):
    assert paths(index.search("müller", path_prefix="daily/")) == ["daily/2026-09-01.md"]
    assert paths(index.search("müller", path_prefix="/daily")) == ["daily/2026-09-01.md"]
    assert index.search("müller", path_prefix="nirgends/")["total"] == 0
    # "people/" is the folder; without the slash it is a string prefix.
    assert index.search("müller", path_prefix="peo/")["total"] == 0
    assert paths(index.search("müller", path_prefix="peo")) == ["people/Müller.md"]
    assert index.search("müller", path_prefix="./")["total"] == 3

    limited = index.search("müller", max_results=1)
    assert limited["total"] == 1 and limited["truncated"] is True
    assert index.search("müller", max_results=3)["truncated"] is False


def test_query_without_words_is_an_error_not_a_crash(index):
    payload = index.search("+++ --- ...")
    assert payload["error"] and payload["results"] == []


def test_literal_terms():
    terms = literal_terms("file-provider  C++ vault_mcp")
    assert [term.match for term in terms] == ['"file provider"', '"C"', '"vault_mcp"']
    assert [term.word for term in terms] == [None, "C", "vault_mcp"]  # a phrase has no single word
    (prefix,) = literal_terms("änder* --- änder*")
    assert (prefix.text, prefix.match, prefix.word) == ("änder*", '"änder" *', None)

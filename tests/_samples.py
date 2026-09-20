"""Sample notes shared by the fixtures and the tests that assert on their content."""

HOWTO = """---
type: howto
title: LiveSync mit Traefik
description: "Obsidian LiveSync hinter dem Traefik File-Provider mit CouchDB."
aliases: ["LiveSync Howto", "Bridge-Setup"]
tags: [obsidian, homelab]
---

# LiveSync mit Traefik

## Entscheidung

Die Änderung an der Bridge betrifft Müller. E2EE bleibt aus.
Traefik File-Provider nachgerüstet, Zertifikate bleiben erhalten.

```bash
# kein Heading, nur ein Kommentar
podman-compose up -d
```
"""

PERSON = """---
type: person
aliases: [MUE]
---

Ansprechpartner für Podman auf dem Server.
"""

DAILY = """## What came up

Mit Müller über Traefik gesprochen. Zertifikate laufen aus.
"""

PROJECT = "Website Relaunch: neues Theme, Umzug auf den neuen Server.\n"

ENGLISH = "Renewing the wildcard: it expires yearly, so the job retries nightly.\n"

SINGULAR = "Ein Zertifikat von Let's Encrypt.\n"

# Filler so the corpus is large enough for BM25: with four notes a term that occurs in
# two of them has an inverse document frequency of zero and every score collapses to 0.
FILLER = {
    f"notizen/notiz-{i:02d}.md": f"Notiz {i} zum Thema {topic}.\n"
    for i, topic in enumerate(
        ["Backup", "Monitoring", "Rechnung", "Urlaub", "Hardware", "Lizenz", "Schulung", "Netzwerk"], start=1
    )
}

NAMED_NOTES = 6
NOTE_COUNT = NAMED_NOTES + len(FILLER)


def paths(payload: dict) -> list[str]:
    return [hit["path"] for hit in payload["results"]]

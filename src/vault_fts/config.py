"""Environment configuration for the FTS extension.

Read once at import time, mirroring how obsidian_vault_mcp.config works.
"""

import os
from pathlib import Path

# ":memory:" keeps the index in RAM (rebuilt on every start). Any other value is a
# file path. The file holds vault plaintext, so it must live OUTSIDE the vault: a
# sync tool watching the vault would otherwise replicate it to every device.
_raw_db_path = os.environ.get("VAULT_FTS_DB_PATH", "").strip()
VAULT_FTS_DB_PATH: str | Path = (
    ":memory:"
    if _raw_db_path == ":memory:"
    else Path(_raw_db_path or Path.home() / ".local" / "share" / "vault-mcp" / "fts.sqlite").expanduser()
)

# Notes larger than this are not indexed. Guards against a multi-megabyte export
# renamed to .md bloating the index; ordinary notes are far below it.
VAULT_FTS_MAX_FILE_BYTES = int(os.environ.get("VAULT_FTS_MAX_FILE_BYTES", "2000000"))

# Languages for stemming and stopwords, as ISO codes or Snowball names: "en" (default),
# "en,de", "english, german, french". "none" switches both off. Changing this needs no
# reindex; only the small stem table is rebuilt on the next start.
VAULT_FTS_LANGUAGES = os.environ.get("VAULT_FTS_LANGUAGES", "").strip() or "en"

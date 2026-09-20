"""Environment configuration for the FTS extension.

Read once at import time, mirroring how obsidian_vault_mcp.config works.
"""

import os
from pathlib import Path

VAULT_FTS_DB_PATH = Path(
    os.environ.get(
        "VAULT_FTS_DB_PATH",
        Path.home() / ".local" / "share" / "vault-mcp" / "fts.sqlite",
    )
).expanduser()

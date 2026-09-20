"""Console entry point: run obsidian-web-mcp with the FTS extension loaded."""

from obsidian_vault_mcp.server import serve

from .extension import FtsExtension


def main() -> None:
    serve([FtsExtension()])


if __name__ == "__main__":
    main()

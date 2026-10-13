"""Compatibility entry point: `uv run server.py ...` runs the same command as `bakalari-mcp ...`.

Kept so that existing services and Claude Desktop configurations that start server.py keep working."""

import sys

from bakalari_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

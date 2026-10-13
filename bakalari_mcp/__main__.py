"""`python -m bakalari_mcp <command>` runs the same command line as `bakalari-mcp`."""

import sys

from bakalari_mcp.cli import main

sys.exit(main())

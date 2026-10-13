#!/usr/bin/env bash
# Linux and macOS helper from the old instructions: runs the cross-platform `bakalari-mcp setup` from this folder.
# Windows: run `uv run bakalari-mcp setup` in this folder instead.
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
exec uv --directory "$APP_DIR" run bakalari-mcp setup "$@"

#!/usr/bin/env bash
# Serve mkdocs-material site and print a URL reachable from Windows.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${1:-8000}"

# Activate venv
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    source "$REPO_ROOT/.venv/bin/activate"
else
    echo "Error: .venv not found at $REPO_ROOT/.venv" >&2
    exit 1
fi

# Ensure mkdocs-material is installed
if ! command -v mkdocs &>/dev/null; then
    echo "Installing mkdocs-material..."
    pip install -r "$REPO_ROOT/requirements-docs.txt"
fi

# Get a reachable IP for display
if command -v ip &>/dev/null; then
    # Linux / WSL
    HOST_IP="$(hostname -I | awk '{print $1}')"
else
    # macOS
    HOST_IP="$(ipconfig getifaddr en0 2>/dev/null || echo '127.0.0.1')"
fi

echo ""
echo "========================================="
echo "  MkDocs serving on port $PORT"
echo ""
echo "  http://${HOST_IP}:${PORT}"
echo "  http://127.0.0.1:${PORT}"
echo "========================================="
echo ""

cd "$REPO_ROOT"
exec mkdocs serve -a "0.0.0.0:${PORT}"

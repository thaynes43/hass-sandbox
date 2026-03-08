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

# Get the WSL IP reachable from Windows
WSL_IP="$(hostname -I | awk '{print $1}')"

echo ""
echo "========================================="
echo "  MkDocs serving on port $PORT"
echo ""
echo "  From Windows browser:"
echo "    http://${WSL_IP}:${PORT}"
echo ""
echo "  From WSL:"
echo "    http://127.0.0.1:${PORT}"
echo "========================================="
echo ""

cd "$REPO_ROOT"
exec mkdocs serve -a "0.0.0.0:${PORT}"

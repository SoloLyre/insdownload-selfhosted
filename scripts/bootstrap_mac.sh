#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m venv .venv-app
./.venv-app/bin/python -m pip install --upgrade pip
./.venv-app/bin/python -m pip install -e .

echo "Bootstrap complete."
echo "Next:"
echo "  cp config.example.toml config.toml"
echo "  ./scripts/run_local.sh"

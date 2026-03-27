#!/bin/zsh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x ./.venv-app/bin/insdownload-web ]; then
  echo "Bootstrap is incomplete. Re-run ./scripts/bootstrap_mac.sh." >&2
  exit 1
fi

./.venv-app/bin/insdownload-web

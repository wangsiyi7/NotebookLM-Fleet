#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export PYTHONUTF8=1

URL="http://127.0.0.1:8765"
if command -v open >/dev/null 2>&1; then
  (sleep 1 && open "$URL") >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
  (sleep 1 && xdg-open "$URL") >/dev/null 2>&1 &
fi

if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  exec "$REPO_ROOT/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
else
  exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
fi

#!/bin/bash
# Run the backend locally with production DB + secrets from backend/.env
# Usage:  bash scripts/run_local_backend.sh [port] [workers]
#
# NOTE: uses 4 workers by default so Moby's internal httpx calls to
#       /api/explorer/search don't deadlock (single-worker would block).
#       --reload is incompatible with --workers, so hot-reload is disabled.
#       Restart the script manually after code changes.
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
PORT="${1:-8000}"
WORKERS="${2:-4}"

if [ ! -f "$BACKEND_DIR/.env" ]; then
  echo "ERROR: backend/.env not found. Run this to generate it:"
  echo "   python3 scripts/gen_local_env.py"
  exit 1
fi

echo "Starting backend on http://localhost:$PORT (workers=$WORKERS)"
echo "    DB: $(grep '^DATABASE_URL' "$BACKEND_DIR/.env" | cut -d= -f2 | sed 's/@.*/\@***/')"
echo ""

cd "$BACKEND_DIR"
set -a; source .env; set +a

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "$WORKERS" \
  --log-level info

#!/bin/bash
# Run the backend locally with production DB + secrets from backend/.env
# Usage:  bash scripts/run_local_backend.sh [port]
set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
PORT="${1:-8000}"

if [ ! -f "$BACKEND_DIR/.env" ]; then
  echo "❌  backend/.env not found. Run this to generate it:"
  echo "   python3 scripts/gen_local_env.py"
  exit 1
fi

echo "🚀  Starting backend on http://localhost:$PORT"
echo "    DB: $(grep '^DATABASE_URL' "$BACKEND_DIR/.env" | cut -d= -f2 | sed 's/@.*/\@***/')"
echo ""

cd "$BACKEND_DIR"
set -a; source .env; set +a

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --reload \
  --reload-dir app \
  --log-level info

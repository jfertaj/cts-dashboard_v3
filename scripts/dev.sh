#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# dev.sh — Start CTS Dashboard locally (backend + frontend)
#
# Usage:
#   bash scripts/dev.sh          # start everything
#   bash scripts/dev.sh --setup  # regenerate backend/.env from AWS first
#
# Prerequisites (first time):
#   1. aws sso login --profile juan
#   2. bash scripts/dev.sh --setup
#   3. In Salesforce: Setup → App Manager → CTS Dashboard → Edit →
#      add callback URL: http://localhost:8000/api/salesforce/oauth/callback
#
# Then visit:
#   Frontend:  http://localhost:5173
#   Backend:   http://localhost:8000/docs
#   SF Login:  http://localhost:8000/api/salesforce/auth  (first time only)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"

# ── Setup mode: regenerate .env from AWS ──────────────────────────────────────
if [[ "${1:-}" == "--setup" ]]; then
  echo "==> Fetching secrets from AWS Secrets Manager..."
  python3 "$REPO_ROOT/scripts/gen_local_env.py"
  echo ""
fi

# ── Check prerequisites ───────────────────────────────────────────────────────
if [ ! -f "$BACKEND_DIR/.env" ]; then
  echo "❌  backend/.env not found. Run first:"
  echo "     aws sso login --profile juan"
  echo "     bash scripts/dev.sh --setup"
  exit 1
fi

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "==> Installing frontend dependencies..."
  cd "$FRONTEND_DIR" && npm install
fi

# ── Start backend ─────────────────────────────────────────────────────────────
echo ""
echo "==> Starting backend  → http://localhost:8000"
echo "==> Starting frontend → http://localhost:5173"
echo ""
echo "    SF Login (first time): http://localhost:8000/api/salesforce/auth"
echo "    Press Ctrl+C to stop both."
echo ""

# Run backend in background, frontend in foreground
cd "$BACKEND_DIR"
set -a; source .env; set +a

uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4 \
  --log-level warning &
BACKEND_PID=$!

# Give backend a moment to start
sleep 2

# Run frontend (Vite dev server)
cd "$FRONTEND_DIR"
npm run dev &
FRONTEND_PID=$!

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "==> Stopping..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  echo "Done."
}
trap cleanup EXIT INT TERM

wait "$FRONTEND_PID"

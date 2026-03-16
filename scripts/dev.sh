#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# dev.sh — Start CTS Dashboard locally (backend + frontend)
#
# USAGE
#   bash scripts/dev.sh                  # start (uses existing backend/.env)
#   bash scripts/dev.sh --setup          # refresh .env from AWS, then start
#   bash scripts/dev.sh --login          # aws sso login, refresh .env, then start
#   bash scripts/dev.sh --stop           # kill any running dev processes
#   bash scripts/dev.sh --status         # show whether backend/frontend are running
#
# FIRST TIME
#   1. aws sso login --profile juan
#   2. bash scripts/dev.sh --setup
#   3. Go to http://localhost:5173 → click Login (SF OAuth)
#
# DAILY
#   bash scripts/dev.sh               # (if .env is still valid)
#   bash scripts/dev.sh --setup       # (if secrets might have rotated)
#
# AFTER CODE CHANGES
#   Ctrl+C → bash scripts/dev.sh      # frontend hot-reloads; backend needs restart
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"
FRONTEND_DIR="$REPO_ROOT/frontend"
PID_FILE="/tmp/cts-dashboard-dev.pids"
BACKEND_LOG="/tmp/cts-backend.log"
FRONTEND_LOG="/tmp/cts-frontend.log"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-8080}"

# ── Colours ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; RESET=''
fi

info()    { echo -e "${CYAN}==>${RESET} $*"; }
success() { echo -e "${GREEN}✓${RESET}  $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "${RED}✗${RESET}  $*" >&2; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── Helpers ───────────────────────────────────────────────────────────────────
port_in_use() { lsof -ti tcp:"$1" >/dev/null 2>&1; }

kill_port() {
  local port="$1"
  local pids
  pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "$pids" | xargs kill -TERM 2>/dev/null || true
    sleep 1
    pids=$(lsof -ti tcp:"$port" 2>/dev/null || true)
    if [ -n "$pids" ]; then
      echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
  fi
}

wait_for_port() {
  local port="$1" label="$2" max_wait="${3:-20}" interval=1 elapsed=0
  while ! lsof -ti tcp:"$port" >/dev/null 2>&1; do
    sleep "$interval"; elapsed=$((elapsed + interval))
    if [ "$elapsed" -ge "$max_wait" ]; then
      error "$label did not start within ${max_wait}s"
      return 1
    fi
    printf '.'
  done
  echo ""
}

open_browser() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    sleep 1 && open "$url" &
  fi
}

# ── --stop ────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--stop" ]]; then
  header "Stopping CTS Dashboard dev servers"
  if [ -f "$PID_FILE" ]; then
    while read -r pid; do
      kill "$pid" 2>/dev/null && info "Killed PID $pid" || true
    done < "$PID_FILE"
    rm -f "$PID_FILE"
  fi
  kill_port "$BACKEND_PORT"
  kill_port "$FRONTEND_PORT"
  success "Done."
  exit 0
fi

# ── --status ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
  header "CTS Dashboard dev status"
  if port_in_use "$BACKEND_PORT"; then
    success "Backend  → http://localhost:${BACKEND_PORT}  (running)"
  else
    warn "Backend  → port ${BACKEND_PORT} not in use"
  fi
  if port_in_use "$FRONTEND_PORT"; then
    success "Frontend → http://localhost:${FRONTEND_PORT}  (running)"
  else
    warn "Frontend → port ${FRONTEND_PORT} not in use"
  fi
  exit 0
fi

# ── --login: SSO + setup ──────────────────────────────────────────────────────
if [[ "${1:-}" == "--login" ]]; then
  header "AWS SSO Login"
  aws sso login --profile juan
  shift
  # Fall through to --setup logic
  set -- "--setup"
fi

# ── --setup: regenerate .env from AWS ────────────────────────────────────────
if [[ "${1:-}" == "--setup" ]]; then
  header "Fetching secrets from AWS Secrets Manager"
  python3 "$REPO_ROOT/scripts/gen_local_env.py"
  echo ""
fi

# ── Preflight checks ──────────────────────────────────────────────────────────
header "Preflight checks"

if [ ! -f "$BACKEND_DIR/.env" ]; then
  error "backend/.env not found. Run:"
  echo "     aws sso login --profile juan"
  echo "     bash scripts/dev.sh --setup"
  exit 1
fi
success "backend/.env found"

if ! command -v uvicorn >/dev/null 2>&1; then
  error "uvicorn not found. Install backend deps: pip install -r backend/requirements.txt"
  exit 1
fi
success "uvicorn found"

if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  info "Installing frontend dependencies (first time)..."
  cd "$FRONTEND_DIR" && npm install
fi
success "node_modules found"

# ── Port conflict check ───────────────────────────────────────────────────────
for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  if port_in_use "$port"; then
    warn "Port $port already in use — killing existing process"
    kill_port "$port"
    sleep 1
  fi
done

# ── Start backend ─────────────────────────────────────────────────────────────
header "Starting services"

cd "$BACKEND_DIR"
# Load .env safely: eval each line so quoted values (e.g. 'val with spaces') are handled correctly,
# but never treat a value as a shell command. set -a exports every assignment automatically.
set -a
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*# ]] && continue   # skip comments
  [[ -z "${line//[[:space:]]/}" ]] && continue   # skip blank lines
  eval "export $line" 2>/dev/null || true
done < .env
set +a

uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$BACKEND_PORT" \
  --workers 4 \
  --log-level warning \
  > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

info "Backend starting (PID $BACKEND_PID)..."
printf "  Waiting"
if wait_for_port "$BACKEND_PORT" "Backend" 20; then
  success "Backend  → http://localhost:${BACKEND_PORT}"
  success "API docs → http://localhost:${BACKEND_PORT}/docs"
else
  error "Backend failed to start. Check logs: tail -50 $BACKEND_LOG"
  kill "$BACKEND_PID" 2>/dev/null || true
  exit 1
fi

# ── Start frontend ────────────────────────────────────────────────────────────
cd "$FRONTEND_DIR"

npm run dev \
  > "$FRONTEND_LOG" 2>&1 &
FRONTEND_PID=$!

info "Frontend starting (PID $FRONTEND_PID)..."
printf "  Waiting"
if wait_for_port "$FRONTEND_PORT" "Frontend" 45; then
  success "Frontend → http://localhost:${FRONTEND_PORT}"
else
  error "Frontend failed to start. Check logs: tail -50 $FRONTEND_LOG"
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  exit 1
fi

# ── Save PIDs ─────────────────────────────────────────────────────────────────
printf "%s\n%s\n" "$BACKEND_PID" "$FRONTEND_PID" > "$PID_FILE"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}─────────────────────────────────────────────${RESET}"
echo -e "${GREEN}  CTS Dashboard running locally${RESET}"
echo -e "${BOLD}─────────────────────────────────────────────${RESET}"
echo ""
echo -e "  ${BOLD}Frontend${RESET}  http://localhost:${FRONTEND_PORT}"
echo -e "  ${BOLD}Backend${RESET}   http://localhost:${BACKEND_PORT}"
echo -e "  ${BOLD}API docs${RESET}  http://localhost:${BACKEND_PORT}/docs"
echo ""
echo -e "  ${YELLOW}SF Login${RESET}  Click 'Login' in the app header"
echo -e "           (OAuth → redirects back automatically)"
echo ""
echo -e "  ${CYAN}Logs${RESET}"
echo -e "    Backend:  tail -f $BACKEND_LOG"
echo -e "    Frontend: tail -f $FRONTEND_LOG"
echo ""
echo -e "  ${BOLD}To stop:${RESET}  Ctrl+C  or  bash scripts/dev.sh --stop"
echo -e "${BOLD}─────────────────────────────────────────────${RESET}"
echo ""

# Open browser (macOS)
open_browser "http://localhost:${FRONTEND_PORT}"

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
  echo ""
  info "Stopping services..."
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  rm -f "$PID_FILE"
  success "Done."
}
trap cleanup EXIT INT TERM

# ── Keep alive (tail backend log so Ctrl+C is visible) ───────────────────────
wait "$BACKEND_PID" "$FRONTEND_PID"

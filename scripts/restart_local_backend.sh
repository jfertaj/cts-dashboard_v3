#!/usr/bin/env bash
# restart_local_backend.sh — Kill whatever is on port 8000 and relaunch the backend.
#
# Usage:
#   bash scripts/restart_local_backend.sh
#   bash scripts/restart_local_backend.sh 8001   # custom port
set -euo pipefail

PORT="${1:-8000}"

# Kill any process listening on PORT
PIDS=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$PIDS" ]; then
    echo "Killing processes on port $PORT: $PIDS"
    echo "$PIDS" | xargs kill -9
    sleep 1
fi

# Exec the standard launch script (inherits PORT if passed)
exec bash "$(dirname "$0")/run_local_backend.sh" "$PORT"

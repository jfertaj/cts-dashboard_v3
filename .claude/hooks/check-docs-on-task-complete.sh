#!/usr/bin/env bash
set -euo pipefail

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"

# Si no hay git, no bloqueamos nada
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi

changed="$(git status --porcelain=v1)"
if [ -z "$changed" ]; then
  exit 0
fi

changed_files="$(printf '%s\n' "$changed" | sed 's/^...//')"

# Solo nos importa si hubo cambios reales de código del proyecto
project_files="$(printf '%s\n' "$changed_files" | grep -E '^(frontend/|backend/|scripts/|infrastructure/|app/)' || true)"
if [ -z "$project_files" ]; then
  exit 0
fi

# Timestamp más reciente entre los archivos de proyecto modificados
newest_project_mtime=0
while IFS= read -r f; do
  if [ -f "$f" ]; then
    mtime=$(stat -f "%m" "$f" 2>/dev/null || stat -c "%Y" "$f" 2>/dev/null || echo 0)
    if [ "$mtime" -gt "$newest_project_mtime" ]; then
      newest_project_mtime=$mtime
    fi
  fi
done <<< "$project_files"

# Timestamp más reciente entre los dos docs
docs_mtime=0
for doc in "docs/current-state.md" "docs/next-steps.md"; do
  if [ -f "$doc" ]; then
    mtime=$(stat -f "%m" "$doc" 2>/dev/null || stat -c "%Y" "$doc" 2>/dev/null || echo 0)
    if [ "$mtime" -gt "$docs_mtime" ]; then
      docs_mtime=$mtime
    fi
  fi
done

# Si algún archivo de proyecto fue modificado más recientemente que los docs → recordatorio
if [ "$newest_project_mtime" -gt "$docs_mtime" ]; then
  echo "Archivos de proyecto modificados desde la última actualización de docs. Si el cambio es significativo (nueva funcionalidad, bug fix de comportamiento, cambio arquitectural), actualiza docs/current-state.md y/o docs/next-steps.md. Si es menor (typo, estilo, ajuste de test), puedes ignorar este aviso." >&2
  exit 2
fi

exit 0

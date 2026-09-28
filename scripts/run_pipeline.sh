#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${GREENAI_PYTHON:-}" ]]; then
  PYTHON="$GREENAI_PYTHON"
elif [[ -x "$ROOT/.venv-linux/bin/python" ]]; then
  PYTHON="$ROOT/.venv-linux/bin/python"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="$(command -v python3 || command -v python)"
fi

exec "$PYTHON" "$ROOT/scripts/run_pipeline.py" "$@"

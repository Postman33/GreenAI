#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
if (($# == 0)); then
  input="$ROOT/Пилотный проект 20 улиц/input_10001759_bound.dxf"
  [[ -f "$input" ]] || { echo 'Input DXF not found. Run bash init.sh first.' >&2; exit 1; }
  exec bash "$ROOT/scripts/run_pipeline.sh" "$input" "$ROOT/output/latest" --mode full --preset dense_mixed
fi
exec bash "$ROOT/scripts/run_pipeline.sh" "$@"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skip_install=0
install_args=()
for arg in "$@"; do
  case "$arg" in
    --skip-install) skip_install=1 ;;
    *) install_args+=("$arg") ;;
  esac
done

if (( ! skip_install )); then
  bash "$ROOT/scripts/install.sh" "${install_args[@]}"
elif (( ${#install_args[@]} )); then
  echo 'Installer options cannot be combined with --skip-install.' >&2
  exit 2
fi

python="$ROOT/.venv-linux/bin/python"
if [[ ! -x "$python" ]]; then
  echo "Python environment was not found: $python. Run bash init.sh without --skip-install first." >&2
  exit 1
fi
"$python" "$ROOT/scripts/download_demo_dxf.py"

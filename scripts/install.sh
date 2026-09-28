#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
WITH_BLENDER=0
SKIP_DOCKER=0
NO_SYSTEM_INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --with-blender) WITH_BLENDER=1 ;;
    --skip-docker) SKIP_DOCKER=1 ;;
    --no-system-install) NO_SYSTEM_INSTALL=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "Error: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
sudo_cmd=()
apt_updated=0
if [[ "$(id -u)" -ne 0 && "$NO_SYSTEM_INSTALL" -eq 0 ]]; then
  have sudo || die 'sudo is required for system packages; install them manually and use --no-system-install'
  sudo_cmd=(sudo)
fi

os_id=''
os_codename=''
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  os_id="${ID:-}"
  os_codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
fi

apt_install() {
  [[ "$NO_SYSTEM_INSTALL" -eq 0 ]] || die "Missing system package(s): $*. Install manually and rerun."
  [[ "$os_id" == ubuntu || "$os_id" == debian ]] || die 'Automatic system installation supports Ubuntu/Debian. Install prerequisites manually and rerun with --no-system-install.'
  if [[ "$apt_updated" -eq 0 ]]; then
    "${sudo_cmd[@]}" apt-get update
    apt_updated=1
  fi
  "${sudo_cmd[@]}" apt-get install -y --no-install-recommends "$@"
}

python=''
for candidate in python3.11 python3.12 python3.10 python3; do
  if have "$candidate" && "$candidate" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,13) else 1)' 2>/dev/null; then
    python="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$python" ]]; then
  apt_install python3 python3-venv python3-pip
  python="$(command -v python3)"
  "$python" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,13) else 1)' || die 'Python 3.10–3.12 is required.'
fi
if [[ "$NO_SYSTEM_INSTALL" -eq 0 && ( "$os_id" == ubuntu || "$os_id" == debian ) ]]; then
  apt_install libgomp1 fonts-dejavu-core ca-certificates curl
fi

go_version="$(awk '$1 == "go" {print $2; exit}' go.mod)"
[[ -n "$go_version" ]] || die 'Could not read Go version from go.mod.'
go_dir="$ROOT/.tools/go-$go_version"
if [[ -x "$go_dir/go/bin/go" ]]; then
  export PATH="$go_dir/go/bin:$PATH"
fi
go_usable=0
if have go; then
  current_go="$(go version | sed -n 's/.* go\([0-9][0-9.]*\) .*/\1/p')"
  if [[ -n "$current_go" ]] && "$python" -c 'import sys; a=tuple(map(int,sys.argv[1].split("."))); b=tuple(map(int,sys.argv[2].split("."))); sys.exit(0 if a >= b else 1)' "$current_go" "$go_version"; then
    go_usable=1
  fi
fi
if [[ "$go_usable" -eq 0 ]]; then
  case "$(uname -m)" in
    x86_64) go_arch=amd64 ;;
    aarch64) go_arch=arm64 ;;
    *) die 'Automatic Go installation supports Linux x86_64/aarch64 only.' ;;
  esac
  have curl || die 'curl is required to download Go.'
  temp_dir="$(mktemp -d)"
  trap 'rm -rf "$temp_dir"' EXIT
  archive_name="go$go_version.linux-$go_arch.tar.gz"
  curl --fail --location --retry 3 'https://go.dev/dl/?mode=json&include=all' --output "$temp_dir/releases.json"
  expected_sha="$("$python" -c 'import json,sys; data=json.load(open(sys.argv[1])); print(next(f["sha256"] for r in data for f in r["files"] if f["filename"] == sys.argv[2]))' "$temp_dir/releases.json" "$archive_name")"
  curl --fail --location --retry 3 "https://go.dev/dl/$archive_name" --output "$temp_dir/$archive_name"
  printf '%s  %s\n' "$expected_sha" "$temp_dir/$archive_name" | sha256sum --check --status || die 'Go archive checksum verification failed.'
  mkdir -p "$go_dir"
  tar -xzf "$temp_dir/$archive_name" -C "$go_dir"
  export PATH="$go_dir/go/bin:$PATH"
fi
echo "Python: $python"
go version

venv="$ROOT/.venv-linux"
if [[ ! -x "$venv/bin/python" ]]; then
  if ! "$python" -m venv "$venv" >/dev/null 2>&1; then
    apt_install python3-venv python3-pip
    "$python" -m venv "$venv"
  fi
fi
"$venv/bin/python" -m pip install --upgrade pip
"$venv/bin/python" -m pip install -r requirements.txt
"$venv/bin/python" -c 'import ezdxf, shapely, onnxruntime, ortools, reportlab'

mkdir -p .gotmp .gocache
export GOCACHE="$ROOT/.gocache"
go build -buildvcs=false -o .gotmp/dxf_extract_go ./parser/dxf_extract_go

if [[ "$SKIP_DOCKER" -eq 0 ]]; then
  if ! have docker || ! docker compose version >/dev/null 2>&1; then
    [[ "$NO_SYSTEM_INSTALL" -eq 0 ]] || die 'Docker Compose is missing. Install Docker Engine and the Compose plugin.'
    [[ "$os_id" == ubuntu || "$os_id" == debian ]] || die 'Automatic Docker installation supports Ubuntu/Debian only.'
    "${sudo_cmd[@]}" install -m 0755 -d /etc/apt/keyrings
    "${sudo_cmd[@]}" curl -fsSL "https://download.docker.com/linux/$os_id/gpg" -o /etc/apt/keyrings/docker.asc
    "${sudo_cmd[@]}" chmod a+r /etc/apt/keyrings/docker.asc
    printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
      "$os_id" "$os_codename" "$(dpkg --print-architecture)" | "${sudo_cmd[@]}" tee /etc/apt/sources.list.d/docker.sources >/dev/null
    apt_updated=0
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    if have systemctl; then "${sudo_cmd[@]}" systemctl enable --now docker; fi
  fi
  docker compose config >/dev/null
  if docker info >/dev/null 2>&1; then
    docker compose pull postgis
  else
    echo 'Docker Engine is not accessible. Start it and grant this user access, then rerun to pull PostGIS.' >&2
  fi
fi

if [[ "$WITH_BLENDER" -eq 1 ]] && ! have blender; then
  apt_install blender
fi

echo 'Installation complete. Run scripts/run_pipeline_interactive.sh or scripts/run_pipeline.sh.'

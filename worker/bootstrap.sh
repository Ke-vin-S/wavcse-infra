#!/usr/bin/env bash
set -Eeuo pipefail

readonly BOOTSTRAP_VERSION="${1:?bootstrap version argument is required}"
readonly UV_VERSION="0.10.9"
# `system` is a disposable, root-capable worker image: it installs packages and uv.
# `static` is an operator-owned account this tool must not install anything into: it
# writes only inside STATIC_ROOT and refuses to run when a tool it needs is missing.
readonly PROFILE="${2:-system}"
readonly STATIC_ROOT="${3:-}"

if [[ ! "${BOOTSTRAP_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf 'Invalid bootstrap version: %s\n' "${BOOTSTRAP_VERSION}" >&2
  exit 2
fi

# The confined root is the only directory the static profile writes beneath, so it is
# validated before anything is created: an empty, relative, or filesystem-root value would
# silently unconfine the whole run.
require_confined_root() {
  if [[ "${STATIC_ROOT}" != /* ]]; then
    printf 'Static bootstrap requires an absolute confined root; received %s\n' \
      "${STATIC_ROOT:-<empty>}" >&2
    exit 2
  fi
  if [[ "${STATIC_ROOT}" == / ]]; then
    printf 'Static bootstrap refuses the filesystem root as its confined root.\n' >&2
    exit 2
  fi
  local parent
  parent="$(dirname -- "${STATIC_ROOT}")"
  if [[ ! -d "${parent}" || ! -w "${parent}" ]]; then
    printf 'Static bootstrap requires a writable parent directory for %s\n' \
      "${STATIC_ROOT}" >&2
    exit 2
  fi
}

# Every tool the static profile uses must already exist. This tool installs nothing on a
# host it does not own, so a missing prerequisite is reported as one complete list.
require_static_tools() {
  local -a missing=()
  local tool
  for tool in curl git gzip python3 tar; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      missing+=("${tool}")
    fi
  done
  local python_version=''
  local python_ok=false
  if command -v python3 >/dev/null 2>&1; then
    python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    case "${python_version}" in
    3.1[0-9] | 3.[2-9][0-9] | [4-9].*) python_ok=true ;;
    esac
  fi
  if ((${#missing[@]} == 0)) && [[ "${python_ok}" == true ]]; then
    return 0
  fi
  printf 'Static bootstrap cannot continue on this host. The operator installs these;' >&2
  printf ' this tool installs nothing on a host it does not own:\n' >&2
  for tool in "${missing[@]}"; do
    printf '  - %s: not found on PATH\n' "${tool}" >&2
  done
  if [[ "${python_ok}" != true ]]; then
    printf '  - python3: version %s found; 3.10 or newer is required\n' \
      "${python_version:-unknown}" >&2
  fi
  exit 1
}

if [[ "${PROFILE}" == static ]]; then
  require_confined_root
  require_static_tools
elif [[ "${PROFILE}" != system ]]; then
  printf 'Unknown worker bootstrap profile: %s\n' "${PROFILE}" >&2
  exit 2
fi

STATE_DIR="$(
  if [[ "${PROFILE}" == static ]]; then
    printf '%s/state\n' "${STATIC_ROOT}"
  else
    printf '%s/.local/state/wavcse-worker\n' "${HOME}"
  fi
)"
readonly STATE_DIR
readonly VERSION_FILE="${STATE_DIR}/bootstrap-version"

temporary_directory=""
marker_temporary=""
cleanup() {
  if [[ -n "${temporary_directory}" ]]; then
    rm -rf -- "${temporary_directory}"
  fi
  if [[ -n "${marker_temporary}" ]]; then
    rm -f -- "${marker_temporary}"
  fi
}
trap cleanup EXIT

if [[ "${PROFILE}" == system ]]; then
  if ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg-query >/dev/null 2>&1; then
    printf 'Unsupported worker image: apt-get and dpkg-query are required. Use a supported Ubuntu-based RunPod image.\n' >&2
    exit 1
  fi

  root_command=()
  if ((EUID != 0)); then
    if ! command -v sudo >/dev/null 2>&1 || ! sudo -n true; then
      printf 'Worker bootstrap requires root or non-interactive sudo for package installation.\n' >&2
      exit 1
    fi
    root_command=(sudo -n)
  fi

  required_packages=(
    ca-certificates
    curl
    git
    gzip
    procps
    python3
    python3-venv
    tar
    tmux
    util-linux
  )
  missing_packages=()
  for package in "${required_packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q '^install ok installed$'; then
      missing_packages+=("${package}")
    fi
  done

  if ((${#missing_packages[@]} > 0)); then
    "${root_command[@]}" apt-get update
    "${root_command[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
      --no-install-recommends "${missing_packages[@]}"
  fi

  if ! command -v uv >/dev/null 2>&1; then
    temporary_directory="$(mktemp -d)"
    installer="${temporary_directory}/uv-installer.sh"
    curl --proto '=https' --tlsv1.2 -LsSf \
      "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "${installer}"
    "${root_command[@]}" env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 \
      sh "${installer}"
  fi

  "${root_command[@]}" install -d -m 0755 /workspace
else
  install -d -m 0700 -- "${STATIC_ROOT}"
fi

command -v git >/dev/null 2>&1
command -v python3 >/dev/null 2>&1
command -v curl >/dev/null 2>&1
command -v tar >/dev/null 2>&1
command -v gzip >/dev/null 2>&1
if [[ "${PROFILE}" == system ]]; then
  command -v uv >/dev/null 2>&1
fi

install -d -m 0700 "${STATE_DIR}"
marker_temporary="$(mktemp "${STATE_DIR}/.bootstrap-version.XXXXXX")"
printf '%s\n' "${BOOTSTRAP_VERSION}" >"${marker_temporary}"
chmod 0600 "${marker_temporary}"
mv -f -- "${marker_temporary}" "${VERSION_FILE}"
marker_temporary=""

printf 'wavcse_bootstrap_complete\t%s\n' "${BOOTSTRAP_VERSION}"

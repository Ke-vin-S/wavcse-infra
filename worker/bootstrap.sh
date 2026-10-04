#!/usr/bin/env bash
set -Eeuo pipefail

readonly BOOTSTRAP_VERSION="${1:?bootstrap version argument is required}"
readonly UV_VERSION="0.10.9"
readonly STATE_DIR="${HOME}/.local/state/wavcse-worker"
readonly VERSION_FILE="${STATE_DIR}/bootstrap-version"

if [[ ! "${BOOTSTRAP_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  printf 'Invalid bootstrap version: %s\n' "${BOOTSTRAP_VERSION}" >&2
  exit 2
fi

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

if ! command -v uv >/dev/null 2>&1; then
  temporary_directory="$(mktemp -d)"
  installer="${temporary_directory}/uv-installer.sh"
  curl --proto '=https' --tlsv1.2 -LsSf \
    "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "${installer}"
  "${root_command[@]}" env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 \
    sh "${installer}"
fi

command -v git >/dev/null 2>&1
command -v python3 >/dev/null 2>&1
command -v uv >/dev/null 2>&1
command -v curl >/dev/null 2>&1
command -v tar >/dev/null 2>&1
command -v gzip >/dev/null 2>&1

"${root_command[@]}" install -d -m 0755 /workspace
install -d -m 0700 "${STATE_DIR}"
marker_temporary="$(mktemp "${STATE_DIR}/.bootstrap-version.XXXXXX")"
printf '%s\n' "${BOOTSTRAP_VERSION}" >"${marker_temporary}"
chmod 0600 "${marker_temporary}"
mv -f -- "${marker_temporary}" "${VERSION_FILE}"
marker_temporary=""

printf 'wavcse_bootstrap_complete\t%s\n' "${BOOTSTRAP_VERSION}"

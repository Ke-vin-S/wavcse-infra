#!/usr/bin/env bash
set -Eeuo pipefail

readonly UV_VERSION="${WAVCSE_INFRA_UV_VERSION:-0.12.19}"
REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPOSITORY_ROOT
CONTROLLER_USER=''
CONTROLLER_HOME=''

fail() {
  printf 'Controller bootstrap failed: %s\n' "$*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  printf 'Controller bootstrap failed at line %s (exit %s).\n' "${BASH_LINENO[0]}" "${exit_code}" >&2
  exit "${exit_code}"
}

trap on_error ERR

controller_user() {
  if [[ -n "${WAVCSE_INFRA_CONTROLLER_USER:-}" ]]; then
    printf '%s\n' "${WAVCSE_INFRA_CONTROLLER_USER}"
  elif [[ "${EUID}" -ne 0 ]]; then
    id -un
  elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    printf '%s\n' "${SUDO_USER}"
  elif id ubuntu >/dev/null 2>&1; then
    printf 'ubuntu\n'
  else
    fail 'set WAVCSE_INFRA_CONTROLLER_USER when running as root outside a standard Ubuntu EC2 image'
  fi
}

run_as_controller() {
  if [[ "$(id -un)" == "${CONTROLLER_USER}" ]]; then
    "$@"
  else
    runuser --user "${CONTROLLER_USER}" -- "$@"
  fi
}

require_ubuntu() {
  [[ -r /etc/os-release ]] || fail '/etc/os-release is missing; only Ubuntu is supported'
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || fail "unsupported operating system '${ID:-unknown}'; use Ubuntu"
}

install_os_packages() {
  local -a elevate=()
  if [[ "${EUID}" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || fail 'sudo is required to install controller packages'
    elevate=(sudo)
  fi

  "${elevate[@]}" apt-get update
  "${elevate[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
    ca-certificates \
    curl \
    git \
    shellcheck \
    shfmt \
    tmux
}

install_uv() {
  local uv_bin="${CONTROLLER_HOME}/.local/bin/uv"
  local installed_version=''
  if [[ -x "${uv_bin}" ]]; then
    installed_version="$("${uv_bin}" --version | awk '{print $2}')"
  fi
  if [[ "${installed_version}" == "${UV_VERSION}" ]]; then
    printf 'uv %s is already installed.\n' "${UV_VERSION}"
    return
  fi

  local installer
  installer="$(mktemp)"
  if ! curl --fail --location --proto '=https' --tlsv1.2 --silent --show-error \
    "https://astral.sh/uv/${UV_VERSION}/install.sh" --output "${installer}"; then
    rm -f -- "${installer}"
    fail "could not download the uv ${UV_VERSION} installer"
  fi
  chmod 0644 "${installer}"
  if ! run_as_controller env UV_UNMANAGED_INSTALL="${CONTROLLER_HOME}/.local/bin" \
    sh "${installer}"; then
    rm -f -- "${installer}"
    fail "uv ${UV_VERSION} installation failed"
  fi
  rm -f -- "${installer}"
  [[ -x "${uv_bin}" ]] || fail "uv installer did not create ${uv_bin}"
}

sync_project() {
  local uv_bin="${CONTROLLER_HOME}/.local/bin/uv"
  run_as_controller "${uv_bin}" python install 3.12
  run_as_controller "${uv_bin}" sync --locked --all-groups --project "${REPOSITORY_ROOT}"
  run_as_controller mkdir -p "${CONTROLLER_HOME}/.local/bin"
  run_as_controller ln -sfn "${REPOSITORY_ROOT}/.venv/bin/infra" \
    "${CONTROLLER_HOME}/.local/bin/infra"
}

main() {
  require_ubuntu

  CONTROLLER_USER="$(controller_user)"
  readonly CONTROLLER_USER
  id "${CONTROLLER_USER}" >/dev/null 2>&1 || fail "controller user does not exist: ${CONTROLLER_USER}"
  CONTROLLER_HOME="$(getent passwd "${CONTROLLER_USER}" | cut -d: -f6)"
  readonly CONTROLLER_HOME
  [[ -n "${CONTROLLER_HOME}" && -d "${CONTROLLER_HOME}" ]] ||
    fail "could not determine home directory for ${CONTROLLER_USER}"

  printf 'Bootstrapping controller for %s from %s\n' "${CONTROLLER_USER}" "${REPOSITORY_ROOT}"
  install_os_packages
  install_uv
  sync_project

  printf 'Controller bootstrap complete.\n'
  printf 'Next: configure ~/.config/wavcse-infra/config.toml and RUNPOD_API_KEY, then run infra doctor.\n'
}

main "$@"

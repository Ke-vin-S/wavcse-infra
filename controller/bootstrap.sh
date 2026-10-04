#!/usr/bin/env bash
set -Eeuo pipefail

readonly UV_VERSION="${WAVCSE_INFRA_UV_VERSION:-0.12.19}"
# Pinned Google Colab CLI. Bootstrap installs the tool only; it never authenticates
# and never requests compute. The pinned CLI defaults to the interactive oauth2
# provider, so every invocation must pass this project's explicit `--auth=adc`.
readonly COLAB_CLI_VERSION='0.7.4'
readonly COLAB_ADC_SCOPES='openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory'
REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPOSITORY_ROOT
readonly INSTALL_AGENTS_SCRIPT="${REPOSITORY_ROOT}/controller/install-agents.sh"
CONTROLLER_USER=''
CONTROLLER_HOME=''
SKIP_AGENTS=false

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

usage() {
  cat <<'EOF'
Usage: ./controller/bootstrap.sh [--skip-agents]

Bootstrap the controller, install controller-only agent tools by default, and
install the pinned Google Colab CLI. No authentication or paid resource request
is performed.
EOF
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
    --skip-agents)
      SKIP_AGENTS=true
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
    esac
  done
}

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

install_colab_cli() {
  local uv_bin="${CONTROLLER_HOME}/.local/bin/uv"
  local colab_bin="${CONTROLLER_HOME}/.local/bin/colab"
  local installed_version=''
  if [[ -x "${colab_bin}" ]]; then
    installed_version="$("${colab_bin}" version 2>/dev/null | awk '{print $2}')" ||
      installed_version=''
  fi

  if [[ "${installed_version}" == "${COLAB_CLI_VERSION}" ]]; then
    printf 'Google Colab CLI %s is already installed.\n' "${COLAB_CLI_VERSION}"
  else
    printf 'Installing pinned Google Colab CLI %s.\n' "${COLAB_CLI_VERSION}"
    run_as_controller "${uv_bin}" tool install --force \
      "google-colab-cli==${COLAB_CLI_VERSION}"
  fi
  [[ -x "${colab_bin}" ]] ||
    fail "uv tool install reported success but ${colab_bin} is not executable"
  installed_version="$("${colab_bin}" version 2>/dev/null | awk '{print $2}')" ||
    fail "could not query the installed Colab CLI version"
  [[ "${installed_version}" == "${COLAB_CLI_VERSION}" ]] ||
    fail "expected Colab CLI ${COLAB_CLI_VERSION}, observed ${installed_version:-unknown}"

  printf 'Google Colab CLI is installed but not authenticated; bootstrap performs no login.\n'
  printf 'For headless ADC, run this once as %s, then verify with: %s --auth=adc sessions\n' \
    "${CONTROLLER_USER}" "${colab_bin}"
  printf '  gcloud auth application-default login --scopes=%s\n' "${COLAB_ADC_SCOPES}"
  printf 'Always pass --auth=adc: this CLI defaults to the interactive oauth2 provider.\n'
}

install_agent_tools() {
  if [[ "${SKIP_AGENTS}" == true ]]; then
    printf 'Skipping controller agent tools (--skip-agents).\n'
    return
  fi
  [[ -x "${INSTALL_AGENTS_SCRIPT}" ]] ||
    fail "agent installer is missing or not executable: ${INSTALL_AGENTS_SCRIPT}"
  env WAVCSE_INFRA_CONTROLLER_USER="${CONTROLLER_USER}" "${INSTALL_AGENTS_SCRIPT}"
}

install_omp_overlay() {
  local script="${REPOSITORY_ROOT}/controller/omp-overlay.sh"
  if [[ "${SKIP_AGENTS}" == true ]]; then
    return
  fi
  [[ -x "${script}" ]] || fail "OMP overlay script is missing: ${script}"
  # The wavCSE checkout may not exist yet on a fresh controller, and the overlay
  # is a convenience for research sessions, so this must never fail a bootstrap.
  if ! env WAVCSE_INFRA_CONTROLLER_USER="${CONTROLLER_USER}" "${script}"; then
    printf 'OMP overlay not configured; run %s once the wavCSE checkout exists.\n' \
      "${script}"
  fi
}

ensure_user_config() {
  local config_directory="${CONTROLLER_HOME}/.config/wavcse-infra"
  local config_file="${config_directory}/config.toml"
  local example_config="${REPOSITORY_ROOT}/config/infra.example.toml"

  [[ -r "${example_config}" ]] || fail "example configuration is not readable: ${example_config}"
  run_as_controller install -d -m 0700 -- "${config_directory}"

  if [[ -e "${config_file}" || -L "${config_file}" ]]; then
    printf 'Preserving existing controller configuration: %s\n' "${config_file}"
    return
  fi

  # Positional parameters expand inside the child Bash process.
  # shellcheck disable=SC2016
  if run_as_controller bash -c \
    'set -o noclobber; umask 077; cat -- "$1" > "$2"' \
    bootstrap-config-copy "${example_config}" "${config_file}"; then
    printf 'Created controller configuration: %s\n' "${config_file}"
    printf 'Next: edit %s and replace template values before running infra doctor.\n' \
      "${config_file}"
  elif [[ -e "${config_file}" || -L "${config_file}" ]]; then
    printf 'Preserving controller configuration created concurrently: %s\n' "${config_file}"
  else
    fail "could not create controller configuration: ${config_file}"
  fi
}

install_app_config() {
  local script="${REPOSITORY_ROOT}/controller/app-config.sh"
  [[ -x "${script}" ]] || fail "app config script is missing: ${script}"
  env WAVCSE_INFRA_CONTROLLER_USER="${CONTROLLER_USER}" "${script}" --preserve-existing
}

main() {
  parse_args "$@"
  require_ubuntu

  CONTROLLER_USER="$(controller_user)"
  readonly CONTROLLER_USER
  id "${CONTROLLER_USER}" >/dev/null 2>&1 || fail "controller user does not exist: ${CONTROLLER_USER}"
  CONTROLLER_HOME="$(getent passwd "${CONTROLLER_USER}" | cut -d: -f6)"
  readonly CONTROLLER_HOME
  [[ -n "${CONTROLLER_HOME}" && -d "${CONTROLLER_HOME}" ]] ||
    fail "could not determine home directory for ${CONTROLLER_USER}"

  printf 'Bootstrapping controller for %s from %s\n' "${CONTROLLER_USER}" "${REPOSITORY_ROOT}"
  ensure_user_config
  install_app_config
  install_os_packages
  install_uv
  sync_project
  install_colab_cli
  install_agent_tools
  install_omp_overlay

  printf 'Controller bootstrap complete.\n'
  printf 'Next: configure the controller and authenticate agent providers, then run infra doctor.\n'
  printf 'If you will use Colab workers, run the printed ADC login as %s first.\n' \
    "${CONTROLLER_USER}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi

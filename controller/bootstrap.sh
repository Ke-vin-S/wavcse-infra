#!/usr/bin/env bash
set -Eeuo pipefail

readonly UV_VERSION="${WAVCSE_INFRA_UV_VERSION:-0.12.19}"
# Pinned review UI for the operator: a terminal Git client for reading agent commits and
# working-tree changes on the controller. Bootstrap installs the binary only and never
# authenticates, downloads a repository, or writes to a checkout. The release archive is
# accepted only when its SHA-256 matches the digest pinned for the detected architecture,
# and the mirrored settings disable in-binary self-update so an upgrade stays a reviewed
# repository change.
readonly DEFAULT_LAZYGIT_VERSION='0.66.0'
readonly DEFAULT_LAZYGIT_X86_64_SHA256='5b45541155d20bd32bf2cc5ab5b7e3d91c2eebf0fb1242281350edc27d59d2b7'
readonly DEFAULT_LAZYGIT_AARCH64_SHA256='9a4fc4656897ac9f7877b835473ce1a75620cc267f554c57fc4ff266407f3257'
readonly LAZYGIT_VERSION="${WAVCSE_INFRA_LAZYGIT_VERSION:-${DEFAULT_LAZYGIT_VERSION}}"
# Terminal locale for interactive controller sessions: box drawing, Nerd Font icons, and
# rendered diffs are multi-byte, and a C/POSIX locale corrupts all three.
readonly CONTROLLER_LOCALE='en_US.UTF-8'
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

Bootstrap the controller, install the pinned review UI and the controller-only
agent tools by default, and install the pinned Google Colab CLI. No
authentication or paid resource request is performed.

The review UI (lazygit) and its diff renderer (git-delta) are operator tools:
--skip-agents does not skip them.
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
    git-delta \
    locales \
    shellcheck \
    shfmt \
    tmux
}

# A UTF-8 locale is a correctness requirement for the interactive review UI, not a
# preference: a C/POSIX locale makes box drawing, Nerd Font icon code points, and
# rendered diffs unusable. /etc/default/locale is what PAM applies to login sessions,
# which is where an SSH review session starts. An existing UTF-8 value is preserved.
configure_locale() {
  local current=''
  if [[ -r /etc/default/locale ]]; then
    current="$(sed -n 's/^LANG=//p' /etc/default/locale | tail -n 1)"
    current="${current%\"}"
    current="${current#\"}"
  fi

  if [[ "${current}" == *.[Uu][Tt][Ff]-8 ]]; then
    printf 'Controller locale is already UTF-8: LANG=%s\n' "${current}"
    return 0
  fi

  local -a elevate=()
  if [[ "${EUID}" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || fail 'sudo is required to configure the controller locale'
    elevate=(sudo)
  fi

  "${elevate[@]}" locale-gen "${CONTROLLER_LOCALE}" ||
    fail "could not generate the locale ${CONTROLLER_LOCALE}"
  "${elevate[@]}" update-locale "LANG=${CONTROLLER_LOCALE}" ||
    fail "could not select the locale ${CONTROLLER_LOCALE}"
  printf 'Selected UTF-8 locale %s for login sessions (was %s); reconnect to pick it up.\n' \
    "${CONTROLLER_LOCALE}" "${current:-unset}"
}

configure_git() {
  local user_name="${WAVCSE_INFRA_GIT_USER_NAME:-}"
  local user_email="${WAVCSE_INFRA_GIT_USER_EMAIL:-}"

  if [[ -z "${user_name}" && -z "${user_email}" ]]; then
    printf 'Git identity not configured; set WAVCSE_INFRA_GIT_USER_NAME and\n'
    printf 'WAVCSE_INFRA_GIT_USER_EMAIL for %s, or run git config --global user.name ' \
      "${CONTROLLER_USER}"
    printf 'and user.email yourself.\n'
    return 0
  fi
  [[ -n "${user_name}" && -n "${user_email}" ]] ||
    fail 'set both WAVCSE_INFRA_GIT_USER_NAME and WAVCSE_INFRA_GIT_USER_EMAIL, or neither'
  [[ -n "${user_name//[[:space:]]/}" ]] || fail 'WAVCSE_INFRA_GIT_USER_NAME must not be blank'
  [[ "${user_email}" =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] ||
    fail "invalid WAVCSE_INFRA_GIT_USER_EMAIL: ${user_email}"
  command -v git >/dev/null 2>&1 || fail 'git is required to configure the controller identity'

  local current_name
  local current_email
  current_name="$(run_as_controller env HOME="${CONTROLLER_HOME}" \
    git config --global --get user.name || true)"
  current_email="$(run_as_controller env HOME="${CONTROLLER_HOME}" \
    git config --global --get user.email || true)"

  if [[ "${current_name}" == "${user_name}" && "${current_email}" == "${user_email}" ]]; then
    printf 'Git identity already configured: %s <%s>\n' "${user_name}" "${user_email}"
    return 0
  fi

  # The identity is supplied deliberately for this controller, so an operator-provided
  # value wins, but the previous one is printed rather than replaced silently.
  if [[ -n "${current_name}" || -n "${current_email}" ]]; then
    printf 'Replacing Git identity %s <%s> with %s <%s>\n' \
      "${current_name:-unset}" "${current_email:-unset}" "${user_name}" "${user_email}"
  fi
  run_as_controller env HOME="${CONTROLLER_HOME}" git config --global user.name "${user_name}"
  run_as_controller env HOME="${CONTROLLER_HOME}" git config --global user.email "${user_email}"
  printf 'Configured Git identity for %s: %s <%s>\n' \
    "${CONTROLLER_USER}" "${user_name}" "${user_email}"
}

# Emit "<release archive>\t<expected sha256>" for the detected architecture. The binary
# is read from a release asset whose name embeds the pinned version, and the digest is
# the one published in that release's checksums.txt.
lazygit_release_details() {
  local architecture
  architecture="$(uname -m)"
  case "${architecture}" in
  x86_64 | amd64)
    printf 'lazygit_%s_linux_x86_64.tar.gz\t%s\n' "${LAZYGIT_VERSION}" \
      "${WAVCSE_INFRA_LAZYGIT_X86_64_SHA256:-${DEFAULT_LAZYGIT_X86_64_SHA256}}"
    ;;
  aarch64 | arm64)
    printf 'lazygit_%s_linux_arm64.tar.gz\t%s\n' "${LAZYGIT_VERSION}" \
      "${WAVCSE_INFRA_LAZYGIT_AARCH64_SHA256:-${DEFAULT_LAZYGIT_AARCH64_SHA256}}"
    ;;
  *) fail "lazygit has no configured release archive for architecture ${architecture}" ;;
  esac
}

# An overridden version is only accepted with an explicit digest for this architecture:
# a pin without a verified digest is not a pin.
validate_lazygit_version_override() {
  [[ "${LAZYGIT_VERSION}" == "${DEFAULT_LAZYGIT_VERSION}" ]] && return 0
  case "$(uname -m)" in
  x86_64 | amd64)
    [[ -n "${WAVCSE_INFRA_LAZYGIT_X86_64_SHA256:-}" ]] ||
      fail 'set WAVCSE_INFRA_LAZYGIT_X86_64_SHA256 when overriding WAVCSE_INFRA_LAZYGIT_VERSION'
    ;;
  aarch64 | arm64)
    [[ -n "${WAVCSE_INFRA_LAZYGIT_AARCH64_SHA256:-}" ]] ||
      fail 'set WAVCSE_INFRA_LAZYGIT_AARCH64_SHA256 when overriding WAVCSE_INFRA_LAZYGIT_VERSION'
    ;;
  esac
}

# `lazygit --version` prints one line of comma-separated build fields, e.g.
# "commit=..., build date=..., version=0.66.0, os=linux, ...". Split on commas so the
# trailing "git version=..." field can never be mistaken for the tool's own version.
lazygit_installed_version() {
  local binary=$1 output=''
  [[ -x "${binary}" ]] || return 0
  output="$(run_as_controller env HOME="${CONTROLLER_HOME}" "${binary}" --version 2>/dev/null)" ||
    output=''
  printf '%s\n' "${output}" | tr ',' '\n' | sed -n 's/^ *version=//p' | head -n 1
}

install_lazygit() {
  local lazygit_bin="${CONTROLLER_HOME}/.local/bin/lazygit"
  local installed_version
  installed_version="$(lazygit_installed_version "${lazygit_bin}")"
  if [[ "${installed_version}" == "${LAZYGIT_VERSION}" ]]; then
    printf 'lazygit %s is already installed.\n' "${LAZYGIT_VERSION}"
    return 0
  fi
  validate_lazygit_version_override
  command -v sha256sum >/dev/null 2>&1 || fail 'sha256sum is required to verify the lazygit archive'

  local asset expected_sha256 archive actual_sha256 url temporary_directory
  IFS=$'\t' read -r asset expected_sha256 < <(lazygit_release_details)
  url="https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/${asset}"
  printf 'Installing lazygit %s from jesseduffield/lazygit.\n' "${LAZYGIT_VERSION}"
  temporary_directory="$(run_as_controller env HOME="${CONTROLLER_HOME}" mktemp -d)"
  archive="${temporary_directory}/${asset}"

  if ! run_as_controller env HOME="${CONTROLLER_HOME}" curl --fail --location \
    --proto '=https' --tlsv1.2 --silent --show-error "${url}" --output "${archive}"; then
    run_as_controller rm -rf -- "${temporary_directory}"
    fail "could not download the official lazygit archive from ${url}"
  fi
  actual_sha256="$(
    run_as_controller env HOME="${CONTROLLER_HOME}" sha256sum "${archive}" | awk '{print $1}'
  )"
  if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
    run_as_controller rm -rf -- "${temporary_directory}"
    fail "lazygit archive checksum mismatch (expected ${expected_sha256}, got ${actual_sha256})"
  fi
  if ! run_as_controller env HOME="${CONTROLLER_HOME}" tar --extract --gzip \
    --file "${archive}" --directory "${temporary_directory}" lazygit; then
    run_as_controller rm -rf -- "${temporary_directory}"
    fail 'lazygit archive did not contain an extractable lazygit binary'
  fi
  run_as_controller env HOME="${CONTROLLER_HOME}" install -D -m 0755 -- \
    "${temporary_directory}/lazygit" "${lazygit_bin}"
  run_as_controller rm -rf -- "${temporary_directory}"

  installed_version="$(lazygit_installed_version "${lazygit_bin}")"
  [[ "${installed_version}" == "${LAZYGIT_VERSION}" ]] ||
    fail "expected lazygit ${LAZYGIT_VERSION}, observed ${installed_version:-unknown}"
  printf 'Review UI installed for %s: %s\n' "${CONTROLLER_USER}" "${lazygit_bin}"
  printf 'Its settings are mirrored from apps/lazygit/config.yml; apply them with make app-config.\n'
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
  configure_locale
  configure_git
  install_lazygit
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

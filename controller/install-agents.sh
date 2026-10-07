#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEFAULT_OMP_VERSION='v18.8.0'
readonly DEFAULT_OMP_X86_64_SHA256='6d0bd5d624f96b42513859558bc001d523a1eada1d71446ab7ca743ec2aa7de1'
readonly DEFAULT_OMP_AARCH64_SHA256='72b563479eac18fe60605e5374628ec3200b3878a32a2f5bf76591d4a5347409'
readonly DEFAULT_CODEX_VERSION='0.157.1'
readonly DEFAULT_AGF_VERSION='v0.15.1'
readonly DEFAULT_AGF_X86_64_SHA256='db21c06f0a288f832879828278303cafa6d8f1e967751ee7da9962a3c0c0aeeb'
readonly DEFAULT_AGF_AARCH64_SHA256='671f34411c49806ccc0e9f580f0c3205f6693d8e893dc114872107c60ff2645e'
readonly DEFAULT_OPENCODE_VERSION='1.18.35'
readonly DEFAULT_OPENCODE_X86_64_SHA256='c8f888b451f5494a18f858fffb0e0b68f4e4baa9c241761c5f206884f0fa640d'
readonly DEFAULT_OPENCODE_X86_64_BASELINE_SHA256='90c97d4a24d36437bce36f27195c9cd2e0b70ed037a04d1c6a6740eb246c2a73'
readonly DEFAULT_OPENCODE_AARCH64_SHA256='f7f2ba59ee8aa94d388f9696575a32d20e71c2ee48def9f80fc693a60fec6c72'

readonly OMP_VERSION="${WAVCSE_INFRA_OMP_VERSION:-${DEFAULT_OMP_VERSION}}"
readonly CODEX_VERSION="${WAVCSE_INFRA_CODEX_VERSION:-${DEFAULT_CODEX_VERSION}}"
readonly AGF_VERSION="${WAVCSE_INFRA_AGF_VERSION:-${DEFAULT_AGF_VERSION}}"
readonly OPENCODE_VERSION="${WAVCSE_INFRA_OPENCODE_VERSION:-${DEFAULT_OPENCODE_VERSION}}"

CONTROLLER_USER=''
CONTROLLER_HOME=''
CONTROLLER_PATH=''
CONTROLLER_LOGIN_SHELL=''
ONLY_TOOL=''
UPGRADE=false

declare -A INSTALL_STATUS=()

fail() {
  printf 'Controller agent installation failed: %s\n' "$*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  printf 'Controller agent installation failed at line %s (exit %s).\n' \
    "${BASH_LINENO[0]}" "${exit_code}" >&2
  exit "${exit_code}"
}

usage() {
  cat <<'EOF'
Usage: ./controller/install-agents.sh [--only omp|codex|agf|opencode] [--upgrade]

Install the pinned controller agent tools. Existing commands are preserved unless
--upgrade is supplied. Installation never performs provider authentication.
EOF
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
    --only)
      (($# >= 2)) || fail '--only requires omp, codex, agf, or opencode'
      [[ -z "${ONLY_TOOL}" ]] || fail '--only may be supplied once'
      case "$2" in
      omp | codex | agf | opencode) ONLY_TOOL="$2" ;;
      *) fail "unsupported tool for --only: $2" ;;
      esac
      shift 2
      ;;
    --upgrade)
      UPGRADE=true
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
    esac
  done
}

controller_user() {
  if [[ -n "${WAVCSE_INFRA_CONTROLLER_USER:-}" ]]; then
    printf '%s\n' "${WAVCSE_INFRA_CONTROLLER_USER}"
  elif [[ "${EUID}" -ne 0 ]]; then
    id -un
  elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != 'root' ]]; then
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
    command -v runuser >/dev/null 2>&1 || fail 'runuser is required when installing as another user'
    runuser --user "${CONTROLLER_USER}" -- "$@"
  fi
}

run_controller() {
  run_as_controller env \
    HOME="${CONTROLLER_HOME}" \
    PATH="${CONTROLLER_PATH}" \
    "$@"
}

require_ubuntu() {
  [[ -r /etc/os-release ]] || fail '/etc/os-release is missing; only Ubuntu is supported'
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ "${ID:-}" == 'ubuntu' ]] || fail "unsupported operating system '${ID:-unknown}'; use Ubuntu"
}

install_apt_packages() {
  local -a elevate=()
  if [[ "${EUID}" -ne 0 ]]; then
    command -v sudo >/dev/null 2>&1 || fail 'sudo is required to install controller prerequisites'
    elevate=(sudo)
  fi

  "${elevate[@]}" apt-get update
  "${elevate[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install \
    --yes --no-install-recommends "$@"
}

ensure_download_prerequisites() {
  local -a packages=()
  if ! dpkg-query --show --showformat='${db:Status-Abbrev}' ca-certificates 2>/dev/null |
    grep -q '^ii '; then
    packages+=(ca-certificates)
  fi
  command -v curl >/dev/null 2>&1 || packages+=(curl)
  if { selected codex || selected agf || selected opencode; } && ! command -v tar >/dev/null 2>&1; then
    packages+=(tar)
  fi
  command -v sha256sum >/dev/null 2>&1 || packages+=(coreutils)
  if ((${#packages[@]} > 0)); then
    printf 'Installing missing download prerequisites: %s\n' "${packages[*]}"
    install_apt_packages "${packages[@]}"
  fi
}

profile_path() {
  case "${CONTROLLER_LOGIN_SHELL##*/}" in
  zsh) printf '%s/.zprofile\n' "${CONTROLLER_HOME}" ;;
  bash)
    if [[ -e "${CONTROLLER_HOME}/.bash_profile" ]]; then
      printf '%s/.bash_profile\n' "${CONTROLLER_HOME}"
    elif [[ -e "${CONTROLLER_HOME}/.bash_login" ]]; then
      printf '%s/.bash_login\n' "${CONTROLLER_HOME}"
    else
      printf '%s/.profile\n' "${CONTROLLER_HOME}"
    fi
    ;;
  *) printf '%s/.profile\n' "${CONTROLLER_HOME}" ;;
  esac
}

ensure_path_configuration() {
  local profile
  local begin_marker='# >>> wavcse-infra controller tools >>>'
  local end_marker='# <<< wavcse-infra controller tools <<<'
  profile="$(profile_path)"

  run_controller touch -- "${profile}"
  if run_controller grep -Fqx -- "${begin_marker}" "${profile}"; then
    run_controller grep -Fqx -- "${end_marker}" "${profile}" ||
      fail "incomplete wavcse-infra PATH block in ${profile}"
    printf 'Controller tool PATH is already configured in %s.\n' "${profile}"
    return
  fi
  if run_controller grep -Fqx -- "${end_marker}" "${profile}"; then
    fail "incomplete wavcse-infra PATH block in ${profile}"
  fi

  # The managed block must expand HOME and PATH only when the login shell reads it.
  # shellcheck disable=SC2016
  run_controller bash -c '
    printf "\n%s\n" "$2" >>"$1"
    printf "%s\n" '\''case ":${PATH}:" in'\'' >>"$1"
    printf "%s\n" '\''  *:"${HOME}/.cargo/bin":*) ;;'\'' >>"$1"
    printf "%s\n" '\''  *) PATH="${HOME}/.cargo/bin:${PATH}" ;;'\'' >>"$1"
    printf "%s\n" '\''esac'\'' >>"$1"
    printf "%s\n" '\''case ":${PATH}:" in'\'' >>"$1"
    printf "%s\n" '\''  *:"${HOME}/.bun/bin":*) ;;'\'' >>"$1"
    printf "%s\n" '\''  *) PATH="${HOME}/.bun/bin:${PATH}" ;;'\'' >>"$1"
    printf "%s\n" '\''esac'\'' >>"$1"
    printf "%s\n" '\''case ":${PATH}:" in'\'' >>"$1"
    printf "%s\n" '\''  *:"${HOME}/.local/bin":*) ;;'\'' >>"$1"
    printf "%s\n" '\''  *) PATH="${HOME}/.local/bin:${PATH}" ;;'\'' >>"$1"
    printf "%s\n" '\''esac'\'' >>"$1"
    printf "%s\n" '\''export PATH'\'' >>"$1"
    printf "%s\n" "$3" >>"$1"
  ' configure-agent-path "${profile}" "${begin_marker}" "${end_marker}"
  printf 'Configured controller tool PATH in %s.\n' "${profile}"
}

find_tool() {
  local tool="$1"
  # Positional parameters expand inside the child Bash process.
  # shellcheck disable=SC2016
  run_controller bash -c 'command -v -- "$1"' find-controller-tool "${tool}" 2>/dev/null
}

selected() {
  [[ -z "${ONLY_TOOL}" || "${ONLY_TOOL}" == "$1" ]]
}

installation_required() {
  [[ "${UPGRADE}" == true ]] && return 0
  if selected omp && ! tool_is_installed omp; then
    return 0
  fi
  if selected codex && ! tool_is_installed codex; then
    return 0
  fi
  if selected agf && ! tool_is_installed agf; then
    return 0
  fi
  if selected opencode && ! tool_is_installed opencode; then
    return 0
  fi
  return 1
}

tool_is_installed() {
  local path
  path="$(find_tool "$1" || true)"
  [[ -n "${path}" && -x "${path}" ]]
}

should_install() {
  local display_name="$1"
  local command_name="$2"
  local path
  path="$(find_tool "${command_name}" || true)"
  if [[ -n "${path}" && -x "${path}" && "${UPGRADE}" == false ]]; then
    printf '%s is already installed at %s; preserving it.\n' "${display_name}" "${path}"
    INSTALL_STATUS["${command_name}"]='already installed'
    return 1
  fi
  if [[ -n "${path}" && "${UPGRADE}" == true ]]; then
    printf 'Upgrade requested for %s (currently %s).\n' "${display_name}" "${path}"
  fi
  return 0
}

download_installer() {
  local url="$1"
  local label="$2"
  local installer
  installer="$(run_controller mktemp)"
  if ! run_controller curl --fail --location --proto '=https' --tlsv1.2 \
    --silent --show-error "${url}" --output "${installer}"; then
    run_controller rm -f -- "${installer}"
    fail "could not download the official ${label} installer from ${url}"
  fi
  run_controller chmod 0600 -- "${installer}"
  printf '%s\n' "${installer}"
}

omp_expected_sha256() {
  case "$(uname -m)" in
  x86_64 | amd64)
    printf '%s\n' "${WAVCSE_INFRA_OMP_X86_64_SHA256:-${DEFAULT_OMP_X86_64_SHA256}}"
    ;;
  aarch64 | arm64)
    printf '%s\n' "${WAVCSE_INFRA_OMP_AARCH64_SHA256:-${DEFAULT_OMP_AARCH64_SHA256}}"
    ;;
  *) fail "OMP has no configured release binary for architecture $(uname -m)" ;;
  esac
}

validate_omp_version_override() {
  [[ "${OMP_VERSION}" == "${DEFAULT_OMP_VERSION}" ]] && return
  case "$(uname -m)" in
  x86_64 | amd64)
    [[ -n "${WAVCSE_INFRA_OMP_X86_64_SHA256:-}" ]] ||
      fail 'set WAVCSE_INFRA_OMP_X86_64_SHA256 when overriding WAVCSE_INFRA_OMP_VERSION'
    ;;
  aarch64 | arm64)
    [[ -n "${WAVCSE_INFRA_OMP_AARCH64_SHA256:-}" ]] ||
      fail 'set WAVCSE_INFRA_OMP_AARCH64_SHA256 when overriding WAVCSE_INFRA_OMP_VERSION'
    ;;
  esac
}

install_omp() {
  local installer
  local installer_url="https://raw.githubusercontent.com/can1357/oh-my-pi/${OMP_VERSION}/scripts/install.sh"
  local temporary_directory
  local expected_sha256
  local actual_sha256
  if ! should_install 'OMP' omp; then
    return 0
  fi
  validate_omp_version_override

  printf 'Installing OMP %s from can1357/oh-my-pi.\n' "${OMP_VERSION}"
  installer="$(download_installer "${installer_url}" 'OMP')"
  temporary_directory="$(run_controller mktemp -d)"
  if ! run_controller env PI_INSTALL_DIR="${temporary_directory}" \
    sh "${installer}" --binary --ref "${OMP_VERSION}"; then
    run_controller rm -f -- "${installer}"
    run_controller rm -rf -- "${temporary_directory}"
    fail "OMP ${OMP_VERSION} installation failed"
  fi
  run_controller rm -f -- "${installer}"
  expected_sha256="$(omp_expected_sha256)"
  actual_sha256="$(run_controller sha256sum "${temporary_directory}/omp" | awk '{print $1}')"
  if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
    run_controller rm -rf -- "${temporary_directory}"
    fail "OMP binary checksum mismatch (expected ${expected_sha256}, got ${actual_sha256})"
  fi
  run_controller install -m 0755 -- "${temporary_directory}/omp" \
    "${CONTROLLER_HOME}/.local/bin/omp"
  run_controller rm -rf -- "${temporary_directory}"
  tool_is_installed omp || fail 'OMP installer completed but omp was not found on PATH'
  INSTALL_STATUS[omp]='installed'
}

install_codex() {
  local installer
  if ! should_install 'Codex' codex; then
    return 0
  fi

  printf 'Installing Codex CLI %s from OpenAI.\n' "${CODEX_VERSION}"
  installer="$(download_installer 'https://chatgpt.com/codex/install.sh' 'Codex')"
  if ! run_controller env \
    CODEX_INSTALL_DIR="${CONTROLLER_HOME}/.local/bin" \
    CODEX_NON_INTERACTIVE=true \
    sh "${installer}" --release "${CODEX_VERSION}"; then
    run_controller rm -f -- "${installer}"
    fail "Codex CLI ${CODEX_VERSION} installation failed"
  fi
  run_controller rm -f -- "${installer}"
  tool_is_installed codex || fail 'Codex installer completed but codex was not found on PATH'
  INSTALL_STATUS[codex]='installed'
}

agf_release_details() {
  local architecture
  architecture="$(uname -m)"
  case "${architecture}" in
  x86_64 | amd64)
    printf 'agf-x86_64-unknown-linux-gnu.tar.gz\t%s\n' \
      "${WAVCSE_INFRA_AGF_X86_64_SHA256:-${DEFAULT_AGF_X86_64_SHA256}}"
    ;;
  aarch64 | arm64)
    printf 'agf-aarch64-unknown-linux-gnu.tar.gz\t%s\n' \
      "${WAVCSE_INFRA_AGF_AARCH64_SHA256:-${DEFAULT_AGF_AARCH64_SHA256}}"
    ;;
  *) fail "AGF has no configured release archive for architecture ${architecture}" ;;
  esac
}

validate_agf_version_override() {
  [[ "${AGF_VERSION}" == "${DEFAULT_AGF_VERSION}" ]] && return
  case "$(uname -m)" in
  x86_64 | amd64)
    [[ -n "${WAVCSE_INFRA_AGF_X86_64_SHA256:-}" ]] ||
      fail 'set WAVCSE_INFRA_AGF_X86_64_SHA256 when overriding WAVCSE_INFRA_AGF_VERSION'
    ;;
  aarch64 | arm64)
    [[ -n "${WAVCSE_INFRA_AGF_AARCH64_SHA256:-}" ]] ||
      fail 'set WAVCSE_INFRA_AGF_AARCH64_SHA256 when overriding WAVCSE_INFRA_AGF_VERSION'
    ;;
  esac
}

install_agf() {
  local archive_name
  local expected_sha256
  local temporary_directory
  local archive
  local actual_sha256
  local extracted_binary
  local url
  if ! should_install 'AGF' agf; then
    return 0
  fi
  validate_agf_version_override

  IFS=$'\t' read -r archive_name expected_sha256 < <(agf_release_details)
  url="https://github.com/subinium/agf/releases/download/${AGF_VERSION}/${archive_name}"
  temporary_directory="$(run_controller mktemp -d)"
  archive="${temporary_directory}/${archive_name}"
  printf 'Installing AGF %s from subinium/agf.\n' "${AGF_VERSION}"

  if ! run_controller curl --fail --location --proto '=https' --tlsv1.2 \
    --silent --show-error "${url}" --output "${archive}"; then
    run_controller rm -rf -- "${temporary_directory}"
    fail "could not download the official AGF archive from ${url}"
  fi
  actual_sha256="$(run_controller sha256sum "${archive}" | awk '{print $1}')"
  if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
    run_controller rm -rf -- "${temporary_directory}"
    fail "AGF archive checksum mismatch (expected ${expected_sha256}, got ${actual_sha256})"
  fi

  run_controller tar -xzf "${archive}" -C "${temporary_directory}"
  extracted_binary="$(run_controller find "${temporary_directory}" -type f -name agf -print -quit)"
  if [[ -z "${extracted_binary}" ]]; then
    run_controller rm -rf -- "${temporary_directory}"
    fail "AGF archive did not contain an agf binary"
  fi
  run_controller install -D -m 0755 -- "${extracted_binary}" \
    "${CONTROLLER_HOME}/.local/bin/agf"
  run_controller rm -rf -- "${temporary_directory}"
  tool_is_installed agf || fail 'AGF archive was installed but agf was not found on PATH'
  INSTALL_STATUS[agf]='installed'
}

opencode_release_details() {
  local architecture
  architecture="$(uname -m)"
  case "${architecture}" in
  x86_64 | amd64)
    if [[ -r /proc/cpuinfo ]] && ! grep -qwi avx2 /proc/cpuinfo; then
      printf 'opencode-linux-x64-baseline.tar.gz\t%s\n' \
        "${WAVCSE_INFRA_OPENCODE_X86_64_BASELINE_SHA256:-${DEFAULT_OPENCODE_X86_64_BASELINE_SHA256}}"
    else
      printf 'opencode-linux-x64.tar.gz\t%s\n' \
        "${WAVCSE_INFRA_OPENCODE_X86_64_SHA256:-${DEFAULT_OPENCODE_X86_64_SHA256}}"
    fi
    ;;
  aarch64 | arm64)
    printf 'opencode-linux-arm64.tar.gz\t%s\n' \
      "${WAVCSE_INFRA_OPENCODE_AARCH64_SHA256:-${DEFAULT_OPENCODE_AARCH64_SHA256}}"
    ;;
  *) fail "OpenCode has no configured release archive for architecture ${architecture}" ;;
  esac
}

validate_opencode_version_override() {
  [[ "${OPENCODE_VERSION}" == "${DEFAULT_OPENCODE_VERSION}" ]] && return
  case "$(uname -m)" in
  x86_64 | amd64)
    if [[ -r /proc/cpuinfo ]] && ! grep -qwi avx2 /proc/cpuinfo; then
      [[ -n "${WAVCSE_INFRA_OPENCODE_X86_64_BASELINE_SHA256:-}" ]] ||
        fail 'set WAVCSE_INFRA_OPENCODE_X86_64_BASELINE_SHA256 when overriding WAVCSE_INFRA_OPENCODE_VERSION'
    else
      [[ -n "${WAVCSE_INFRA_OPENCODE_X86_64_SHA256:-}" ]] ||
        fail 'set WAVCSE_INFRA_OPENCODE_X86_64_SHA256 when overriding WAVCSE_INFRA_OPENCODE_VERSION'
    fi
    ;;
  aarch64 | arm64)
    [[ -n "${WAVCSE_INFRA_OPENCODE_AARCH64_SHA256:-}" ]] ||
      fail 'set WAVCSE_INFRA_OPENCODE_AARCH64_SHA256 when overriding WAVCSE_INFRA_OPENCODE_VERSION'
    ;;
  esac
}

install_opencode() {
  local archive_name
  local expected_sha256
  local temporary_directory
  local archive
  local actual_sha256
  local extracted_binary
  local url
  if ! should_install 'OpenCode' opencode; then
    return 0
  fi
  validate_opencode_version_override

  IFS=$'\t' read -r archive_name expected_sha256 < <(opencode_release_details)
  url="https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/${archive_name}"
  temporary_directory="$(run_controller mktemp -d)"
  archive="${temporary_directory}/${archive_name}"
  printf 'Installing OpenCode %s from anomalyco/opencode.\n' "${OPENCODE_VERSION}"

  if ! run_controller curl --fail --location --proto '=https' --tlsv1.2 \
    --silent --show-error "${url}" --output "${archive}"; then
    run_controller rm -rf -- "${temporary_directory}"
    fail "could not download the official OpenCode archive from ${url}"
  fi
  actual_sha256="$(run_controller sha256sum "${archive}" | awk '{print $1}')"
  if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
    run_controller rm -rf -- "${temporary_directory}"
    fail "OpenCode archive checksum mismatch (expected ${expected_sha256}, got ${actual_sha256})"
  fi

  run_controller tar -xzf "${archive}" -C "${temporary_directory}"
  extracted_binary="$(run_controller find "${temporary_directory}" -type f -name opencode -print -quit)"
  if [[ -z "${extracted_binary}" ]]; then
    run_controller rm -rf -- "${temporary_directory}"
    fail "OpenCode archive did not contain an opencode binary"
  fi
  run_controller install -D -m 0755 -- "${extracted_binary}" \
    "${CONTROLLER_HOME}/.local/bin/opencode"
  run_controller rm -rf -- "${temporary_directory}"
  tool_is_installed opencode || fail 'OpenCode archive was installed but opencode was not found on PATH'
  INSTALL_STATUS[opencode]='installed'
}

tool_version() {
  local command_name="$1"
  local path
  local output
  path="$(find_tool "${command_name}" || true)"
  if [[ -z "${path}" ]]; then
    printf 'missing'
    return
  fi
  output="$(run_controller "${path}" --version 2>/dev/null | head -n 1 || true)"
  if [[ -n "${output}" ]]; then
    printf '%s' "${output}"
  else
    printf 'available at %s (version unavailable)' "${path}"
  fi
}

print_summary() {
  printf '\nInstalled:\n'
  if selected omp; then
    printf '  PASS OMP    (%s)\n' "${INSTALL_STATUS[omp]:-available}"
  fi
  if selected codex; then
    printf '  PASS Codex  (%s)\n' "${INSTALL_STATUS[codex]:-available}"
  fi
  if selected agf; then
    printf '  PASS AGF    (%s)\n' "${INSTALL_STATUS[agf]:-available}"
  fi
  if selected opencode; then
    printf '  PASS OpenCode (%s)\n' "${INSTALL_STATUS[opencode]:-available}"
  fi

  printf '\nController agent tools:\n\n'
  if selected omp; then
    printf '  %-11s %s\n' 'OMP' "$(tool_version omp)"
  fi
  if selected codex; then
    printf '  %-11s %s\n' 'Codex' "$(tool_version codex)"
  fi
  if selected agf; then
    printf '  %-11s %s\n' 'AGF' "$(tool_version agf)"
  fi
  if selected opencode; then
    printf '  %-11s %s\n' 'OpenCode' "$(tool_version opencode)"
  fi

  printf '\nManual authentication may still be required:\n'
  if selected omp; then
    printf '  OMP: start omp, run /login (or /login <provider>), then follow the prompt.\n'
  fi
  if selected codex; then
    printf '  Codex: run codex login --device-auth on a headless controller.\n'
  fi
  if selected agf; then
    printf '  AGF: no authentication required; it reads local agent session stores.\n'
  fi
  if selected opencode; then
    printf '  OpenCode: run opencode auth login, then follow the prompt.\n'
  fi
  printf '\nStart a new login shell before relying on persisted PATH changes.\n'
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
  CONTROLLER_LOGIN_SHELL="$(getent passwd "${CONTROLLER_USER}" | cut -d: -f7)"
  readonly CONTROLLER_LOGIN_SHELL
  CONTROLLER_PATH="${CONTROLLER_HOME}/.local/bin:${CONTROLLER_HOME}/.cargo/bin:${CONTROLLER_HOME}/.bun/bin:${PATH}"
  readonly CONTROLLER_PATH

  printf 'Installing controller agent tools for %s.\n' "${CONTROLLER_USER}"
  run_controller install -d -m 0755 -- "${CONTROLLER_HOME}/.local/bin"
  ensure_path_configuration
  if installation_required; then
    ensure_download_prerequisites
  fi

  selected omp && install_omp
  selected codex && install_codex
  selected agf && install_agf
  selected opencode && install_opencode

  print_summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap on_error ERR
  main "$@"
fi

#!/usr/bin/env bash
set -Eeuo pipefail

# Materialize the machine-local OMP overlay that lets an agent working in the
# wavCSE checkout reach the control plane's specialist skills without copying
# them or changing directory.
#
# The overlay is generated here, not committed, because it names real checkout
# paths: repository state stays path-agnostic, machine state stays machine-local.
# It is idempotent and touches only a managed block in the login profile.

SUBDIRECTORY='.config/wavcse-infra'
OVERLAY_NAME='omp-overlay.yml'
CONFIG_NAME='config.toml'
BEGIN_MARKER='# >>> wavcse-infra omp overlay >>>'
END_MARKER='# <<< wavcse-infra omp overlay <<<'

CONTROLLER_USER=''
CONTROLLER_HOME=''
CHECK_ONLY=false
PRINT_PATH=false

fail() {
  printf 'OMP overlay configuration failed: %s\n' "$*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  printf 'OMP overlay configuration failed at line %s (exit %s).\n' \
    "${BASH_LINENO[0]}" "${exit_code}" >&2
  exit "${exit_code}"
}

usage() {
  cat <<'EOF'
Usage: ./controller/omp-overlay.sh [--check] [--print-path]

Write (or verify) the machine-local OMP overlay that registers the control
plane's agent skills for sessions rooted in the wavCSE checkout.

  --check        verify the overlay without writing anything
  --print-path   print the overlay path and exit

The wavCSE checkout is taken from WAVCSE_INFRA_WAVCSE_PATH, or from paths.wavcse
in the resolved controller configuration. Nothing is guessed and nothing is
committed.
EOF
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
    --check)
      CHECK_ONLY=true
      shift
      ;;
    --print-path)
      PRINT_PATH=true
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

require_ubuntu() {
  [[ -r /etc/os-release ]] || fail '/etc/os-release is missing; only Ubuntu is supported'
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ "${ID:-}" == 'ubuntu' ]] || fail "unsupported operating system '${ID:-unknown}'"
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
    fail 'set WAVCSE_INFRA_CONTROLLER_USER when running as root outside a standard Ubuntu image'
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
  run_as_controller env HOME="${CONTROLLER_HOME}" "$@"
}

controller_home() {
  local user_home
  if [[ -n "${WAVCSE_INFRA_CONTROLLER_HOME:-}" ]]; then
    user_home="${WAVCSE_INFRA_CONTROLLER_HOME}"
  else
    user_home="$(getent passwd "${CONTROLLER_USER}" | cut -d: -f6)"
  fi
  [[ -n "${user_home}" && -d "${user_home}" ]] ||
    fail "could not determine home directory for ${CONTROLLER_USER}"
  printf '%s\n' "${user_home}"
}

profile_path() {
  local shell_name
  shell_name="$(getent passwd "${CONTROLLER_USER}" | cut -d: -f7)"
  case "${shell_name##*/}" in
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

# Read paths.wavcse from the resolved controller configuration without a TOML parser.
configured_wavcse_path() {
  local config_file="${CONTROLLER_HOME}/${SUBDIRECTORY}/${CONFIG_NAME}"
  [[ -r "${config_file}" ]] || return 1
  awk '
    /^\[/ { section = $0 }
    section == "[paths]" && $1 == "wavcse" {
      value = $0
      sub(/^[^=]*=[[:space:]]*/, "", value)
      gsub(/^"|"$/, "", value)
      print value
      exit
    }
  ' "${config_file}"
}

resolve_wavcse_path() {
  local candidate=''
  if [[ -n "${WAVCSE_INFRA_WAVCSE_PATH:-}" ]]; then
    candidate="${WAVCSE_INFRA_WAVCSE_PATH}"
  else
    candidate="$(configured_wavcse_path || true)"
  fi
  [[ -n "${candidate}" ]] || fail \
    "set WAVCSE_INFRA_WAVCSE_PATH, or paths.wavcse in ${CONTROLLER_HOME}/${SUBDIRECTORY}/${CONFIG_NAME}, to the wavCSE checkout"
  candidate="${candidate/#\~/${CONTROLLER_HOME}}"
  [[ -d "${candidate}" ]] || fail "wavCSE checkout does not exist: ${candidate}"
  printf '%s\n' "${candidate}"
}

overlay_path() {
  printf '%s/%s/%s\n' "${CONTROLLER_HOME}" "${SUBDIRECTORY}" "${OVERLAY_NAME}"
}

write_overlay() {
  local infra_root="$1"
  local skills_root="${infra_root}/.agents/skills"
  [[ -d "${skills_root}" ]] || fail "control plane skills are missing at ${skills_root}"
  local destination
  destination="$(overlay_path)"
  run_controller mkdir -p "${CONTROLLER_HOME}/${SUBDIRECTORY}"

  local temporary
  temporary="${destination}.tmp.$$"
  # The overlay only adds skill directories; it overrides nothing else.
  run_controller tee "${temporary}" >/dev/null <<EOF
# Generated by controller/omp-overlay.sh -- do not edit, and do not commit.
# Registers the control plane's specialist skills for sessions started anywhere
# on this controller, so an agent working in the wavCSE checkout can read them
# without a second checkout and without duplicating their text.
skills:
  customDirectories:
    - ${skills_root}
EOF
  run_controller mv "${temporary}" "${destination}"
  printf 'Wrote OMP overlay: %s\n' "${destination}"
}

ensure_profile_export() {
  local destination="$1"
  local profile
  profile="$(profile_path)"
  run_controller touch -- "${profile}"
  if run_controller grep -Fqx -- "${BEGIN_MARKER}" "${profile}"; then
    run_controller grep -Fqx -- "${END_MARKER}" "${profile}" ||
      fail "incomplete managed block in ${profile}"
    printf 'OMP overlay is already referenced from %s\n' "${profile}"
    return
  fi
  if run_controller grep -Fqx -- "${END_MARKER}" "${profile}"; then
    fail "incomplete managed block in ${profile}"
  fi

  # Only export when nothing else already configures overlays; clobbering a
  # user's own PI_CONFIG_FILES would silently drop their configuration.
  local existing=''
  existing="$(run_controller printenv PI_CONFIG_FILES || true)"
  if [[ -n "${existing}" ]]; then
    printf 'PI_CONFIG_FILES is already set (%s); add %s to it manually.\n' \
      "${existing}" "${destination}"
    return
  fi

  # shellcheck disable=SC2016  # expanded by the login shell, not here
  run_controller bash -c '
    printf "\n%s\n" "$2" >>"$1"
    printf "%s\n" "if [[ -z \"\${PI_CONFIG_FILES:-}\" ]]; then" >>"$1"
    printf "%s\n" "  export PI_CONFIG_FILES=\"$3\"" >>"$1"
    printf "%s\n" "fi" >>"$1"
    printf "%s\n" "$4" >>"$1"
  ' configure-omp-overlay "${profile}" "${BEGIN_MARKER}" "${destination}" "${END_MARKER}"
  printf 'Referenced the OMP overlay from %s\n' "${profile}"
}

check_overlay() {
  local infra_root="$1"
  local destination
  destination="$(overlay_path)"
  [[ -r "${destination}" ]] || fail "overlay is missing: ${destination}"
  local skills_root="${infra_root}/.agents/skills"
  run_controller grep -Fq -- "${skills_root}" "${destination}" ||
    fail "overlay does not reference ${skills_root}"
  local wavcse_root
  wavcse_root="$(resolve_wavcse_path)"
  printf 'OMP overlay OK: %s (skills from %s, wavCSE at %s)\n' \
    "${destination}" "${skills_root}" "${wavcse_root}"
}

main() {
  parse_args "$@"
  CONTROLLER_USER="$(controller_user)"
  readonly CONTROLLER_USER
  id "${CONTROLLER_USER}" >/dev/null 2>&1 || fail "controller user does not exist: ${CONTROLLER_USER}"
  CONTROLLER_HOME="$(controller_home)"
  readonly CONTROLLER_HOME

  local infra_root
  infra_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
  readonly infra_root

  if [[ "${PRINT_PATH}" == true ]]; then
    overlay_path
    return 0
  fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    check_overlay "${infra_root}"
    return 0
  fi

  require_ubuntu
  resolve_wavcse_path >/dev/null
  write_overlay "${infra_root}"
  ensure_profile_export "$(overlay_path)"
  printf 'A new login shell is required before a plain omp session picks this up.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap on_error ERR
  main "$@"
fi

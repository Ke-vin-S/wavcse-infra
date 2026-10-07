#!/usr/bin/env bash
set -Eeuo pipefail

# Apply the repository's mirrored application configuration (apps/manifest) to the
# controller account's home directory.
#
# The repository is the source of truth. A file that already matches is left alone,
# an absent file is installed, and a file whose content differs is never replaced
# without an explicit --yes or an interactive confirmation, and is backed up first.
# --check reports drift and writes nothing.

APP_NAME=''
CONTROLLER_USER=''
CONTROLLER_HOME=''
CHECK_ONLY=false
PRESERVE_EXISTING=false
ASSUME_YES=false
REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPOSITORY_ROOT
readonly MANIFEST="${REPOSITORY_ROOT}/apps/manifest"
temporary_file=''

fail() {
  printf 'controller app config failed: %s\n' "$*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  remove_temporary
  printf 'controller app config failed at line %s (exit %s).\n' \
    "${BASH_LINENO[0]}" "${exit_code}" >&2
  exit "${exit_code}"
}

remove_temporary() {
  if [[ -n "${temporary_file}" ]]; then
    rm -f -- "${temporary_file}"
    temporary_file=''
  fi
}

usage() {
  cat <<'EOF'
Usage: ./controller/app-config.sh [--app NAME] [--yes] [--preserve-existing] [--check]

Apply the repository's mirrored application configuration to this controller.

  --app NAME           limit to one mirrored application
  --yes                replace a differing file without prompting, backing it up first
  --preserve-existing  never prompt and never replace a differing file
  --check              report drift without writing anything; exit 1 if anything drifted

With no flag, a differing file is replaced only after an interactive confirmation.
EOF
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
    --app)
      [[ $# -ge 2 ]] || fail "--app requires an application name"
      APP_NAME="$2"
      shift 2
      ;;
    --yes)
      ASSUME_YES=true
      shift
      ;;
    --preserve-existing)
      PRESERVE_EXISTING=true
      shift
      ;;
    --check)
      CHECK_ONLY=true
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      printf 'controller app config failed: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
    esac
  done
  if [[ "${ASSUME_YES}" == true && "${PRESERVE_EXISTING}" == true ]]; then
    printf 'controller app config failed: --yes and --preserve-existing contradict each other\n' >&2
    exit 2
  fi
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
    command -v runuser >/dev/null 2>&1 ||
      fail 'runuser is required when installing as another user'
    runuser --user "${CONTROLLER_USER}" -- "$@"
  fi
}

manifest_error() {
  fail "${MANIFEST} line $1: $2"
}

reject_unsafe() {
  local line=$1 value=$2 label=$3
  case "${value}" in
  '' | /* | .. | ../* | */../* | */..)
    manifest_error "${line}" "unsafe ${label} '${value}'; expected a relative path without .."
    ;;
  esac
}

# Emit "<app>\t<source>\t<destination>\t<mode>\t<target>" for every manifest entry,
# refusing an ambiguous or unsafe registry rather than silently applying part of it.
manifest_entries() {
  local -A seen=()
  local line='' line_number=0 app
  local -a fields
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_number=$((line_number + 1))
    read -r -a fields <<<"${line}"
    [[ "${#fields[@]}" -gt 0 ]] || continue
    [[ "${fields[0]}" == \#* ]] && continue
    if [[ "${#fields[@]}" -lt 3 || "${#fields[@]}" -gt 5 ]]; then
      manifest_error "${line_number}" \
        'expected <app> <source> <destination> [mode] [target]'
    fi
    app="${fields[0]}"
    if ! [[ "${app}" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
      manifest_error "${line_number}" "invalid app name '${app}'"
    fi
    reject_unsafe "${line_number}" "${fields[1]}" source
    reject_unsafe "${line_number}" "${fields[2]}" destination
    local mode='0644'
    if [[ "${#fields[@]}" -ge 4 ]]; then
      mode="${fields[3]}"
      if ! [[ "${mode}" =~ ^[0-7]{3,4}$ ]]; then
        manifest_error "${line_number}" "invalid mode '${mode}'; expected 3 or 4 octal digits"
      fi
    fi
    local target='both'
    if [[ "${#fields[@]}" -eq 5 ]]; then
      target="${fields[4]}"
      case "${target}" in
      controller | worker | both) ;;
      *) manifest_error "${line_number}" \
        "invalid target '${target}'; expected controller, worker, or both" ;;
      esac
    fi
    if [[ -n "${seen[${fields[2]}]:-}" ]]; then
      manifest_error "${line_number}" \
        "duplicate destination '${fields[2]}', already declared on line ${seen[${fields[2]}]}"
    fi
    seen["${fields[2]}"]="${line_number}"
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "${app}" "${fields[1]}" "${fields[2]}" "${mode}" "${target}"
  done <"${MANIFEST}"
}

# This script applies to the controller account, so worker-only entries are filtered
# out before any app selection; a --app that names a worker-only entry is an error.
selected_entries() {
  local entries matched available all_entries
  all_entries="$(manifest_entries)"
  if [[ -n "${all_entries}" ]]; then
    entries="$(awk -F'\t' '$5 != "worker"' <<<"${all_entries}")"
  else
    entries=''
  fi
  if [[ -n "${all_entries}" && -z "${entries}" ]]; then
    fail "application configuration manifest has no controller entries: ${MANIFEST}"
  fi
  if [[ -n "${entries}" && -n "${APP_NAME}" ]]; then
    matched="$(awk -F'\t' -v app="${APP_NAME}" '$1 == app' <<<"${entries}")"
    if [[ -z "${matched}" ]]; then
      if awk -F'\t' -v app="${APP_NAME}" '$1 == app' <<<"${all_entries}" | grep -q .; then
        fail "app '${APP_NAME}' is worker-only and is not applied on this controller"
      fi
      available="$(
        cut -f1 <<<"${entries}" | sort -u | paste -sd, - | sed 's/,/, /g'
      )"
      fail "unknown app '${APP_NAME}'; mirrored apps are: ${available}"
    fi
    entries="${matched}"
  fi
  [[ -n "${entries}" ]] ||
    fail "application configuration manifest has no entries: ${MANIFEST}"
  printf '%s\n' "${entries}"
}

backup_path() {
  printf '%s.wavcse-backup-%s\n' "$1" "$(date -u +%Y%m%dT%H%M%SZ)"
}
install_entry() {
  local source_file=$1 target=$2 mode=$3
  local directory
  directory="$(dirname -- "${target}")"
  if [[ ! -d "${directory}" ]]; then
    run_as_controller install -d -m 0700 -- "${directory}" ||
      fail "could not create ${directory}"
  fi
  temporary_file="$(run_as_controller mktemp --tmpdir="${directory}" .wavcse-app-config.XXXXXX)" ||
    fail "could not stage an application configuration file in ${directory}"
  run_as_controller install -m "${mode}" -- "${source_file}" "${temporary_file}" ||
    fail "could not write the staged application configuration file: ${temporary_file}"
  run_as_controller mv -f -- "${temporary_file}" "${target}" ||
    fail "could not move the staged application configuration into place: ${target}"
  temporary_file=''
}

target_state() {
  local source_file=$1 target=$2
  if [[ -L "${target}" ]]; then
    printf 'error\n'
  elif [[ -f "${target}" ]]; then
    if cmp -s -- "${source_file}" "${target}"; then
      printf 'matches\n'
    else
      printf 'differs\n'
    fi
  elif [[ -e "${target}" ]]; then
    printf 'error\n'
  else
    printf 'absent\n'
  fi
}

override_entry() {
  local app=$1 source=$2 destination=$3 mode=$4
  local source_file="${REPOSITORY_ROOT}/apps/${app}/${source}"
  local target="${CONTROLLER_HOME}/${destination}"
  if [[ "${ASSUME_YES}" != true ]]; then
    printf 'Override ~/%s with apps/%s/%s? The current file is backed up [y/N] ' \
      "${destination}" "${app}" "${source}"
    local answer=''
    read -r answer || answer=''
    case "${answer}" in
    y | Y) ;;
    *)
      printf '%s: skipped ~/%s\n' "${app}" "${destination}"
      return 0
      ;;
    esac
  fi
  local backup
  backup="$(backup_path "${target}")"
  cp -p -- "${target}" "${backup}" ||
    fail "could not back up ${target} to ${backup}"
  install_entry "${source_file}" "${target}" "${mode}"
  printf '%s: overridden ~/%s (backup ~/%s)\n' \
    "${app}" "${destination}" "${backup#"${CONTROLLER_HOME}/"}"
}

apply_entry() {
  local app=$1 source=$2 destination=$3 mode=$4
  local source_file="${REPOSITORY_ROOT}/apps/${app}/${source}"
  local target="${CONTROLLER_HOME}/${destination}"

  [[ -r "${source_file}" ]] ||
    fail "app '${app}' source file is missing or unreadable: ${source_file}"

  local state
  state="$(target_state "${source_file}" "${target}")"
  if [[ "${state}" == error ]]; then
    fail "app '${app}' destination is not a regular file: ${target}"
  fi

  case "${state}" in
  matches)
    if [[ "${CHECK_ONLY}" == true ]]; then
      printf '%s: current ~/%s\n' "${app}" "${destination}"
    else
      printf '%s: unchanged ~/%s\n' "${app}" "${destination}"
    fi
    return 0
    ;;
  absent)
    if [[ "${CHECK_ONLY}" == true ]]; then
      printf '%s: absent ~/%s\n' "${app}" "${destination}"
      return 1
    fi
    install_entry "${source_file}" "${target}" "${mode}"
    printf '%s: installed ~/%s\n' "${app}" "${destination}"
    return 0
    ;;
  differs)
    if [[ "${CHECK_ONLY}" == true ]]; then
      printf '%s: drifted ~/%s\n' "${app}" "${destination}"
      return 1
    fi
    if [[ "${PRESERVE_EXISTING}" == true || "${ASSUME_YES}" != true && ! -t 0 ]]; then
      printf '%s: preserved ~/%s (local content differs; rerun with --yes to override)\n' \
        "${app}" "${destination}"
      return 0
    fi
    override_entry "${app}" "${source}" "${destination}" "${mode}"
    return 0
    ;;
  esac
}

main() {
  parse_args "$@"
  [[ -r "${MANIFEST}" ]] ||
    fail "application configuration manifest is missing: ${MANIFEST}"

  CONTROLLER_USER="$(controller_user)"
  readonly CONTROLLER_USER
  id "${CONTROLLER_USER}" >/dev/null 2>&1 ||
    fail "controller user does not exist: ${CONTROLLER_USER}"
  CONTROLLER_HOME="${WAVCSE_INFRA_CONTROLLER_HOME:-$(getent passwd "${CONTROLLER_USER}" | cut -d: -f6)}"
  readonly CONTROLLER_HOME
  [[ -n "${CONTROLLER_HOME}" && -d "${CONTROLLER_HOME}" ]] ||
    fail "could not determine home directory for ${CONTROLLER_USER}"

  local entries app source destination mode target status=0
  local -a rows=()
  entries="$(selected_entries)"
  # The rows are read here, not in the apply loop, so the loop's stdin stays the
  # operator's terminal and a differing file can still be confirmed interactively.
  mapfile -t rows <<<"${entries}"
  for row in "${rows[@]}"; do
    IFS=$'\t' read -r app source destination mode target <<<"${row}"
    if ! apply_entry "${app}" "${source}" "${destination}" "${mode}"; then
      status=1
    fi
  done
  return "${status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap on_error ERR
  main "$@"
fi

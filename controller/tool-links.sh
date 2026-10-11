#!/usr/bin/env bash
set -Eeuo pipefail

# Publish this repository's managed controller tools into a system-wide directory.
#
# Every tool the repository installs lands in the controller account's ~/.local/bin, and the
# only PATH entry for that directory is the block controller/install-agents.sh writes into
# the account's *login* profile. Bash reads that file only as a login shell: a non-login
# shell — `ssh controller '<command>'`, an agent's shell tool, a cron job, a systemd unit —
# reads none of the login files, so it resolves none of the tools and a fully provisioned
# controller looks unprovisioned.
#
# Linking the tools into /usr/local/bin fixes every one of those contexts without creating a
# second installation location: each entry resolves to the binary the installers manage, so
# a version bump or --upgrade is picked up automatically.
#
# A tool that is not installed is reported and skipped, never an error. An entry this
# repository did not create is never replaced. --check reports drift and writes nothing.

# The tools this repository installs into the controller account's ~/.local/bin. A tool
# absent from that directory is reported, not created.
readonly -a MANAGED_TOOLS=(omp codex agf opencode colab infra lazygit uv)
readonly DEFAULT_LINK_DIRECTORY='/usr/local/bin'
# The PATH a shell gets when it reads no login file, used to prove visibility.
readonly SYSTEM_PATH_DIRECTORIES='/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin'

LINK_DIRECTORY="${WAVCSE_INFRA_TOOL_LINK_DIR:-${DEFAULT_LINK_DIRECTORY}}"
CONTROLLER_USER=''
CONTROLLER_HOME=''
CHECK_ONLY=false

fail() {
  printf 'controller tool links failed: %s\n' "$*" >&2
  exit 1
}

on_error() {
  local exit_code=$?
  printf 'controller tool links failed at line %s (exit %s).\n' \
    "${BASH_LINENO[0]}" "${exit_code}" >&2
  exit "${exit_code}"
}

usage() {
  cat <<'EOF'
Usage: ./controller/tool-links.sh [--check]

Publish this repository's managed controller tools into a system-wide directory (by
default /usr/local/bin) so a non-login shell resolves them: `ssh controller
'<command>'`, an agent's shell tool, cron, and systemd units.

  --check   report drift without writing anything; exit 1 if anything drifted

A tool absent from the controller account's ~/.local/bin is reported and skipped. An
entry this repository did not create is never replaced: the run reports it and fails.
EOF
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
    --check)
      CHECK_ONLY=true
      shift
      ;;
    --help | -h)
      usage
      exit 0
      ;;
    *)
      printf 'controller tool links failed: unknown argument: %s\n' "$1" >&2
      exit 2
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

# Linking into a system directory needs root; a directory the invoking user can already
# write to needs nothing, which also lets a test point the link directory at a temporary
# path.
run_elevated() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
    return 0
  fi
  if [[ -d "${LINK_DIRECTORY}" && -w "${LINK_DIRECTORY}" ]]; then
    "$@"
    return 0
  fi
  command -v sudo >/dev/null 2>&1 ||
    fail "sudo is required to publish tools into ${LINK_DIRECTORY}"
  if [[ ! -t 0 ]]; then
    # Without a terminal sudo cannot prompt, so report the reason instead of its message.
    sudo --non-interactive -- true 2>/dev/null ||
      fail "publishing into ${LINK_DIRECTORY} needs root: run this with sudo, or from a terminal that can authenticate"
    sudo --non-interactive -- "$@"
    return 0
  fi
  sudo -- "$@"
}

tool_source() {
  printf '%s\n' "${CONTROLLER_HOME}/.local/bin/$1"
}

tool_link() {
  printf '%s\n' "${LINK_DIRECTORY}/$1"
}

# not-installed | orphaned | missing | current | foreign
#
# 'current' compares resolved paths, so a relative or differently spelled symlink that
# already points at this repository's binary is accepted, while a regular file, a symlink
# into another installation, or a symlink to something else is 'foreign'. A tool that is
# no longer installed but still has an entry here is 'orphaned': a dangling system-wide
# entry is exactly the "looks uninstalled" trap, so it is reported rather than ignored.
link_state() {
  local source target linked resolved_source resolved_target
  source="$(tool_source "$1")"
  target="$(tool_link "$1")"
  if [[ ! -x "${source}" ]]; then
    if [[ -e "${target}" || -L "${target}" ]]; then
      printf 'orphaned\n'
    else
      printf 'not-installed\n'
    fi
    return 0
  fi
  if [[ ! -e "${target}" && ! -L "${target}" ]]; then
    printf 'missing\n'
    return 0
  fi
  linked="$(readlink -- "${target}" 2>/dev/null || true)"
  resolved_source="$(readlink -f -- "${source}" 2>/dev/null || true)"
  resolved_target="$(readlink -f -- "${target}" 2>/dev/null || true)"
  if [[ -n "${linked}" && -n "${resolved_source}" &&
    "${resolved_target}" == "${resolved_source}" ]]; then
    printf 'current\n'
  else
    printf 'foreign\n'
  fi
}

publish_link() {
  run_elevated ln --symbolic --no-dereference --force -- \
    "$(tool_source "$1")" "$(tool_link "$1")"
}

# Prove the link resolves in a shell that reads no login file — the context the links exist
# for — rather than trusting that creating it was enough.
visible_in_non_login_shell() {
  env -i "PATH=${LINK_DIRECTORY}:${SYSTEM_PATH_DIRECTORIES}" \
    sh -c "command -v $1" >/dev/null 2>&1
}

ensure_link_directory() {
  [[ -d "${LINK_DIRECTORY}" ]] && return 0
  [[ "${CHECK_ONLY}" == true ]] && return 0
  # Creating it where the invoking user already may (a custom link directory) needs no
  # elevation; only a system path falls back to sudo.
  if mkdir -p -- "${LINK_DIRECTORY}" 2>/dev/null; then
    return 0
  fi
  run_elevated mkdir -p -- "${LINK_DIRECTORY}"
}

main() {
  parse_args "$@"

  CONTROLLER_USER="$(controller_user)"
  readonly CONTROLLER_USER
  id "${CONTROLLER_USER}" >/dev/null 2>&1 ||
    fail "controller user does not exist: ${CONTROLLER_USER}"
  CONTROLLER_HOME="${WAVCSE_INFRA_CONTROLLER_HOME:-$(getent passwd "${CONTROLLER_USER}" | cut -d: -f6)}"
  readonly CONTROLLER_HOME
  [[ -n "${CONTROLLER_HOME}" && -d "${CONTROLLER_HOME}" ]] ||
    fail "could not determine home directory for ${CONTROLLER_USER}"

  ensure_link_directory

  local tool state status=0 published=0 current=0 skipped=0
  local -a foreign=() orphaned=()
  for tool in "${MANAGED_TOOLS[@]}"; do
    state="$(link_state "${tool}")"
    case "${state}" in
    not-installed)
      printf '%s: not installed in ~/.local/bin; nothing to publish\n' "${tool}"
      skipped=$((skipped + 1))
      ;;
    orphaned)
      printf '%s: not installed in ~/.local/bin but %s remains; reinstall the tool or remove that entry\n' \
        "${tool}" "$(tool_link "${tool}")" >&2
      orphaned+=("${tool}")
      # A leftover entry is drift worth reporting, but not a reason to fail a bootstrap that
      # has just reinstalled everything else.
      if [[ "${CHECK_ONLY}" == true ]]; then
        status=1
      fi
      ;;
    current)
      printf '%s: current %s\n' "${tool}" "$(tool_link "${tool}")"
      current=$((current + 1))
      ;;
    missing)
      if [[ "${CHECK_ONLY}" == true ]]; then
        printf '%s: missing %s\n' "${tool}" "$(tool_link "${tool}")"
        status=1
        continue
      fi
      publish_link "${tool}"
      visible_in_non_login_shell "${tool}" ||
        fail "${tool} is linked at $(tool_link "${tool}") but a non-login shell still cannot resolve it"
      printf '%s: published %s -> %s\n' \
        "${tool}" "$(tool_link "${tool}")" "$(tool_source "${tool}")"
      published=$((published + 1))
      ;;
    foreign)
      printf '%s: %s exists and this repository did not create it\n' \
        "${tool}" "$(tool_link "${tool}")" >&2
      foreign+=("$(tool_link "${tool}")")
      status=1
      ;;
    esac
  done

  if ((${#foreign[@]} > 0)); then
    printf 'Refusing to replace: %s\n' "${foreign[*]}" >&2
    printf 'Remove or rename those paths, then re-run; they were left untouched.\n' >&2
  fi
  if ((${#orphaned[@]} > 0)); then
    printf 'Stale entries left in place: %s\n' "${orphaned[*]}" >&2
  fi

  if ((${#foreign[@]} > 0)); then
    # A refusal is a reported outcome, not an unexpected failure: silence the error trap so
    # the operator reads the report instead of a trap line.
    trap - ERR
    return 1
  fi

  if [[ "${CHECK_ONLY}" == true ]]; then
    if ((status == 0)); then
      printf 'Controller tools are published in %s (%s current, %s not installed).\n' \
        "${LINK_DIRECTORY}" "${current}" "${skipped}"
    fi
  else
    printf 'Controller tools published in %s (%s linked, %s already current, %s not installed).\n' \
      "${LINK_DIRECTORY}" "${published}" "${current}" "${skipped}"
  fi
  if ((status != 0)); then
    trap - ERR
  fi
  return "${status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap on_error ERR
  main "$@"
fi

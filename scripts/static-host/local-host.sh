#!/usr/bin/env bash
# Local static-host fixture: a throwaway container that looks like a non-root SSH
# account on a shared host, so the static-worker flow can be proven before any
# real machine is touched.
#
# This is a developer aid, not part of `make check`: `make check` must stay
# runnable on a machine without Docker. See scripts/static-host/Dockerfile.
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly CONTAINER_NAME="static-host-wavcse"
readonly IMAGE_TAG="wavcse-static-host:local"
readonly SSH_HOST="127.0.0.1"
readonly SSH_PORT=2222
readonly SSH_USER="hpc"
readonly READY_TIMEOUT_SECONDS=30

# State lives outside the repository (`.gitignore` already covers `id_ed25519*`)
# so the working tree stays clean and the throwaway keypair survives `down`.
readonly STATE_HOME_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}"
readonly STATE_DIR="${STATE_HOME_DIR}/wavcse-infra/static-host"
readonly KEY_PATH="${STATE_DIR}/id_ed25519"
readonly KEY_COMMENT="wavcse-static-host"

usage() {
  cat <<'EOF'
Usage: scripts/static-host/local-host.sh <command>

Commands:
  up      Build the fixture image, (re)start the container, wait until key-based
          SSH works, then print the [[static.workers]] block to paste into the
          controller config.toml.
  status  Report whether the container exists and runs, and whether SSH works.
          Exits non-zero when the fixture is not running.
  down    Remove the container. The state directory (the reused keypair) is kept.
  --help  Show this message.

State directory (keypair and container name):
  $XDG_STATE_HOME/wavcse-infra/static-host, defaulting to
  ~/.local/state/wavcse-infra/static-host.
EOF
}

die() {
  printf '%s\n' "$*" >&2
  exit 1
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    die 'docker is required for the static-host fixture but was not found on PATH.'
  fi
}

ssh_probe() {
  ssh -i "${KEY_PATH}" -F /dev/null \
    -o BatchMode=yes \
    -o PasswordAuthentication=no \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=3 \
    -p "${SSH_PORT}" "${SSH_USER}@${SSH_HOST}" true
}

container_running() {
  [[ "$(docker inspect --format '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null)" == "true" ]]
}

print_config_block() {
  # Replace only the leading home directory with `~`, so the printed block is
  # copy-pasteable. The replacement must stay quoted: bash mis-parses a bare
  # trailing `~` there.
  local private_key_display="${KEY_PATH/#"${HOME}"/"~"}"

  cat <<EOF

Add this to the controller config.toml:

[[static.workers]]
name = "local-static"
host = "${SSH_HOST}"
port = ${SSH_PORT}
username = "${SSH_USER}"
gpu_type = "NVIDIA A100-SXM4-80GB"
gpu_count = 1
root = "/home/hpc/wavcse"
private_key = "${private_key_display}"
max_concurrent_jobs = 1

  # local-static is a local verification fixture only: a throwaway container,
  # never a daemon, a service or a runtime dependency of the control plane.
EOF
}

cmd_up() {
  require_docker

  install -d -m 0700 "${STATE_DIR}"
  printf '%s\n' "${CONTAINER_NAME}" >"${STATE_DIR}/container-name"

  if [[ ! -f "${KEY_PATH}" ]]; then
    printf 'Generating throwaway keypair at %s\n' "${KEY_PATH}"
    ssh-keygen -t ed25519 -f "${KEY_PATH}" -N '' -C "${KEY_COMMENT}" >/dev/null
  else
    printf 'Reusing existing keypair at %s\n' "${KEY_PATH}"
  fi

  printf 'Building image %s\n' "${IMAGE_TAG}"
  docker build -t "${IMAGE_TAG}" "${SCRIPT_DIR}"

  # Replace any pre-existing container so `up` is idempotent.
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

  printf 'Starting container %s on %s:%s\n' "${CONTAINER_NAME}" "${SSH_HOST}" "${SSH_PORT}"
  docker run -d --name "${CONTAINER_NAME}" \
    -p "${SSH_HOST}:${SSH_PORT}:22" \
    -e "HPC_PUBLIC_KEY=$(cat "${KEY_PATH}.pub")" \
    "${IMAGE_TAG}" >/dev/null

  printf 'Waiting for SSH on %s:%s ...\n' "${SSH_HOST}" "${SSH_PORT}"
  local waited=0
  until ssh_probe 2>/dev/null; do
    if ((waited >= READY_TIMEOUT_SECONDS)); then
      printf 'Timed out after %ss waiting for SSH on %s:%s. Inspect with: docker logs %s\n' \
        "${READY_TIMEOUT_SECONDS}" "${SSH_HOST}" "${SSH_PORT}" "${CONTAINER_NAME}" >&2
      exit 1
    fi
    sleep 1
    waited=$((waited + 1))
  done

  printf 'Static host fixture ready: %s@%s:%s (container %s)\n' \
    "${SSH_USER}" "${SSH_HOST}" "${SSH_PORT}" "${CONTAINER_NAME}"
  print_config_block
}

cmd_status() {
  require_docker

  if ! docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME}" >/dev/null 2>&1; then
    printf "Container %s does not exist. Run 'make static-host-up' first.\n" "${CONTAINER_NAME}" >&2
    exit 1
  fi

  local status
  status="$(docker inspect --format '{{.State.Status}}' "${CONTAINER_NAME}")"
  if [[ "${status}" != "running" ]]; then
    printf 'Container %s exists but is not running (status: %s).\n' "${CONTAINER_NAME}" "${status}" >&2
    exit 1
  fi

  if ! ssh_probe 2>/dev/null; then
    printf 'Container %s is running but key-based SSH to %s@%s:%s failed.\n' \
      "${CONTAINER_NAME}" "${SSH_USER}" "${SSH_HOST}" "${SSH_PORT}" >&2
    exit 1
  fi

  printf 'Container %s is running; SSH to %s@%s:%s succeeds.\n' \
    "${CONTAINER_NAME}" "${SSH_USER}" "${SSH_HOST}" "${SSH_PORT}"
}

cmd_down() {
  require_docker

  if docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1; then
    printf 'Removed container %s. Keypair kept at %s.\n' "${CONTAINER_NAME}" "${STATE_DIR}"
  else
    printf 'Container %s was not present; nothing to remove.\n' "${CONTAINER_NAME}"
  fi
}

main() {
  if (($# != 1)); then
    usage >&2
    exit 2
  fi

  case "$1" in
  up) cmd_up ;;
  status) cmd_status ;;
  down) cmd_down ;;
  -h | --help) usage ;;
  *)
    printf 'Unknown command: %s\n\n' "$1" >&2
    usage >&2
    exit 2
    ;;
  esac
}

main "$@"

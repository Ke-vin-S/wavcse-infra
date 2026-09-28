#!/usr/bin/env bash
set -Eeuo pipefail

readonly EXPECTED_BOOTSTRAP_VERSION="${1:?expected bootstrap version argument is required}"
readonly DISK_PATH="${2:-/}"
readonly REQUIRED_MOUNT_PATH="${3:-}"
readonly VERSION_FILE="${HOME}/.local/state/wavcse-worker/bootstrap-version"

emit() {
  local key="$1"
  local value="$2"
  value="${value//$'\t'/ }"
  value="${value//$'\n'/; }"
  printf '%s\t%s\n' "${key}" "${value}"
}

emit wavcse_health_schema 1

bootstrap_version=""
if [[ -f "${VERSION_FILE}" ]]; then
  IFS= read -r bootstrap_version <"${VERSION_FILE}" || true
fi
emit bootstrap_version "${bootstrap_version}"
emit expected_bootstrap_version "${EXPECTED_BOOTSTRAP_VERSION}"

git_version=""
if command -v git >/dev/null 2>&1; then
  git_version="$(git --version 2>&1 || true)"
fi
emit git_version "${git_version}"

python_version=""
if command -v python3 >/dev/null 2>&1; then
  python_version="$(python3 --version 2>&1 || true)"
fi
emit python_version "${python_version}"

uv_version=""
if command -v uv >/dev/null 2>&1; then
  uv_version="$(uv --version 2>&1 || true)"
fi
emit uv_version "${uv_version}"

emit disk_path "${DISK_PATH}"
disk_available_bytes=""
disk_inspection_ok=false
if [[ -d "${DISK_PATH}" ]] && disk_available_bytes="$(
  df -PB1 -- "${DISK_PATH}" 2>/dev/null | awk 'NR == 2 { print $4 }'
)" && [[ "${disk_available_bytes}" =~ ^[0-9]+$ ]]; then
  disk_inspection_ok=true
fi
emit disk_inspection_ok "${disk_inspection_ok}"
emit disk_available_bytes "${disk_available_bytes}"

required_mount_present=""
if [[ -n "${REQUIRED_MOUNT_PATH}" ]]; then
  required_mount_present=false
  if mountpoint -q -- "${REQUIRED_MOUNT_PATH}"; then
    required_mount_present=true
  fi
fi
emit required_mount_path "${REQUIRED_MOUNT_PATH}"
emit required_mount_present "${required_mount_present}"

nvidia_smi_available=false
nvidia_smi_ok=false
gpu_rows=""
cuda_version=""
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia_smi_available=true
  if gpu_rows="$(nvidia-smi --query-gpu=name,memory.total,driver_version \
    --format=csv,noheader,nounits 2>/dev/null)"; then
    nvidia_smi_ok=true
    cuda_version="$(nvidia-smi 2>/dev/null | sed -nE \
      's/.*CUDA Version: ([0-9.]+).*/\1/p' | head -n 1 || true)"
  fi
fi
gpu_rows="${gpu_rows//$'\n'/||}"
emit nvidia_smi_available "${nvidia_smi_available}"
emit nvidia_smi_ok "${nvidia_smi_ok}"
emit gpu_rows "${gpu_rows}"
emit cuda_version "${cuda_version}"

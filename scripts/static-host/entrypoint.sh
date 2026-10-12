#!/usr/bin/env bash
# Runs inside the static-host fixture container. Idempotent: it (re)installs the
# operator-supplied public key for the unprivileged account, then execs sshd.
set -Eeuo pipefail

readonly account="${HPC_USER:-hpc}"
readonly public_key="${HPC_PUBLIC_KEY:-}"

if [[ -z "${public_key}" ]]; then
  printf 'HPC_PUBLIC_KEY is empty. Pass the public key text, e.g.\n' >&2
  printf '  docker run -e HPC_PUBLIC_KEY="<contents of id_ed25519.pub>" ...\n' >&2
  exit 1
fi

account_home="$(getent passwd "${account}" | cut -d: -f6)"
if [[ -z "${account_home}" ]]; then
  printf 'Account %s does not exist in this image; the fixture image is not built correctly.\n' \
    "${account}" >&2
  exit 1
fi

readonly account_home
readonly ssh_dir="${account_home}/.ssh"

install -d -m 0700 -o "${account}" -g "${account}" "${ssh_dir}"

# Write atomically so a crash never leaves a partial or world-readable key file.
authorized_keys="${ssh_dir}/authorized_keys"
staged="$(mktemp "${ssh_dir}/authorized_keys.XXXXXX")"
printf '%s\n' "${public_key}" >"${staged}"
chmod 0600 "${staged}"
chown "${account}:${account}" "${staged}"
mv -f "${staged}" "${authorized_keys}"

install -d -m 0755 /run/sshd
exec /usr/sbin/sshd -D -e

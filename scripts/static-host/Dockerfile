# Local fixture: an Ubuntu container that looks like a shared HPC login account
# (a non-root user, no sudo, key-only SSH). It exists only so the static SSH
# worker path can be verified locally; it is never a daemon, a service or a
# runtime dependency of the control plane.
FROM ubuntu:24.04

# The unprivileged account name is a build argument, not a literal, so no
# machine-specific path is baked into the image or these sources.
ARG HPC_USER=hpc

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gzip \
    openssh-server \
    procps \
    python3 \
    tar \
    tmux \
    util-linux \
  && rm -rf /var/lib/apt/lists/*

# Deliberately no `sudo` package and no sudoers entry: its absence is what makes
# a hidden privilege escalation in the bootstrap profile fail loudly.
#
# The password field is set to `*` rather than left at useradd's `!`: sshd's
# non-PAM account check (UsePAM no) refuses an account whose shadow entry starts
# with `!` as "locked", while `*` can never match a password. Password
# authentication stays off, so key-only login remains the only way in.
RUN useradd --create-home --shell /bin/bash "${HPC_USER}" \
  && usermod --password '*' "${HPC_USER}" \
  && ssh-keygen -A \
  && install -d -m 0755 /run/sshd

# Fixture accelerator: no container has a GPU, but bootstrap and health parsing
# require a supported one to be present.
COPY nvidia-smi /usr/local/bin/nvidia-smi
RUN chmod 0755 /usr/local/bin/nvidia-smi

COPY sshd.conf /etc/ssh/sshd_config.d/wavcse-static-host.conf

COPY entrypoint.sh /static-host/entrypoint.sh
RUN chmod 0755 /static-host/entrypoint.sh

# Expose the account name to the entrypoint; the home directory is resolved at
# run time from the passwd database rather than assumed.
ENV HPC_USER=${HPC_USER}

EXPOSE 22

CMD ["/static-host/entrypoint.sh"]

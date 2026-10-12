# Static university SSH host

A **declared** execution provider for a machine this tool does not own and never provisions.
`static_ssh` is a provider identity on the existing `ssh` execution transport
(ADR-029, ADR-038); it adds no transport code and no lifecycle. Jobs execute the same
reviewed runner, verify the same exact commit, and publish the same S3-canonical
artifacts as a Pod or a Colab session.

The host is shared and not ours. The account has no root, no package-manager rights, and no
system-wide write access, so every write this tool performs lives under one directory in
that account's home, and this tool installs nothing there. Nothing about the host is
canonical: the code lives in Git, the artifacts live in S3, and losing the account's
directory loses only cache and logs.

## The declaration is the registration

There is no `register` command and no provider object to create. A host exists to this tool
exactly when it is written into the controller's `config.toml`:

```toml
[[static.workers]]
name = "university"
host = "<hostname or address>"
port = 22
username = "<account>"
gpu_type = "NVIDIA A100-SXM4-80GB"
gpu_count = 1
root = "/home/<account>/wavcse"
private_key = "~/.ssh/<key-name>"
env = { CUDA_VISIBLE_DEVICES = "0" }
max_concurrent_jobs = 1
```

| Field | Meaning |
|---|---|
| `name` | The exact worker ID every command takes: `infra worker bootstrap university`. |
| `host`, `port`, `username` | The endpoint, written explicitly. See below for why the SSH alias cannot be used. |
| `gpu_type`, `gpu_count` | What this machine must prove with `nvidia-smi` before it reaches READY. |
| `root` | The one directory this tool confines itself to. Absolute, never `/`, never `~`. |
| `private_key` | Optional. Overrides `ssh.private_key` for this host only. |
| `env` | Optional. Merged *under* a job spec's `runtime.environment`, so a spec can override it. |
| `max_concurrent_jobs` | How many recorded jobs this host may run at once. Checked before a job starts. |

`infra config validate` parses this without any network call, and rejects a relative `root`,
the filesystem root, and two hosts with the same `name`.

## Why the SSH alias cannot be used

`SshExecutor` deliberately passes `-F /dev/null` with an explicit `-i`, so the operator's
`~/.ssh/config` never applies to a worker — that is what keeps the automation's
authentication story explicit and auditable. An alias in `~/.ssh/config` therefore cannot be
named in `host`. Translate it once:

```bash
ssh -G <alias> | awk '/^(hostname|user|port|identityfile) /{print}'
```

Copy `hostname` into `host`, `user` into `username`, `port` into `port`, and the first
`identityfile` into `private_key`. `~` is expanded by the configuration loader for
`private_key`; `root` is not, because it is a remote path and must be absolute.

`StrictHostKeyChecking=accept-new` applies on first contact, with a dedicated known-hosts
file at `ssh.known_hosts_file`, so the operator's own `known_hosts` is never modified. If the
host key changes after a rebuild, delete only that host's entry from the dedicated file and
re-run bootstrap.

Password authentication is not an option: `ssh.py` sets `BatchMode=yes`,
`PasswordAuthentication=no`, `KbdInteractiveAuthentication=no`, and `IdentitiesOnly=yes`.
A key must be in place.

## What the operator must install

This tool installs nothing on the host. Before the first bootstrap, the account needs:

`python3` (3.10 or newer), `git`, `curl`, `tar`, `gzip`.

Bootstrap reports **every** missing prerequisite in one list and exits non-zero rather than
installing any of them. `uv` is *not* in the list: it is a `WARN` when absent, because only a
job specification's own argv ever invokes it, never the reviewed runner.

## The confined root

```
<root>/                 0700  the only directory this tool writes beneath
<root>/state/           0700  bootstrap marker: bootstrap-version
<root>/job_runner.py    0600  the reviewed runner, digest-verified on every install
<root>/jobs/<job-id>/         one directory per job, holding source/, inputs/, outputs/, logs/
<root>/cache/                 rebuildable artifact cache (never canonical)
```

The mirrored settings files are the only thing written outside `<root>`, and they are the
same home-relative, digest-verified files the controller receives: `.tmux.conf`,
`.config/opencode/opencode.jsonc`, `.config/lazygit/config.yml`, and
`.config/yazi/yazi.toml`. A differing remote file is never replaced without an explicit
`--yes` and is backed up beside itself first.

## Commands

```bash
infra worker list --provider static_ssh    # declared hosts, with the readiness proved locally
infra worker show <name>                   # endpoint, state, and this controller's readiness
infra worker wait-ssh <name>               # prove the endpoint answers
infra worker exec <name> -- id -un         # one argv over direct, non-interactive SSH
infra worker bootstrap <name>              # confined bootstrap, then require READY health
infra worker health <name>                 # re-inspect without changing anything
infra worker apply-config <name>           # mirror the operator set; --yes to override drift
infra worker ssh <name>                    # interactive shell
infra job submit <spec.json> --worker <name> [--wait]
infra job status <job-id> / logs / cancel
```

`infra worker start`, `stop`, and `destroy` **refuse** a declared host: this tool did not
create it and cannot delete or stop it. The refusal names the `[[static.workers]]` entry
whose removal is the way to stop using the host.

`infra worker list --provider static_ssh` shows `RUNNING` for every declared host — the
declaration *is* its existence — with the readiness this controller actually proved in
parentheses, for example `RUNNING (SSH_READY)`. The provider state never changes, so the
local readiness is the part that decides whether a job can run there.

## Cost

There is none to report, and none is invented: `hourly_cost` is `None`, `infra provider list`
shows the literal `-` in the cost-model column, and no USD/hour or compute-unit unit is
borrowed from another provider. `[[static.workers]]` is not a paid resource, so no
provisioning guard applies; the guard that does apply is `max_concurrent_jobs`, which is
checked against this controller's active job records before a submission starts.

## Recorded jobs on this provider

The job contract is unchanged: an explicit full commit SHA, a clean detached checkout
verified equal to the request on both sides, the same reviewed runner, and S3 for declared
inputs and outputs. One difference matters when writing a spec:

> `command.argv` is executed **directly**, never through a shell
> (`jobs/models.py`, `worker/job_runner.py`). `module load`, `conda activate`, or any other
> shell built-in therefore cannot carry state between `setup.argv` and `command.argv`.

Write specs that name absolute interpreter paths and declare their environment explicitly:

```json
{
  "schema_version": 1,
  "name": "example",
  "source": {"repository": "https://github.com/<owner>/wavCSE.git", "commit": "<40-hex>"},
  "command": {
    "argv": ["/usr/bin/python3", "-m", "improvements.run_improvements", "--seed", "42"],
    "working_directory": "."
  },
  "runtime": {"environment": {"PYTHONUNBUFFERED": "1"}}
}
```

`runtime.environment` from the spec wins over the declaration's `env`; both are delivered
inside the descriptor on the SSH stdin stream, exactly like a presigned URL, so neither
appears in a process argument list or in durable job state.

Declared inputs and outputs need canonical storage configured on the controller
(`storage.bucket`, `aws.region`, and role-based credentials) exactly as they do on a Pod;
without it, only a specification that declares no artifacts can run.

Concurrency is per declaration: a second submission while `max_concurrent_jobs` jobs are
active is refused, naming the host, the limit, and the running job IDs. `infra job cancel`
terminates only that job's own process group; it never stops anything on the host.

## Local verification before touching the host

`scripts/static-host/` builds a throwaway Ubuntu container that looks like this account: a
non-root user with no `sudo`, no `uv`, key-only SSH, the same confined-root layout, and a
fixture `nvidia-smi` stub so readiness can be exercised without a GPU. It is a developer aid,
not a daemon, service, or runtime dependency of the control plane.

```bash
make static-host-up     # prints the [[static.workers]] block to paste into config.toml
make static-host-down
```

Both targets are opt-in and stay out of `make check`, which must remain runnable on a machine
without Docker. The stub proves transport, bootstrap, confinement, settings mirroring, and
job execution — never real GPU capability. Only a health check on the real host shows that,
which is why the first contact with it is a read-only probe and bootstrap refuses a
declaration whose `gpu_type` the machine does not prove.

## Current limitations

- A host is reached directly only; there is no proxy, tunnel, or jump-host support, and
  allocation-only access (a Slurm login node with no GPU) is out of scope: it needs
  batch-script supervision and its own design, not this provider.
- No per-host concurrency above `max_concurrent_jobs`, no queueing, and no cross-host
  scheduling: placement picks one eligible READY host and submits one job.
- The cache under `<root>/cache` is rebuildable and never canonical; deleting it costs a
  re-download, never an artifact. An absent cache root is not an error either: the cache is
  optional by design, and declared artifacts then materialize from canonical S3 storage with
  a warning.
- `infra doctor` does not probe a declared host: doctor reports controller *capability*, and
  a host's reachability is readiness, observed on demand.

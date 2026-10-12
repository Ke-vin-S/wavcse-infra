# Plan — a static SSH provider (university server) for `infra`

> **Superseded by `docs/STATIC-SSH.md` and ADR-038.** This draft proposed registration
> commands, a `scratch_root` field, and a `UNKNOWN`-until-probed provider state; the
> implemented design declares hosts in configuration, declines to reuse the root-capable
> bootstrap profile, and reports a declared host as `RUNNING` with reachability recorded as
> readiness. Kept for the reasoning, not as a procedure.

Status: proposal, not implemented. Every claim below cites current code.

## 1. Verdict

Adding a static university host as a third execution provider is **feasible and
architecturally pre-authorized**: ADR-029 already records that "future static university
SSH workers do not provision or destroy but can share the existing SSH transport and
exact-commit job semantics" (`docs/DECISIONS.md:1465-1468`). The SSH job path is
provider-neutral in everything that matters — exact-commit checkout, `HEAD` verification,
detached supervision, digest-checked inputs, independent S3 output read-back.

It is **not** a configuration change. It is a real vertical slice, because the current
design hard-assumes "the provider is an API that creates a root container you own":

| Assumption | Where it lives | Static host reality |
| --- | --- | --- |
| `apt-get`/`dpkg-query` present, root or **passwordless sudo** | `worker/bootstrap.sh:14-25` | no sudo → bootstrap exits 1 today |
| `uv` installed into `/usr/local/bin` | `worker/bootstrap.sh:68-71` | unwritable without root |
| `/workspace` created by root | `worker/bootstrap.sh:81` | no such path; no root |
| worker paths `/workspace/...`, `/root/...` | `config.py:187-192` | must be a per-user scratch root |
| worker is a RunPod container | `models.py:13-17` `ProviderKind` = `runpod`, `colab` | no third member, no lifecycle |
| endpoint published by the provider, state polled as `RUNNING` | `workers/ssh.py:117-126`, `WorkerSshWaiter` | no provider metadata; reachability is the only state |
| key-only, non-interactive SSH | `workers/ssh.py:133-176`, `280-300` | fine **if** we install a key; password auth is not modeled |
| GPU metadata carries `NVIDIA` | `workers/bootstrap.py:467-472` | must be declared by the operator and prove out via `nvidia-smi` |
| ready worker is chosen, never provisioned | `config.py:244-256`, `cli.py:2419-2455` | static fits this exactly |
| no create/destroy cost guard | `workers/lifecycle.py:36-58` `WorkerProvider` | static has **no** price; do not invent one |

## 2. Decision gate (do this first, no code)

The plan splits on one question the code cannot answer. Run this on the university host
from the controller, using a key you add yourself (`ssh-copy-id` needs no sudo; it appends
to `~/.ssh/authorized_keys`):

```bash
K=~/.ssh/wavcse_static
ssh-keygen -t ed25519 -f "$K" -N ''            # dedicated identity, per ADR-013
ssh -i "$K" user@uni 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys' < "$K.pub"
ssh -i "$K" -o BatchMode=yes user@uni '
  id -u; uname -a; echo "HOME=$HOME"; df -h "$HOME" | tail -1
  command -v python3 && python3 -V
  command -v git uv tmux gzip tar
  command -v nvidia-smi && nvidia-smi -L
  command -v sbatch srun squeue                     # <-- decides the shape
  command -v module; command -v conda
  timeout 20 python3 -c "import urllib.request as u; print(u.urlopen(\"https://github.com\",timeout=10).status)"
'
```

- **Shape A — directly usable host** (you can run GPU processes on it): the plan below.
- **Shape B — Slurm-only login node** (`sbatch`/`srun` exist and GPUs are unreachable
  directly): stop. Phase 5 sketches it; it is a different transport and a different
  supervision model. Do not start Phase 1-4 expecting it to work.

Also confirm: `sftp`/`rsync` not needed (spec forbids SCP dependence), outbound HTTPS to
GitHub **and** S3 works (anonymous clone per ADR-006, presigned URLs per ADR-015), and
`python3` on the host is **>= 3.10** (`worker/job_runner.py:1-20` requires 3.10 and stdlib
only).

## 3. Shape A — implementation plan

### Phase 1 — provider identity, registration, read-only visibility

Goal: `infra provider list` and `infra worker list/show` know about the host as a worker,
with no create/destroy and no cloud calls.

- `models.py:13-17` — add `ProviderKind.STATIC_SSH = "static_ssh"`. Reuse
  `ExecutionTransport.SSH` (`models.py:27-30`) unchanged.
- `config.py` — new `StaticConfig` (`FrozenModel`) with an array-of-tables
  `[[static.workers]]`: `name`, `host`, `port`, `username`, `gpu_type`, `scratch_root`,
  `worker_root`, `runner_path`, `env` (extra environment, e.g. `PATH`,
  `LD_LIBRARY_PATH`), `max_concurrent_jobs = 1`. Reuse `_absolute_worker_path`
  (`config.py:169-181`). Add `Settings.static` and the corresponding
  `WAVCSE_INFRA_STATIC_*` entries to `ENVIRONMENT_FIELDS` (`config.py:276-336`).
- `providers/static.py` (new) — a read-only "client": `get_worker(name)`,
  `list_workers()`. It synthesizes `Worker` from config + local state. Model it on
  `providers/colab.py` (no HTTP), **not** on `providers/runpod.py`.
  - `execution_transport = SSH`, `ssh_direct = WorkerConnectionInfo(kind="direct", ...)`,
    `gpu_type` from config, `hourly_cost = None` (there is no price; never fabricate one).
  - `state`: `WorkerState.RUNNING` only after a successful SSH probe, else
    `WorkerState.UNKNOWN`. Do not emit `STOPPED`/`DESTROYED`; a static host is reachable
    or not.
- `state.py` — add `record_registered(...)` and `forget(worker_id)`. Reuse
  `_upsert`/`write_json_atomically`/`locked` (`state.py:68-104`, `202-243`, `714-717`).
  `provider_worker_id == infra_identity == name`, as Colab already does
  (`state.py:285-288`). Extend `provider_for` (`state.py:720`).
  - **Correctness point:** `WorkerStateStore.reconcile` marks a tracked-but-unlisted worker
    `DESTROYED` + `provider_absent=True` (`state.py:557-567`). That is right for a paid
    resource and wrong for a static host. It is already provider-scoped
    (`state.py:546-547`), so the fix is simply to **never** call
    `reconcile(provider=ProviderKind.STATIC_SSH)`: static records are observed, never
    reconciled against a provider listing, and an unreachable host must never be marked
    absent or destroyed.
- `workers/static.py` (new) — register / observe / forget only. It deliberately does
  **not** satisfy `WorkerProvider` (`workers/lifecycle.py:36-58`), which mandates
  `create_worker`/`start_worker`/`stop_worker`/`destroy_worker`. This provider has none of
  those, and adding no-op implementations would be a lie.
- `cli.py` — `infra worker register --name --host --port --username --gpu-type
  [--scratch-root ...]`, `infra worker forget <id>` (symmetric with `infra volume forget`).
  Extend the `is ProviderKind.COLAB else RunPod` branches in `list_providers`
  (`cli.py:207-233`), `list_workers` (`:251-312`), `show_worker` (`:315-353`).
  Make `worker start/stop/destroy` refuse for `static_ssh` with an actionable message
  (`forget` is the only removal). Keep `worker ssh/exec/apply-config/bootstrap/health`
  working by generalizing `_require_runpod_transport` (`cli.py:2736-2742`) into a
  "requires an SSH transport" guard.

Acceptance: `infra worker register` writes one record; `infra worker show <id>` reports
provider state from a live probe; `infra worker list --provider static_ssh` prints it;
`infra worker destroy <id>` refuses; the unit suite still proves no network access
(`tests/unit/conftest.py:22-42`).

### Phase 2 — non-root worker profile

Goal: bootstrap and health work as an ordinary account with no sudo.

- `worker/bootstrap.sh` — add `--profile user|system` (default `system`, so RunPod is
  bit-for-bit unchanged):
  - `user`: skip the `apt-get`/`dpkg-query` gate (`:14-25`) and the package install
    (`:48-51`); require `python3 >= 3.10`, `git`, `curl`, `tar`, `gzip` to already exist
    and fail with an actionable message naming what is missing;
  - install `uv` with `UV_INSTALL_DIR="${HOME}/.local/bin"` (`:68-71`);
  - create the scratch/worker roots from arguments instead of `/workspace` (`:81`);
  - keep the same idempotent version marker and 0700 state dir, and the same
    `wavcse_bootstrap_complete\t<version>` protocol line.
- `worker/health-check.sh` — unchanged; it is already unprivileged (`df`, `mountpoint`,
  `nvidia-smi`, `python3 --version`).
- `workers/bootstrap.py` — pass the profile; keep `_require_supported_accelerator`
  (`:467-472`) but source `gpu_type` from config for static workers so the check is
  meaningful rather than absent.
  - Decide deliberately: a static host **without** a working `nvidia-smi` must not reach
    `READY`. If the university host is CPU-only, wavCSE has nothing to run there and the
    provider should not be registered at all.

Acceptance: bootstrap completes as a non-root user on the real host, the parsed health
report reaches `WorkerReadinessState.READY`, and a second run is a no-op.

### Phase 3 — recorded jobs on a static worker

Goal: `infra job submit jobs/<id>.json [--worker <static>]` works end to end.

- `cli.py` — add a `static_ssh` branch to `_job_context_for` (`cli.py:2347-2353`) built
  exactly like `_colab_job_context` (`:2323-2345`): reuse `SshExecutor` and
  `WorkerSshWaiter` unchanged, and override `JobsConfig` per worker:
  `worker_root`/`runner_path` from `[[static.workers]]` instead of `/workspace/...` and
  `/root/...` (`config.py:187-192`).
- A thin `StaticJobExecutor(JobExecutor)` subclass overriding only
  `transport_environment()` (`jobs/execution.py:213-221`, merged at `:299`) with the
  configured `env` for that worker — the university environment (`PATH` with the conda/module
  bin dir, `LD_LIBRARY_PATH`). This is the designed hook; Colab uses the same one for
  `/usr/lib64-nvidia` (`jobs/colab_transport.py:220-223`).
  - **Known limitation:** `command.argv` is executed directly, never through a shell
    (`jobs/models.py:180-193`, `worker/job_runner.py:76-78` pins a baseline `PATH`), so
    `module load`/`conda activate` cannot be relied on to persist between `setup.argv`
    (`jobs/models.py:205-211`) and `command.argv`. Job specs for this provider must use an
    absolute interpreter path and declare environment explicitly. Document this.
- `cli.py:2419-2455` `_select_job_worker` — allow `static_ssh` in
  `placement.preferred_providers` (`config.py:244-256`). Recommended order
  `["static_ssh", "colab", "runpod"]`: the university host is the cheapest real capacity
  that requires no provisioning, but this is a placement *preference*, never an implicit
  substitution (ADR-030).
- Cache root: `jobs/context.py:cache_root()` currently returns
  `WorkerRecord.network_volume_mount_path`, which is RunPod volume vocabulary. Add a
  provider-neutral `WorkerRecord.cache_root: str | None` (set from `scratch_root` for
  static) and prefer it in `cache_root()`; leave `network_volume_mount_path` for RunPod.
- Concurrency: enforce `max_concurrent_jobs = 1` per static worker before `start`, so infra
  never steps on the account's own interactive use of a shared machine.

Acceptance: a real recorded job runs on the university host with a declared S3 input and a
declared S3 output, the output is published only after controller-side read-back and
SHA-256 verification, and `infra job cancel` terminates only the job's own process group.

### Phase 4 — documentation, ADR, agent assets

- New ADR (`docs/DECISIONS.md`, after ADR-035):

  > **Static SSH workers are registered, not provisioned.** A static university host has no
  > provider API, no create/destroy, no USD/hour price and no root. Provider identity is
  > `static_ssh` with the existing `ssh` transport; identity is operator-supplied and
  > recorded before use; reachability is the only state; the SSH key is the only credential
  > and password authentication stays disabled. It is still a worker under ADR-005: it is
  > never the sole location of a change or an artifact, and it never runs OMP or recorded
  > work from a dirty tree. It remains multi-user, so `environment_secrets` travel on stdin
  > and never in `argv`.

- `docs/STATIC-SSH.md` (new, modeled on `docs/COLAB.md`): key setup, the non-root profile,
  the environment/`argv` limitation, scratch-root and quota expectations, no-cost-guard
  rationale, and the explicit "this host is not canonical storage" rule.
- Update the places that currently say this does not exist: `docs/ARCHITECTURE.md:39`,
  `docs/SPEC.md:952`, and the `.agents/skills/compute-placement/SKILL.md:13` line
  ("Future static university compute … is not implemented"). Also `docs/SPEC.md:195-211`
  (provider architecture), `docs/SECURITY.md` (multi-user host threat assumptions),
  `docs/OPERATIONS.md`, `config/infra.example.toml`, `.env.example`.
- `make check` and `make agents-check` must pass; `make check` is the required gate
  (`Makefile:62-68`).

### Phase 5 — deferred: Slurm login node (Shape B)

Only if the probe shows `sbatch`/`srun`. Sketch, not scheduled:

- A new `ExecutionTransport.SLURM_EXEC` submitting a reviewed sbatch script that runs
  `worker/job_runner.py __supervise__` inside the allocation.
- `start`/`inspect`/`cancel` become allocation-aware (`squeue`/`scancel`), and
  `worker/job_runner.py`'s detached-supervisor model (ADR-018) is replaced by a batch
  script that the scheduler owns. Node-local storage means the job workspace and the
  runner path are per-node, so `prepare` cannot assume a stable path across phases.
- This is a genuinely different supervision model and needs its own ADR. Do not shoehorn
  it into Phase 1-4.

## 4. Invariants that stay intact

- Exact-commit execution, `HEAD` verification, clean-tree gate (ADR-006, ADR-017) — the
  runner is transport-independent.
- No long-lived cloud credential on the host (invariant 4). Concretely: short-lived
  presigned URLs on stdin only; the controller's worker SSH key stays on the controller;
  the host receives no AWS keys, no GitHub credential, no RunPod token.
- The static host is **not** canonical (invariant 3). Never create or destroy it; never
  treat its disk as durable; `infra volume` remains RunPod-only.
- No fabricated cost. A static provider has no USD/hour and no CU; `hourly_cost = None`,
  and no price guard is applied because nothing is provisioned (invariant 7). The
  acceptable-use/quota policy of the university is the operator's, not the tool's.
- Destructive operations stay explicit (invariant 6): `forget` names the exact worker and
  confirms; `destroy` refuses.

## 5. Residual risks

| Risk | Mitigation |
| --- | --- |
| Shared multi-user host exposes job secrets | secrets already travel on stdin, never `argv` (`jobs/models.py:58-68`), and `mode 0700` for worker state; verify `/proc` and shell history on the host |
| Host killed mid-job by university policy | documented; reconciliation already treats an unreachable worker as unknown, never terminal (ADR-023/024) |
| Home-directory quota too small for the job workspace | `scratch_root` is explicit config; the disk check warns below ~20 GiB (`workers/bootstrap.py:_disk_check`) |
| `python3 < 3.10` on the host | Phase 0 probe; fail with an actionable message rather than a syntax error |
| University-only password auth (no key) | **reject** `sshpass`/askpass: it puts a secret in `argv` or a shell prompt. Add an authorized key instead; if the host truly forbids keys, this provider is not usable |
| Host key pinning across rebuilds | permanent `known_hosts` entry; `StrictHostKeyChecking=accept-new` only for first contact (ADR-013). Manual removal is operator action |

# Architecture decision log

This file records decisions that materially shape `wavcse-infra`. Decisions are
append-only: superseded entries remain for context and link to their replacement.

## ADR-001: Python control plane and Bash machine bootstrap

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

The control plane needs structured configuration, typed provider responses, HTTP
clients, AWS SDK integration, and testable orchestration. Fresh machines still need a
small amount of operating-system setup before Python tooling is available.

### Decision

Use Python 3.12 or newer for control-plane logic and small, strict Bash scripts for
machine bootstrap. Bash scripts use `set -Eeuo pipefail` and are checked with
ShellCheck and shfmt.

### Alternatives considered

- Bash for all orchestration: rejected because state, retries, API normalization, and
  error handling would become difficult to test and maintain.
- Python for first-boot package installation: rejected because it assumes the runtime
  that bootstrap is meant to establish.

### Consequences

Business logic remains unit-testable Python. Bootstrap remains auditable and small,
and must not grow into an orchestration engine.

## ADR-002: No Ansible in v1

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

v1 targets one controller and disposable workers with a limited bootstrap contract.

### Decision

Use idempotent Bash bootstrap scripts rather than Ansible.

### Alternatives considered

- Ansible roles and inventories: rejected because they add a second orchestration
  model before repeated machine-configuration complexity justifies it.

### Consequences

Bootstrap scripts must remain deliberately small. Revisit only if real operational
use produces configuration drift that Bash cannot safely manage.

## ADR-003: No Terraform or OpenTofu in v1

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

The controller is persistent and manually provisioned infrequently. The immediate
automation target is disposable RunPod compute, not declarative AWS account setup.

### Decision

Document controller prerequisites and reconstruction without managing EC2, IAM, or S3
resources through Terraform/OpenTofu.

### Alternatives considered

- A Terraform stack for controller, role, and bucket: deferred until repeated
  provisioning demonstrates a need and ownership boundaries are agreed.

### Consequences

AWS resources are prerequisites, not resources owned by this repository. Operations
documentation must make their required configuration explicit.

## ADR-004: S3 is canonical large-artifact storage

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

Workers are temporary, and the initial embedding collection is about 20 GiB. RunPod
local and network storage can disappear or remain tied to a provider location.

### Decision

Treat private S3 objects as the authoritative copy of reusable embeddings and
explicitly persisted large outputs. RunPod disks and volumes are caches only.

### Alternatives considered

- Git/Git LFS: rejected for large generated research artifacts.
- RunPod network volumes as canonical storage: rejected because they couple durability
  to the compute provider and datacenter.

### Consequences

Future destructive worker operations must first verify durable outputs. Workers will
receive narrowly scoped, expiring presigned URLs rather than AWS credentials.

## ADR-005: Workers are disposable execution environments

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

The controller is the writable development environment; GPU workers exist to execute
committed research workloads.

### Decision

Do not put OMP on normal workers and do not allow workers to become the sole location
of source changes or durable artifacts.

### Alternatives considered

- Developing directly on long-lived GPU machines: rejected because it weakens recovery,
  reproducibility, and cost control.

### Consequences

Worker loss is an expected operational event. Any development-worker fix must return
to the controller and be committed before a recorded run.

## ADR-006: Recorded jobs execute exact Git commits

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

Branches and working trees are mutable and cannot identify a reproducible experiment.

### Decision

Require an immutable commit SHA for every recorded job, use detached checkout, verify
`HEAD`, and reject dirty state unless a future explicit debugging mode allows it.

### Alternatives considered

- Branch or tag references: rejected because they can move.
- Shipping an uncommitted working tree: rejected because GitHub would no longer be the
  code distribution and recovery source.

### Consequences

The controller must commit and push research changes before dispatch. Runtime metadata
will include the verified commit SHA.

## ADR-007: RunPod is the first and only v1 GPU provider

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

RunPod is the current provider. Speculative cloud portability would add interfaces with
no second implementation to validate them.

### Decision

Implement a narrow boundary that normalizes RunPod responses into internal worker
models, but add no unused generic provider framework or alternate implementation.

### Alternatives considered

- A broad multi-cloud provider SDK: rejected as speculative platform engineering.
- Leaking RunPod JSON into CLI commands: rejected because it couples presentation and
  later lifecycle code to an unstable wire schema.

### Consequences

Provider-specific parsing and HTTP behavior live together. Internal models contain only
fields the application uses, plus native status for diagnostics.

## ADR-008: Use stable RunPod REST API v1 while REST API v2 is beta

- **Status:** Superseded by ADR-012
- **Date:** 2026-09-27

### Context

The specification says to use the current RunPod REST API but does not select a
version. Current RunPod documentation exposes both APIs. REST API v2 uses
`https://api.runpod.io/v2` and offers stricter validation and an OpenAPI document, but
RunPod explicitly labels it public beta and warns that endpoints and behavior may
change before general availability. The current stable Pod references document
`GET https://rest.runpod.io/v1/pods` and
`GET https://rest.runpod.io/v1/pods/{podId}` with bearer authentication.

### Decision

Use the documented stable REST API v1 read endpoints for Phase 2. Keep the base URL
configurable and isolate response parsing so a deliberate v2 migration is small. Review
this decision when v2 reaches general availability; do not silently switch APIs.

### Alternatives considered

- Adopt v2 immediately: rejected for this production control plane while the provider
  warns its contract can change.
- Use the legacy GraphQL API: rejected because both the specification and current
  provider direction require REST.

### Consequences

The normalized state mapping is based on v1 `desiredStatus` values (`RUNNING`, `EXITED`,
`TERMINATED`) and retains unknown native values rather than guessing. v2 improvements
are deferred and the specification should explicitly name the selected version or an
upgrade policy.

### Official sources

- [RunPod REST API v1: List Pods](https://docs.runpod.io/api-reference/pods/GET/pods)
- [RunPod REST API v1: Find a Pod by ID](https://docs.runpod.io/api-reference/pods/GET/pods/podId)
- [RunPod REST API v2 announcement and beta caveat](https://www.runpod.io/blog/runpods-rest-api-v2-is-here-one-api-for-your-entire-gpu-stack)

## ADR-009: Model RunPod's two key-authenticated SSH paths explicitly

- **Status:** Accepted for future SSH phase
- **Date:** 2026-09-27

### Context

Current RunPod documentation distinguishes basic SSH proxied through RunPod from full
SSH over a public IP. Basic SSH is available on Pods but does not support SCP/SFTP.
Full SSH requires public-IP support, TCP port 22 exposure, and an SSH daemon in the
container. Official templates often provide the daemon; custom images might not.

### Decision

Do not assume every API-visible `publicIp` is an immediately usable SSH endpoint. In the
future SSH phase, represent proxy and public-IP endpoints separately, require key
authentication, and avoid making artifact transport depend on SCP.

### Alternatives considered

- Treat every Pod as `root@publicIp:22`: rejected because RunPod maps container port 22
  to a provider-assigned external port and not every machine supports a public IP.
- Depend on proxied SCP: rejected because the documented basic SSH path does not support
  SCP or SFTP.

### Consequences

Phase 3 may display normalized v2 `ssh.proxy` and `ssh.direct` endpoint data but does not
claim SSH readiness. S3 remains the planned data path.

### Official source

- [RunPod: Connect to a Pod with SSH](https://docs.runpod.io/pods/configuration/use-ssh)

## ADR-010: Seed controller configuration once from a committed template

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

Controllers need machine-specific non-secret settings, while the repository needs a
complete, reviewable example. Re-running bootstrap must not erase operational choices.

### Decision

Commit `config/infra.example.toml` and have bootstrap copy it to
`~/.config/wavcse-infra/config.toml` only when that file is absent. Keep the established
precedence of CLI, environment, user TOML, then defaults. Keep secrets out of TOML.

### Alternatives considered

- Read the committed example directly at runtime: rejected because controller-specific
  edits would dirty the repository and risk accidental commits.
- Overwrite the user file during bootstrap: rejected because bootstrap must be
  idempotent and preserve operator configuration.
- Put all settings in environment variables: rejected because durable non-secret
  configuration is easier to inspect and reconstruct as TOML.

### Consequences

The committed template and runtime configuration can evolve independently. Operators
must merge newly introduced settings into an existing controller file deliberately;
doctor reports the loaded file and actionable missing settings.

## ADR-011: Install controller agents from pinned official releases

- **Status:** Accepted
- **Date:** 2026-09-27

### Context

The controller needs OMP, Codex CLI, and AGF after reconstruction, but their current
upstream distribution methods are not identical. The base bootstrap must remain
auditable, normal GPU workers must not receive agent tooling, and rerunning bootstrap
must not replace an existing working installation unexpectedly.

Upstream behavior also differs from older assumptions: OMP's recommended Linux path is
now a prebuilt release installer rather than a required Bun package, Codex recommends
its standalone installer rather than requiring Node/npm, and AGF publishes official
release binaries so Cargo/Rust is optional rather than required.

### Decision

Keep agent setup in `controller/install-agents.sh` and have controller bootstrap call it
by default, with `--skip-agents` as the explicit opt-out. Never call it from normal
worker bootstrap.

Install reviewed release pins by default:

- OMP `v18.3.2` through the installer in that exact upstream Git tag, forced to binary
  mode; verify the resulting Linux binary against the release SHA-256 digest.
- Codex CLI `0.157.1` through OpenAI's official standalone installer and its explicit
  `--release` option; the upstream installer verifies the downloaded release digest.
- AGF `v0.15.1` from the official GitHub release archive, verified against the release
  SHA-256 digest, without installing Rust or Cargo.

Install controller-owned binaries in `~/.local/bin`. Persist an idempotent PATH block in
the target user's login-shell profile for both `~/.local/bin` and historical
`~/.cargo/bin`/`~/.bun/bin` installations. Normal runs preserve any command already
found on PATH. `--upgrade` explicitly reinstalls the selected configured release;
version overrides require matching checksum overrides where this repository performs
verification.

Installation never performs agent authentication or writes provider tokens.

### Alternatives considered

- Install OMP with Bun and Codex with npm: rejected because neither runtime is required
  by the current recommended upstream Linux installers.
- Install AGF with `cargo install agf --locked`: supported upstream, but rejected for
  the controller default because the official prebuilt archive avoids an otherwise
  unnecessary Rust toolchain and C compiler.
- Track unpinned `latest` releases: rejected because two fresh controllers could then
  receive different binaries from the same infrastructure commit.
- Put the commands directly in `bootstrap.sh`: rejected because it would obscure the
  stable base-controller setup and make standalone repair harder.

### Consequences

Tool upgrades are deliberate repository maintenance: review upstream changes, update
the release pins and checksums, run validation, then use `--upgrade` on a controller.
An operator can also provide documented environment overrides for a controlled
one-off upgrade. Existing authentication under `~/.omp` and `~/.codex` is preserved.
Historical Cargo or Bun installations remain discoverable because their user-local
binary directories stay on the login-shell PATH.

### Official sources

- [OMP repository and install options](https://github.com/can1357/oh-my-pi#install)
- [OMP official installer](https://omp.sh/install)
- [OpenAI Codex CLI installation](https://developers.openai.com/codex/cli)
- [OpenAI Codex authentication](https://developers.openai.com/codex/auth)
- [AGF repository and install options](https://github.com/subinium/agf#install)
- [AGF v0.15.1 release](https://github.com/subinium/agf/releases/tag/v0.15.1)

## ADR-012: Migrate worker management to RunPod REST API v2

- **Status:** Accepted; supersedes ADR-008
- **Date:** 2026-09-28

### Context

Before Phase 3, RunPod's current official documentation was reviewed again. REST API v1
is now deprecated and scheduled for retirement on November 15, 2026. REST API v2 is no
longer described as public beta, covers Pod create/read/start/stop/delete, and adds a
GPU catalog with count- and cloud-scoped availability and prices. Extending the v1 read
client would create new lifecycle code on an endpoint family with a near-term retirement
date and would still lack the v2 catalog needed for safe pre-creation plans.

REST v2 currently does not include the v1 `interruptible` Pod-create property. It also
does not publish a Pod-create idempotency key or provider-enforced unique-name field.

### Decision

Use `https://api.runpod.io/v2` for Phase 3 worker discovery and lifecycle operations.
Keep the existing bearer credential resolver and provider boundary. Normalize v2 wire
objects into the internal worker and GPU-offer models; do not introduce GraphQL.

Use current catalog list prices and availability for the creation plan and client-side
maximum-price guard. Reject interruptible requests explicitly instead of silently
creating on-demand capacity. Generate a high-entropy exact Pod name, issue create once,
and reconcile ambiguous responses by exact name without retrying the POST.

### Alternatives considered

- Continue with REST v1: rejected because it is deprecated, has a published retirement
  date, and lacks the current REST catalog.
- Mix v1 create with v2 discovery/lifecycle to retain interruptible Pods: rejected
  because it splits one resource across incompatible request/response contracts and
  depends on the retiring endpoint.
- Use GraphQL for pricing or spot creation: rejected because the supported REST surface
  covers the required on-demand lifecycle, and Phase 3 should not add a second API solely
  to recover a field missing from v2.
- Retry create after an exact-name list returns no match: rejected because Pod names are
  not provider-enforced unique and list visibility may lag the create response.

### Consequences

Existing user configuration that pins `https://rest.runpod.io/v1` must be changed to
`https://api.runpod.io/v2`. Phase 2 fixtures and normalization move to the v2 schema.
The provider can safely discover current on-demand offers but cannot create spot Pods;
that limitation remains visible. Maximum-price enforcement is a client preflight guard,
not an atomic provider price reservation. Ambiguous create failures may require manual
inspection, but the automation will not intentionally duplicate a paid Pod.

### Official sources

- [RunPod REST API v2 overview](https://docs.runpod.io/api-reference-v2/overview)
- [RunPod migration guide](https://docs.runpod.io/api-reference-v2/migrate-from-v1)
- [RunPod v2 OpenAPI schema](https://api.runpod.io/v2/openapi.json)
- [RunPod v2 GPU catalog](https://docs.runpod.io/api-reference-v2/catalog/list-gpu-types)
- [RunPod v2 create Pod](https://docs.runpod.io/api-reference-v2/pods/create-a-pod)

## ADR-013: Isolate ephemeral-worker SSH with dedicated TOFU state

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

RunPod exposes command-only proxy SSH and, on eligible machines with `22/tcp`, direct
SSH through a mapped public port. Pods and endpoint mappings are ephemeral. Disabling
host verification would hide interception, while writing these short-lived endpoints
to the user's normal SSH state would mix automation trust with unrelated hosts.

### Decision

Invoke system OpenSSH with an explicit dedicated worker identity, `-F /dev/null`,
batch/key-only authentication, and bounded timeouts. Store accepted keys only in
`~/.local/state/wavcse-infra/known_hosts` with `StrictHostKeyChecking=accept-new`.
Require the direct mapped endpoint for command execution and refresh provider endpoint
metadata while waiting. Permit the basic proxy only for an explicitly interactive PTY
session. Do not depend on SCP/SFTP.

### Alternatives considered

- `StrictHostKeyChecking=no`: rejected because it accepts changed keys silently.
- The user's global `~/.ssh/known_hosts`: rejected because ephemeral infrastructure
  should not mutate or weaken unrelated SSH trust state.
- A Python SSH dependency: rejected because OpenSSH already provides the required key,
  timeout, host-verification, and subprocess behavior with a smaller dependency surface.

### Consequences

The first connection uses trust on first use and is therefore not protected against a
first-contact network attacker. Subsequent changed keys fail closed. Operators must
inspect the exact provider endpoint before removing a stale entry. The RunPod proxy is
unsuitable for non-interactive automation. Phase 4 streams only small reviewed scripts
through direct SSH stdin and later artifacts use S3.

### Official sources

- [RunPod: Connect to a Pod with SSH](https://docs.runpod.io/pods/configuration/use-ssh)
- [RunPod REST v2: Get a Pod](https://docs.runpod.io/api-reference-v2/pods/get-a-pod)
- [RunPod REST v2: Create a Pod](https://docs.runpod.io/api-reference-v2/pods/create-a-pod)

## ADR-014: Constrain direct-SSH Pods with RunPod GraphQL public-IP placement

- **Status:** Accepted; narrows ADR-012's REST-only decision
- **Date:** 2026-09-28

### Context

Live Community Cloud validation produced a Pod with `startSsh` and `22/tcp` but only the
basic PTY proxy. REST v2 catalog and create schemas do not expose a public-IP placement
constraint. Current official RunPod interfaces do: the GraphQL schema exposes
`supportPublicIp` in both compatible-price lookup and `podFindAndDeployOnDemand`, and
the official CLI exposes it as `pod create --public-ip`.

### Decision

Keep REST v2 for reads and ordinary lifecycle operations. When direct SSH is required,
use GraphQL only for public-IP-filtered offer discovery and the single create mutation.
Require `startSsh`, `22/tcp`, and `supportPublicIp: true` together. Apply the existing
availability, confirmation, exact-name reconciliation, and maximum-price safeguards to
the compatible offer rather than the broader REST catalog offer.

### Consequences

Community Cloud remains usable when compatible capacity exists. An unavailable
compatible offer fails before confirmation, and the placement mutation cannot silently
select a no-public-IP host. Secure Cloud may have more consistent provider-managed
capacity, but it is neither required nor used as an implicit fallback. GraphQL remains
isolated inside the RunPod provider and is not introduced as a general abstraction.

### Official sources

- [RunPod GraphQL schema](https://graphql-spec.runpod.io/)
- [RunPod CLI Pod reference](https://docs.runpod.io/runpodctl/reference/runpodctl-pod)
- [RunPod SSH methods](https://docs.runpod.io/pods/configuration/use-ssh)

## ADR-015: Transfer artifacts through presigned URLs carried on SSH stdin

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

Phase 5 must move artifacts between canonical S3 and disposable workers without giving a
worker any durable cloud identity. Two mechanisms were available: presigned URLs over
HTTP, or reusing the direct SSH channel that Phase 4 already established. A presigned URL
is a bearer secret, so where it travels matters as much as how it is scoped: a command
line is visible to `ps` on both sides and tends to reach logs, while an environment dump
is equivalent.

### Decision

Use S3 presigned URLs as the only worker artifact transport, and send the URL on the
direct SSH session's stdin rather than in a command line. The controller streams one
generated assignment line followed by the reviewed transfer module, so the worker runs
exactly the code the repository contains and the URL exists only in the SSH stream and
in the worker process's memory.

Keep that transfer program stdlib-only and packaged inside `wavcse_infra` so the same
file is unit-tested on the controller and executed on the worker. Do not add boto3, the
AWS CLI, or the wavcse-infra package to normal workers.

### Alternatives considered

- `curl`-based worker scripts: rejected because the transfer logic (partial file, size
  and digest enforcement, atomic rename, structured result) would be duplicated in Bash
  and not exercisable by the unit suite in the same form.
- Passing the URL as a remote command argument: rejected because it exposes the
  signature in process argument lists and in any command echo.
- Installing boto3 or the AWS CLI on workers: rejected because it grows the worker
  dependency surface for no capability gain and invites durable credential handling.
- Shipping the wavcse-infra package to workers: rejected because the worker contract is
  a small reviewed program, not a control-plane installation.

### Consequences

Workers need only Python 3, which bootstrap already installs and health-checks. The
transfer module may not import anything outside the standard library and may not use a
`from __future__` import, because the controller prepends two assignments before its first
line; both properties are enforced by tests. Artifact bytes still travel through S3, so a
worker never becomes a data path between machines, and losing a worker cannot lose the
only copy of an artifact.

## ADR-016: Verify stored artifacts by metadata; trust digests recorded at creation

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

Reproducibility depends on being able to tell whether a large artifact is the one that
was recorded. The embedding set is approximately 20 GiB. Re-hashing it on the controller
after every upload or before every job would cost a full download, consume controller
bandwidth, and still not prove the data was readable by the worker that consumes it. An
S3 ETag is also not a content digest: it depends on multipart part boundaries.

### Decision

Compute SHA-256 once, at artifact creation, and record it in the version 1 manifest.
Verify presence, stored size, and manifest consistency at the controller. Enforce the
digest on the machine that will actually consume the artifact: the worker download path
verifies expected size and expected SHA-256 before materializing a file. Refuse to treat
an ETag as a checksum, and state the limitation explicitly in `infra storage verify`
output and in the verification model.

### Alternatives considered

- Download-and-rehash at the controller for verification: rejected as wasteful at this
  artifact size and still weaker than verifying where the data is used.
- Trust the S3 ETag: rejected because it is not a content digest and multipart uploads
  make it implementation-defined.
- Skip digest enforcement when a digest is supplied: rejected because a silent truncation
  or partial write would surface later as an unexplainable research failure.

### Consequences

`infra storage verify` is a metadata-level check and says so. A worker-reported digest
after upload is producer-claimed evidence; stored size is provider-verified evidence;
cryptographic confirmation happens only where the bytes are read. Artifact integrity
therefore rests on the creation step being trustworthy, which is why manifests validate
digest form, forbid bearer material, and require a full Git commit ID rather than a
branch name when a generator commit is recorded.

## ADR-017: Recorded jobs execute a verified commit and prove it per phase

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

Phase 6 must make a recorded experiment reproducible. A specification could name a
branch, a tag, a short prefix, or a working tree, and any of those would identify
different bytes at different times. The controller also cannot observe what actually ran
unless the machine that executed the job reports it independently.

### Decision

Require a full 40- or 64-character commit object ID in a version 1 JSON job
specification, and make the worker resolve, check out, and verify it:

```text
spec.source.commit
  -> git clone --no-checkout --filter=blob:none --no-tags --depth 1
  -> git fetch --depth 1 origin <commit>   (fallback: full fetch, then require the object)
  -> git checkout --detach --force <commit>
  -> git rev-parse HEAD  ==  spec.source.commit   (hard failure otherwise)
  -> git status --porcelain must be empty
```

The controller requires the executing worker to report `HEAD` back, and compares it with
the requested commit before starting the command; the worker repeats the comparison at
start time. A mismatch is fatal on both sides, and the durable record stores the reported
executed commit. Source materialization is anonymous HTTPS only, so a disposable worker
never receives a GitHub credential or key.

### Alternatives considered

- Branch, tag, or `HEAD` references: rejected because they move and cannot identify an
  experiment.
- Trusting the controller's clone and shipping a working tree: rejected because the
  worker would then execute unverified bytes and GitHub would stop being the source of
  truth for the run.
- Recording the requested commit as executed: rejected because the run would then claim
  provenance it never proved.
- Private-source credentials on the worker: rejected; the wavCSE repository is reachable
  anonymously over HTTPS, and a read-only mechanism would be needed before supporting a
  private remote.

### Consequences

A commit that has not been pushed cannot be executed; the failure names that explicitly.
Dirty in-place debugging on a worker is impossible in Phase 6. Git LFS blobs are
deliberately not fetched (`GIT_LFS_SKIP_SMUDGE=1`), so a job that needs large LFS content
must materialize it as a declared S3 input instead.

## ADR-018: Detached workered execution without a daemon or tmux

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

A recorded GPU job can run for hours. If the job were a child of the SSH session, closing
the terminal or losing the connection would kill it, and job status would be unknown. The
controller is stoppable, so nothing durable may live only in a controller-side process
either. Phase 4 installs only stable Ubuntu prerequisites and no process multiplexer; the
architecture forbids adding a worker daemon.

### Decision

Install one reviewed, stdlib-only Python file (`worker/job_runner.py`) on the worker at
`jobs.runner_path`, verified by SHA-256 and written atomically, and drive it with one
bounded SSH command per phase. `start` launches a supervisor in a new session
(`start_new_session=True`) with the job's environment, stdout/stderr appended to
`logs/job.log`, and no controlling terminal, so the job survives SSH disconnection and
controller exit. Each stage runs in its own process group so a timeout or cancellation can
terminate the whole tree without signalling the supervisor or anything else.

Status, logs, and cancellation are read from the worker's own recorded files
(`state/pid.json`, `state/finished.json`, `state/cancelled.json`, `logs/job.log`). No
tmux, no daemon, no message queue, and no controller-side long-running process.

### Alternatives considered

- tmux on workers: rejected because Phase 4 does not install it, adding it would grow the
  worker bootstrap contract, and a session multiplexer is not needed to detach one job.
- `nohup`/`setsid` around a shell string: rejected because the exit status, timeout, and
  cancellation logic would become an un-reviewed shell program rather than a tested one.
- A worker-side job service or queue: rejected as a daemon, explicitly out of scope.
- Keeping the job as an SSH child: rejected because a dropped connection would kill a
  multi-hour experiment and destroy its status.

### Consequences

Because the runner is installed at a path, a running job keeps executing the reviewed code
it started with, and reinstalling an identical digest is a no-op. Cancellation verifies
the recorded `/proc/<pid>/stat` process start time and process-group identity before
signalling; a pidfd pins the PID during signalling, and the supervisor command line is
checked too. A worker that disappears leaves the job
record with `worker_absent` and an explicitly unknown outcome instead of a false
`RUNNING`.

## ADR-019: wavCSE keeps MLflow ownership; Phase 6 supplies provenance and secrets by name

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

wavCSE already creates MLflow runs (`improvements/mlflow_utils.py`, driven by each run
script) and loads `MLFLOW_TRACKING_*` from a gitignored `.env`. Writing MLflow runs from
`wavcse-infra` would duplicate and then compete with the research layer, and inventing a
second tracking mechanism is forbidden. At the same time, a real research run needs its
tracking credentials on the worker, and the repository forbids sending controller
credentials to workers.

### Decision

`wavcse-infra` never writes to MLflow and never creates a run. Phase 6 supplies the
non-secret provenance environment the specification already documents (`INFRA_PROVIDER`,
`INFRA_WORKER_ID`, `INFRA_GPU`, `INFRA_GPU_COUNT`, `INFRA_GIT_COMMIT`, `INFRA_JOB_ID`,
`INFRA_WORKER_NAME`) for wavCSE to log, and records the same facts locally.

Job tracking credentials are referenced by *name* only, in
`runtime.environment_secrets`. At submit time the controller resolves those names from its
own process environment and sends the values inside the descriptor JSON on the SSH stdin
stream. They are never written to the specification, local state, logs, command lines, or
worker files. Reserved name families (`AWS_`, `RUNPOD_`, `WAVCSE_`, `INFRA_`, `SSH_`, and
anything containing `PRIVATE_KEY`) are rejected, so a specification cannot request a
controller credential.

### Alternatives considered

- Creating MLflow runs in `wavcse-infra`: rejected as a competing layer that would
  misattribute research metadata.
- Copying a controller `.env` or credential file to the worker: rejected because workers
  are less trusted and must not receive durable controller credentials.
- Putting the tracking token in the job specification or local state: rejected because
  specifications and state are reviewable, persisted artifacts.
- Passing secrets as remote argv: rejected because process argument lists are visible to
  other processes on the worker.

### Consequences

A job that needs MLflow reporting declares `runtime.environment_secrets` and the operator
exports those variables in the submitting shell. Missing values fail before any record is
created. The worker holds the values only in process memory for the life of the job, which
matches the existing threat assumption that a compromised worker may expose short-lived
values delivered to it.

## ADR-020: Parallel ranged, resumable worker artifact downloads

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

Phase 5 downloads one S3 object with a single `urllib` connection and stages it in a
PID/random sibling file, so an interrupted multi-gigabyte transfer restarts from byte
zero. A live Phase 6 job on a Community worker in France pulling a 1.26 GB object from
`ap-south-1` exposed the cost: that one long-lived TCP flow averaged roughly 0.28 MB/s
while eight parallel HTTP ranges over the same path reached roughly 22.7 MB/s. The
observation is one measured path, not a target or an SLA. Worker CPU, GPU, disk, and
general connectivity were demonstrably not the bottleneck; the pathology was a single flow
over roughly 181 ms RTT with loss and reordering.

### Decision

Keep one downloader with two transports, selected by information Phase 5 already has:

- unknown expected size, or below 64 MiB: the existing single-connection path, unchanged;
- known expected size at or above 64 MiB: inclusive HTTP byte ranges.

Ranged downloads use bounded concurrency — default 8, hard maximum 16, selectable through
`infra storage download --concurrency` — with a 16 MiB range size. Each range is written
at its own offset with `os.pwrite` into a deterministic `<destination>.wavcse-partial`
file, and its durability is recorded by atomically replacing
`<destination>.wavcse-partial.json`. That record is keyed by destination, expected size,
expected SHA-256, and range size, and contains no bearer material, so a later invocation
holding a new presigned URL resumes instead of restarting. State that does not match the
current identity, cannot be parsed, or belongs to an artifact that now uses the
single-connection path is discarded.

Per-range retries are bounded: four attempts with exponential backoff, for connection
resets, timeouts, 408/425/429, temporary 5xx, and truncated bodies. An expired or rejected
authorization, a malformed or mismatched `Content-Range`, an announced-size mismatch, or
extra bytes beyond the requested range fail immediately. An endpoint that answers a range
request with HTTP 200 triggers one sequential full-object fallback rather than
concatenating whole bodies.

Completion of every range is never treated as artifact integrity: the assembled file must
match the expected size and the whole-object SHA-256 before it is placed at the
destination, and placement stays atomic (sibling hard link, or `os.replace` with
`--overwrite`).

### Alternatives considered

- `curl`/`aria2` on the worker: rejected because it adds a worker dependency, duplicates
  size and digest enforcement outside the unit-tested module, and tends to want the URL on
  an argument list.
- Keeping one connection and raising timeouts: rejected because the bottleneck was RTT,
  loss, and reordering, not a local timeout.
- One file per completed range in a partial directory: rejected because assembly needs a
  concatenation pass and roughly doubles peak disk use for a multi-gigabyte artifact.
- Recording range progress in a database or controller-side service: rejected because
  workers are disposable and the transfer program is stdlib-only.
- Treating S3 ETags as digests for resume verification: rejected; multipart ETags depend on
  part boundaries and are not SHA-256.
- Always using ranged transfer: rejected because tiny objects do not justify the machinery.
- S3 Transfer Acceleration, a CDN, or a region-local mirror: out of scope here; each is a
  separate architectural decision with its own cost and security surface.

### Consequences

The worker stays stdlib-only (`concurrent.futures`, `fcntl`, and `json` are standard
library), and Phase 6 keeps its command line and gains the faster transport by default.
A worker can now hold one artifact's partial state on disk between attempts, which the
operator owns and deletes to abandon a transfer. Concurrency multiplies sockets per
worker, which is why the bound is explicit and small. Measured throughput remains a
property of the path between one worker and one bucket region, so the numbers above are
recorded as an observation and not as an expectation.

## ADR-021: Harden the resumable ranged downloader

- **Status:** Accepted
- **Date:** 2026-09-28

### Context

ADR-020 introduced parallel ranged downloads with resumable partial state. A read-only
adversarial review of that implementation found six defects: HTTP body-read failures
escaped the bounded retry and redaction path; staging files were trusted from their
pathname alone; resume identity accepted an expected size without a digest, which allowed
two same-sized artifacts to be combined; the destination lock was attached to the staging
inode, so it disappeared when that name was renamed during completion; one future was
created per range instead of per worker slot; and the security documentation claimed
staging files are always removed, which contradicted the deliberate retention of resumable
state.

### Decision

Amend the ADR-020 design without changing its transport:

- Opening a response and consuming its body are one controlled attempt. Connection resets,
  timeouts, `http.client.HTTPException` (including `IncompleteRead`), and truncated bodies
  raised during a read are mapped to the same bounded per-range retry, and every surfaced
  transport message is passed through URL redaction. `main` also converts any unexpected
  exception into the sanitized error protocol instead of a traceback. A range is recorded
  only after its bytes are complete and fsynced, and every retry rewrites its whole range,
  so a failed attempt cannot leave a half-trusted range behind.
- Staging files, the metadata record, and the lock file are validated through the returned
  descriptor: `O_NOFOLLOW`, regular-file mode, exactly one hard link, and a pathname that
  still names that inode. Placement hard-links the open inode relative to an open
  destination-directory descriptor (through `linkat` on `/proc/self/fd` on a Linux worker,
  with a re-verified pathname fallback where procfs is unavailable). Residue that merely
  hard-links an already-placed artifact is removed under the lock.
- Resume requires the expected SHA-256. A large object without one is still fetched in
  parallel, but into a one-shot staging file with no persisted state, and any leftover
  resumable state for that destination is discarded first. The record schema is version 2;
  version 1 records are never reused.
- The destination-wide lock lives in its own fixed file that is never unlinked, and it is
  held across the entire critical section.
- Range work is scheduled through a rolling window sized by the configured concurrency, and
  the executor is always joined before failure returns, so no write continues afterwards.

### Alternatives considered

- Keeping inode-coupled locking and relying on short critical sections: rejected because the
  completion rename is exactly when the lock must still hold.
- Requiring no digest and validating resume state by re-hashing recorded ranges: rejected
  because per-range digests were not recorded, and adding them would still not identify an
  artifact whose expected digest is unknown.
- Falling back to a purely sequential transfer when no digest is supplied: rejected because
  it would remove the measured parallelism benefit from direct CLI downloads while
  providing no additional safety over one-shot staging.
- Refusing to download without a digest: rejected as unnecessarily restrictive; size remains
  checked and nothing resumable is stored.
- Addressing placement by pathname with an `os.path.isfile` pre-check: rejected because it
  does not close the validation/use window.
- Adopting a transfer library for range scheduling and staging: rejected; the standard
  library covers it and the worker program must stay stdlib-only.

### Consequences

Interrupted resumable downloads keep a validated staging file and record; integrity
failures remove them; non-resumable transfers keep nothing. Placement no longer depends on
a pathname that another actor could have replaced, at the documented cost of using
`/proc/self/fd` on Linux and a re-verified pathname fallback elsewhere. A worker can hold a
staging file plus an empty lock file next to a destination, which operators own and can
remove. Direct CLI downloads without `--expected-sha256` are parallel but non-resumable,
which is the documented, safe default when no immutable content identity is known.

## ADR-022: Inode-anchored placement, destination-wide locking, and crash-residue recovery

- **Status:** Accepted
- **Date:** 2026-09-28
- **Amends:** ADR-020, ADR-021

### Context

A second read-only adversarial review of the ranged downloader found that placement could
still be redirected through a pathname. `--overwrite` linked the verified inode to a
sibling `... .wavcse-stage-*` name and then renamed that name onto the destination, and the
no-procfs fallback linked from the staging pathname after re-checking it. Both are
"re-check a pathname, then operate on the pathname" windows: a third party that replaced
the name in the window could make the destination whatever the *substituted* file was.
Reproduced against the previous code, a swapped rename source made `download` report the
verified digest while the destination held `attacker-bytes`.

Three further defects accompanied it. Only ranged downloads took the destination lock, so
a small, size-unknown, or digest-less download could run concurrently with a ranged one.
A crash between creating the `... .wavcse-stage-*` link and the rename left a second hard
link for the staging file, which the `st_nlink != 1` guard then refused on every later
attempt: a permanent wedge needing manual cleanup. And the shutdown harvest added ranges
that finished during a failure to the in-memory set without marking the record dirty, so
those durable ranges were not persisted and were re-fetched on the next attempt.

### Decision

- Placement never moves a pathname. The destination entry is created with `linkat` from
  the open verified inode (through `/proc/self/fd`), then re-checked so the created entry
  is proven to be that inode; anything else is unlinked and reported. `--overwrite` removes
  the previous entry first, which makes it a two-step replace with a briefly absent
  destination name, in exchange for never placing bytes that were not verified. A racing
  writer that takes the name first is never overwritten.
- Where `/proc/self/fd` is unavailable the destination is linked from the staging name
  after re-proving that name, and the created entry is then verified and removed again if
  it is not the verified inode. That fallback is documented as requiring a destination
  directory that is not adversarially writable.
- Every download takes the destination lock, so serialization does not depend on the
  transport, the size, or the presence of a digest. The lock file remains an empty,
  never-unlinked sibling.
- A staging file with extra links is refused as before, but aliases in our own
  `... .wavcse-stage-*` namespace that resolve to that exact inode are released first as
  placement crash residue. A hard link to any other file keeps the refusal, so the
  shared-file defense is unchanged.
- The shutdown harvest marks the record dirty, and persisting runs in a nested `finally`,
  so ranges that complete while a transfer shuts down are resumable afterwards.

### Alternatives considered

- Keeping the rename and verifying the destination afterwards: rejected because the
  previous destination content is already gone by then, so a detected substitution cannot
  be undone without losing data.
- `renameat2` with `RENAME_EXCHANGE` to swap and restore: rejected because Python does not
  expose it, and the worker program must stay stdlib-only.
- Refusing to place at all without procfs: rejected as a hard portability regression; the
  verified-then-undone fallback is weaker but never silently wrong.
- Creating a lock file only when a ranged transfer is chosen: rejected because it is exactly
  the bypass the review found.
- Globally releasing any extra link on a staging file: rejected because it would delete a
  link planted to a victim file and silently write through a shared inode.
- Persisting on every harvest regardless of dirtiness: rejected as needless metadata
  writes; the dirty flag is set by the harvest instead.

### Consequences

`--overwrite` is no longer a single atomic replacement: readers can observe the destination
absent in the interval between removing the old entry and creating the new one, and a crash
there leaves the previous entry gone. In exchange, the destination can only ever be the
verified inode, and the crash leaves the resumable staging file and range record intact
with one link, so the next attempt continues rather than wedging. Small downloads now leave
an empty lock file next to their destination like ranged ones.

## ADR-023: Keep readiness monotone and never fabricate a terminal job state

- **Status:** Accepted
- **Date:** 2026-09-28
- **Amends:** ADR-017, ADR-018

### Context

The first real wavCSE embedding-generation bring-up produced three infrastructure defects.

A fully bootstrapped, healthy worker was recorded as `READY`, then a read-only SSH/exec
operation on that worker rewrote local readiness to `SSH_READY`. Submission requires
`READY`, so the next `infra job submit` failed, and only `infra worker health` restored it.
`WorkerStateStore.mark_ssh_ready` wrote `SSH_READY` unconditionally, even though an SSH
probe proves strictly less than a bootstrap plus a health inspection.

A ~1.18 GiB checkpoint download exceeded `ssh.transfer_timeout_seconds = 3600`, which was
equal to `storage.presign_expiry_seconds`. The controller recorded the job `FAILED` and
stopped watching, while the worker-side download kept running and completed successfully
about fifteen minutes later. An autonomous caller reading `FAILED` could have retried or
reallocated while the original transfer still held the machine and bandwidth.

For the same job the CLI reported that "the job workspace was removed or the worker was
rebuilt". Inspection showed the workspace present, the input materialization still running,
`state/` empty, and the experiment command never started. Status collapsed every
"no finished record and no live process" case into one conclusion that the evidence did
not support, because the worker reported nothing about how far preparation had reached.

### Decision

- Readiness is a ladder, not a last-writer-wins flag. `mark_ssh_ready` and
  `mark_gpu_healthy` record *at least* their capability and never replace a stronger one;
  a full health inspection remains authoritative in both directions, and leaving `RUNNING`,
  destruction, and provider reconciliation still reset readiness.
- A controller-side bound is not evidence about a remote phase. A bounded SSH timeout or a
  dropped connection during a transfer or a job phase raises a distinct
  `RemoteOperationInterruptedError`, and the job stays `PREPARING` with
  `reconciliation_required`, the preparation phase it had reached, and the interruption
  time. No `failure_reason` and no `finished_at` are written.
- `FAILED` is written only from evidence: a reported command failure, a recorded process
  that is gone with no outcome, a workspace that is absent although preparation had
  completed, or a launch that provably never recorded a process. A remote process that may
  still be alive is never reported as a failure.
- Reconciliation is the continuation of an idempotent preparation pass, not a new
  mechanism. `infra job status` (and `--wait`) reuses the same install/prepare/materialize/
  start code as submission. A destination that already exists is verified by size and
  SHA-256 on the worker before it is trusted; a transfer another process still owns is
  reported instead of duplicated; a command that may already have been launched is never
  launched again.
- The worker runner records how far preparation reached: workspace present, checkout
  prepared for this job, launch slot claimed, and whether the recorded launch process is
  still alive. `prepare` is serialized per job with an advisory lock, and the launch slot
  records its owner's identity so a later inspection can distinguish "still starting" from
  "died before recording a process" without guessing.
- One bounded transfer attempt can never outlive the URL that authorises it: the attempt
  bound is `min(ssh.transfer_timeout_seconds, presign_expiry_seconds - 30)`. Long transfers
  are retried with a freshly presigned URL and resume from the ranges recorded beside the
  destination, which is what makes a per-attempt bound safe.
- The worker transfer module gained a read-only `verify` subcommand that reports one
  already-placed artifact (and whether a transfer currently holds its lock) without
  downloading, writing, or carrying a URL. The download path is unchanged.

### Alternatives rejected

- Making readiness fully monotone: rejected. A genuine invalidation (worker left `RUNNING`,
  destroyed, failed its health inspection) must still clear `READY`, or a dead worker would
  keep accepting jobs.
- Converting the controller timeout into `RUNNING` or a new terminal `UNKNOWN` state:
  rejected as a state-machine change that would still let a caller act on an outcome the
  controller does not know. `PREPARING` plus explicit evidence and a reconciliation flag
  keeps the documented vocabulary and tells the caller what to do next.
- Declaring `FAILED` once a reconciliation deadline expires, regardless of liveness:
  rejected, because a long transfer that is still making progress would be reported as
  failed, which is the original incident.
- Re-downloading a destination that the worker already holds: rejected. The resumable
  transfer is verified first, so completed bytes are reused and never silently replaced.
- A background reconciler, daemon, or queue: rejected. Reconciliation is a bounded,
  synchronous pass driven by an operator or a caller command, exactly as Phase 6 requires.

### Consequences

`infra job status` can now perform work: for a job that is still `PREPARING` it resumes an
interrupted input materialization and may start the command that never started. That is
the deliberate recovery for an interrupted submission, and `infra job cancel` is the
explicit opt-out. `infra job submit` exits 1 while a job is `PREPARING` with
`reconciliation_required`, so a scripted caller cannot mistake "unknown" for "running".

Durable job records gained three optional fields (`preparation_phase`, `interrupted_at`,
`reconciliation_required`) with defaults, so existing records and existing job states load
unchanged. A worker-side input materialization that the controller no longer waits for may
still finish inside that job's own directory; it is reported, not silently assumed to have
stopped, and it disappears with the disposable worker.

## ADR-024: One reconcilable-uncertainty model, and worker-side ordering for every decision

- **Status:** Accepted
- **Date:** 2026-09-29
- **Amends:** ADR-017, ADR-018, ADR-023

### Context

An independent review of the Phase 6.1 remediation found that the same class of defect —
turning an observation failure into a terminal job result — survived in six more places, and
that two decisions were being made without mutual exclusion.

`inspect` read the worker's outcome, process, and launch records as independent files and
then concluded from one snapshot: a supervisor that wrote `finished.json` and exited between
the outcome read and the process check could be reported as "no process, no outcome", and a
launch record written during inspection could be reported as "never started". A launch and a
cancellation of the same job were serialized only by controller timing: a cancellation that
observed no start slot could write CANCELLED while the launch claimed the slot a moment
later and ran the command anyway. The controller wrote whole-document JSON without any
compare-and-swap, so a slower process could overwrite newer state. A transfer's attempt
bound was derived from the URL's nominal lifetime even though waiting for SSH had already
consumed part of it, and a URL signed with temporary credentials dies with them. Reloading
during recovery skipped re-verifying inputs a previous attempt had recorded as materialized.
A provider `STOPPED` was treated as evidence about the command. An interrupted output upload
became a failed job. A controller-side storage hiccup became a failed job. An attempt that
exhausted its bounded range retries became a failed job even though the resumable state was
intact. A descendant that outlived its stage leader was invisible to both inspection and
cancellation. And verification re-opened the destination by name after inspecting it, so a
swap in between could make a verified digest describe another file.

### Decision

- One semantic distinction carries all of it: `ReconcilableOperationError`. An interrupted
  observation, an exhausted resumable attempt, an unfinished competing transfer, and a
  transient canonical-storage failure are uncertainty; no caller may record FAILED,
  SUCCEEDED, or CANCELLED from them. Reported remote failures, integrity mismatches,
  authorization or configuration errors, and definitively absent objects remain evidence.
  Storage failures are typed along that line rather than converted wholesale.
- `inspect` derives its report from an ordered, bounded snapshot: read the recorded outcome,
  observe the process state, then re-read the outcome and launch records before concluding
  that nothing is recorded and nothing is running. A terminal conclusion is never reached
  from one inconsistent read.
- Creating a job's process and cancelling a job that has no process yet take a per-job
  lifecycle lock, so exactly one of them decides. A cancellation that wins records a durable
  pre-start exclusion that every later launch refuses; a launch that wins is terminated
  through its own recorded process identity, and a cancellation that arrives while the
  launch is creating its process is told so rather than answered with a false CANCELLED.
- Every mutating controller operation takes a per-job local lock and re-reads the record
  inside it. Atomic replacement plus reload-under-lock is the whole concurrency story for one
  controller machine.
- Readiness is established before a URL is signed; the attempt bound comes from the lifetime
  actually granted; and a lifetime signed with temporary credentials is capped by those
  credentials' remaining validity, refused as transient when too short to be useful.
- Every declared input is verified from worker evidence immediately before the command may
  start, except one established by a transfer in the same pass. A controller-side
  "materialized" flag is a record of a past observation, never present proof.
- A provider state that a started worker can return from keeps its job reconcilable; only
  states that make the workspace permanently unreadable end it, and their reason says the
  outcome can no longer be read.
- An unknown output-upload outcome keeps the job reconcilable and is resolved by inspecting
  the canonical object (key, size, and recency) instead of being recorded as a failure.
- A process group whose leader exited is still this job's execution: it is reported as
  running, it is terminated through its verified group id, and the supervisor reaps
  survivors before it writes the outcome, so a terminal record means nothing of the job is
  still running. PID reuse is ruled out by the recorded start ticks.
- Verification opens the destination once with `O_NOFOLLOW` and hashes that descriptor,
  then re-checks that the pathname still names the inspected inode.

### Alternatives rejected

- A second worker-side daemon or a job supervisor process: rejected. The runner already owns
  the job's lifecycle; two advisory `flock`s and one identity file are the smallest
  synchronization that makes the decisions mutually exclusive.
- Compare-and-swap revisions on every record field: rejected as a much larger change than
  the semantics need. A per-job lock plus reload-under-lock gives the same guarantee on a
  single controller machine.
- Treating every provider state as terminal, or every state as reconcilable: rejected.
  Destruction makes reconciliation impossible, and leaving such a job open forever is its
  own false claim.
- Re-downloading every input on every preparation pass: rejected as wasteful. Verification is
  evidence and costs a read; a transfer costs a download.
- Retrying a transfer attempt forever inside one command: rejected. Bounded attempts plus a
  job-level retry with fresh credentials is what keeps both the URL and the wait bounded.
- Signing a URL with an unbounded lifetime to cover long transfers: rejected; the credential
  cap and resume make a bounded lifetime sufficient.

### Consequences

`infra job status` does more work than before: it re-verifies recorded inputs, may re-attempt
a required output's upload, and may terminate a surviving process group. All of it is bounded
by one attempt per pass, and all of it is reported. A job whose worker is merely stopped now
stays reconcilable indefinitely; an operator ends it explicitly with `infra job cancel` or by
destroying the worker, and a destroyed worker is reported as unrecoverable rather than as a
command failure. Legacy records written before the runner recorded a stage's group id cannot
name a surviving descendant, so a leftover process from such a run must be cleaned up by hand;
every run started by this version reaps its own survivors.

## ADR-025: Bind output provenance to bytes, and never signal an unproven process group

- **Status:** Accepted
- **Date:** 2026-09-29
- **Amends:** ADR-022, ADR-024

### Context

A final bounded review of the Phase 6.1 work found nine remaining defects, five of them
correctness or safety issues:

1. Output recovery accepted an object at the declared key from its size and modification
   time and then recorded *the worker file's* SHA-256 against it, so a different object of
   the same size - an earlier run's artifact, for example - could be recorded as this run's
   output with a digest that never described it.
2. A worker could durably establish that a pre-start cancellation won and that the launch
   was excluded, lose the acknowledgement to a dropped SSH session, and be reported later
   as `FAILED` for having no executed commit.
3. The presign lifetime cap read `credentials.expiry_time`, which botocore's
   `RefreshableCredentials` does not define, so exactly the credentials that expire
   produced no cap at all.
4. Output persistence rebuilt the output list from the outputs completed so far and saved
   it after each one, so an interruption left fewer durable entries than the specification
   declared and a restart failed against a strict pairing of declared and recorded outputs.
5. A process group whose recorded start ticks were missing could still be signalled, and a
   recorded identity could be *erased* by a leader that exited normally - which both
   risked signalling an unrelated group and made a genuine surviving descendant
   unrecognizable.

### Decision

- Canonical storage is asked for the bytes, not for a promise about them. A new
  `verify_object_content` streams one object through a SHA-256, binds the read to the
  version the metadata read reported when the bucket provides one, and compares both the
  size and the digest to the expected values. A freshly uploaded output is verified that
  way before it is recorded, and a recovered object is accepted only when the same check
  passes. The recorded digest is therefore always one the controller observed at the
  declared key. Where a bucket has no versioning, the remaining assumption is that a single
  `GetObject` returns one consistent object; a replacement can only change the answer to
  "these are not the expected bytes", never to a false acceptance.
- Pre-start cancellation is carried out of the worker as explicit evidence: `inspect`
  reports the `pre_start` flag from the worker's durable cancellation record, the controller
  treats it as CANCELLED before it considers commit evidence, and only a cancellation that
  actually executed still requires the verified commit.
- The credential-expiry adapter freezes the credentials (which refreshes them when the
  chain says so) and reads the expiry of the object that would sign. Static credentials,
  which have no expiry, produce no cap.
- Durable job state keeps exactly one slot per declared output, filled in place and saved
  as a whole list, so an interruption leaves a shorter *progress* record, never a shorter
  *structure*. A slot missing from an older record is padded, so a declared required output
  can never become invisible to the success check.
- Process-group identity is a precondition for every signal. `terminate_job_group` refuses
  when the recorded start ticks are missing, refuses when an observed leader has different
  ticks, and otherwise signals only through the group number that a live member proves is
  still allocated to this job's group. The recorded start ticks are no longer overwritten
  with "unknown" when the same leader exits, so an ordinary background process that
  outlives its stage leader remains identifiable and terminable.
- The supervisor re-checks the groups it terminated and refuses to write `finished.json`
  while any of them is still alive, leaving the job nonterminal with its live group
  reported instead of claiming a clean outcome.
- Presign failures are classified as definitive only when the evidence is: no credentials
  or an invalid request. A credential refresh, transport, or service failure during signing
  is `StorageUnavailableError`, so the job stays reconcilable. A generic HTTP 400 is no
  longer transient; only named retryable conditions are.
- Worker-state mutations take a local document lock, reload under it, merge, and write
  atomically, matching the per-job store. The lock's reentrancy is scoped to the thread, so
  a second thread of one process is not mistaken for a nested call by the same holder.

### Alternatives rejected

- Adding a checksum header to the presigned PUT and trusting S3 to echo it: it would avoid
  the read-back, but it depends on undefined behaviour for an unsigned `x-amz-checksum-*`
  header on a presigned URL, and could not be verified offline. Reading the object back is
  verifiable by construction and costs one read per persisted output.
- Recording the worker's digest and comparing only sizes: rejected outright. That is the
  misattribution this ADR removes.
- Reaching for S3 version ids as the sole binding: rejected as insufficient on its own,
  because an unversioned bucket is a supported configuration; the digest is the authority
  and the version id only narrows the read window.
- Tracking process lineage in more detail than start ticks: rejected. If the recorded
  identity cannot prove continuity, the group is reported as unproven and left alone.
- Rewriting the output list as a partial record and repairing it on load: rejected. Keeping
  the structure complete at every write is simpler and cannot lose a declared output.

### Consequences

Persisting a declared output now reads it back once, so large outputs cost one extra
transfer; that is the price of a cryptographic claim about canonical storage. A job whose
own process group cannot be terminated stays nonterminal until an operator intervenes,
which is deliberate: a live survivor is recoverable, a killed unrelated workload is not.
Legacy job records that predate recorded group identity remain unidentifiable, and
cancelling such a job now fails loudly instead of guessing. The Phase 6 limitations
recorded in ADR-024 - an orphaned transfer may outrun the controller, cancellation can wait
behind an in-flight lock, a stopped worker stays reconcilable, and no scheduler, daemon,
database, or automatic reaper exists - are unchanged and remain accepted.

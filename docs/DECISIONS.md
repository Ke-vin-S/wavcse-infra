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

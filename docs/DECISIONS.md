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

- **Status:** Accepted
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

Phase 2 may display the documented `publicIp` and `portMappings["22"]` data but does not
claim SSH readiness. S3 remains the planned data path.

### Official source

- [RunPod: Connect to a Pod with SSH](https://docs.runpod.io/pods/configuration/use-ssh)

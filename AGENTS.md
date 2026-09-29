# AGENTS.md — wavcse-infra

## What this repository is

`wavcse-infra` is the infrastructure control plane for reproducible wavCSE research
workloads. It prepares and operates a persistent but stoppable AWS EC2 controller, creates
and inspects disposable RunPod GPU workers, transfers artifacts, and executes research jobs
against an exact commit, all through the `infra` CLI. Machine bootstrap, worker and volume
lifecycle, remote execution, storage transfer, environment diagnostics, and orchestration
are its responsibilities.

It is not the wavCSE research repository: research code, models, experiment definitions,
tests, study documentation, and MLflow/DagsHub reporting stay in the separate `wavCSE`
checkout. Never move research logic into this repository.

## Boundary — what does not belong here

- Research logic, experiment definitions, and research state: the `wavCSE` repository.
- Detailed behaviour, configuration, and recovery narrative: `README.md`, `docs/SPEC.md`,
  `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md`, `docs/RUNPOD.md`,
  `docs/DECISIONS.md`. Point at them; do not restate them here.
- Procedure a skill already covers: reference the skill (see below); do not duplicate it.
- Large artifacts, checkpoints, and embeddings: S3, never Git.
- Do not add without an explicit request: Kubernetes, Slurm, Ray, Celery, Airflow, Ansible,
  Terraform/OpenTofu, a web UI, a daemon, a database, a message broker, a custom experiment
  tracker or secrets manager, multi-user authorization, or generic cloud abstraction beyond
  keeping provider code isolated.
- Build small correct vertical slices; no speculative platform engineering.

## Hard invariants — never violate

Non-negotiable. A skill mentioning one of these does not relax it.

1. **The controller is the authoritative writable environment; workers are disposable.**
   Source changes are made, tested, committed, and pushed in the controller's `wavCSE`
   checkout, then fetched by a worker at an exact pushed commit. A worker MUST NOT be the
   only location holding a change, MUST NOT hold a push credential, and a normal worker
   MUST NOT run OMP. An explicitly requested development/debug worker may carry extra
   tooling, but recorded runs still execute committed code.
2. **Every recorded experiment executes an explicit immutable commit SHA.** Never infer a
   commit from the checked-out branch or from an unspecified working tree. Prefer full SHAs
   over branch names. Detached checkout, `HEAD` verified equal to the requested object on
   both sides, clean-tree requirement for recorded jobs, and the SHA recorded in execution
   metadata.
3. **Storage layers are not interchangeable.** S3 is canonical; a network volume is a
   rebuildable warm cache; container disk is ephemeral scratch. Losing a worker or a volume
   must lose nothing canonical. An artifact is trusted because its identity was verified,
   not because a file with the expected name exists: verification is streaming SHA-256 (an
   S3 ETag is never a checksum), and publication is not complete at upload acknowledgement.
4. **No long-lived cloud credentials on GPU workers.** Workers never receive AWS access
   keys, a controller SSH key, a GitHub write credential, or a provider API token. S3 access
   is through time-limited, object-scoped presigned URLs, treated as secrets until expiry
   and never logged in full. The controller uses its EC2 IAM role via the normal SDK
   credential chain, under least privilege scoped to the configured bucket and prefix, which
   stays private (public-read artifacts are out of scope); no static AWS keys on the
   controller.
5. **Secrets are never logged or persisted.** Redact authorization headers, API tokens,
   presigned URL query strings, AWS temporary credentials, and private keys; never print a
   full environment dump. Secret values come from the environment or an external credential
   store, never from committed configuration; `.env.example` carries names only.
6. **Destructive operations require explicit intent and confirmation.** Worker, volume, and
   storage deletion targets an explicit identifier, prints the exact target, and requires
   confirmation unless `--yes` is given; `--yes` never bypasses validation or a price guard.
   Automated cleanup may destroy only resources this tool created and tracks, never by
   naming convention alone.
7. **Cost is a first-class constraint.** Never silently provision GPU resources. Creation
   exposes GPU type and count, cloud tier, maximum acceptable hourly price, storage, and
   interruptibility policy, prints the selected resource and the provider-observed hourly
   price, and refuses to guess an absent price. The price ceiling belongs to a human.
8. **Provider state is authoritative; local state is convenience.** Reconcile against the
   provider before acting, and never let a lost response become a duplicate paid resource:
   an ambiguous create is reconciled by exact generated identity and the paid request is
   never blindly retried.
9. **Infrastructure actions go through the `infra` CLI.** Raw provider HTTP calls, ad-hoc
   `aws` invocations, and manual `ssh` mutation discard the cost guards, identity
   safeguards, bounded retries, and provenance the CLI provides, and are defects.
10. **Failures must be actionable and credential-free.** Every operation identifies the
    provider, resource or job ID, operation, and result, and reports the observed state; no
    bare "error occurred" and no leaked tokens. Retries are bounded and must never turn a
    destructive operation into unintended duplicate provisioning.
11. **Idempotence where practical.** Bootstrap and configuration operations must be safe to
    rerun and converge on the same desired state, never corrupting or duplicating it.

## Validation

`make check` is the required gate for any change; run the narrower target while iterating.

```bash
make check     # uv lock check + format check + lint + tests + cloud-init schema
make test      # pytest
make format    # ruff fix/format, shfmt -w
make lint      # ruff check, shellcheck
make doctor    # read-only controller checks via uv run infra doctor
```

Bash scripts keep a Bash shebang, `set -Eeuo pipefail`, quoted expansions, actionable
errors, and stay ShellCheck-clean and shfmt-formatted; substantial orchestration state
machines do not belong in Bash. Python is Ruff-clean and type-hinted. Unit tests mock
provider HTTP and AWS calls. No normal test or validation command creates, starts, stops, or
destroys paid infrastructure and CI runs without cloud credentials; integration tests that
could create paid resources are opt-in, explicitly flagged, self-cleaning, and never in CI.

## Skills — load the one that fits the work

- `.agents/skills/wavcse-infra-operator/SKILL.md` — operating the control plane with `infra`:
  reconciling provider and local state, worker and network-volume lifecycle, exact-commit job
  submission, secrets handling, destructive operations, retries and cost guards.
- `.agents/skills/gpu-research-operator/SKILL.md` — GPU selection and price ceilings,
  measuring throughput before scaling, bottleneck diagnosis, stopping paid compute.
- `.agents/skills/wavcse-artifact-pipeline/SKILL.md` — artifact identity, the S3 / network
  volume / scratch model, cache-hit criteria, transfer semantics, version 1 manifests,
  deterministic packaging, publication verification, cleanup.
- `.agents/commands/{infra-status,infra-gpu,infra-artifact}.md` — read-only reconciliation,
  GPU-economics, and artifact-flow entry points.

Load the operator skill before any CLI-driven change; add the GPU or artifact skill when the
work touches capacity economics or artifact bytes.

## Current state — reconcile, never assume

Worker IDs, endpoints, ports, prices, availability, job outcomes, and volume placement are
never trustworthy from conversation memory or an earlier session. Re-derive them:

```bash
infra doctor            # configuration, credential source, endpoints, identity, S3
infra config validate   # configuration parse only, no network calls
infra worker list       # reconciles tracked workers against the provider
infra volume list       # also warns about unresolved create intents
infra job status <job-id>   # may drive an interrupted job forward from worker evidence
```

Authority order: the provider API (RunPod), then AWS/S3, then the controller-local non-secret
state records (`workers.json`, `volumes.json`, `jobs/`) written atomically by the CLI beneath
the CLI state directory. Local state is supplemental, never the sole source of truth, and is
not hand-edited. Before acting on a disagreement, read `README.md` and the "Local state and
reconciliation" section of `docs/OPERATIONS.md`.

## Agent-facing assets

- `.agents/skills/<name>/SKILL.md` and `.agents/commands/<name>.md` are the canonical
  versioned agent assets; OMP discovers both natively and Codex discovers the skills.
- `.omp/AGENTS.md` is a relative symlink to this file, so this file is the single source of
  truth for project instructions — edit it here, never through the symlink.
- When an agent-facing asset is added, renamed, or moved, keep the layout and the symlink
  consistent and run `make agents-check`.

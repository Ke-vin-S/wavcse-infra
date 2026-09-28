# Architecture

## Boundaries

`wavcse-infra` owns infrastructure bootstrap, provider communication, controller
diagnostics, Pod lifecycle, SSH readiness, worker bootstrap, exact-commit job execution,
and artifact transport. It does not own research code, experiment semantics, model
dependencies, or MLflow instrumentation.

The components are:

- **AWS EC2 controller:** persistent but stoppable; authoritative writable environment
  containing OMP, Codex CLI, and AGF.
- **wavCSE repository:** research code and experiment source of truth.
- **wavcse-infra repository:** infrastructure CLI and machine bootstrap.
- **RunPod Pods:** disposable GPU execution environments.
- **GitHub:** immutable code distribution after changes are committed and pushed.
- **Private S3:** canonical large-artifact and embedding storage.
- **MLflow/DagsHub:** experiment tracking owned by wavCSE.

## Control and data flow

```text
OMP edits wavCSE on controller
  -> tests
  -> immutable Git commit
  -> GitHub
  -> RunPod worker checks out exact commit
  -> wavCSE executes and reports to MLflow

Controller IAM role
  -> private S3
  -> time-limited, object-scoped presigned URL
  -> worker materializes input or uploads requested output
  -> controller verifies durable object
```

Every storage key is resolved beneath the configured `storage.prefix`, so the
controller can only address its own namespace. The presigned URL is transported on the
direct SSH stdin stream rather than as a process argument, and worker-side code holds no
AWS credential.

Phase 5 adds canonical S3 access, version 1 artifact manifests, and worker artifact
transfer. Phase 6 adds the versioned job specification, exact-commit materialization,
detached execution, status, logs, cancellation, and durable output persistence. Research
dependency installation remains an explicit argv declared by the job, not an infra-owned
package manager.

## Recorded job flow

```text
infra job submit jobs/dg-0004.json --worker <id>
  -> validate the version 1 JSON specification
  -> require provider RUNNING + locally READY worker (never create/bootstrap/destroy here)
  -> install worker/job_runner.py at jobs.runner_path, verified by SHA-256
  -> create <jobs.worker_root>/<job-id>/{source,inputs,outputs,logs,state}
  -> clone/fetch/checkout spec.source.commit; verify HEAD == commit; require a clean tree
  -> materialize declared inputs from S3 through presigned GET (Phase 5)
       -> required input failure aborts before the command starts
  -> start the command detached in the worker's own session
       -> verify HEAD and clean tree again before setup and command argv
       -> stdout/stderr -> logs/job.log, exit code -> state/finished.json

infra job status <job-id>
  -> read worker evidence (state files recording the commit verified at launch) over direct SSH
  -> persist declared outputs through presigned PUT + controller size verification
  -> SUCCEEDED only when the command exited 0 and every required output is persisted
  -> CANCELLED when a cancellation was recorded and the command did not exit 0
  -> FAILED with the exit code, stage, and timeout flag preserved

infra job logs <job-id>      -> bounded tail of logs/job.log (or the local copy)
infra job cancel <job-id>    -> terminate the job's process group only; never the worker
```

The controller keeps one durable non-secret document per job beneath
`~/.local/state/wavcse-infra/jobs/<job-id>.json` plus a bounded local log copy. Provider
state and the worker's own files remain authoritative; a local record is never upgraded to
`SUCCEEDED` from a stale assumption.

## Implemented modules

- `config.py` validates and merges built-in, TOML, environment, and CLI settings.
- `credentials.py` resolves the RunPod key once per client from the environment or an
  SSM `SecureString` through Boto3's normal AWS credential chain.
- `cli.py` defines the stable `infra` interface and global configuration options.
- `doctor.py` runs independent read-only controller and connectivity probes.
- `models.py` defines provider-neutral worker requests/views, lifecycle states, cloud
  types, and GPU offers used outside the provider client.
- `providers/runpod.py` owns RunPod REST v2 wire parsing, safe read retries, GPU catalog
  discovery, exact-ID lifecycle requests, and ambiguous-create reconciliation.
- `state.py` stores only supplemental non-secret created-worker metadata with atomic
  same-directory replacement beneath `~/.local/state/wavcse-infra/`.
- `workers/lifecycle.py` owns cost/availability guards, bounded polling, transitions,
  and provider-authoritative reconciliation.
- `workers/ssh.py` invokes system OpenSSH with an explicit identity, isolated
  known-hosts file, bounded timeouts, captured streams, and provider-refreshed endpoint
  readiness polling. Interactive shells may use RunPod's PTY proxy, while automation
  requires true SSH through a mapped public `22/tcp` endpoint.
- `workers/bootstrap.py` streams reviewed Bash scripts to `bash -s` over direct SSH,
  parses normalized health facts, and gates local readiness without changing provider
  lifecycle state.
- `worker/bootstrap.sh` and `worker/health-check.sh` are the idempotent worker-side
  setup and inspection contracts packaged with the CLI.
- `storage/keys.py` is the single namespace rule: it canonicalizes the prefix and
  rejects absolute, escaping, ambiguous, or repeated keys before any API call.
- `storage/s3.py` owns Boto3 object operations (list, metadata, small-object read,
  presign, verification) for one configured bucket and prefix, and redacts presigned
  URLs unless a caller explicitly reveals one.
- `storage/manifests.py` defines schema version 1 artifact manifests, their deterministic
  JSON form, and the embedding archive key conventions.
- `storage/worker_transfer.py` is the stdlib-only program executed on the worker: it
  streams a presigned transfer, verifies size and SHA-256, materializes atomically, and
  reports a tab-separated result. Large objects with a known expected size download as
  bounded parallel HTTP byte ranges over a deterministic resumable partial file and
  range record; small or size-unknown objects keep the single-connection path. It is also
  importable, so the same code is unit-tested on the controller.
- `storage/transfer.py` presigns, streams that module over direct SSH stdin with the URL
  on the same stream, parses the worker result strictly, and verifies uploaded objects.
- `redaction.py` removes authorization values, known secret assignments, and URL
  query strings from user-facing external errors, and provides the shared
  `contains_bearer_material` check used by manifests and job specifications.
- `jobs/models.py` defines the version 1 job specification, the explicit job state
  machine, and the durable job record; it rejects branches, short prefixes, shell command
  strings, traversal paths, reserved environment names, and bearer values.
- `jobs/state.py` stores one atomic, non-secret JSON document per job plus a bounded local
  log copy beneath `~/.local/state/wavcse-infra/jobs/`.
- `jobs/execution.py` installs the reviewed worker runner (digest-verified), drives
  `prepare`/`start`/`inspect`/`logs`/`cancel` over direct SSH, and parses the runner's
  schema-versioned protocol strictly. Descriptors, including secret values, travel on
  stdin and never in an argument list.
- `jobs/collect.py` maps declared inputs/outputs to worker paths inside the job workspace
  and refuses any path that escapes it.
- `jobs/submit.py` enforces worker preconditions, resolves declared secrets from the
  controller environment, materializes inputs, verifies the commit, starts the job, and
  records FAILED with evidence when a phase fails.
- `jobs/status.py` reconciles job state from worker evidence, persists declared outputs,
  captures a bounded local log copy, and implements idempotent cancellation.
- `worker/job_runner.py` is the stdlib-only, Python 3.10-compatible worker program: it
  materializes the exact commit, supervises detached execution with a timeout, reports
  status, tails logs, and terminates only its own process group.
- `controller/bootstrap.sh` converges supported Ubuntu controllers on required tools
  and the locked project environment, then delegates controller agent installation.
- `controller/install-agents.sh` installs pinned, verified OMP, Codex CLI, and AGF
  releases for the controller user without performing authentication.
- `controller/cloud-init.yaml` performs only initial public clone and bootstrap dispatch.

No generic provider base class exists; RunPod is the only implemented provider.

Controller agent installation is not part of the worker lifecycle. Normal GPU workers
remain minimal execution environments and do not receive OMP, Codex, AGF, or controller
authentication state.

## Configuration flow

```text
built-in defaults
  <- ~/.config/wavcse-infra/config.toml
  <- environment
  <- CLI overrides
  -> immutable Pydantic Settings

config/infra.example.toml
  -- bootstrap copies once if absent --> user TOML
```

The example is committed; the controller-specific user TOML is not. Bootstrap never
overwrites an existing user file. Secrets are absent from both TOML roles.
`runpod.api_key_parameter` is a non-secret SSM reference. At RunPod client construction,
the credential resolver prefers a non-empty `RUNPOD_API_KEY`, otherwise calls SSM
`GetParameter` with decryption. The resolved value remains in memory and is reused by
that client; it is not copied into configuration, local state, or files.

```text
RUNPOD_API_KEY (if non-empty)
  -> in-memory RunPod client credential

otherwise:
config runpod.api_key_parameter
  -> SSM GetParameter(WithDecryption=True) via EC2 instance profile
  -> in-memory RunPod client credential
```

## Artifact transfer flow

```text
infra storage download <key> <worker-path> --worker <id>
  -> controller resolves <key> beneath storage.prefix
  -> HEAD confirms the object exists
  -> presigned GET, bounded lifetime, one object
  -> direct SSH: `python3 - download ...` with stdin =
       WAVCSE_PRESIGNED_URL = '<url>'
       WAVCSE_IF_NONE_MATCH = False
       <reviewed worker module source>
  -> one destination has one transfer at a time: the whole critical section holds
       <path>.wavcse-transfer.lock, for every transport
  -> small or size-unknown object: one-shot staging <path>.wavcse-partial-<pid>-<random>
  -> large object with a known expected size: bounded parallel HTTP byte ranges
       appended at explicit offsets into <path>.wavcse-partial
       (one-shot random staging instead when no expected SHA-256 is supplied)
       completed range indices recorded atomically in <path>.wavcse-partial.json
       (no bearer material, so a later run with a fresh URL resumes the same digest)
  -> whole assembled artifact checked against expected size and SHA-256
  -> only then placement at <path>: the destination is linked from the open staging
       inode and the created entry is verified, never moved from a pathname
       (--overwrite removes the previous entry first)
  -> tab-separated result parsed and validated at the controller

infra storage upload <key> <worker-path> --worker <id>
  -> HEAD confirms the key is free unless --overwrite
  -> presigned PUT with signed If-None-Match: * unless --overwrite
  -> worker hashes the bytes sent, reports size and digest
  -> controller HEAD verifies the stored size
```

The URL never appears in a process argument list on either side; it travels only inside
the encrypted SSH channel's stdin. `infra storage verify` and
`infra storage presign-*` are controller-only and never touch a worker.

## Worker lifecycle flow

```text
explicit WorkerSpec
  -> current exact GPU/cloud/count offer
  -> optional GraphQL public-IP scheduler filter
  -> availability and maximum-price guard
  -> operator-visible plan and confirmation
  -> one create POST (REST v2, or GraphQL when direct SSH is required)
  -> persist provider ID atomically
  -> bounded GET polling to RUNNING
```

Start, stop, and destroy also use exact provider IDs and bounded reconciliation. Destroy
never resolves a loose name. `--yes` bypasses only create/destroy confirmation; it does
not bypass request validation, availability checks, or the price guard.

Each CLI create generates an exact high-entropy name. If the create response is lost,
the provider client lists Pods and matches only that complete name. It adopts one match,
reports duplicates, or fails safely. It never retries the paid create POST because
RunPod v2 exposes neither an idempotency key nor a provider-enforced unique Pod name.

After provider `RUNNING`, readiness proceeds independently:

```text
RUNNING
  -> refresh ssh.direct / ssh.proxy from GET /pods/{id}
  -> authenticated non-interactive SSH marker
  -> SSH_READY
  -> versioned idempotent bootstrap through SSH exec
  -> BOOTSTRAPPED
  -> execution-disk/tool/nvidia-smi health
  -> requested persistent/network mount verification, when applicable
  -> GPU_HEALTHY
  -> READY
```

The direct endpoint is required for automation because it is the Pod's true SSH daemon
through a mapped public `22/tcp` port. RunPod's basic proxy requires a PTY and is used
only by `infra worker ssh`; it is never an automation fallback. Endpoint metadata is
refreshed during polling because IP/port publication can lag provider `RUNNING`.
Stopping or destroying a tracked Pod resets local readiness without redefining its
provider state.

## Reliability stance

Read-only HTTP operations use explicit timeouts and bounded exponential backoff, with a
per-delay cap, for transport failures, HTTP 429, and HTTP 5xx responses. Redirects and
other 4xx responses fail immediately with a redacted, actionable provider error.
Mutation requests are issued once; ambiguous start/stop/destroy results are reconciled
through GET polling, while ambiguous create uses exact-name reconciliation and no POST
retry.

Unknown provider statuses remain visible as native status and normalize to `UNKNOWN`.
Missing optional provider fields remain `None`; the parser does not invent metadata.

## Deferred architecture

Research dependency installation beyond an explicit declared argv, multi-worker
scheduling, resume/checkpoint orchestration, multipart artifact upload, and automatic
worker provisioning remain deferred. S3 remains the artifact transport; direct SSH carries
only small reviewed programs, their descriptors, and presigned URLs on stdin, never
artifact bytes.

# Architecture

## Boundaries

`wavcse-infra` owns infrastructure bootstrap, provider communication, controller
diagnostics, Pod lifecycle, network-volume lifecycle, SSH readiness, worker bootstrap,
exact-commit job execution, and artifact transport. It does not own research code,
experiment semantics, model dependencies, or MLflow instrumentation.

The components are:

- **AWS EC2 controller:** persistent but stoppable; authoritative writable environment
  containing OMP, Codex CLI, and AGF.
- **wavCSE repository:** research code and experiment source of truth.
- **wavcse-infra repository:** infrastructure CLI and machine bootstrap.
- **RunPod Pods:** disposable GPU execution environments.
- **RunPod network volumes:** provider-attached storage mounted by Secure Cloud Pods as a
  rebuildable artifact cache. Private S3 stays canonical; a volume can be rebuilt and a
  container disk is ephemeral scratch.
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
  -> persist declared outputs through presigned PUT + controller content verification against
     the worker-reported size and digest, read back from canonical storage
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
  types, GPU offers, and the network-volume models (`NetworkVolume`, `DataCenterInfo`,
  `NetworkVolumeBilling`) used outside the provider client.
- `providers/runpod.py` owns RunPod REST v2 wire parsing, safe read retries, GPU catalog
  discovery, exact-ID lifecycle requests, and ambiguous-create reconciliation. It also
  owns all network-volume wire handling (`GET`/`POST` `/v2/network-volumes`,
  `GET`/`DELETE` `/v2/network-volumes/{id}`, `GET` `/v2/catalog/datacenters`, and `GET`
  `/v2/billing/network-volumes`), normalized into those provider-neutral models so no
  RunPod response object leaks outside the provider module.
- `state.py` stores only supplemental non-secret created-worker metadata with atomic
  same-directory replacement beneath `~/.local/state/wavcse-infra/`. Its
  `VolumeStateStore` shares that atomic-write and advisory-lock machinery for
  `volumes.json`, keyed by infra identity rather than provider ID because the identity is
  known before the paid create and the ID only after a response.
- `volumes/lifecycle.py` plans, creates, observes, and destroys network volumes and owns
  the Pod placement constraint: a Pod that mounts a volume is constrained to the volume's
  data center before any paid request, a contradicting explicit data center is rejected
  rather than overridden, and an ambiguous create is reconciled by exact infra identity.
- `workers/lifecycle.py` owns cost/availability guards, bounded polling, transitions,
  and provider-authoritative reconciliation. After a create completes it verifies that
  the provider actually placed the Pod in the requested data center and actually attached
  the requested volume; a create answer that omits placement or the mount is refreshed
  once from `GET /pods/{id}` before any conclusion is drawn, and only an affirmative
  contradiction raises.
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
  importable, so the same code is unit-tested on the controller. The same program
  implements the worker-side cache operations (`cache-materialize`, `cache-populate`,
  `cache-stats`): they take no presigned URL, require a 64-hexadecimal digest and
  symlink-free paths, use an entry only when identity, recorded size, and re-hashed bytes
  agree, quarantine an entry that contradicts its recorded identity, and publish a
  verified entry with a single directory `rename`. There is no second worker program and
  no new install step.
- `storage/transfer.py` presigns, streams that module over direct SSH stdin with the URL
  on the same stream, parses the worker result strictly, and verifies uploaded objects. It
  derives each attempt's command bound from the URL lifetime, and classifies a bounded
  timeout or a dropped connection as an unknown remote outcome rather than as a failed
  transfer; a transfer that another process already owns, and a destination that already
  exists, are reported as evidence for the caller to check.
- `storage/cache.py` orchestrates cache-aware input materialization on the controller: it
  decides whether to consult the worker cache at all (both a cache root and a declared
  SHA-256 are required), offers a verified canonical download to the cache afterwards, and
  turns every cache failure into a warning so the canonical path proceeds. Its
  `WorkerArtifactCache` reports integrity problems through an injected warning sink.
- `redaction.py` removes authorization values, known secret assignments, and URL
  query strings from user-facing external errors, and provides the shared
  `contains_bearer_material` check used by manifests and job specifications.
- `jobs/models.py` defines the version 1 job specification, the explicit job state
  machine, and the durable job record; it rejects branches, short prefixes, shell command
  strings, traversal paths, reserved environment names, and bearer values.
- `jobs/state.py` stores one atomic, non-secret JSON document per job plus a bounded local
  log copy beneath `~/.local/state/wavcse-infra/jobs/`. Each job also has a local advisory
  lock; every mutating operation takes it and re-reads the record inside it, so two
  controller processes on one machine cannot let the slower one overwrite newer state.
- `jobs/execution.py` installs the reviewed worker runner (digest-verified), drives
  `prepare`/`start`/`inspect`/`logs`/`cancel` over direct SSH, and parses the runner's
  schema-versioned protocol strictly. Descriptors, including secret values, travel on
  stdin and never in an argument list.
- `jobs/collect.py` maps declared inputs/outputs to worker paths inside the job workspace
  and refuses any path that escapes it.
- `jobs/submit.py` enforces worker preconditions, resolves declared secrets from the
  controller environment, and then runs one bounded, idempotent preparation pass (install
  the runner, verify the commit, materialize inputs, start the command). The same pass
  serves a first submission and a later reconciliation. A controller-side interruption
  records the preparation phase and `reconciliation_required` and leaves the job in
  PREPARING; a definitive failure is what records FAILED. Input materialization is
  cache-aware: an input with both a cache root and a declared SHA-256 consults the worker
  cache first, otherwise it uses the canonical presigned download exactly as before, and
  a successful canonical download is then offered to the cache. Each input records a
  `source` of `canonical` or `cache`.
- `jobs/status.py` reconciles job state from worker evidence, persists declared outputs,
  captures a bounded local log copy, and implements idempotent cancellation. For a job
  that is still PREPARING it either advances the same preparation pass or reports the
  worker's own evidence (a launch still starting, a workspace absent, a process gone
  without an outcome) instead of guessing. A worker that is stopped or restarting keeps its
  job reconcilable; required-output persistence whose outcome is unknown is verified
  against the canonical object rather than failed; and a job is only ever failed from
  affirmative evidence.
- `errors.py` names the one distinction the control plane turns on: a
  `ReconcilableOperationError` is an interrupted observation, an exhausted resumable
  attempt, an unfinished competing transfer, or a transient canonical-storage failure, and
  no caller may record a terminal job result from it. Everything else — a reported remote
  failure, an integrity mismatch, an authorization or configuration error, a definitively
  absent object — is evidence and may be terminal.
- `worker/job_runner.py` is the stdlib-only, Python 3.10-compatible worker program: it
  materializes the exact commit, supervises detached execution with a timeout, reports
  status, tails logs, and terminates only its own process group. It serializes `prepare`
  per job with an advisory lock, records the identity that owns each job's launch slot, and
  reports preparation evidence (workspace present, prepared, started, launch alive) so a
  controller can tell "the command never started" apart from "the workspace is gone".
  Creating a job's process and cancelling a job that has none yet are serialized by a
  second per-job lifecycle lock, so exactly one of them decides the outcome. `inspect`
  derives its report from an ordered, bounded snapshot: the recorded outcome is read first,
  the process state is observed next, and anything that appeared in between is re-read
  before a "nothing is recorded and nothing is running" conclusion is reported. A process
  group whose leader exited is still reported as running execution, and a command that left
  descendants behind is reaped before its outcome is written.
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

`[volumes] mount_path` (environment `WAVCSE_INFRA_VOLUMES_MOUNT_PATH`, default
`/workspace/cache`) is where a Pod mounts a RunPod network volume, and that mount is also
the worker's rebuildable artifact-cache root; canonical artifacts stay in S3. The value
must be an absolute path without traversal segments, and it is configuration rather than
a secret. An explicit `--volume-mount-path` wins, and a Pod with no network volume keeps
the historical `/workspace` default, so job scratch and the job workspace stay on
ephemeral container disk.

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

Each rung is proven by a stronger observation than the one below it, so a successful
weaker observation never replaces a stronger one: an SSH probe records *at least*
`SSH_READY` and leaves a `READY` worker `READY`. A full health inspection is authoritative
in both directions, and a provider state that leaves `RUNNING`, destruction, or provider
reconciliation resets readiness because the evidence is no longer valid.

The direct endpoint is required for automation because it is the Pod's true SSH daemon
through a mapped public `22/tcp` port. RunPod's basic proxy requires a PTY and is used
only by `infra worker ssh`; it is never an automation fallback. Endpoint metadata is
refreshed during polling because IP/port publication can lag provider `RUNNING`.
Stopping or destroying a tracked Pod resets local readiness without redefining its
provider state.

## Network volume and cache flow

```text
infra volume create --data-center <dc> --size <gb> [--tier <tier>]
  -> read the public catalog and billing views: data center, size, tier, price
  -> write a durable PENDING_CREATE intent locally, before the paid request
  -> print the target plan, the exact JSON request body, and an estimated monthly cost
  -> operator confirmation unless --yes
  -> one create POST, never retried
  -> reconcile an ambiguous answer by exact infra identity
  -> AVAILABLE

infra worker create --network-volume-id <volume-id> ...
  -> the volume exists in exactly one data center
  -> constrain the requested Pod to that data center before any paid request
       (an explicit --data-center that contradicts the volume is rejected)
  -> one create POST
  -> refresh GET /pods/{id} and verify the Pod is in that data center
       and that the requested volume is actually attached
  -> an answer that omits placement or the mount is refreshed once, and only an
       affirmative contradiction raises, naming the created (billing) Pod
```

```text
infra job submit ... (input materialization, per declared input)
  -> cache root and declared SHA-256 both known?
       no  -> canonical presigned GET (Phase 5), verify size and SHA-256
       yes -> worker cache-materialize --root <root> --expected-sha256 <digest>
                 -> hit: requested identity, recorded size, and re-hashed bytes agree
                    -> copy into <destination>.wavcse-partial-<pid>-<random>
                    -> place through the inode-anchored hard link
                    -> source = cache
                 -> miss, quarantine, unusable root, or interrupted lookup
                    -> warning, then fall through to the canonical download
  -> a successful canonical download is offered to the cache (cache-populate)
  -> the job record stores the input's source: canonical or cache
```

The data-center invariant is a property of the provider, not a new trust boundary:
RunPod attaches a network volume only to Secure Cloud Pods, a volume exists in exactly one
data center, and a Pod that mounts it must be in that data center. The constraint is
applied before any paid request — an explicit operator data center that contradicts the
volume is rejected rather than silently overridden — and the provider's own placement
answer is verified after creation. A Pod reported in the wrong data center, or reported
without the requested mount, is surfaced as an error naming the created Pod.

Creating a volume is a billable persistent resource, so the local store records a
`PENDING_CREATE` intent with the exact requested placement before the create POST is
issued. That POST is never retried; an ambiguous outcome is reconciled by matching the
provider's own listing against the complete infra identity, which is why the volume
document is keyed by identity rather than by provider ID.

The cache layout beneath the mount point is:

```text
cache.json                                  marker: schema version and purpose
artifacts/sha256/<first-two-hex>/<digest>/content
artifacts/sha256/<first-two-hex>/<digest>/metadata.json
staging/                                    in-progress work; safe to delete
```

The digest is the identity, so two artifacts that share a filename and differ in content
occupy different directories. A staged copy is verified and then published by a single
directory `rename`, so a partial artifact can never appear at an entry path and two
concurrent writers cannot produce a falsely complete entry. Materialization copies a
verified entry into the standard `<destination>.wavcse-partial-<pid>-<random>` staging
file and places it through the same inode-anchored hard-link path a download uses.

Every cache failure degrades: an absent entry, a quarantined entry, an unusable cache
root, or an interrupted lookup can only make a job slower, never fail it, and the
canonical download proceeds. Only a definitive protocol violation the worker reports
raises. An entry that contradicts its recorded identity is quarantined into `staging/`
and treated as a miss, so the next canonical download rebuilds it. A job record now
carries a per-input `source` of `canonical` or `cache`, and `infra job status` prints it.

No automatic eviction, LRU, or size cap exists. `infra volume cache stats --worker <id>`
reports entries, cached bytes, staging bytes, unverified entries, and the marker schema
version; cleanup is an explicit operator action that removes a digest directory or the
`staging/` area.

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
worker provisioning remain deferred. Automatic cache eviction, a cache size cap or LRU
policy, multi-worker cache concurrency beyond the single-writer-per-entry guarantee, and
cross-data-center cache replication are also deferred: cache cleanup is an explicit
operator action, and a cache is confined to the one data center that holds its volume.
S3 remains the artifact transport; direct SSH carries only small reviewed programs, their
descriptors, and presigned URLs on stdin, never artifact bytes.

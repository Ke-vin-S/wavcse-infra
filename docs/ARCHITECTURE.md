# Architecture

## Boundaries

`wavcse-infra` owns infrastructure bootstrap, provider communication, controller
diagnostics, Pod lifecycle, SSH readiness, and worker bootstrap. Later phases add
exact-commit execution and artifact transport. It does not own research code,
experiment semantics, model dependencies, or MLflow instrumentation.

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
transfer. It does not perform repository checkout, research dependency installation,
or job execution.

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
  reports a tab-separated result. It is also importable, so the same code is unit-tested
  on the controller.
- `storage/transfer.py` presigns, streams that module over direct SSH stdin with the URL
  on the same stream, parses the worker result strictly, and verifies uploaded objects.
- `redaction.py` removes authorization values, known secret assignments, and URL
  query strings from user-facing external errors.
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
  -> worker streams to <path>.wavcse-partial-<pid>-<random>
  -> expected size/SHA-256 checked when supplied, then atomic materialization
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

Exact-commit checkout/execution, research environments, MLflow execution, jobs, and
scheduling remain deferred. S3 remains the artifact transport; direct SSH carries only
the small transfer program and its presigned URL on stdin, never artifact bytes.

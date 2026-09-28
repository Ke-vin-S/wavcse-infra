# Architecture

## Boundaries

`wavcse-infra` owns infrastructure bootstrap, provider communication, controller
diagnostics, and—only in later phases—worker lifecycle, remote execution, and artifact
transport. It does not own research code, experiment semantics, model dependencies, or
MLflow instrumentation.

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

Phase 3 manages the RunPod Pod resource lifecycle and local operational metadata. It
does not perform SSH, worker bootstrap, artifact transfer, repository checkout, or job
execution.

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

## Worker lifecycle flow

```text
explicit WorkerSpec
  -> current exact GPU/cloud/count catalog offer
  -> availability and maximum-price guard
  -> operator-visible plan and confirmation
  -> one create POST
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

SSH, worker bootstrap, S3 transfer, exact-commit jobs, and job state remain deferred.
Directories and interfaces for those features will be added only with working
responsibilities.

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

Phase 2 can inspect provider state but does not perform any worker-lifecycle or
artifact-transfer portion of this flow.

## Implemented modules

- `config.py` validates and merges built-in, TOML, environment, and CLI settings.
- `credentials.py` resolves the RunPod key once per client from the environment or an
  SSM `SecureString` through Boto3's normal AWS credential chain.
- `cli.py` defines the stable `infra` interface and global configuration options.
- `doctor.py` runs independent read-only controller and connectivity probes.
- `models.py` defines the provider-neutral worker view used by CLI presentation.
- `providers/runpod.py` owns the RunPod REST v1 read client, wire parsing, retries, and
  normalization.
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

## Reliability stance

Read-only HTTP operations use explicit timeouts and bounded exponential backoff, with
a per-delay cap, for transport failures, HTTP 429, and HTTP 5xx responses. Redirects
and 4xx responses fail immediately with a redacted, actionable provider error. This
retry behavior must not be copied to resource creation: a lost create response can
otherwise duplicate paid infrastructure.

Unknown provider statuses remain visible as native status and normalize to `UNKNOWN`.
Missing optional provider fields remain `None`; the parser does not invent metadata.

## Deferred architecture

Local worker/job state, SSH, worker bootstrap, S3 transfer, exact-commit jobs, and
destructive lifecycle commands are future vertical slices. Directories and interfaces
for those features will be added only with working responsibilities.

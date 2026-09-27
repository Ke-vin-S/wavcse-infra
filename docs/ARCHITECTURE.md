# Architecture

## Boundaries

`wavcse-infra` owns infrastructure bootstrap, provider communication, controller
diagnostics, and—only in later phases—worker lifecycle, remote execution, and artifact
transport. It does not own research code, experiment semantics, model dependencies, or
MLflow instrumentation.

The components are:

- **AWS EC2 controller:** persistent but stoppable; authoritative writable environment.
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

Phases 0–1 do not perform any worker-lifecycle or artifact-transfer portion of this flow.

## Implemented modules

- `config.py` validates and merges built-in, TOML, environment, and CLI settings.
- `cli.py` defines the stable `infra` interface and global configuration options.
- `doctor.py` runs independent read-only controller and connectivity probes.
- `redaction.py` removes authorization values, known secret assignments, and URL
  query strings from user-facing external errors.
- `controller/bootstrap.sh` converges supported Ubuntu controllers on required tools
  and the locked project environment.
- `controller/cloud-init.yaml` performs only initial public clone and bootstrap dispatch.

Worker models and the RunPod read client are the next vertical slice. No generic
provider base class exists.

## Configuration flow

```text
built-in defaults
  <- user TOML
  <- environment
  <- CLI overrides
  -> immutable Pydantic Settings
```

Secrets are absent from committed configuration. `RUNPOD_API_KEY` is read only from the
process environment and stored as a Pydantic secret value.

## Reliability stance

Phase 2 read-only HTTP operations will use explicit timeouts and bounded exponential
backoff for transport failures, HTTP 429, and HTTP 5xx responses. Other 4xx responses fail
immediately with a redacted, actionable provider error. This retry behavior must not be
copied to resource creation: a lost create response can otherwise duplicate paid
infrastructure.

Unknown provider statuses will remain visible as native status and normalize to `UNKNOWN`.
Missing optional provider fields remain `None`; the parser does not invent metadata.

## Deferred architecture

Local worker/job state, SSH, worker bootstrap, S3 transfer, exact-commit jobs, and
destructive lifecycle commands are future vertical slices. Directories and interfaces
for those features will be added only with working responsibilities.

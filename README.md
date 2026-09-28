# wavcse-infra

`wavcse-infra` is the infrastructure control plane for reproducible wavCSE
research workloads. It prepares a persistent AWS EC2 controller, inspects disposable
RunPod GPU workers, prepares them through SSH, and will later coordinate exact-commit
execution and durable S3 artifact transfer.

It is not the wavCSE research repository. Model code, experiments, research
configuration, tests, and MLflow integration remain in the separate `wavCSE`
repository.

## Delivery status

The repository currently implements Phases 0–6 of the v1 specification:

- a typed `infra` CLI and layered TOML/environment configuration;
- Ruff, pytest, ShellCheck, and shfmt validation;
- a locked `uv` environment and credential-free CI;
- idempotent Ubuntu controller bootstrap and thin cloud-init;
- reproducible controller-only installation of OMP, Codex CLI, and AGF;
- `infra doctor` controller, credential-source, configuration, and connectivity checks;
- normalized RunPod REST v2 list/show and GPU offer discovery;
- explicit create/start/stop/destroy with bounded polling and exact-ID safeguards;
- current provider price/availability plans, interactive confirmation, `--yes`, and a
  maximum-hourly-price guard;
- conservative ambiguous-create reconciliation without automatic POST retries;
- atomic non-secret local worker state beneath `~/.local/state/wavcse-infra/`;
- normalized direct/proxy RunPod SSH discovery with separate interactive and automation
  modes and bounded authenticated direct-SSH readiness;
- idempotent stdin-streamed worker bootstrap plus version, tool, disk, and NVIDIA GPU
  health;
- a separate local readiness model in which provider `RUNNING` does not imply `READY`;
- a prefix-constrained S3 namespace with listing, metadata, existence, and verification;
- bounded, object-scoped presigned GET/PUT URLs that are redacted from logs, state, and
  errors;
- version 1 artifact manifests with streaming SHA-256 semantics and embedding archive
  conventions;
- worker artifact download and upload through presigned URLs only, with temporary-file
  materialization, optional expected-checksum enforcement, and controller-side size
  verification of the stored object;
- provider-neutral worker/request/offer models and bounded retries for safe reads;
- a versioned JSON job specification that rejects branches, short prefixes, shell command
  strings, traversal paths, reserved environment names, and bearer values;
- an explicit job state machine (`PENDING`, `PREPARING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
  `CANCELLED`) with frozen terminal states and durable non-secret records;
- one reviewed, SHA-256-verified worker runner installed over SSH and driven one bounded
  command per phase, with no worker daemon, tmux, or message queue;
- exact-commit source materialization with detached checkout, `HEAD` verification on both
  sides, a clean-tree requirement, and anonymous HTTPS-only access;
- detached execution that survives SSH or controller interruption, with an enforced
  timeout and a combined stdout/stderr log;
- `infra job submit`, `status`, `logs`, and `cancel`, where cancellation targets only the
  job's own verified process group and never the worker;
- declared inputs materialized through Phase 5 presigned GET before the command starts,
  and declared outputs persisted through presigned PUT plus controller size verification
  before `SUCCEEDED` is recorded;
- non-secret execution provenance handed to the research process as `INFRA_*` variables
  while MLflow run creation stays owned by wavCSE;
- initial architecture, security, operations, provider, and decision documentation.

Phase 6 stops at single-job execution against an explicitly provided worker. Automatic
provisioning, multi-worker scheduling, resume orchestration, and research dependency
installation beyond an explicit declared argv remain unimplemented.

## Architecture

The persistent/stoppable EC2 controller is the writable development environment. It
contains OMP, Codex CLI, AGF, the `wavCSE` checkout, this repository, and the `infra`
CLI. AWS access comes from an EC2 instance profile. The RunPod API key comes from the
`RUNPOD_API_KEY` environment variable for local/temporary use or, on the controller,
from an AWS Systems Manager Parameter Store `SecureString` resolved at runtime.

Disposable GPU workers execute immutable wavCSE commits. GitHub distributes code, a
private S3 bucket is the canonical store for large artifacts, and wavCSE retains
ownership of MLflow/DagsHub reporting.

See [Architecture](docs/ARCHITECTURE.md), [Security](docs/SECURITY.md), and the
[decision log](docs/DECISIONS.md) for boundaries and rationale.

## Local setup

Requirements:

- Python 3.12 or newer;
- [uv](https://docs.astral.sh/uv/);
- ShellCheck, shfmt, and cloud-init for the complete validation suite.

Install locked dependencies and verify the CLI:

```bash
uv sync --locked --all-groups
uv run infra --help
make check
```

On a supported Ubuntu EC2 controller:

```bash
git clone https://github.com/Ke-vin-S/wavcse-infra.git
cd wavcse-infra
./controller/bootstrap.sh
nano ~/.config/wavcse-infra/config.toml
# Configure runpod.api_key_parameter, authenticate OMP/Codex, and clone wavCSE, then:
infra doctor
```

On its first run, bootstrap copies the committed non-secret example to the controller's
user configuration. It never overwrites an existing `config.toml`, so the command is
safe to rerun. It delegates agent installation to `controller/install-agents.sh` and
preserves commands that are already installed. Bootstrap never configures credentials
or creates cloud resources. Use `./controller/bootstrap.sh --skip-agents` only when
agent installation is intentionally managed separately. See
[Operations](docs/OPERATIONS.md) for controller setup and reconstruction.

## Controller agent tools

Install or repair the controller-only tools independently with either command:

```bash
make install-agents
# or
./controller/install-agents.sh
```

The installer uses reviewed upstream release pins, reports detected versions, and
supports `--only omp`, `--only codex`, `--only agf`, and the explicit `--upgrade` mode.
It places managed binaries in `~/.local/bin` and configures the controller login shell
to find `~/.local/bin`, `~/.cargo/bin`, and `~/.bun/bin` without duplicate profile
entries. The latter two preserve compatibility with historical installations; the
default installer does not require Cargo or Bun.

Installation and authentication are separate. On a headless controller, authenticate
after installation:

```text
OMP:   start omp, then run /login (or /login <provider>)
Codex: codex login --device-auth
AGF:   no authentication required
```

OMP, Codex, and AGF are controller development tools. They are not installed on normal
GPU training workers. The selected upstream mechanisms and version policy are recorded
in [ADR-011](docs/DECISIONS.md#adr-011-install-controller-agents-from-pinned-official-releases).

## Configuration

Configuration and credential storage have five distinct roles:

| Location | Role |
| --- | --- |
| `config/infra.example.toml` | Committed example containing all supported non-secret settings |
| `~/.config/wavcse-infra/config.toml` | Controller-specific runtime configuration; never overwritten by bootstrap |
| `.env.example` | Committed reference for supported environment variables, including secret variables |
| AWS SSM Parameter Store `SecureString` | Persistent controller storage for the RunPod key |
| Process environment | Optional temporary/local `RUNPOD_API_KEY` override |

The default runtime user configuration is:

```text
~/.config/wavcse-infra/config.toml
```

Bootstrap creates it from [`config/infra.example.toml`](config/infra.example.toml) when
missing. Replace `CHANGE_ME` before running workloads; doctor treats it as unconfigured.
Precedence remains:

1. CLI options
2. environment variables
3. user configuration file
4. built-in defaults

The TOML contains only the non-secret SSM parameter name:

```toml
[runpod]
api_key_parameter = "/wavcse-infra/runpod/api-key"
api_url = "https://api.runpod.io/v2"
graphql_url = "https://api.runpod.io/graphql"
```

The key itself remains in an SSM `SecureString`. Boto3 reads it with decryption through
the controller's EC2 instance profile; no permanent AWS access keys are installed.
`RUNPOD_API_KEY`, when non-empty, takes precedence for local development, CI, and
temporary testing. Do not put the key in TOML or commit a populated `.env` file.
[`.env.example`](.env.example) documents variables but is not automatically loaded.

Validate without making network calls:

```bash
uv run infra config validate
```

## Commands

```bash
infra --help
infra config validate
infra doctor
infra worker list
infra worker show <worker-id>
infra worker gpu-types --cloud COMMUNITY --gpu-count 1 --require-direct-ssh
infra worker create --gpu <exact-type-id> --cloud COMMUNITY --image <image> --start-ssh --require-direct-ssh --max-price <usd-hour>
infra worker wait-ssh <exact-worker-id>
infra worker ssh <exact-worker-id>
infra worker exec <exact-worker-id> -- <command> [args...]
infra worker bootstrap <exact-worker-id>
infra worker health <exact-worker-id>
infra worker stop <exact-worker-id>
infra worker start <exact-worker-id>
infra worker destroy <exact-worker-id>
infra storage list [--prefix <relative-key-prefix>] [--limit <n>] [--json]
infra storage presign-download <artifact> [--expires-in <seconds>]
infra storage presign-upload <artifact> [--expires-in <seconds>] [--overwrite]
infra storage verify <artifact> [--expected-size <bytes>] [--manifest <key> | --manifest-file <path>]
infra storage download <artifact> <absolute-worker-path> --worker <exact-worker-id>
infra storage upload <artifact> <absolute-worker-path> --worker <exact-worker-id> [--overwrite]
infra job submit <job-spec.json> --worker <exact-worker-id> [--wait] [--wait-timeout <seconds>]
infra job status <job-id> [--json]
infra job logs <job-id> [--tail-bytes <n>] [--local]
infra job cancel <job-id> [--json]
```

Global `--config`, `--runpod-api-url`, `--runpod-timeout`, and `--verbose` options must
appear before the command name.

## Worker lifecycle

Phase 3 implements the Pod-resource lifecycle: discover an exact current GPU offer,
enforce availability and price limits, print and confirm a creation plan, create once,
persist the provider ID, and poll to a bounded provider state. Phase 4 then discovers a
current direct SSH endpoint, proves remote execution with a completion marker, streams
each small reviewed script over stdin, and runs normalized health checks. A Pod can be
RunPod `RUNNING` while its local readiness remains `NOT_READY`, `SSH_READY`,
`BOOTSTRAPPED`, `GPU_HEALTHY`, or `FAILED`; only all required checks produce `READY`.
Automation always disables PTY allocation and requires the mapped public-IP direct SSH
endpoint. The RunPod basic proxy is reserved for `infra worker ssh`, which forces a PTY;
it is never accepted by exec, bootstrap, health, or readiness probing.

Start, stop, and destroy use exact provider IDs. Create and destroy require confirmation
unless `--yes` is supplied; that flag never bypasses validation or `--max-price`.

Every CLI-created Pod receives a high-entropy `wavcse-...` identity. If a create response
is lost, the client reconciles by the complete identity and never blindly retries the
paid POST. RunPod remains authoritative; local state is supplemental and contains no
credentials.

Stopping retains the Pod. Compute cost stops according to the provider status, but
persistent or network storage can continue to incur charges. Destroying terminates the
Pod after showing the exact target and does not delete separately managed network
volumes.

REST API v2 currently does not expose interruptible/spot Pod creation. The CLI rejects
`--interruptible` instead of silently falling back to on-demand capacity. See
[RunPod provider notes](docs/RUNPOD.md) and [Operations](docs/OPERATIONS.md) for the safe
first-worker procedure and current limitations.

Normal GPU bootstrap installs only stable Ubuntu prerequisites: Git, Python, uv, curl,
CA certificates, archive tools, and basic process/filesystem utilities. It does not
install OMP, Codex, AGF, PyTorch, wavCSE, or research dependencies. The current GPU
health contract supports NVIDIA workers with `nvidia-smi`; unsupported accelerators are
never silently marked ready.

## Storage model

S3 is canonical for embeddings, checkpoints, and explicitly persisted large outputs.
RunPod local disks and network volumes are caches. Workers receive time-limited
presigned URLs for individual transfers; they never receive long-lived AWS credentials,
a controller SSH key, or the RunPod API key.

Every ordinary storage operation resolves its argument beneath the configured
`storage.bucket`/`storage.prefix`:

```text
infra storage presign-download embeddings/v1/voxceleb-minpooling.tar
  -> s3://<bucket>/wavcse/embeddings/v1/voxceleb-minpooling.tar
```

Keys are relative, normalized, and unambiguous. `../x`, `/x`, `a//b`, `a/`, trailing
whitespace, `?`/`#`, a bucket/URL, and a key that repeats the configured prefix are all
rejected instead of being rewritten. No storage command accepts a bucket or an absolute
key, so a typo cannot reach an unrelated part of the account.

Presigned URLs are bearer secrets with a bounded lifetime (default
`storage.presign_expiry_seconds = 3600`, maximum 604800 seconds). They are scoped to one
bucket, one exact object, and one operation; they are printed only when a command was
asked to produce one, and their representation is redacted everywhere else.

The canonical embedding layout is dataset-level plain TAR archives with one version 1
sidecar manifest per archive:

```text
wavcse/embeddings/<embedding-version>/
├── voxceleb-minpooling.tar
├── voxceleb-minpooling.manifest.json
├── keyword-spotting-minpooling.tar
├── keyword-spotting-minpooling.manifest.json
├── emotion-recognition-minpooling.tar
└── emotion-recognition-minpooling.manifest.json
```

Each sidecar manifest is schema version 1 and records the artifact name/type, dataset, the
key relative to the namespace prefix, byte size, SHA-256, creation time, and, when
actually known, the generator commit and extracted destination. It never stores a
presigned URL or any credential.

`infra storage download` presigns a GET URL, streams the reviewed worker transfer module
over direct SSH stdin together with the URL, downloads into a temporary sibling file,
verifies size and SHA-256 when expectations are supplied, and only then materializes it
atomically. `infra storage upload` hashes the bytes it sends, PUTs them through a
presigned URL, and then verifies the stored object's size at the controller. Default
uploads use a signed no-replacement header; `--overwrite` opts out. A single PUT is
limited to 5 GB. `infra storage verify` proves existence, size, and
manifest consistency — and states explicitly that it does not verify content, because
the object body is never downloaded back to the controller.

Git stores code and small metadata, not generated tensors or archives. MLflow/DagsHub
continues to own experiment metadata.

## Recorded job model

Phase 6 executes one versioned JSON job specification on one explicit worker the operator
already created and bootstrapped. It never creates, starts, bootstraps, or destroys a
worker, and it never reruns a failed job.

A job names an anonymous `https://` repository plus a **full commit ID**. The worker
clones, fetches that exact object, checks it out detached, requires a clean tree, and
reports `HEAD`; the controller compares that report with the requested commit before the
command starts. A mismatch is a hard failure, so a recorded experiment can never claim
provenance it did not prove.

```json
{
  "schema_version": 1,
  "name": "dg-0004-seed-42",
  "source": {
    "repository": "https://github.com/Synergy-io/wavCSE.git",
    "commit": "<full 40 or 64 character commit ID>"
  },
  "setup": {"argv": ["uv", "sync", "--locked"]},
  "command": {"argv": ["uv", "run", "python", "-m", "improvements.run_improvements"]},
  "runtime": {
    "timeout_seconds": 21600,
    "environment_secrets": ["MLFLOW_TRACKING_USERNAME", "MLFLOW_TRACKING_PASSWORD"]
  },
  "inputs": [{"artifact": "embeddings/wavcse-base-v1-minpool/voxceleb-minpooling.tar",
              "destination": "voxceleb-minpooling.tar",
              "manifest": "embeddings/wavcse-base-v1-minpool/voxceleb-minpooling.manifest.json"}],
  "outputs": [{"path": "outputs/kfold_summary.json",
               "artifact": "jobs/dg-0004-seed-42/kfold_summary.json"}]
}
```

Commands are argv arrays; a shell command string is not representable and no shell is
used. Declared secrets are referenced by name, resolved from the submitting shell, and
delivered on the SSH stdin stream, so no value reaches a specification, a local record, a
log line, or a process argument list. Each job runs in its own worker-side directory
(`<jobs.worker_root>/<job-id>/{source,inputs,outputs,logs,state}`), detached in its own
session so an SSH or controller interruption does not kill it.

Status is derived from the worker's own evidence. `SUCCEEDED` requires the command to exit
0 **and** every required declared output to be persisted through S3 with its stored size
verified by the controller; a missing required input prevents the command from starting
at all. Failures keep their exit code, stage, timeout flag, logs, and provenance, and
optional outputs are still persisted for debugging. `infra job cancel` terminates only the
job's own verified process group. The controller keeps one durable non-secret record and a
bounded log copy per job beneath `~/.local/state/wavcse-infra/jobs/`.

wavCSE owns MLflow; `wavcse-infra` never creates a run. Each job receives non-secret
`INFRA_*` provenance variables for the research process to log itself.

See [Operations](docs/OPERATIONS.md#recorded-job-operations) for the job specification
reference, operator procedure, and the two Phase 6 integration tests.

## Security model

- The controller is trusted and uses its EC2 IAM role through the normal AWS SDK
  credential chain (Boto3's provider chain, never an explicit metadata fetch).
- RunPod credentials resolve in memory from an environment override or SSM
  `SecureString`; authorization values are redacted and never persisted.
- GPU workers are temporary and less trusted than the controller. They receive
  presigned URLs only, over direct SSH stdin, and never an AWS credential, `~/.aws`
  profile, controller SSH key, GitHub write credential, or RunPod token.
- S3 buckets remain private; presigned URLs are bearer secrets until expiry and are
  redacted from logs and errors.
- SSH uses the configured dedicated controller key. OpenSSH ignores user configuration,
  writes only to a wavcse-infra known-hosts file, and uses trust-on-first-use with
  `accept-new`; changed keys are rejected. Global host verification is never disabled.
- Artifacts are verified with streaming SHA-256; an S3 ETag is never treated as a
  SHA-256 checksum.
- Recorded jobs execute a verified commit: the worker must report `HEAD` equal to the
  requested object ID, or the job fails before the command runs. Source access is
  anonymous HTTPS only, and Git LFS content is not fetched.
- Declared job secrets are delivered by name reference on the SSH stdin stream, never as
  argv, environment dumps, files, or durable state; reserved `AWS_`/`RUNPOD_`/`WAVCSE_`/
  `INFRA_`/`SSH_` names are rejected so a specification cannot request a controller
  credential.
- `infra job cancel` signals only the job's own process group after verifying the PID, and
  Phase 6 never destroys a worker automatically.

See [Security](docs/SECURITY.md) for the threat assumptions and IAM guidance.

## Recovery

A controller can be reconstructed by launching supported Ubuntu, attaching the scoped
instance profile, applying the thin cloud-init configuration, cloning both repositories,
restoring user-managed authentication, and running `infra doctor`. Source remains in
GitHub, large artifacts remain in S3, and experiment metadata remains in MLflow/DagsHub.
Local operational state is never the sole source of truth.

Detailed steps are in [Operations](docs/OPERATIONS.md).

## Development

```bash
make format
make lint
make test
make check
```

CI runs the same non-live checks without AWS or RunPod credentials. Provider HTTP is
mocked in tests; no normal test or validation command creates, starts, stops, or destroys
paid infrastructure.

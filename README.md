# wavcse-infra

`wavcse-infra` is the infrastructure control plane for reproducible wavCSE
research workloads. It prepares a persistent AWS EC2 controller, operates
RunPod Pods over SSH and opt-in ephemeral Colab sessions through the pinned
CLI, and executes exact-commit jobs with S3-canonical artifacts.

It is not the wavCSE research repository. Model code, experiments, research
configuration, tests, and MLflow integration remain in the separate `wavCSE`
repository.

## Delivery status

The repository implements the v1 RunPod flow and the opt-in Colab execution
slice:

- a typed `infra` CLI and layered TOML/environment configuration;
- Ruff, pytest, ShellCheck, and shfmt validation;
- a locked `uv` environment and credential-free CI;
- idempotent Ubuntu controller bootstrap and thin cloud-init;
- controller Git commit identity applied by bootstrap from non-secret environment settings;
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
- repository-mirrored application configuration (`apps/`) installed on the controller at
  bootstrap and on workers at bootstrap, updated only by an explicit, permission-gated
  command that never overwrites a differing remote file without confirmation and a backup;
- a separate local readiness model in which provider `RUNNING` does not imply `READY`;
- a prefix-constrained S3 namespace with listing, metadata, existence, and verification;
- bounded, object-scoped presigned GET/PUT URLs that are redacted from logs, state, and
  errors;
- version 1 artifact manifests with streaming SHA-256 semantics and embedding archive
  conventions;
- worker artifact download and upload through presigned URLs only, with temporary-file
  materialization, optional expected-checksum enforcement, and controller-side size
  verification of the stored object;
- provider-identified worker views, RunPod-specific create specs, and bounded safe-read retries;
- a versioned JSON job specification that rejects branches, short prefixes, shell command
  strings, traversal paths, reserved environment names, and bearer values;
- an explicit job state machine (`PENDING`, `PREPARING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
  `CANCELLED`) with frozen terminal states and durable non-secret records;
- one reviewed, SHA-256-verified worker runner driven over SSH (RunPod) or an
  uploaded file envelope with fixed Colab launcher, without a worker daemon;
- exact-commit source materialization with detached checkout, `HEAD` verification on both
  sides, a clean-tree requirement, and anonymous HTTPS-only access;
- detached execution that survives SSH or controller interruption, with an enforced
  timeout and a combined stdout/stderr log;
- `infra job submit`, `status`, `logs`, and `cancel`, where cancellation targets only the
  job's own verified process group and never the worker;
- declared inputs materialized through presigned GET before execution, and
  outputs persisted through presigned PUT plus independent controller read-back
  and SHA-256 verification before `SUCCEEDED`;
- non-secret execution provenance handed to the research process as `INFRA_*` variables
  while MLflow run creation stays owned by wavCSE;
- a RunPod network volume lifecycle (`infra volume list`, `show`, `datacenters`, `create`,
  `destroy`) with data-center placement constraints and an ambiguous-create recovery
  intent record;
- a rebuildable content-addressed artifact cache on a mounted network volume, with
  cache-aware job input materialization for digest-identified inputs;
- initial architecture, security, operations, provider, and decision documentation.

Phase 6.2 adds persistent working storage for RunPod Secure Cloud: a network
volume is a rebuildable content-addressed cache, never canonical. Dynamic
RunPod provisioning from a job spec, multi-worker scheduling, research
dependency installation beyond an explicit argv, and automatic cache
eviction remain unimplemented.

Colab is an opt-in dynamic execution provider with native compute-unit
limits. One owned ephemeral session is allocated at a time, measured against
account usage before and after allocation, and released if the observed
incremental CU/hour breaches policy. This post-allocation check can consume a
small amount of CU. The same exact-commit runner and durable S3 artifact
verification are used through uploaded, short-lived envelopes; no presigned
URL is embedded in normal `colab exec` source. Jobs without `--worker` select
an existing READY Colab worker first, then RunPod; paid resources are never
implicitly provisioned by job submission. See [Colab execution](docs/COLAB.md).

## Architecture

The persistent/stoppable EC2 controller is the writable development environment. It
contains OMP, Codex CLI, AGF, the `wavCSE` checkout, this repository, and the `infra`
CLI. AWS access comes from temporary role credentials resolved by Boto3's standard chain
(an EC2 instance profile, or a role-assuming `credential_process` on another host). The
RunPod API key comes from the `RUNPOD_API_KEY` environment variable for local/temporary
use or, on the controller, from an AWS Systems Manager Parameter Store `SecureString`
resolved at runtime.

Disposable GPU workers execute immutable wavCSE commits. GitHub distributes code, a
private S3 bucket is the canonical store for large artifacts, and wavCSE retains
ownership of MLflow/DagsHub reporting.

Colab is opt-in. `controller/bootstrap.sh` installs
`google-colab-cli==0.7.4` without authenticating; a human mints ADC once.
Every invocation uses `--auth=adc`. The trusted controller accepts the CLI's
plaintext execution history and restricts local permissions. Workers never
receive ADC or long-lived cloud credentials. See [Colab](docs/COLAB.md).

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
preserves commands that are already installed. Supplying `WAVCSE_INFRA_GIT_USER_NAME` and
`WAVCSE_INFRA_GIT_USER_EMAIL` sets the controller's Git commit identity, which the
writable controller needs to commit; without them bootstrap prints what it needs and
changes nothing. Bootstrap never configures credentials
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

## Google Colab CLI

Bootstrap also installs the pinned `google-colab-cli==0.7.4` with `uv tool install`, into
the same `~/.local/bin`. The step is idempotent and performs no authentication and no
compute request. Authentication is a separate, one-time, human step: mint ADC with the four
required scopes and then verify it.

```bash
gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
colab --auth=adc sessions
```

Pass `--auth=adc` before the subcommand on every invocation, because the pinned CLI
defaults to the interactive `oauth2` provider. Colab support is off until
`colab.enabled = true`. See [Colab provider notes](docs/COLAB.md) for the lifecycle,
limitations, and the plaintext `exec`-history warning.

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
the controller's temporary role credentials; no permanent AWS access keys are installed.
`RUNPOD_API_KEY`, when non-empty, takes precedence for local development, CI, and
temporary testing. Do not put the key in TOML or commit a populated `.env` file.
[`.env.example`](.env.example) documents variables but is not automatically loaded.

Colab is optional and disabled until enabled in the operator's controller
configuration. Google ADC comes from the ambient human-minted credential
chain; no Colab credential belongs in TOML.

```toml
[colab]
enabled = true
default_gpu = "T4"
max_simultaneous_workers = 1
allow_free_tier = true
minimum_balance_cu = 5
max_incremental_rate_cu_per_hour = 3
max_job_cu = 10

[placement]
preferred_providers = ["colab", "runpod"]
```

The CLI's `Current balance` is the account's `paidComputeUnitsBalance`, so a
zero balance is not zero compute entitlement: it selects best-effort
free-tier execution when `allow_free_tier = true`. Paid CU limits apply only
while the paid balance is positive. No Colab USD/hour approximation is used.
Job selection considers existing READY leases, never silent provisioning.

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
                    [--network-volume-id <volume-id>] [--volume-mount-path <path>]
infra worker wait-ssh <exact-worker-id>
infra worker ssh <exact-worker-id>
infra worker exec <exact-worker-id> -- <command> [args...]
infra worker bootstrap <exact-worker-id>
infra worker health <exact-worker-id>
infra worker stop <exact-worker-id>
infra worker start <exact-worker-id>
infra worker destroy <exact-worker-id>
infra volume list [--json]
infra volume show <volume-id> [--json]
infra volume datacenters [--json]
infra volume create --data-center <exact-dc-id> --size <gb> [--tier standard|high_performance] [--name <prefix>] [--yes]
infra volume destroy <volume-id> [--wait-timeout <seconds>] [--yes]
infra volume forget <infra-identity> [--yes]
infra volume cache stats --worker <worker-id> [--wait-timeout <s>] [--command-timeout <s>] [--json]
infra storage list [--prefix <relative-key-prefix>] [--limit <n>] [--json]
infra storage presign-download <artifact> [--expires-in <seconds>]
infra storage presign-upload <artifact> [--expires-in <seconds>] [--overwrite]
infra storage verify <artifact> [--expected-size <bytes>] [--manifest <key> | --manifest-file <path>]
infra storage download <artifact> <absolute-worker-path> --worker <exact-worker-id>
                       [--expected-size <bytes>] [--expected-sha256 <hex>] [--concurrency <n>] [--overwrite]
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
`BOOTSTRAPPED`, `GPU_HEALTHY`, or `FAILED`; only all required checks produce `READY`. The
ladder is monotone under weaker observations: a read-only SSH probe on a `READY` worker
leaves it `READY`, while a full health inspection, a worker that leaves `RUNNING`, or a
destroyed worker still resets it.
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

A network volume exists in exactly one data center, so a Pod that mounts one is
constrained to that data center before any paid request, and the provider's own placement
answer is verified afterwards: a Pod reported elsewhere, or reported without the requested
volume mount, raises a placement error naming the created billing Pod and how to remove
it. A network volume also forces Secure Cloud; `--cloud community` with
`--network-volume-id` is rejected before the billable create request (after the read-only
lookups that resolve the volume and its data center). Destroying a volume never
touches a Pod, and destroying a Pod never touches a volume.

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

Readiness is established *before* a URL is signed, so waiting for SSH cannot consume the
credential's lifetime; one bounded attempt never outlives the URL that authorises it,
because the bound is `min(ssh.transfer_timeout_seconds, granted_lifetime - 30)`. A URL
signed with temporary credentials is additionally capped by those credentials' own
remaining validity, since the URL stops working when the session token does. Longer
transfers are handled by retrying with a freshly presigned URL, which the resumable
downloader continues from the ranges it already recorded.

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
over direct SSH stdin together with the URL, and materializes one artifact atomically after
verifying its size and, when supplied, its SHA-256. A large artifact whose expected size is
known is fetched as bounded parallel HTTP byte ranges — worker default 8 streams,
`--concurrency` up to 16 — and a download that also knows the expected SHA-256 resumes
after an interruption instead of restarting from byte zero: it stages into
`<destination>.wavcse-partial` with a `<destination>.wavcse-partial.json` record of
completed inclusive ranges, and that record holds no presigned URL. Without a digest the
same parallel transport still runs, but into a one-shot staging file with no resumable
state, so an artifact of the same size can never be mixed with another. One destination has
one transfer at a time, serialized by `<destination>.wavcse-transfer.lock` for every
download, resumable or not. Nothing reaches the destination path until the whole assembled
file matches the expected size and, when supplied, its digest; placement links the verified
inode rather than moving a pathname; and an endpoint that ignores `Range` falls back to the
single-connection path.
`infra storage upload` hashes the bytes it sends, PUTs them through a presigned URL, and
then verifies the stored object's size at the controller. Default uploads use a signed
no-replacement header; `--overwrite` opts out. A single PUT is limited to 5 GB.
`infra storage verify` proves existence, size, and manifest consistency — and states
explicitly that it does not verify content, because the object body is never downloaded
back to the controller.

A RunPod network volume mounted on a Secure Cloud Pod is a rebuildable working cache, not
canonical storage; S3 stays canonical, so losing the volume must never lose the only copy
of a canonical artifact. The mount point is the cache root:

```text
<cache-root>/cache.json                                  rebuildable cache marker
<cache-root>/artifacts/sha256/<first-two-hex>/<digest>/content
                                                         verified artifact bytes
<cache-root>/artifacts/sha256/<first-two-hex>/<digest>/metadata.json
                                                         digest, size, artifact, cached_at
<cache-root>/staging/                                    in-progress work and
                                                         quarantine-*; safe to delete
```

Identity is the artifact's SHA-256, never its filename, so two artifacts with the same name
and different content can never collide. A hit requires the requested identity, the
recorded size, and the bytes on disk to all agree: the entry directory exists, its
`metadata.json` parses under the supported schema and names the requested digest, and the
content is a regular non-symlink file whose actual size and SHA-256 match. Anything else is
a miss or an explicit integrity failure; corrupt bytes are never accepted because they came
from the mount. An entry that contradicts its recorded identity is moved into
`staging/quarantine-<digest>-<random>`, reported, and treated as a miss, so the next
canonical download rebuilds it. Quarantine keeps the failing bytes for diagnosis instead of
deleting them.

Population copies the artifact into `staging/`, verifies it while copying, writes the
metadata document, fsyncs, and then publishes the whole directory with a single `rename`,
so a partially written artifact can never appear at an entry path. Two concurrent writers
stage separately and one `rename` wins; the loser verifies the winner's entry and reports
it rather than replacing anything. Materialization copies a verified entry into a partial
staging file while hashing it, then places it through the same inode-anchored hard-link
path a canonical download uses, so a hit carries exactly the integrity guarantee of a fresh
download.

Cache use is enabled only when the worker has a network-volume mount path recorded and the
declared input has a SHA-256 (declared directly or recorded in its manifest). An input
identified only by size is downloaded from canonical storage directly, because a
content-addressed cache cannot answer a question about an unidentified artifact. On a
miss, a quarantined entry, an unusable cache root, or an interrupted lookup, the canonical
presigned download proceeds and the verified result is then offered to the cache. Every
cache problem degrades to a warning: the cache can only make a job faster, never make it
fail. The cache never receives a presigned URL; bytes only enter it from a file the
canonical download already verified, and no URL, credential, or other bearer material is
ever written to the mounted volume.

No automatic eviction or LRU is implemented. `infra volume cache stats --worker <id>`
reports the cache root, the number of entries, recorded cached bytes, staged bytes, entries
with unusable metadata, and the marker's schema version. Sizes come from each entry's
metadata document rather than from re-reading artifacts, so inspecting a full volume stays
cheap. Cleanup is operator-managed through
`infra worker exec <worker-id> -- rm -rf <path>`.

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

- The controller is trusted and uses its temporary AWS role through the normal AWS SDK
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

A controller can be reconstructed by launching supported Ubuntu, providing the scoped
temporary role credentials, applying the thin cloud-init configuration, cloning both
repositories, restoring user-managed authentication, and running `infra doctor`. Source
remains in GitHub, large artifacts remain in S3, and experiment metadata remains in
MLflow/DagsHub. Local operational state is never the sole source of truth.

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

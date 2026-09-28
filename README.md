# wavcse-infra

`wavcse-infra` is the infrastructure control plane for reproducible wavCSE
research workloads. It prepares a persistent AWS EC2 controller, inspects disposable
RunPod GPU workers, and will later coordinate exact-commit execution and durable S3
artifact transfer.

It is not the wavCSE research repository. Model code, experiments, research
configuration, tests, and MLflow integration remain in the separate `wavCSE`
repository.

## Delivery status

The repository currently implements Phases 0–3 of the v1 specification:

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
- provider-neutral worker/request/offer models and bounded retries for safe reads;
- initial architecture, security, operations, provider, and decision documentation.

Phase 3 manages Pod resources only. SSH connections, worker bootstrap, artifact
transfer, wavCSE checkout/execution, MLflow runs, and jobs remain unimplemented.

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
infra worker gpu-types --cloud COMMUNITY --gpu-count 1
infra worker create --gpu <exact-type-id> --cloud COMMUNITY --image <image> --max-price <usd-hour>
infra worker stop <exact-worker-id>
infra worker start <exact-worker-id>
infra worker destroy <exact-worker-id>
```

Global `--config`, `--runpod-api-url`, `--runpod-timeout`, and `--verbose` options must
appear before the command name.

## Worker lifecycle

Phase 3 implements the Pod-resource portion of the lifecycle: discover an exact current
GPU offer, enforce availability and price limits, print and confirm a creation plan,
create once, persist the provider ID, and poll to a bounded provider state. Start, stop,
and destroy use exact provider IDs. Create and destroy require confirmation unless
`--yes` is supplied; that flag never bypasses validation or `--max-price`.

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

Normal GPU workers do not install OMP, Codex, AGF, or other agent-development tooling.
Any future development-worker profile must add that behavior explicitly without
changing the training-worker contract.

## Storage model

S3 is canonical for embeddings, checkpoints, and explicitly persisted large outputs.
RunPod local disks and network volumes are caches. Future workers will receive
time-limited presigned URLs for individual transfers; they will not receive long-lived
AWS credentials. Git stores code and small metadata, not generated tensors or archives.

## Security model

- The controller is trusted and uses its EC2 IAM role through the normal AWS SDK
  credential chain.
- RunPod credentials resolve in memory from an environment override or SSM
  `SecureString`; authorization values are redacted and never persisted.
- GPU workers are temporary and less trusted than the controller.
- S3 buckets remain private; presigned URLs are bearer secrets until expiry.
- SSH uses keys and explicit host-key policy. Global host verification bypass is not
  permitted.

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

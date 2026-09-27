# wavcse-infra

`wavcse-infra` is the infrastructure control plane for reproducible wavCSE
research workloads. It prepares a persistent AWS EC2 controller, inspects disposable
RunPod GPU workers, and will later coordinate exact-commit execution and durable S3
artifact transfer.

It is not the wavCSE research repository. Model code, experiments, research
configuration, tests, and MLflow integration remain in the separate `wavCSE`
repository.

## Delivery status

The repository currently implements Phase 0 of the v1 specification:

- a typed `infra` CLI and layered TOML/environment configuration;
- Ruff, pytest, ShellCheck, and shfmt validation;
- a locked `uv` environment and credential-free CI;
- initial architecture, security, operations, provider, and decision documentation.

Controller bootstrap, `infra doctor`, and RunPod reads are the next phases. No
implemented command contacts a cloud API or can provision a paid resource.

## Architecture

The persistent/stoppable EC2 controller is the writable development environment. It
contains OMP, the `wavCSE` checkout, this repository, and the `infra` CLI. AWS access
comes from an EC2 instance profile. The RunPod API key comes from the environment.

Disposable GPU workers execute immutable wavCSE commits. GitHub distributes code, a
private S3 bucket is the canonical store for large artifacts, and wavCSE retains
ownership of MLflow/DagsHub reporting.

See [Architecture](docs/ARCHITECTURE.md), [Security](docs/SECURITY.md), and the
[decision log](docs/DECISIONS.md) for boundaries and rationale.

## Local setup

Requirements:

- Python 3.12 or newer;
- [uv](https://docs.astral.sh/uv/);
- ShellCheck and shfmt for the complete validation suite.

Install locked dependencies and verify the CLI:

```bash
uv sync --locked --all-groups
uv run infra --help
make check
```

On a supported Ubuntu EC2 controller, Phase 1 will provide:

```bash
./controller/bootstrap.sh
```

The bootstrap is safe to rerun. It does not install OMP, configure user credentials,
or create cloud resources. See [Operations](docs/OPERATIONS.md) for controller setup
and reconstruction.

## Configuration

The default user configuration is:

```text
~/.config/wavcse-infra/config.toml
```

Copy [`config/infra.example.toml`](config/infra.example.toml) and adjust non-secret
values. Precedence is:

1. CLI options
2. environment variables
3. user configuration file
4. built-in defaults

`RUNPOD_API_KEY` is accepted only from the environment. Do not put it in TOML. The
supported variables are listed in [`.env.example`](.env.example) and documented in
[Operations](docs/OPERATIONS.md).

Validate without making network calls:

```bash
uv run infra config validate
```

## Commands

```bash
infra --help
infra config validate
```

Global `--config`, `--runpod-api-url`, `--runpod-timeout`, and `--verbose` options must
appear before the command name.

## Worker lifecycle

The planned lifecycle is create, wait for provider readiness, discover SSH, bootstrap,
health-check, execute an exact committed wavCSE revision, persist requested outputs,
and explicitly stop or destroy. Phase 0 implements none of this lifecycle. Creation and
destruction remain intentionally unavailable pending review.

## Storage model

S3 is canonical for embeddings, checkpoints, and explicitly persisted large outputs.
RunPod local disks and network volumes are caches. Future workers will receive
time-limited presigned URLs for individual transfers; they will not receive long-lived
AWS credentials. Git stores code and small metadata, not generated tensors or archives.

## Security model

- The controller is trusted and uses its EC2 IAM role through the normal AWS SDK
  credential chain.
- RunPod credentials are environment-only and authorization values are redacted.
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
mocked in tests and CI never creates paid infrastructure.

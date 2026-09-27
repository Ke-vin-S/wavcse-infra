# Operations

## Configuration

Configuration files have separate responsibilities:

| Location | Purpose | Committed |
| --- | --- | --- |
| `config/infra.example.toml` | Complete non-secret configuration template | Yes |
| `~/.config/wavcse-infra/config.toml` | Controller-specific runtime configuration | No |
| `.env.example` | Reference for supported environment variables | Yes |
| Real environment/secrets facility | Runtime secrets such as `RUNPOD_API_KEY` | No |

`controller/bootstrap.sh` creates the runtime file from the template when it is absent.
It prints the path that needs editing and never overwrites an existing file. To create it
manually instead:

```bash
mkdir -p ~/.config/wavcse-infra
cp --no-clobber config/infra.example.toml ~/.config/wavcse-infra/config.toml
```

Supported environment variables:

| Variable | Purpose |
| --- | --- |
| `RUNPOD_API_KEY` | RunPod bearer token; environment only |
| `WAVCSE_INFRA_AWS_REGION` | Region for STS and S3 diagnostics |
| `WAVCSE_INFRA_CONFIG` | Alternate user TOML path |
| `WAVCSE_INFRA_RUNPOD_API_URL` | RunPod REST base URL |
| `WAVCSE_INFRA_RUNPOD_TIMEOUT_SECONDS` | Per-request timeout |
| `WAVCSE_INFRA_RUNPOD_READ_ATTEMPTS` | Total safe read attempts |
| `WAVCSE_INFRA_RUNPOD_RETRY_BACKOFF_SECONDS` | Initial retry delay |
| `WAVCSE_INFRA_S3_BUCKET` | Private canonical artifact bucket |
| `WAVCSE_INFRA_S3_PREFIX` | Bucket prefix, default `wavcse` |
| `WAVCSE_INFRA_WAVCSE_PATH` | Controller wavCSE checkout |
| `WAVCSE_INFRA_SSH_PRIVATE_KEY` | Dedicated worker key path |
| `WAVCSE_INFRA_EXPECT_OMP` | Whether doctor requires `omp` |
| `WAVCSE_INFRA_MLFLOW_URL` | Optional MLflow health endpoint |

Agent-installer overrides are intentionally separate from runtime configuration:

| Variable | Purpose |
| --- | --- |
| `WAVCSE_INFRA_CONTROLLER_USER` | Target account when root cannot infer the controller user |
| `WAVCSE_INFRA_OMP_VERSION` | Controlled OMP release-tag override |
| `WAVCSE_INFRA_OMP_X86_64_SHA256` | Required x86-64 digest when overriding the OMP pin |
| `WAVCSE_INFRA_OMP_AARCH64_SHA256` | Required ARM64 digest when overriding the OMP pin |
| `WAVCSE_INFRA_CODEX_VERSION` | Controlled Codex release override |
| `WAVCSE_INFRA_AGF_VERSION` | Controlled AGF release-tag override |
| `WAVCSE_INFRA_AGF_X86_64_SHA256` | Required x86-64 digest when overriding the AGF pin |
| `WAVCSE_INFRA_AGF_AARCH64_SHA256` | Required ARM64 digest when overriding the AGF pin |

Empty values are treated as unset. CLI options override environment values. Validate the
result without network calls:

```bash
infra config validate
```

The default precedence is CLI arguments, environment variables, the runtime user TOML,
then application defaults. `CHANGE_ME` is a template marker and is treated as missing
configuration. `.env.example` is documentation only; this project does not automatically
load `.env` files.

## Initial controller setup

Prerequisites outside this repository:

1. Launch a supported Ubuntu EC2 instance.
2. Attach an instance profile with least-privilege access to the private artifact
   bucket/prefix. Do not create local static AWS credentials.
3. Configure controller SSH access and host security through normal AWS operations.
4. Apply `controller/cloud-init.yaml` as user data, or run:

   ```bash
   git clone https://github.com/Ke-vin-S/wavcse-infra.git
   cd wavcse-infra
   ./controller/bootstrap.sh
   nano ~/.config/wavcse-infra/config.toml
   ```

   Bootstrap creates the user configuration if missing and preserves it on every later
   run. It installs controller agent tools by default; use `--skip-agents` only when
   they are managed separately.
5. Complete user-specific GitHub, RunPod, and DagsHub/MLflow authentication. Agent
   authentication remains manual:

   ```text
   OMP:   start omp, then run /login (or /login <provider>)
   Codex: codex login --device-auth
   AGF:   no authentication required
   ```

   Standard browser-based Codex authentication is also available with `codex login`.
6. Clone the separate wavCSE repository under `~/projects/wavCSE`.
7. Run `infra doctor`.

Bootstrap installs controller prerequisites and the locked Python project. It is
idempotent and safe to rerun. It delegates OMP, Codex, and AGF installation to
`controller/install-agents.sh`; it does not inject secrets or provision cloud resources.

## Controller agent installation

`controller/install-agents.sh` is controller-only. Normal GPU training workers must
not run it or install agent-development tooling.

The reviewed default pins and upstream mechanisms are:

| Tool | Default | Installation mechanism | Installed command |
| --- | --- | --- | --- |
| OMP | `v18.3.2` | Installer from the exact `can1357/oh-my-pi` Git tag, binary mode, release SHA-256 verified | `~/.local/bin/omp` |
| Codex CLI | `0.157.1` | OpenAI standalone installer with `--release`; upstream release digest verification | `~/.local/bin/codex` |
| AGF | `v0.15.1` | Official GitHub release archive with pinned SHA-256 | `~/.local/bin/agf` |

Current OMP and Codex Linux installers do not require Node, npm, or Bun. AGF supports
`cargo install agf --locked`, which requires Rust 1.88 or newer and a C compiler, but
the selected official prebuilt archive does not require Rust/Cargo. The controller
therefore does not install those development runtimes solely for these tools.

Run the full installer or repair one missing tool:

```bash
make install-agents
./controller/install-agents.sh --only omp
./controller/install-agents.sh --only codex
./controller/install-agents.sh --only agf
```

A normal run detects and preserves any existing command on the controller PATH,
including historical Cargo/Bun/global-package installations. To replace an installed
command with the configured pinned release, request it explicitly:

```bash
./controller/install-agents.sh --only omp --upgrade
```

For a controlled one-off version override, set the matching version variable. OMP and
AGF overrides must also provide the matching architecture-specific SHA-256 variable.
Repository maintenance should normally update the reviewed pins and digests together,
after which `git pull` followed by `--upgrade` converges the controller.

The installer adds one managed block to the target login-shell profile (`~/.profile`,
an existing Bash-specific login profile, or `~/.zprofile` for Zsh). The block adds
`~/.local/bin`, `~/.cargo/bin`, and `~/.bun/bin` only when absent, so repeated runs do
not duplicate PATH entries or overwrite existing shell configuration. Cargo and Bun
paths preserve historical installations; the default installation does not require
either runtime. Start a new login shell after the first run.

The installer downloads official installer scripts to a temporary file before
execution; it does not use an opaque `curl | sudo bash` pipeline. It never runs login,
writes provider credentials, or changes existing OMP/Codex authentication stores.

## Routine read-only operations

```bash
infra doctor
```

Inspect existing RunPod workers without changing provider state:

```bash
infra worker list
infra worker show <worker-id>
```

`infra doctor` reports the loaded configuration path, AWS region, S3 bucket, and worker
SSH key as separate checks before checking local tools, Python, OMP, Codex, AGF, the
wavCSE path, RunPod credential presence, network endpoints, EC2 instance-profile
identity, and S3 access. Missing agent commands point to `controller/install-agents.sh`;
doctor remains read-only. Missing configuration values identify the TOML key and
environment override that can fix them. Required failures produce exit 1; invalid TOML
produces exit 2.

Example configuration section:

```text
PASS Config: /home/ubuntu/.config/wavcse-infra/config.toml
PASS AWS region: us-east-1
PASS S3 bucket: wavcse-research-artifacts
PASS Worker SSH key: /home/ubuntu/.ssh/wavcse_worker
```

`worker list` and `worker show` call only documented GET endpoints. The API token must
be present as `RUNPOD_API_KEY`; it is never read from TOML or a CLI option. These
commands do not change provider state.

## Controller reconstruction

1. Recreate an Ubuntu EC2 instance and attach the existing scoped instance profile.
2. Apply the thin cloud-init or clone `wavcse-infra` and run bootstrap.
3. Restore user-managed authentication from its authoritative secret systems.
4. Clone `wavCSE` and check out the required development branch.
5. Restore non-secret user configuration.
6. Run `infra doctor`, then reconcile RunPod state with `infra worker list`.

The recovery process does not copy state from a worker. Code comes from GitHub, large
artifacts from S3, and experiment metadata from MLflow/DagsHub.

## Failure handling

- RunPod 401/403: verify `RUNPOD_API_KEY` in the current process; do not print it.
- RunPod 404 on `worker show`: verify the immutable provider worker ID and account.
- RunPod 429/5xx or transport failure: safe reads retry within the configured bound.
- AWS identity failure: verify an instance profile is attached and IMDS access is not
  blocked. Do not work around it by creating permanent access keys.
- S3 failure: verify region, bucket, prefix, and role policy separately.
- Tool check failure: rerun bootstrap, then `make check`.
- OMP/Codex/AGF check failure: run `make install-agents`, start a new login shell, and
  rerun `infra doctor`.

No automated cleanup is present in Phases 0–2, and no paid resource is created.

## Bootstrap implementation notes

- Supported target: Ubuntu with the normal `apt` repositories and either a non-root
  invoking user, `SUDO_USER`, or the standard `ubuntu` account.
- `WAVCSE_INFRA_CONTROLLER_USER` explicitly selects the target account when needed.
- `WAVCSE_INFRA_UV_VERSION` can override the documented pinned uv version for a
  controlled upgrade.
- uv is installed into the target user's `~/.local/bin` without modifying shell files.
- Python 3.12 and the exact `uv.lock` environment are synchronized on every run.
- Agent setup is delegated to `controller/install-agents.sh`; `--skip-agents` is the
  explicit bootstrap opt-out.
- The configuration directory is created with mode `0700` and a new `config.toml` with
  mode `0600`; an existing file is preserved byte-for-byte.
- Credentials and user-specific external authentication remain explicit post-bootstrap
  steps.

The cloud-init file assumes the default Ubuntu account and the public canonical
repository URL. Customize those two non-secret values in an EC2 launch template when
necessary. It intentionally does not update an existing checkout, preventing first-boot
automation from overwriting controller work.

## Official operational references

- [AWS: IAM roles for Amazon EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html)
- [Boto3 credential provider chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html)
- [cloud-init boot stages](https://cloudinit.readthedocs.io/en/latest/explanation/boot.html)
- [cloud-init module reference](https://cloudinit.readthedocs.io/en/latest/reference/modules.html)
- [uv installation](https://docs.astral.sh/uv/getting-started/installation/)
- [uv installer configuration](https://docs.astral.sh/uv/reference/installer/)
- [OMP install options](https://github.com/can1357/oh-my-pi#install)
- [OMP provider authentication](https://omp.sh/docs/providers)
- [OpenAI Codex CLI installation](https://developers.openai.com/codex/cli)
- [OpenAI Codex authentication](https://developers.openai.com/codex/auth)
- [AGF install options](https://github.com/subinium/agf#install)

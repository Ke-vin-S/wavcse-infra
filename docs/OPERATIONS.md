# Operations

## Configuration

Configuration files have separate responsibilities:

| Location | Purpose | Committed |
| --- | --- | --- |
| `config/infra.example.toml` | Complete non-secret configuration template | Yes |
| `~/.config/wavcse-infra/config.toml` | Controller-specific runtime configuration | No |
| `.env.example` | Reference for supported environment variables | Yes |
| AWS SSM Parameter Store `SecureString` | Persistent RunPod API key | No |
| Process environment | Optional temporary/local `RUNPOD_API_KEY` override | No |

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
| `RUNPOD_API_KEY` | Optional RunPod bearer token override; takes precedence over SSM |
| `WAVCSE_INFRA_AWS_REGION` | Region for STS and S3 diagnostics |
| `WAVCSE_INFRA_CONFIG` | Alternate user TOML path |
| `WAVCSE_INFRA_RUNPOD_API_KEY_PARAMETER` | Non-secret SSM parameter-name override |
| `WAVCSE_INFRA_RUNPOD_API_URL` | RunPod REST base URL |
| `WAVCSE_INFRA_RUNPOD_CREATE_RECONCILE_ATTEMPTS` | Exact-name checks after an ambiguous create |
| `WAVCSE_INFRA_RUNPOD_LIFECYCLE_TIMEOUT_SECONDS` | Default create/start/stop/destroy wait timeout |
| `WAVCSE_INFRA_RUNPOD_MAX_POLL_INTERVAL_SECONDS` | Maximum lifecycle polling delay |
| `WAVCSE_INFRA_RUNPOD_POLL_INTERVAL_SECONDS` | Initial lifecycle polling delay |
| `WAVCSE_INFRA_RUNPOD_TIMEOUT_SECONDS` | Per-request timeout |
| `WAVCSE_INFRA_RUNPOD_READ_ATTEMPTS` | Total safe read attempts |
| `WAVCSE_INFRA_RUNPOD_RETRY_BACKOFF_SECONDS` | Initial retry delay |
| `WAVCSE_INFRA_S3_BUCKET` | Private canonical artifact bucket |
| `WAVCSE_INFRA_S3_PREFIX` | Bucket prefix, default `wavcse` |
| `WAVCSE_INFRA_WAVCSE_PATH` | Controller wavCSE checkout |
| `WAVCSE_INFRA_SSH_PRIVATE_KEY` | Dedicated worker key path |
| `WAVCSE_INFRA_SSH_KNOWN_HOSTS_FILE` | Isolated worker known-hosts file |
| `WAVCSE_INFRA_SSH_CONNECT_TIMEOUT_SECONDS` | Per-attempt OpenSSH connect timeout |
| `WAVCSE_INFRA_SSH_COMMAND_TIMEOUT_SECONDS` | Default worker health-command timeout |
| `WAVCSE_INFRA_SSH_BOOTSTRAP_TIMEOUT_SECONDS` | Worker package bootstrap timeout |
| `WAVCSE_INFRA_SSH_READINESS_TIMEOUT_SECONDS` | Overall SSH-ready polling timeout |
| `WAVCSE_INFRA_SSH_POLL_INTERVAL_SECONDS` | Initial SSH readiness polling delay |
| `WAVCSE_INFRA_SSH_MAX_POLL_INTERVAL_SECONDS` | Maximum SSH readiness polling delay |
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

## RunPod credential setup and rotation

The three credential concerns are deliberately separate:

| Concern | Location |
| --- | --- |
| Non-secret configuration | `~/.config/wavcse-infra/config.toml` |
| Secret storage | AWS SSM Parameter Store `SecureString` |
| AWS authentication | Attached EC2 instance profile using temporary role credentials |

Add the non-secret reference to the controller configuration. Bootstrap includes this
entry for newly created configurations but preserves existing files, so existing
controllers must add it manually:

```toml
[runpod]
api_key_parameter = "/wavcse-infra/runpod/api-key"
```

The controller instance-profile role needs only `ssm:GetParameter` on the exact
parameter ARN:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:<region>:<account-id>:parameter/wavcse-infra/runpod/api-key"
    }
  ]
}
```

When a customer-managed KMS key protects the `SecureString`, add a separate
`kms:Decrypt` permission scoped to that key ARN. The default controller role does not
need `ssm:PutParameter`, broad SSM/KMS permissions, or administrator access.

From a trusted Bash shell whose AWS identity is separately authorized to create or
update the parameter, use this prompt-based command. The secret is sent on standard
input through `file:///dev/stdin`; its literal value is not placed in shell history or
the AWS CLI argument list:

```bash
IFS= read -r -p 'AWS region: ' AWS_REGION
IFS= read -r -s -p 'RunPod API key: ' RUNPOD_SECRET
printf '\n'
printf '%s' "${RUNPOD_SECRET}" | aws ssm put-parameter \
  --region "${AWS_REGION}" \
  --name '/wavcse-infra/runpod/api-key' \
  --type SecureString \
  --value file:///dev/stdin \
  --overwrite
unset RUNPOD_SECRET AWS_REGION
```

For a customer-managed KMS key, add `--key-id <key-id-or-arn>` to that command. Use the
same command with `--overwrite` to rotate the RunPod key. Each new `infra` process reads
the current value when it constructs its RunPod client; it does not cache the value in
a file or local state.

Credential precedence is:

1. non-empty `RUNPOD_API_KEY` environment variable;
2. decrypted SSM parameter named by `runpod.api_key_parameter`;
3. credential unavailable.

## Initial controller setup

Prerequisites outside this repository:

1. Launch a supported Ubuntu EC2 instance.
2. Attach an instance profile with least-privilege access to the private artifact
   bucket/prefix and the configured RunPod SSM parameter. Do not create local static
   AWS credentials.
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
5. Create the RunPod SSM `SecureString`, add its non-secret parameter name to the TOML,
   and grant the instance profile the scoped read permission described above. Complete
   other user-specific GitHub and DagsHub/MLflow authentication. Agent authentication
   remains manual:

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

## RunPod worker operations

```bash
infra doctor
```

Worker management requires the current REST v2 base URL. Controllers created from the older
Phase 2 template must update their user-owned file explicitly:

```toml
[runpod]
api_url = "https://api.runpod.io/v2"
```

Inspect existing RunPod workers and current offers without changing provider state:

```bash
infra worker list
infra worker show <worker-id>
infra worker gpu-types --cloud COMMUNITY --gpu-count 1
infra worker gpu-types --cloud SECURE --gpu-count 1 --json
```

`infra doctor` reports the loaded configuration path, AWS region, S3 bucket, and worker
SSH key as separate checks before checking local tools, Python, OMP, Codex, AGF, the
wavCSE path, RunPod credential resolution, network endpoints, EC2 instance-profile
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

`worker list`, `worker show`, and `worker gpu-types` call only documented GET endpoints.
The API token is resolved once per command from `RUNPOD_API_KEY` or the configured SSM
parameter. The secret is never read from TOML or a CLI option. These commands do not
change provider state.

Validate SSM resolution after a fresh SSH login without an environment override:

```bash
unset RUNPOD_API_KEY
infra doctor
infra worker list
```

Doctor reports the source and may display the non-secret parameter name, but never the
value, length, prefix, suffix, hash, or fingerprint.

### Safe first-worker procedure

1. Inspect Community Cloud offers for one GPU. Choose an exact type with confirmed
   availability and note its displayed total hourly price:

   ```bash
   infra worker gpu-types --cloud COMMUNITY --gpu-count 1
   ```

2. Confirm that the public half of the dedicated key configured as `ssh.private_key`
   is registered in the RunPod account. Keep the private half on the controller and
   restrict it to mode `0600`:

   ```bash
   chmod 0600 ~/.ssh/wavcse_worker
   infra doctor
   ```

   Never copy this private key, a GitHub key, AWS credentials, or the RunPod token into
   a Pod.

3. Create one worker using that exact GPU ID, a reviewed official Ubuntu-based image,
   minimal test storage, SSH setup, and a maximum price at or just above the displayed
   total:

   ```bash
   infra worker create \
     --gpu '<exact-gpu-type-id>' \
     --gpu-count 1 \
     --cloud COMMUNITY \
     --image '<reviewed-container-image>' \
     --container-disk 20 \
     --volume 0 \
     --start-ssh \
     --max-price '<maximum-total-usd-per-hour>'
   ```

   The command prints the generated infra identity, resource selection, storage,
   availability, and current provider list price before prompting. Review the complete
   plan, then answer `y`. For deliberate non-interactive automation, add `--yes`; it
   does not bypass the maximum price or availability checks.

4. Record the provider ID printed after the Pod reaches provider `RUNNING`. Then wait
   for authenticated SSH, bootstrap idempotently, and inspect the resulting readiness:

   ```bash
   infra worker show <exact-worker-id>
   infra worker wait-ssh <exact-worker-id>
   infra worker bootstrap <exact-worker-id>
   infra worker health <exact-worker-id>
   ```

   `RUNNING` alone is not `READY`. Bootstrap first refreshes the v2 SSH endpoint, waits
   for sshd, installs only stable worker prerequisites, and requires the expected marker,
   Git, Python, uv, disk visibility, and a healthy NVIDIA GPU. It is safe to rerun after
   a partial failure. `health` is read-only on the worker apart from the controller's
   supplemental local-state update. JSON is available with `--json`.

5. Stop/start or destroy it using only that exact ID:

   ```bash
   infra worker stop <exact-worker-id>
   infra worker start <exact-worker-id>
   infra worker wait-ssh <exact-worker-id>
   infra worker health <exact-worker-id>
   infra worker destroy <exact-worker-id>
   ```

   Create/start/stop/destroy waits are bounded. Override one command with
   `--wait-timeout <seconds>` when needed.

### Stop versus destroy

`stop` retains the Pod and its persistent configuration. RunPod reports zero current
compute cost for an exited Pod, but retained storage can still incur charges. Current
RunPod documentation says host-local volume storage is charged while stopped and a
network volume continues its independent storage charge. Container disk is erased on
stop.

`destroy` terminates the Pod resource permanently. It shows the exact ID, name, GPU,
state, and known running price, then requires confirmation unless `--yes` is supplied.
It never accepts a loose name or resolves a prefix. An already-absent ID is reported as
such and does not cause another resource to be selected. Separately managed network
volumes are not deleted by this command.

### Local state and reconciliation

Created-worker metadata is stored beneath:

```text
~/.local/state/wavcse-infra/workers.json
```

Writes are atomic and contain no credentials. Provider reads remain authoritative.
`worker list` and `worker show` update known records while leaving unrelated account
Pods unclaimed. Missing tracked Pods are marked absent locally. Readiness timestamps,
bootstrap version, endpoint coordinates, disk availability, and GPU/driver facts are
supplemental; stopping/destroying resets readiness and never changes provider truth.

If create loses its response, the CLI checks for the exact generated infra name. It
adopts one exact match, reports multiple matches, or fails safely after bounded checks.
It never retries the paid create POST. On the uncertain/no-match result, run:

```bash
infra worker list
```

Inspect the generated identity shown in the error before issuing another create.

## Controller reconstruction

1. Recreate an Ubuntu EC2 instance and attach the existing scoped instance profile.
2. Apply the thin cloud-init or clone `wavcse-infra` and run bootstrap.
3. Restore the non-secret SSM reference and verify the instance profile can decrypt the
   existing `SecureString`; do not copy the key onto the controller filesystem.
4. Clone `wavCSE` and check out the required development branch.
5. Restore non-secret user configuration.
6. Run `infra doctor`, then reconcile RunPod state with `infra worker list`.

The recovery process does not copy state from a worker. Code comes from GitHub, large
artifacts from S3, and experiment metadata from MLflow/DagsHub.

## Failure handling

- RunPod credential not configured: set `runpod.api_key_parameter` or temporarily export
  `RUNPOD_API_KEY`; do not put the key in TOML.
- SSM parameter not found: verify the configured parameter name and region.
- SSM access denied: grant the controller instance profile `ssm:GetParameter` on the
  exact parameter ARN. For a customer-managed KMS key, also verify `kms:Decrypt`.
- SSM AWS/network failure: verify the configured region, instance profile, IMDS access,
  and controller connectivity; do not create permanent AWS access keys.
- RunPod 401/403 after successful resolution: rotate or correct the stored RunPod key;
  for 403, also verify that the key has the required resource permission; do not print
  the key.
- REST v1 configuration error: change `runpod.api_url` to `https://api.runpod.io/v2`.
- Invalid/unavailable GPU: rerun `infra worker gpu-types` with the intended cloud and
  count; do not substitute a different resource implicitly.
- Maximum price rejection: select a cheaper explicit offer or deliberately raise the
  limit after reviewing current pricing. `--yes` cannot bypass the guard.
- Ambiguous create: inspect `infra worker list` for the complete generated identity.
  The CLI intentionally did not repeat the create request.
- RunPod 404 on `worker show`: verify the immutable provider worker ID and account.
- RunPod 429/5xx or transport failure: safe reads retry within the configured bound.
- Lifecycle timeout: inspect the exact ID with `infra worker show`; the error includes
  the last known provider state and does not imply the resource is absent.
- SSH endpoint unavailable: verify provider state, create-time `--start-ssh`, port
  `22/tcp`, an SSH-capable image, and a registered RunPod account public key.
- SSH authentication failure: verify the configured private key corresponds to that
  registered public key; do not print or copy the private key.
- SSH host-key mismatch: inspect the exact Pod ID, public IP, and mapped port before
  changing the dedicated wavcse-infra known-hosts entry. Never disable checking.
- SSH timeout/refusal: rerun `wait-ssh` with a deliberate `--wait-timeout`; provider
  `RUNNING` can precede sshd readiness.
- Bootstrap/package failure: verify the image is supported Ubuntu with apt networking,
  then rerun `infra worker bootstrap <id>`; completed steps and the version marker are
  idempotent.
- Health failure: inspect the named failed check. A missing marker requires bootstrap;
  missing/no-GPU `nvidia-smi` prevents `READY`; AMD workers are explicitly unsupported
  in Phase 4.
- AWS identity failure: verify an instance profile is attached and IMDS access is not
  blocked. Do not work around it by creating permanent access keys.
- S3 failure: verify region, bucket, prefix, and role policy separately.
- Tool check failure: rerun bootstrap, then `make check`.
- OMP/Codex/AGF check failure: run `make install-agents`, start a new login shell, and
  rerun `infra doctor`.

No normal test, CI job, or validation target performs a paid RunPod mutation. Operators
must invoke lifecycle commands explicitly.

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
- Bootstrap copies only the non-secret parameter reference for a new configuration.
  Credentials and user-specific external authentication remain explicit post-bootstrap
  steps, and existing configuration is never overwritten.

The cloud-init file assumes the default Ubuntu account and the public canonical
repository URL. Customize those two non-secret values in an EC2 launch template when
necessary. It intentionally does not update an existing checkout, preventing first-boot
automation from overwriting controller work.

## Official operational references

- [AWS: IAM roles for Amazon EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html)
- [Boto3 credential provider chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html)
- [SSM GetParameter API](https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_GetParameter.html)
- [AWS CLI put-parameter](https://docs.aws.amazon.com/cli/latest/reference/ssm/put-parameter.html)
- [Parameter Store IAM access](https://docs.aws.amazon.com/systems-manager/latest/userguide/sysman-paramstore-access.html)
- [cloud-init boot stages](https://cloudinit.readthedocs.io/en/latest/explanation/boot.html)
- [cloud-init module reference](https://cloudinit.readthedocs.io/en/latest/reference/modules.html)
- [uv installation](https://docs.astral.sh/uv/getting-started/installation/)
- [uv installer configuration](https://docs.astral.sh/uv/reference/installer/)
- [OMP install options](https://github.com/can1357/oh-my-pi#install)
- [OMP provider authentication](https://omp.sh/docs/providers)
- [OpenAI Codex CLI installation](https://developers.openai.com/codex/cli)
- [OpenAI Codex authentication](https://developers.openai.com/codex/auth)
- [AGF install options](https://github.com/subinium/agf#install)

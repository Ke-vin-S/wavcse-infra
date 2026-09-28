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
| `WAVCSE_INFRA_S3_PRESIGN_EXPIRY_SECONDS` | Default presigned URL lifetime, 60–604800 |
| `WAVCSE_INFRA_JOBS_WORKER_ROOT` | Worker-side isolated job workspace root |
| `WAVCSE_INFRA_JOBS_RUNNER_PATH` | Absolute worker path of the reviewed job runner |
| `WAVCSE_INFRA_JOBS_DEFAULT_TIMEOUT_SECONDS` | Default job timeout when a spec omits one |
| `WAVCSE_INFRA_JOBS_LOG_TAIL_BYTES` | Bound for the captured local log copy |
| `WAVCSE_INFRA_WAVCSE_PATH` | Controller wavCSE checkout |
| `WAVCSE_INFRA_SSH_PRIVATE_KEY` | Dedicated worker key path |
| `WAVCSE_INFRA_SSH_KNOWN_HOSTS_FILE` | Isolated worker known-hosts file |
| `WAVCSE_INFRA_SSH_CONNECT_TIMEOUT_SECONDS` | Per-attempt OpenSSH connect timeout |
| `WAVCSE_INFRA_SSH_COMMAND_TIMEOUT_SECONDS` | Default worker health-command timeout |
| `WAVCSE_INFRA_SSH_BOOTSTRAP_TIMEOUT_SECONDS` | Worker package bootstrap timeout |
| `WAVCSE_INFRA_SSH_TRANSFER_TIMEOUT_SECONDS` | Worker artifact transfer timeout |
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
   infra worker gpu-types --cloud COMMUNITY --gpu-count 1 --require-direct-ssh
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
     --require-direct-ssh \
     --max-price '<maximum-total-usd-per-hour>'
   ```

   The command prints the generated infra identity, resource selection, storage,
   availability, and current provider list price before prompting. Review the complete
   plan, then answer `y`. For deliberate non-interactive automation, add `--yes`; it
   does not bypass the maximum price or availability checks.

   The direct-SSH constraint uses RunPod's GraphQL scheduler filter and refuses before
   confirmation when no public-IP-compatible offer exists. The create request repeats
   that constraint atomically with placement; it does not select an arbitrary Community
   host from the broader REST catalog.

4. Record the provider ID printed after the Pod reaches provider `RUNNING`. Then wait
   for authenticated SSH, bootstrap idempotently, and inspect the resulting readiness:

   ```bash
   infra worker show <exact-worker-id>
   infra worker wait-ssh <exact-worker-id>
   infra worker bootstrap <exact-worker-id>
   infra worker health <exact-worker-id>
   ```

   `RUNNING` alone is not `READY`. Bootstrap first refreshes the v2 direct SSH endpoint,
   proves non-interactive execution with a marker, installs only stable worker
   prerequisites, and requires the expected bootstrap marker, Git, Python, uv, usable
   execution storage, any explicitly requested volume mount, and a healthy NVIDIA GPU.
   A zero-volume worker checks its ephemeral container filesystem and does not require
   `/workspace` to be a mount. It is safe to rerun after a partial failure.
   `health` is read-only on the worker apart from the controller's supplemental
   local-state update. JSON is available with `--json`.

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

## Artifact storage operations

S3 is canonical. The controller reaches it with its instance profile; the required
policy is in [Security](SECURITY.md#presigned-urls-and-worker-transfer). Workers only
receive one presigned URL per transfer.

Common commands:

```bash
infra storage list --prefix embeddings/
infra storage presign-download embeddings/v1/voxceleb-minpooling.tar --expires-in 21600
infra storage presign-upload embeddings/v1/voxceleb-minpooling.tar
infra storage verify embeddings/v1/voxceleb-minpooling.tar --expected-size 21474836480
infra storage verify embeddings/v1/voxceleb-minpooling.tar --manifest embeddings/v1/voxceleb-minpooling.manifest.json
infra storage download embeddings/v1/voxceleb-minpooling.tar /workspace/embeddings/voxceleb.tar \
  --worker <exact-worker-id> --expected-size 21474836480
infra storage upload scratch/run-0001/outputs.tar /workspace/outputs.tar --worker <exact-worker-id>
```

Keys are relative to `storage.prefix`. `embeddings/v1/x.tar` means
`s3://<bucket>/wavcse/embeddings/v1/x.tar`. Absolute keys, `..`, empty path segments, a
trailing separator on an object key, whitespace, `?`/`#`, a URL, or a key that already
repeats the configured prefix are rejected rather than rewritten. No command accepts a
bucket, an absolute key, or an `--overwrite`-free replacement of a persisted object.

`presign-download` and `presign-upload` print the URL to standard output, because that is
what they were asked to produce. Default PUT URLs sign the `If-None-Match: *` HTTP header;
the caller must send it. `--overwrite` deliberately omits that guard. Treat URL output
as a secret until it expires: do not paste it into tickets, documentation, MLflow
parameters, or shell history.

A version 1 manifest looks like:

```json
{
  "schema_version": 1,
  "artifact_name": "wavcse-base-v1-minpool-voxceleb",
  "artifact_type": "embeddings-archive",
  "dataset": "voxceleb",
  "object_key": "embeddings/wavcse-base-v1-minpool/voxceleb-minpooling.tar",
  "size_bytes": 21474836480,
  "sha256": "<64 lowercase hexadecimal characters>",
  "created_at": "2026-09-28T12:00:00Z",
  "generator_git_commit": "<full 40 or 64 character commit ID>",
  "extracted_destination": "datasets/voxceleb/minpooling",
  "metadata": {"pooling": "minpooling", "sample_rate": "16000"},
  "notes": "Generated by the wavCSE embedding pipeline."
}
```

Keep one such manifest beside each dataset archive. `object_key` is relative to
`storage.prefix`, so a manifest stays valid if the canonical
bucket is replaced while the layout is preserved. Optional fields are omitted when the
value was not actually known. Manifests never contain presigned URLs or credentials;
validation rejects them.

`infra storage verify` confirms existence, size, metadata, and manifest consistency. It
does not download the object, so it cannot confirm content — the command says so
explicitly. Use `download` with `--expected-sha256` when content must be proven on the
machine that will consume it. Single presigned PUT uploads are limited to 5 GB by S3;
larger outputs need a separate multipart transfer workflow.

### Operator test 1 — controller only, no paid resource

This exercises presign, upload, listing, verification, and download against the real
bucket. It creates one tiny object under `scratch/`; delete it only if you own it and
intend to.

```bash
KEY="scratch/phase5-probe-$(date +%s%N).txt"
PROBE_SOURCE="$(mktemp /tmp/wavcse-phase5-source.XXXXXX)"
PROBE_COPY="$(mktemp /tmp/wavcse-phase5-copy.XXXXXX)"
printf 'wavcse phase 5 probe\n' >"${PROBE_SOURCE}"
SHA="$(sha256sum "${PROBE_SOURCE}" | cut -d' ' -f1)"
SIZE="$(stat -c %s "${PROBE_SOURCE}")"

IFS= read -r URL < <(infra storage presign-upload "${KEY}")
curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
  --config - -H 'If-None-Match: *' -X PUT --data-binary "@${PROBE_SOURCE}" \
  < <(printf 'url = "%s"\n' "${URL}")
unset URL

infra storage list --prefix scratch/
infra storage verify "${KEY}" --expected-size "${SIZE}"

IFS= read -r URL < <(infra storage presign-download "${KEY}")
curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
  --config - -o "${PROBE_COPY}" < <(printf 'url = "%s"\n' "${URL}")
unset URL
printf '%s  %s\n' "${SHA}" "${PROBE_COPY}" | sha256sum --check
printf 'Keep for worker test: KEY=%s SHA=%s SIZE=%s\n' "${KEY}" "${SHA}" "${SIZE}"
rm -f -- "${PROBE_SOURCE}" "${PROBE_COPY}"
```

Expected: the PUT and GET succeed, `list` shows the object, `verify` reports the size as
matched and states that content was not verified, and the final `sha256sum --check`
prints `OK`. A rerun fails at `presign-upload` with "already exists", which is the
intended replacement safeguard.

### Operator test 2 — one worker round trip

After the Phase 4 procedure has produced a `READY` worker, reuse the key from test 1:

```bash
KEY="scratch/phase5-probe-<value printed by controller test>.txt"
SHA="<64-character digest printed by controller test>"
SIZE="$(infra storage verify "${KEY}" --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["size_bytes"])')"
WORKER_ID="<exact-ready-worker-id>"
WORKER_PATH="/tmp/$(basename "${KEY}")"
COPY_KEY="scratch/phase5-worker-copy-$(date +%s%N).txt"

infra storage download "${KEY}" "${WORKER_PATH}" \
  --worker "${WORKER_ID}" --expected-size "${SIZE}" --expected-sha256 "${SHA}"
infra worker exec "${WORKER_ID}" -- sha256sum "${WORKER_PATH}"
infra storage upload "${COPY_KEY}" "${WORKER_PATH}" --worker "${WORKER_ID}"
infra storage verify "${COPY_KEY}" --expected-size "${SIZE}"
ROUNDTRIP_COPY="$(mktemp /tmp/wavcse-phase5-roundtrip.XXXXXX)"
IFS= read -r URL < <(infra storage presign-download "${COPY_KEY}")
curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
  --config - -o "${ROUNDTRIP_COPY}" < <(printf 'url = "%s"\n' "${URL}")
unset URL
printf '%s  %s\n' "${SHA}" "${ROUNDTRIP_COPY}" | sha256sum --check
rm -f -- "${ROUNDTRIP_COPY}"
```

The worker-side `sha256sum` and final controller-side `sha256sum --check` must equal the
original digest. `upload` must also report controller verification of the stored size.
`download` refuses to
replace an existing destination unless `--overwrite` is supplied, and a
`--expected-sha256` mismatch fails without touching the destination.

These tests use a small object on purpose. Do not use them to move the real embedding
set; materialize the approximately 20 GiB archives only when a job actually needs them.

## Recorded job operations

Phase 6 executes one versioned job specification on one explicit, already-READY worker.
It never creates, bootstraps, starts, or destroys a worker, and it never reruns a failed
job; a new attempt is a new job ID.

### Job specification version 1

Job specifications are JSON so the infrastructure keeps a dependency-free, exact parser.
The repository deliberately does not add a YAML dependency for this format.

```json
{
  "schema_version": 1,
  "name": "dg-0004-seed-42",
  "source": {
    "repository": "https://github.com/Synergy-io/wavCSE.git",
    "commit": "<full 40 or 64 character commit ID>"
  },
  "setup": {"argv": ["uv", "sync", "--locked"]},
  "command": {
    "argv": ["uv", "run", "python", "-m", "improvements.run_improvements", "--model", "gbc"],
    "working_directory": "improvements"
  },
  "runtime": {
    "timeout_seconds": 21600,
    "environment": {"PYTHONUNBUFFERED": "1"},
    "environment_secrets": ["MLFLOW_TRACKING_USERNAME", "MLFLOW_TRACKING_PASSWORD"]
  },
  "inputs": [
    {
      "artifact": "embeddings/wavcse-base-v1-minpool/voxceleb-minpooling.tar",
      "destination": "voxceleb-minpooling.tar",
      "manifest": "embeddings/wavcse-base-v1-minpool/voxceleb-minpooling.manifest.json"
    }
  ],
  "outputs": [
    {"path": "outputs/kfold_summary.json", "artifact": "jobs/dg-0004-seed-42/kfold_summary.json"}
  ],
  "tracking": {"metadata": {"study": "DG-0004", "seed": "42"}}
}
```

| Field | Meaning |
| --- | --- |
| `source.repository` | Anonymous `https://` remote only; SSH remotes and credential-bearing URLs are rejected |
| `source.commit` | Full immutable commit ID; branches, tags, short prefixes, and `HEAD` are rejected |
| `setup.argv` | Optional reviewed environment preparation, run in the checkout before the command |
| `command.argv` | Required argument vector. A shell command string is not representable |
| `command.working_directory` | Optional relative subdirectory of the verified checkout |
| `runtime.timeout_seconds` | Optional bound; defaults to `jobs.default_timeout_seconds` |
| `runtime.environment` | Non-secret literal variables; credential-shaped and reserved names are rejected |
| `runtime.environment_secrets` | Names resolved from the submitting shell; values are never persisted |
| `inputs[].artifact` / `destination` / `manifest` / `sha256` | Phase 5 key and workspace-relative path; required inputs need a manifest or SHA-256 |
| `outputs[].path` / `artifact` / `required` / `overwrite` | Worker path, S3 key, whether it gates success, replacement opt-in |
| `tracking.metadata` | Non-secret pass-through labels; bearer values are rejected |

Unknown fields, duplicate declarations, absolute or `..` paths, and path escapes are
rejected before any worker is contacted. Output paths may not target `source/` or
`state/`; upload also rejects symlink components. Required inputs without a manifest
or SHA-256 are rejected because size alone does not identify their content.

### Operator procedure

```bash
# 1. Export any tracking credentials the job references (values stay in this shell).
export MLFLOW_TRACKING_USERNAME='...'
export MLFLOW_TRACKING_PASSWORD='...'

# 2. Confirm the worker is RUNNING and READY (bootstrap it first if not).
infra worker show <exact-worker-id>
infra worker health <exact-worker-id>

# 3. Submit against that exact worker. --wait is optional.
infra job submit jobs/dg-0004-seed-42.json --worker <exact-worker-id>

# 4. Inspect it. status reconciles with the worker and persists declared outputs.
infra job status <job-id>
infra job logs <job-id> --tail-bytes 65536

# 5. Cancel a run you no longer want. This kills only the job process tree.
infra job cancel <job-id>
```

`infra job status` exits 1 when the job is `FAILED`, and 0 for `RUNNING`, `SUCCEEDED`, or
`CANCELLED`. `--json` renders the complete durable record.

### Job state machine

```text
PENDING -> PREPARING -> RUNNING -> SUCCEEDED
   |           |           |
   +-----------+-----------+-> FAILED
   |           |           |
   +-----------+-----------+-> CANCELLED
```

Terminal states are frozen: a retry is a new job ID. `SUCCEEDED` requires the command to
exit 0 *and* every required declared output to be persisted and size-verified at the
controller. A scientific failure is a failure: its exit code, stage, timeout flag, logs,
and provenance are preserved, and optional outputs are still persisted for debugging.
The executed commit is verified at launch; the research command and setup must be
trusted not to replace source code during their own execution.

### Local state and logs

```text
~/.local/state/wavcse-infra/jobs/<job-id>.json   durable non-secret record
~/.local/state/wavcse-infra/jobs/<job-id>.log    bounded copy of the job's own output
```

Remote state lives in the job workspace on the worker:

```text
<jobs.worker_root>/<job-id>/
├── source/           detached checkout of the verified commit
├── inputs/           materialized Phase 5 artifacts
├── outputs/          declared experiment outputs
├── logs/job.log      combined stdout/stderr
└── state/            pid, finished, cancelled, and non-secret descriptor copies
```

Logs are worker-side; the local copy is written when a job reaches a terminal state and is
bounded by `jobs.log_tail_bytes`. Durable logs beyond that must be declared as outputs.

### Provenance and MLflow ownership

wavCSE owns MLflow. `wavcse-infra` never creates a run. Each job receives the non-secret
variables `INFRA_PROVIDER`, `INFRA_WORKER_ID`, `INFRA_GPU`, `INFRA_GPU_COUNT`,
`INFRA_GIT_COMMIT`, `INFRA_JOB_ID`, and `INFRA_WORKER_NAME` so the research process can
log its own provenance, and the durable record keeps the same facts locally. See
[ADR-019](DECISIONS.md#adr-019-wavcse-keeps-mlflow-ownership-phase-6-supplies-provenance-and-secrets-by-name).

### Operator test 3 — first recorded job (paid, tiny)

This is the minimal paid Phase 6 test. It is deliberately not training: it proves the
exact commit, input materialization, execution, logs, status, output persistence, and that
the worker survives. Manually create exactly one cheap worker using the Phase 3
`infra worker create` cost guard with an explicit GPU and maximum price, then bootstrap
it to READY using the Phase 4 procedure. Keep that worker alive for both tests.
Other prerequisites are a configured bucket and the `KEY`/`SHA`/`SIZE` values printed
by operator test 1.

The job runs `python3` directly, so no research environment is needed. A real wavCSE run
would instead declare `"setup": {"argv": ["uv", "sync", "--locked"]}` and a
`uv run python -m improvements...` command.

The runner also provides `WAVCSE_JOB_ID`, `WAVCSE_JOB_COMMIT`, `WAVCSE_JOB_DIRECTORY`, and
`WAVCSE_JOB_LOG` to the job, which is how a command addresses its own `inputs/` and
`outputs/` directories; job specifications may not set those names themselves.

```bash
cd ~/projects/wavcse-infra
COMMIT="$(git -C ~/projects/wavCSE rev-parse HEAD)"        # a commit you have pushed
WORKER_ID="<exact-ready-worker-id>"
INPUT_KEY="scratch/phase5-probe-<value printed by operator test 1>.txt"
INPUT_SHA="<SHA printed by operator test 1>"
INPUT_SIZE="<SIZE printed by operator test 1>"
OUTPUT_KEY="scratch/phase6-probe-$(date +%s%N).json"
JOB_SPEC="$(mktemp /tmp/wavcse-phase6-job.XXXXXX.json)"
JOB_RESULT="$(mktemp /tmp/wavcse-phase6-result.XXXXXX.json)"
test -z "$(git -C ~/projects/wavCSE status --porcelain)"
git -C ~/projects/wavCSE cat-file -e "${COMMIT}^{commit}"

python3 - "$COMMIT" "$INPUT_KEY" "$INPUT_SHA" "$INPUT_SIZE" "$OUTPUT_KEY" >"${JOB_SPEC}" <<'JSON'
import json, sys
commit, input_key, input_sha, input_size, output_key = sys.argv[1:6]
print(json.dumps({
    "schema_version": 1,
    "name": "phase6-probe",
    "source": {"repository": "https://github.com/Synergy-io/wavCSE.git", "commit": commit},
    "command": {"argv": [
        "python3", "-c",
        "import hashlib,json,os,pathlib,subprocess,sys;"
        "root=pathlib.Path(os.environ['WAVCSE_JOB_DIRECTORY']);"
        "head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip();"
        "assert head==os.environ['INFRA_GIT_COMMIT'];"
        "data=(root/sys.argv[1]).read_bytes();"
        "target=root/sys.argv[2]; target.parent.mkdir(parents=True,exist_ok=True);"
        "target.write_text(json.dumps({'bytes':len(data),"
        "'sha256':hashlib.sha256(data).hexdigest(),"
        "'job':os.environ['INFRA_JOB_ID'],"
        "'worker':os.environ['INFRA_WORKER_ID'],"
        "'commit':head}));"
        "print('phase6 probe ok', os.path.basename(sys.argv[1]))",
        "inputs/probe.txt", "outputs/probe.json"]},
    "inputs": [{"artifact": input_key, "destination": "probe.txt",
                "sha256": input_sha, "size_bytes": int(input_size)}],
    "outputs": [{"path": "outputs/probe.json", "artifact": output_key}],
    "tracking": {"metadata": {"test": "phase6-probe"}},
}, indent=2))
JSON

infra job submit "${JOB_SPEC}" --worker "${WORKER_ID}" --wait --wait-timeout 900 --json > "${JOB_RESULT}"
```

The worker verifies the declared SHA-256 and size before materializing the input.
Alternatively, use the Phase 5 sidecar manifest as the input's verification source.

Read the returned job ID, verify the durable object's contents through a short-lived
presigned GET, and confirm the worker is still READY. The URL stays on a pipe and is
never printed or placed in a process argument.

```bash
JOB_ID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["job_id"])' "${JOB_RESULT}")"
python3 - "${JOB_RESULT}" "${COMMIT}" <<'PY'
import json, sys
record = json.load(open(sys.argv[1]))
assert record["state"] == "SUCCEEDED"
assert record["requested_commit"] == record["executed_commit"] == sys.argv[2]
assert record["exit_code"] == 0 and record["outputs"][0]["persisted"]
PY
infra job logs "${JOB_ID}"
infra job status "${JOB_ID}" --json
infra storage verify "${OUTPUT_KEY}"
OUT_FILE="$(mktemp /tmp/wavcse-phase6-output.XXXXXX.json)"
infra storage presign-download "${OUTPUT_KEY}" | python3 -c 'import sys,urllib.request;open(sys.argv[1],"wb").write(urllib.request.urlopen(sys.stdin.readline().strip()).read())' "${OUT_FILE}"
python3 - "${OUT_FILE}" "${COMMIT}" "${INPUT_SHA}" "${INPUT_SIZE}" "${JOB_ID}" "${WORKER_ID}" "${JOB_RESULT}" <<'PY'
import hashlib, json, sys
output, commit, input_sha, input_size, job_id, worker_id, result = sys.argv[1:8]
data = json.load(open(output))
assert data == {"bytes": int(input_size), "sha256": input_sha,
                "job": job_id, "worker": worker_id, "commit": commit}
assert hashlib.sha256(open(output, "rb").read()).hexdigest() == json.load(open(result))["outputs"][0]["sha256"]
PY
infra worker health "${WORKER_ID}"     # the worker must still be alive
```

Expected: state `SUCCEEDED`, `Executed commit` and the persisted JSON `commit` equal to
`COMMIT`, the log containing
`phase6 probe ok probe.txt`, the output object present in S3, and the worker still
`READY`. The worker is not stopped or destroyed by any of this.

### Operator test 4 — deterministic job failure

Prove that a scientific failure stays a failure:

```bash
FAIL_SPEC="$(mktemp /tmp/wavcse-phase6-fail.XXXXXX.json)"
FAIL_RESULT="$(mktemp /tmp/wavcse-phase6-failure-result.XXXXXX.json)"
FAIL_OUTPUT_KEY="scratch/phase6-failure-$(date +%s%N).json"
python3 - "${COMMIT}" "${FAIL_OUTPUT_KEY}" >"${FAIL_SPEC}" <<'JSON'
import json, sys
print(json.dumps({
    "schema_version": 1,
    "name": "phase6-probe-failure",
    "source": {"repository": "https://github.com/Synergy-io/wavCSE.git", "commit": sys.argv[1]},
    "command": {"argv": ["python3", "-c",
        "import json,os,pathlib,sys;"
        "root=pathlib.Path(os.environ['WAVCSE_JOB_DIRECTORY']);"
        "(root/'outputs/failure.json').write_text(json.dumps({'exit':9}));"
        "print('intentional failure');sys.exit(9)"]},
    "outputs": [{"path": "outputs/failure.json", "artifact": sys.argv[2],
                 "required": False}],
}, indent=2))
JSON

if infra job submit "${FAIL_SPEC}" --worker "${WORKER_ID}" --wait --wait-timeout 600 --json > "${FAIL_RESULT}"; then
  echo "Unexpected success" >&2
  exit 1
fi
FAIL_JOB_ID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["job_id"])' "${FAIL_RESULT}")"
python3 - "${FAIL_RESULT}" <<'PY'
import json, sys
record = json.load(open(sys.argv[1]))
assert record["state"] == "FAILED" and record["exit_code"] == 9
assert record["outputs"][0]["persisted"]
PY
infra job logs "${FAIL_JOB_ID}"
infra job status "${FAIL_JOB_ID}" --json || test "$?" -eq 1
infra storage verify "${FAIL_OUTPUT_KEY}"
FAIL_OUT_FILE="$(mktemp /tmp/wavcse-phase6-failure-output.XXXXXX.json)"
infra storage presign-download "${FAIL_OUTPUT_KEY}" | python3 -c 'import sys,urllib.request;open(sys.argv[1],"wb").write(urllib.request.urlopen(sys.stdin.readline().strip()).read())' "${FAIL_OUT_FILE}"
python3 - "${FAIL_OUT_FILE}" "${FAIL_RESULT}" <<'PY'
import hashlib, json, sys
assert json.load(open(sys.argv[1])) == {"exit": 9}
assert hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest() == json.load(open(sys.argv[2]))["outputs"][0]["sha256"]
PY
infra worker health "${WORKER_ID}"
```

Expected: `State: FAILED`, `Exit code: 9`, the log containing `intentional failure`,
`state_reason` naming the exit code, the optional debug output persisted, submit exiting
1, and the worker still alive and `READY`.

### Job failure handling

- `does not exist` / `is <state>; a recorded job requires a RUNNING worker`: start the
  worker explicitly, then re-check readiness.
- `local readiness is X, not READY`: run `infra worker bootstrap <worker-id>`.
- `these secret environment variables ... are not set`: export them in the submitting
  shell; nothing was recorded.
- `the remote does not contain commit`: push the exact commit to GitHub.
- `required input ... could not be materialized`: verify the Phase 5 key, manifest, and
  declared size/digest; nothing was started.
- `refusing to run a different revision`: stop and inspect; the worker reported a commit
  that is not the one requested.
- `required outputs were not persisted`: the command succeeded but a declared output is
  missing on the worker or could not be uploaded. The object key may already exist from a
  partial attempt; add `"overwrite": true` to that output only after inspecting it.
- `worker ... no longer exists`: the job outcome is unknown and recorded as such. The
  local record and any captured log copy are preserved.
- `Warning: worker ... could not be inspected`: transient; the job may still be running.
  Retry `infra job status` before drawing a conclusion.
- `refusing to signal process ...`: the recorded PID no longer matches this job; nothing
  was killed. Inspect the worker session manually.
- `No such file or directory` for the runner path: the worker filesystem was reset or the
  worker was rebuilt. `infra job submit` installs the reviewed runner, so submit a new job
  after re-bootstrapping (`infra worker bootstrap <worker-id>`); the previous job's outcome
  is unrecoverable and should be recorded as unknown.

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
  `22/tcp`, an SSH-capable image, a registered RunPod account public key, and a public
  IP with a mapped external port. A proxy-only `ssh.runpod.io` endpoint supports an
  interactive PTY but is insufficient for exec, bootstrap, health, or future jobs.
- Community Cloud proxy-only Pod: it was created without the GraphQL public-IP placement
  requirement. Replace it using both `--start-ssh` and `--require-direct-ssh`; do not use
  the basic PTY proxy for automation.
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
- S3 403 on a storage command: `HeadObject` needs `s3:GetObject` on the object and
  listing needs `s3:ListBucket` with the `s3:prefix` condition on the bucket ARN.
- Storage key rejected: pass a key relative to `storage.prefix` without `..`, empty
  segments, a leading separator, whitespace, `?`/`#`, or a repeated prefix.
- Presigned upload refused: an object already exists at that key. Inspect it, then pass
  `--overwrite` only if replacing persisted data is intended.
- Presigned URL expired: the transfer took longer than the URL lifetime. Raise
  `--expires-in` (up to 604800 seconds) and, for a long transfer,
  `ssh.transfer_timeout_seconds` together.
- Artifact download checksum or size mismatch: nothing is materialized at the
  destination and the temporary file is removed. Verify which digest is authoritative
  before retrying; do not accept a mismatch on a persisted artifact.
- Artifact download destination exists: pass `--overwrite` deliberately, or materialize
  to a new path. Existing files are never replaced implicitly.
- Worker transfer fails immediately with a Python error: run
  `infra worker bootstrap <id>`; the transfer module needs the Python 3 that bootstrap
  guarantees.
- Worker transfer fails during a large download: confirm the worker has free disk
  space, then raise `--expires-in` and `ssh.transfer_timeout_seconds` if the transfer
  itself was cut short.
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

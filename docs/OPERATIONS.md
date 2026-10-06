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

The optional `[volumes]` section sets where a Pod mounts a RunPod network volume. That
mount is also the worker's rebuildable artifact cache root; canonical artifacts stay in
S3.

```toml
[volumes]
mount_path = "/workspace/cache"
```

`mount_path` must be an absolute container path with no `.` or `..` segments. When a
network volume is attached, `infra worker create --volume-mount-path` defaults to this
value; without a volume the default remains `/workspace`.

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
| `WAVCSE_INFRA_VOLUMES_MOUNT_PATH` | Where a Pod mounts a network volume; the rebuildable cache root |
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
| AWS authentication | Temporary role credentials via Boto3's chain: an EC2 instance profile, or a role-assuming `credential_process` on a non-EC2 controller |

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

1. Launch a supported Ubuntu controller. The reference deployment is an EC2 instance;
   any host that can satisfy the credential requirement below is acceptable.
2. Give the controller temporary, least-privilege AWS role credentials for the private
   artifact bucket/prefix and the configured RunPod SSM parameter. Attach an instance
   profile on EC2; on another host configure a role-assuming `credential_process` (for
   example the IAM Roles Anywhere signing helper). Do not create local static AWS
   credentials.
3. Configure controller SSH access and host security through normal AWS operations.
4. Apply `controller/cloud-init.yaml` as user data, or run:

   ```bash
   git clone https://github.com/Ke-vin-S/wavcse-infra.git
   cd wavcse-infra
   ./controller/bootstrap.sh
   nano ~/.config/wavcse-infra/config.toml
   ```

   Bootstrap creates the user configuration if missing and preserves it on every later
   run. It also installs the mirrored application configuration from `apps/`
   (`~/.tmux.conf` from `apps/tmux/tmux.conf`), preserving a differing existing file.
   It installs controller agent tools by default; use `--skip-agents` only when they
   are managed separately.
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

## Application configuration

`apps/manifest` is the source of truth for the operator's application settings, and
each entry names a file under `apps/<app>/` plus its home-relative destination. The
controller applies it with `controller/app-config.sh`, which bootstrap runs with
`--preserve-existing`; workers get the same files through `infra worker bootstrap`
and `infra worker apply-config <id>`.

To change a setting, edit the file in this repository, commit and push it, then apply
it on the controller:

```bash
make app-config-check   # report drift without writing; exits 1 when anything drifted
make app-config         # install an absent file, leave a matching one alone
```

A file whose content differs from the repository is never overwritten implicitly: a
default run prompts, a run without a TTY reports `preserved`, and `--yes` replaces it
after copying the previous file to a `.wavcse-backup-<UTC timestamp>` sibling. Use
`--app NAME` to limit the run to one application. On a worker the same rules apply
through `infra worker apply-config <id> [--yes]`.

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

## Google Colab ephemeral execution

`controller/bootstrap.sh` installs pinned `google-colab-cli==0.7.4` without
authenticating or allocating. A human performs one-time ADC login on controller:

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
```

Set `[colab] enabled = true`, choose CU ceilings in the user-owned config,
then inspect `infra doctor`, `infra provider list` and
`infra worker list --provider colab`. A zero paid CU balance is a healthy free
tier when `allow_free_tier = true`, not a failed account; only
`allow_free_tier = false` with an insufficient paid balance fails. No read-only
check allocates compute. The CU limits also have environment overrides:
`WAVCSE_INFRA_COLAB_ALLOW_FREE_TIER`, `WAVCSE_INFRA_COLAB_MINIMUM_BALANCE_CU`,
`WAVCSE_INFRA_COLAB_MAX_INCREMENTAL_RATE_CU_PER_HOUR`,
`WAVCSE_INFRA_COLAB_MAX_JOB_CU` and existing CLI/timeout overrides. A missing
or expired ADC requires the human login above; do not automate it.

```bash
infra worker create --provider colab --gpu T4
infra worker show <exact-infra-owned-session>
infra worker health <exact-infra-owned-session>
infra worker reconcile <exact-intent-id>
infra job submit <job-spec.json> --worker <exact-infra-owned-session> --wait
infra job status <job-id>
infra worker destroy <exact-infra-owned-session>
infra worker list --provider colab
```

Creation prints billing mode (paid CU or free tier), paid CU balance, observed
usage rate and assignment count before confirmation; then claims a unique
identity, allocates once, reads usage again, checks physical GPU/CUDA,
bootstraps and marks READY. The post-allocation rate ceiling and minimum-balance
checks apply only in `PAID_CU`; in `FREE_TIER` the observed rate is recorded as
metering evidence and the job is still gated on ownership, readiness, exactly
one owned active assignment and the accelerator. A rejected confirmed lease is
released immediately. A small amount of CU may be charged before a paid-mode
rejection; inspect usage after release. An ambiguous intent must be reconciled,
never blindly retried. One active infra-owned lease at a time; release it after
a bounded compatible job batch. An intent whose session never appeared is
retired only by `infra worker reconcile <exact-intent-id>`, which reads the
provider and changes local bookkeeping only; nothing else retires it, and that
recovery refuses a young intent, a failed read, an identity the provider still
lists, or observations that disagree. See [Colab](COLAB.md) §"An abandoned
allocation intent".
`infra job submit <spec>` prefers an existing READY Colab worker over RunPod;
`--provider runpod` restricts placement and `--worker` pins the exact worker.
There is no implicit paid provisioning on submit or provider retry of a failed
experiment. `infra storage upload` can publish a checkpoint from an existing
Colab worker during a long job; coordinate the producer and checkpoint path in
wavCSE. The worker's `/content` is never durable. Colab stop/start, SSH and
network-volume operations remain unsupported. See [Colab](COLAB.md) for
history permissions, capabilities, recovery and retention.

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

Created RunPod network volumes are tracked separately in:

```text
~/.local/state/wavcse-infra/volumes.json
```

That document is schema version 1, written atomically with a same-directory rename under a
local advisory lock, mode 0700 for the directory and 0600 for the file, and contains no
credentials. It is keyed by the generated infra volume identity rather than the provider
ID, because the identity is known before the paid create while the provider ID is only
known after a response. Each record holds the provider, the infra identity, the provider
volume ID (nullable), the name, requested and observed size/data center/tier, creation and
last-observed timestamps, a `lifecycle_state` in {`PENDING_CREATE`, `AVAILABLE`,
`DESTROYED`}, and a `provider_absent` flag.

`PENDING_CREATE` is written before the billable create request is issued. That is what
makes an ambiguous create recoverable: if the response is lost and bounded exact-name
reconciliation finds nothing, the intent survives so a later `infra volume list` can match
the provider's own listing against it. `infra volume list` warns on stderr about any
`PENDING_CREATE` intent with no matching provider volume: RunPod may have created it, so
inspect the provider console before creating anything with that identity.

Worker state gained one field, `network_volume_mount_path`: the provider-reported mount
path of the Pod's network volume, `None` when the Pod has no volume. It is the cache root
a worker uses. Each materialized job input also gained a `source` field, either
`canonical` or `cache`; `infra job status` prints it as `from cache` / `from canonical`.

If create loses its response, the CLI checks for the exact generated infra name. It
adopts one exact match, reports multiple matches, or fails safely after bounded checks.
It never retries the paid create POST. On the uncertain/no-match result, run:

```bash
infra worker list
```

Inspect the generated identity shown in the error before issuing another create.

A Colab allocation intent is written the same way and for the same reason, so it also
survives a lost response. It differs in one respect that matters here: `worker list` does
**not** retire a pending intent it cannot see at the provider, because absence from one
listing is not proof that the allocation never happened, and a retired intent would allow
a second paid session. Such an intent therefore blocks every later Colab allocation until
it is reconciled deliberately with `infra worker reconcile <exact-intent-id>`, which
requires repeated successful listings that omit the exact identity and agree with the
account's assignment count, and an intent at least an hour old. See
[Colab](COLAB.md) §"An abandoned allocation intent".

## Network volume operations

A RunPod network volume is a rebuildable working cache for a Secure Cloud Pod, never
canonical storage: S3 stays canonical, so losing a network volume must never lose the only
copy of a canonical artifact. A volume exists in exactly one data center, attaches only to
Secure Cloud Pods, and must be attached at Pod creation; it cannot be attached or detached
later, and RunPod replaces the Pod's default volume disk with the network volume. Tiers are
`STANDARD` and `HIGH_PERFORMANCE`; the tier is immutable after creation, size can only be
increased, and the provider minimum is 10 GB with a 4096 GB maximum.

Lifecycle commands:

```bash
infra volume list [--json]
infra volume show <volume-id> [--json]
infra volume datacenters [--json]
infra volume create --data-center <exact-dc-id> --size <gb> [--tier standard|high_performance] [--name <prefix>] [--yes]
infra volume destroy <volume-id> [--wait-timeout <seconds>] [--yes]
infra volume forget <infra-identity> [--yes]
```

`infra volume list` and `infra volume show` call only documented GET endpoints and do not
change provider state. `infra volume datacenters` lists catalog data centers with their
supported volume types; an empty `networkVolumeTypes` means the data center cannot host a
network volume at all, which is what makes it a placement guard.

`infra volume create` prints the infra identity, data center, size, storage tier (or "data
center default"), the data center's supported tiers, RunPod's published list price when one
applies, an estimated monthly cost, and the exact JSON create request body, then requires
interactive confirmation unless `--yes` is supplied. The API exposes no network volume
price field: the CLI labels any figure as a published list price, not a provider-reported
charge. RunPod's published list price for STANDARD storage is $0.07/GB/month, quoted for
volumes up to 1 TB, documented at https://docs.runpod.io/pods/pricing; the provider
publishes a different rate for larger volumes of the same tier but does not state its
banding unambiguously, so a request larger than 1 TB prints no estimate rather than one
computed from a rate whose scope is unverified. HIGH_PERFORMANCE is documented
only as a premium to standard storage whose exact per-GB rate varies by data center and
appears only in RunPod's console, so this tool quotes no estimate for it. `infra volume
show` reports the account's actual provider-billed storage instead, fetched from
`/billing/network-volumes`.

### Placement and the Pod ordering constraint

A network volume must be created before the Pod that mounts it, and the Pod must be placed
in the volume's data center. `infra worker create --network-volume-id <volume-id>`:

- forces Secure Cloud; `--cloud community` with a network volume is rejected before the
  billable create request (after the read-only lookups that resolve the volume);
- constrains placement to the volume's data center; an explicit `--data-center` naming
  anything else is rejected in the same place;
- fails before the paid create when the requested GPU has no confirmed availability in the
  volume's data center, naming the volume and its data center;
- verifies the provider's own answer after creation: a Pod reported in another data center,
  or reported without the requested network volume mount, raises a placement error naming
  the created (billing) Pod and how to remove it.

The printed creation plan shows the volume's data center, marked "placement constrained",
and its size, and notes that storage charges are billed separately from GPU compute and
continue while the volume and Pod exist. `--volume-mount-path` defaults to the configured
`volumes.mount_path` when a network volume is attached and keeps `/workspace` otherwise.

### Ambiguous create recovery

Create and delete are never retried. A `PENDING_CREATE` intent is written before the
billable create request is issued. If the response is lost, the client reconciles by
listing volumes and matching the exact infra identity: one match is adopted, several are
reported as ambiguous, and none fails safely and tells the operator to run
`infra volume list`. On that result, run:

```bash
infra volume list
```

and inspect the provider's own listing against the pending identity before issuing another
create.

While any unresolved intent exists, `infra volume create` refuses to plan another volume
and names the identities involved, because a retry after silence is the one way this
command could create a duplicate billable resource. `infra volume list` clears the refusal
by adopting the volume it finds. When the provider really has no such volume, clear it
explicitly:

```bash
infra volume forget <infra-identity>
```

`forget` removes local bookkeeping only: no provider resource is changed or deleted, and a
provider volume that does exist still appears in `infra volume list`, only without a local
create record. A create the provider definitively refuses never leaves an intent behind,
because a refusal is an answer: nothing was created.

Destroying the volume also clears its record, including an intent that was never adopted,
because the destroy path links the volume it is about to delete to the record that names
it.

### Destroying a volume

`infra volume destroy` addresses a volume by exact provider ID only; passing a name yields
"already absent" plus a note that this command never resolves names. It prints the id,
name, data center, size, tier, tracked life cycle, any tracked Pods that mount it (stating
they are not destroyed), and that the rebuildable cache is permanently lost while
canonical S3 objects are untouched. It requires confirmation unless `--yes` is supplied.
Destroying a volume never touches a Pod or an S3 object, and destroying a Pod never touches
a volume. An ambiguous delete is reconciled by bounded polling of `GET
/network-volumes/{id}` until the volume is absent.

### Rebuildable artifact cache

The mount point of the attached network volume is the cache root:

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
content is a regular non-symlink file whose actual size and actual SHA-256 match. Anything
else is a miss or an explicit integrity failure; corrupt bytes are never accepted because
they came from the mount. An entry that contradicts its recorded identity is moved into
`staging/quarantine-<digest>-<random>`, reported, and treated as a miss, so the next
canonical download rebuilds it. Quarantine keeps the failing bytes available for diagnosis
instead of deleting them, and `staging/` is documented as safe to delete.

Population copies the artifact into `staging/`, verifies it while copying, writes the
metadata document, fsyncs, and then publishes the whole directory with a single `rename`,
so a partially written artifact can never appear at an entry path. Two concurrent writers
stage separately and one `rename` wins; the loser verifies the winner's entry and reports
it rather than replacing anything. Materialization copies a verified entry into a partial
staging file while hashing it, then places it through the same inode-anchored hard-link
path a canonical download uses, so a hit carries exactly the integrity guarantee of a fresh
download. `--overwrite` is honored and no staging file is left behind.

Cache use is enabled only when the worker has a network-volume mount path recorded and the
declared input has a SHA-256 (declared directly or recorded in its manifest). An input
identified only by size is downloaded from canonical storage directly, because a
content-addressed cache cannot answer a question about an unidentified artifact. On a miss,
a quarantined entry, an unusable cache root, or an interrupted lookup, the canonical
presigned download proceeds and the verified result is then offered to the cache. Every
cache problem degrades to a warning, so the cache can only make a job faster, never make it
fail; a definitive protocol violation the worker reports still raises. The cache never
receives a presigned URL: bytes only enter it from a file the canonical download already
verified, and no URL, credential, or other bearer material is ever written to the mounted
volume.

Cache operations live in the same reviewed, stdlib-only `worker_transfer.py` program that
is streamed to the worker over direct SSH stdin; there is no separate worker install,
daemon, or package.

### Cache inspection and cleanup

```bash
infra volume cache stats --worker <exact-worker-id> [--wait-timeout <s>] [--command-timeout <s>] [--json]
```

`infra volume cache stats` reports the cache root, the number of entries, recorded cached
bytes, staged bytes, entries with unusable metadata, and the marker's schema version. Sizes
come from each entry's metadata document rather than from re-reading artifacts, so
inspecting a full volume stays cheap; every entry is hashed whenever it is actually used.

No automatic eviction or LRU is implemented. Cleanup is operator-managed: remove a specific
digest directory or everything under `staging/` through
`infra worker exec <worker-id> -- rm -rf <path>`.

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
infra storage read scratch/run-0001/job-ab12/MANIFEST.json --json
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

`infra storage read` is the read-only counterpart for small evidence documents: it
returns one object's bytes (bounded by `--max-bytes`, 16 MiB ceiling) and, when
`--expected-sha256` is given, only after the bytes it read match that digest. It exists so
a caller can inspect the *content* of a stored result — a job manifest, a metrics text
file — against a digest the caller already recorded, instead of trusting a worker's report
about what it wrote. It makes no claim beyond the one bounded read: it is not a substitute
for `verify`, and it never writes, presigns, or replaces anything.

### Large download transport

`infra storage download` and Phase 6 input materialization share one worker-side
downloader with two transports:

| Condition | Transport |
| --- | --- |
| expected size unknown, or below 64 MiB | one sequential connection |
| expected size known and at or above 64 MiB | parallel inclusive HTTP byte ranges |

Parallel downloads use 8 concurrent range requests by default and a 16 MiB range size, so
a 20 GiB artifact is about 1280 ranges. There is no worker-side tuning file: the worker
uses its default unless the controller passes `--concurrency`, whose maximum is 16. Phase
6 uses the default, so recorded jobs keep the same command line. Only a bounded number of
ranges are ever scheduled at once, so an enormous expected size does not allocate
proportional work items.

Resumability requires the expected SHA-256. A digest is the only immutable identity the
worker can verify, so:

| Condition | Transport | Resumable |
| --- | --- | --- |
| known size ≥ threshold, expected SHA-256 supplied | parallel ranges | yes |
| known size ≥ threshold, no expected SHA-256 | parallel ranges | no |
| size unknown or below threshold | one sequential connection | no |

A non-resumable ranged download still runs in parallel, but into a one-shot staging file
with no persisted state, and it discards any leftover resumable state for that destination
first. Its bytes are only checked against the announced size. Phase 6 supplies a digest
whenever the input declares a manifest or a SHA-256, so declared inputs keep resumability;
add `--expected-sha256` to a direct `infra storage download` to get it too.

Each range is written at its own offset in the staging file, and for a resumable transfer
`<destination>.wavcse-partial.json` records the durable range indices together with the
destination, expected size, expected SHA-256, and range size. A rerun for the same
destination, size, and digest skips the recorded ranges even though the controller issues
a new presigned URL. A record whose digest is absent or does not match, who cannot be
parsed, or that belongs to an artifact that now uses the single-connection path is
discarded rather than trusted.

One destination has one transfer at a time, whatever transport it uses. A fixed
`<destination>.wavcse-transfer.lock` is locked for the whole operation — checking the
destination, staging, downloading, verifying, placing, and cleaning up — and is never
deleted, so it survives the staging file being released at completion. Every download
takes it, including a small, size-unknown, or digest-less one, so none of them can bypass
serialization against a ranged transfer to the same path. A second transfer for the same
destination fails with "is already in progress"; it cannot race the first one's completion,
and it does not touch the first one's state. The lock is an advisory `flock`, so it is
released automatically if the transfer process dies; an empty lock file (0 bytes) left
beside the destination is normal and harmless.

The same module answers a read-only `verify` question for one destination
(`python3 - verify --destination <path> [--expected-size N] [--expected-sha256 HEX]`): it
reports the size and SHA-256 of an artifact that is already placed, fails when nothing
complete is there, reports whether another transfer currently holds the lock, and never
downloads or writes anything. A controller whose bounded command stopped waiting uses it
to decide, from worker evidence, whether the transfer finished in the background, is still
running, or died. It carries no presigned URL at all.

Placement creates the destination from the verified staging inode rather than by moving a
pathname. That also means `--overwrite` is a two-step replace — the previous entry is
removed, then the destination is created from the verified inode — so the destination name
is briefly absent, a crash in between leaves the resumable state usable rather than
wedged, and a writer that takes the name first is never overwritten (the transfer fails
with "appeared during the download").

Retries are bounded per range (four attempts, then failure). Connection resets, timeouts,
temporary 5xx responses, truncated bodies, and incomplete reads while a body is streaming
are all retried; an expired URL (HTTP 403), a malformed or mismatched `Content-Range`, an
announced-size mismatch, or an extra byte beyond the requested range fails immediately with
an actionable message. If the endpoint answers a range request with HTTP 200, the
downloader switches to one sequential full-object transfer instead of concatenating full
bodies.

Peak disk use for a ranged download is approximately the artifact size plus the small
range record: ranges are written in place and the verified file is hard-linked or renamed
into position, so the object is neither buffered in memory nor copied twice.

State left behind depends on why the transfer stopped:

| Outcome | Left on disk |
| --- | --- |
| success | only the artifact (and the empty lock file) |
| transient failure of a resumable transfer | `... .wavcse-partial` + `.json` record, kept for the next run |
| crash before or after placement | a staging name that only hard-links the placed artifact is released, and the record is removed, on the next attempt |
| size or digest failure, or a reset | staging files removed |
| transient failure of a non-resumable transfer | nothing |

To abandon retained state deliberately, remove the `... .wavcse-partial` file and its
`.json` record while no transfer is running. A `... .wavcse-stage-*` file next to a
destination is only crash residue from an earlier build; it is released automatically when
it aliases that destination's staging inode, and it can be deleted by hand otherwise. Removing either one alone forces a restart,
because the record is discarded when it does not describe the data file. A staging name
that merely hard-links an already-placed artifact — the residue of a crash between
placement and cleanup — is detected under the lock and removed automatically on the next
attempt; it can be deleted manually as well, and it never holds the only copy of anything.

Whole-object SHA-256 remains authoritative. Completing every range is not evidence of
artifact integrity, and nothing is placed at the destination until the assembled file
matches the expected size and, when supplied, the digest.

#### Controlled large-download benchmark

The parallel transport was motivated by one measured worker-to-`ap-south-1` path, not by a
service-level target. Measure the path in front of you before treating any figure as
expected.

1. Pick an existing artifact with a recorded manifest and confirm it with
   `infra storage verify <key> --manifest <key>.manifest.json`.
2. Use an existing `READY` worker. Do not create one for a benchmark.
3. Baseline: `infra storage download <key> /workspace/bench-a.tar --worker <id>
   --expected-size <bytes> --expected-sha256 <hex> --concurrency 1`.
4. Default: the same command writing `/workspace/bench-b.tar` without `--concurrency`.
5. Compare wall-clock time and confirm both runs report identical size and digest.
6. Remove the benchmark files when done. One sample is not a guarantee; repeat before
   drawing a conclusion.

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
#    A job that is still PREPARING is driven forward from worker evidence, so status may
#    resume an interrupted input materialization and start a command that never started.
infra job status <job-id>
infra job logs <job-id> --tail-bytes 65536

# 5. Cancel a run you no longer want. This kills only the job process tree.
infra job cancel <job-id>
```

`infra job status` exits 1 when the job is `FAILED`, and 0 for `RUNNING`, `PREPARING`,
`SUCCEEDED`, or `CANCELLED`. `infra job submit` exits 1 when the job is `FAILED`,
`CANCELLED`, or still `PREPARING` with `reconciliation_required` set. `--json` renders the
complete durable record.

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

`PREPARING` is a real, resumable phase rather than a waypoint. A bounded controller wait, a
dropped SSH session, or a worker phase that may still be running records
`reconciliation_required` with the preparation phase (`installing_runner`,
`preparing_source`, `materializing_inputs`, `starting_command`), the interruption time, and
no `failure_reason`. `infra job status` then completes the attempt from worker evidence:

- an input whose download finished after the controller stopped watching is verified
  (size, then SHA-256) and reused, never re-downloaded;
- a download that is still running is reported, and a second one is never started for it;
- a download that died is resumed from the ranges already recorded on the worker;
- a command launch that is still starting, or whose identity cannot be verified, keeps the
  job reconcilable instead of guessing;
- a command that may already have been launched is never launched again.

`FAILED` therefore always means the system has evidence: a reported command failure, a
recorded process that is gone with no outcome, a workspace that is absent after the
controller had issued preparation steps for it, or a launch that provably never recorded a
process (and which the runner can never run the command without).

The same rule covers every other observation the controller makes:

- **Stopped or restarting worker.** A provider stop says the worker is unreachable, not that
  the command failed. The job keeps its state and `reconciliation_required`, and the next
  `infra job status` after the worker is `RUNNING` again reads the outcome the command
  already recorded on its disk. Only `TERMINATING`, `DESTROYED`, and `ERROR` — states in
  which the workspace is gone for good — end the job, and their reason says the outcome can
  no longer be read rather than claiming the command failed.
- **Input materialization.** Every declared input is verified from worker evidence (size,
  then SHA-256) immediately before the command may start, except one established by a
  transfer in that same pass, which the worker verified end to end as it placed it. An
  input recorded as materialized by an earlier attempt is re-verified rather than trusted:
  a valid one is not transferred again, a missing or partial one is materialized again, and
  one that no longer matches its declared size or digest is replaced with a verified copy.
- **Required outputs.** An upload whose outcome the controller lost is not a persistence
  failure. The job stays reconcilable and the next pass either retries the upload or, when
  the object is already at the declared key, reads it back from canonical storage and
  accepts it only when the stored bytes match the worker-side file's size and SHA-256. Its
  existence, its size, and its age are never sufficient: only its bytes are.
- **Declared output structure.** The durable record always keeps exactly one slot per
  declared output, in declaration order, so an interruption part-way through a multi-output
  job leaves the structure intact: the outputs already reconciled stay recorded and only
  the unresolved one is retried. A controller restart re-reads that record rather than
  rebuilding it.
- **Terminal means nothing is left running.** The supervisor terminates the process groups
  it recorded before it writes an outcome, and refuses to write one while any of them is
  still alive. Such a job stays nonterminal and its live group is reported, so a leftover
  process from an unkillable run is visible instead of being hidden behind a finished
  record.
- **Transient controller-side failures.** A throttled or failed canonical-storage
  observation, an interrupted transfer, an attempt that exhausted its bounded retries, and
  a worker that never became reachable are all reported as uncertainty. The worker's own
  retries stay bounded; the recovery opportunity is the next job-level attempt, which
  signs fresh credentials and resumes from the ranges already recorded on the worker.

`infra job cancel` is the explicit way to end a job that must not continue.

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
└── state/            pid, finished, cancelled, started, and non-secret descriptor copies
```

`state/start.lock` records the identity (PID, start ticks, timestamp) that owns the job's
single launch slot, so a controller can prove later whether a launch is still running,
completed, or died before recording a process. `state/prepare.lock` is an advisory lock
that serializes the checkout phase and is released by the kernel if its process dies.

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
- `State: PREPARING` with `Reconciliation required: yes`: the controller stopped watching a
  worker phase that may still be running. Run `infra job status <job-id>` to reconcile it;
  `infra job cancel <job-id>` ends it deliberately. Do not submit the work again: a new
  submission is a new job ID and would duplicate the transfer or the run.
- `stays RUNNING because the outcome it recorded on the worker cannot be read`: the worker is
  stopped or restarting. Its disk, and any outcome the command already wrote there, survive
  that; start the worker explicitly and run `infra job status` again.
- `the command launch for job ... is in progress ... and owns the job`: the launch won the
  race against cancellation. Nothing was cancelled; retry `infra job cancel` to terminate
  the process the launch is creating.
- `did not establish the cancellation ... nothing was recorded`: the worker could not
  confirm the cancellation, so no terminal state was invented. Run `infra job status` and
  retry once the worker answers.
- `required output persistence could not be confirmed`: the command finished but its upload
  outcome is unknown. Run `infra job status` again; the canonical object is checked before
  anything is concluded.
- `Could not read metadata for ... this is a service observation failure`: the controller
  could not observe canonical storage. Nothing about the artifact is concluded; retry.
- `the job workspace ... is absent ... after this controller had already issued
  preparation steps for it`: the workspace was removed or the worker was rebuilt after
  preparation had been issued. The command is never re-run implicitly, because it may
  already have executed; inspect the worker and submit a new job if the run must be
  repeated.
- `the command launch ... did not complete: the runner recorded no process`: the launch was
  interrupted before the runner recorded a supervisor. The supervisor refuses to execute a
  command it was never named in, so nothing ran; submit a new job.
- `recorded a job process ... that is no longer running, and recorded no outcome`: the
  process vanished (external kill or worker restart) without writing `finished.json`. The
  command result cannot be verified; inspect `logs/job.log` on the worker.
- `refusing to signal process ...`: the recorded PID no longer matches this job; nothing
  was killed. Inspect the worker session manually.
- `No such file or directory` for the runner path: the worker root filesystem was reset.
  Re-run `infra worker bootstrap <worker-id>`, then `infra job status <job-id>`: the next
  preparation pass reinstalls the reviewed runner. If the job workspace is gone as well,
  status reports the outcome as unknown instead of re-running a command that may have
  executed.

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
- AWS identity failure: verify the controller resolves temporary role credentials. On EC2
  check that an instance profile is attached and IMDS access is not blocked; on another
  host check that the configured `credential_process` returns an assumed-role session.
  Do not work around it by creating permanent access keys.
- S3 failure: verify region, bucket, prefix, and role policy separately.
- S3 403 on a storage command: `HeadObject` needs `s3:GetObject` on the object and
  listing needs `s3:ListBucket` with the `s3:prefix` condition on the bucket ARN.
- Storage key rejected: pass a key relative to `storage.prefix` without `..`, empty
  segments, a leading separator, whitespace, `?`/`#`, or a repeated prefix.
- Presigned upload refused: an object already exists at that key. Inspect it, then pass
  `--overwrite` only if replacing persisted data is intended.
- Presigned URL expired: the transfer took longer than the URL lifetime. Readiness is
  established before a URL is signed, and one bounded attempt is never allowed to outlive
  its URL, so this means a single range request was issued too close to expiry. Retry the
  transfer: a new URL is presigned and the recorded ranges are resumed.
- `too soon to sign a usable transfer URL`: the controller's own temporary credentials are
  about to expire, so no URL could be honoured. Renew them (an attached instance profile
  refreshes automatically) and retry; this is not a statement about the artifact.
- `transient transfer failure; resumable state was kept`: a bounded attempt ran out of
  retries. The partial artifact is kept and the next attempt resumes from it. Run
  `infra job status`, which signs a fresh URL.
- Artifact download checksum or size mismatch: nothing is materialized at the
  destination and the temporary file is removed. Verify which digest is authoritative
  before retrying; do not accept a mismatch on a persisted artifact.
- Artifact download destination exists: pass `--overwrite` deliberately, or materialize
  to a new path. Existing files are never replaced implicitly.
- Worker transfer fails immediately with a Python error: run
  `infra worker bootstrap <id>`; the transfer module needs the Python 3 that bootstrap
  guarantees.
- Worker transfer fails during a large download: confirm the worker has free disk
  space. A large artifact does not need a longer URL lifetime or a longer command bound:
  a bounded attempt that is cut short is retried with a fresh URL and continues from the
  ranges already recorded beside the destination.
- A job whose input materialization was interrupted by the controller keeps its state and
  is completed by `infra job status`, which verifies or resumes the transfer instead of
  repeating it.
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
- `controller/omp-overlay.sh` (also `make omp-overlay`) writes the machine-local OMP
  overlay that registers this checkout's `.agents/skills` directory for agent sessions
  on the controller, so research work rooted in the wavCSE checkout can read the
  operator, GPU and artifact skills without copying them or changing directory. It is
  generated from the real checkout path — never committed — and it only exports
  `PI_CONFIG_FILES` when that variable is unset. Bootstrap runs it best-effort and
  reports the follow-up command when the wavCSE checkout does not exist yet; re-run it
  after cloning or moving that checkout. `make omp-overlay-check` verifies it.

The cloud-init file assumes the default Ubuntu account and the public canonical
repository URL. Customize those two non-secret values in an EC2 launch template when
necessary. It intentionally does not update an existing checkout, preventing first-boot
automation from overwriting controller work.

## Autonomous callers

An automated caller (the wavCSE research orchestrator) may drive this control plane
through the CLI alone. It may rely on exactly this much:

- **It creates and owns its resources.** It passes its scope as the human half of the
  worker name, so a scope-prefixed name is the ownership record, and it records the
  intent before every billable create so a lost response is reconciled by generated
  identity rather than repeated.
- **It supplies its own ceilings.** `--yes` is used because it is non-interactive, and
  `--max-price` always carries its authorization's hourly ceiling; `--yes` never
  bypasses validation or the price guard.
- **It declares inputs and outputs.** Digests are always supplied, so materialization
  is verified and the rebuildable cache can answer for them.
- **It reconciles before retrying.** A job whose acknowledgement was lost is found by
  its deterministic name and spec, never resubmitted.
- **It is the reaper.** Nothing here expires, reaps or cleans up: the caller stops and
  destroys what it created, and a network volume is never a cleanup step for compute.
  A caller that stops running leaves paid resources behind, so the caller installs its
  own persistent enforcement rather than relying on a session. In wavCSE that is
  `improvements.compute reap`, run by a controller-local systemd timer:

  ```bash
  # once per controller: render, install, then deliberately enable
  uv run python -m improvements.compute reap-install            # prints the units
  uv run python -m improvements.compute reap-install --install  # writes them, enables nothing
  uv run python -m improvements.compute reap-install --install --enable

  # what the timer runs, and what an operator can run by hand
  uv run python -m improvements.compute reap            # dry run, every known scope
  uv run python -m improvements.compute reap --execute  # stops what is past its deadline
  ```

  A bare cron entry still works for a single scope, and is the minimum an operator with no
  systemd can do:

  ```bash
  # every 15 minutes: end compute whose deadline has passed, never destroy
  */15 * * * * cd ~/projects/wavCSE && uv run python -m improvements.compute sweep --scope <SCOPE> --execute >> ~/.local/state/wavcse-research/sweep.log 2>&1
  ```

- **It never expects research knowledge here.** Scopes, envelopes and scientific
  policy live in the wavCSE checkout; this control plane only executes what it is
  asked to execute, and refuses what it cannot verify.

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

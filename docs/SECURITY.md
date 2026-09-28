# Security

## Trust boundaries

The controller, GitHub source, private S3 bucket, and MLflow/DagsHub service are trusted
for their documented roles. A RunPod worker is a temporary execution environment and is
not trusted with durable credentials or the only copy of important data.

## Credentials

### Configuration files

`config/infra.example.toml` and `~/.config/wavcse-infra/config.toml` contain only
non-secret configuration. Bootstrap copies the example once with user-only permissions
and refuses to replace an existing file. `.env.example` lists supported variables but
contains no values and is not loaded automatically. `runpod.api_key_parameter` is an
SSM parameter name, not a credential. Real tokens remain in the process environment or
SSM Parameter Store and are never committed.

### AWS

The controller uses an attached EC2 instance profile. Boto3 discovers and refreshes the
temporary role credentials through its standard credential chain; the project does not
accept or install static AWS access keys. `infra doctor` requires Boto3's resolved
credential method to be `iam-role` before making STS or S3 calls, so an accidentally
exported static key is reported rather than used for the diagnostic.

The role should grant only required actions for the configured bucket and `wavcse/`
prefix. Diagnostics use STS `GetCallerIdentity` and, when a bucket is configured, a
bounded S3 prefix listing. Storage operations use narrow `s3:ListBucket` (with an
`s3:prefix` condition), `s3:GetObject`, and `s3:PutObject` permissions; `HeadObject` is
covered by `s3:GetObject`. See [Presigned URLs and worker transfer](#presigned-urls-and-worker-transfer)
for the policy example and the worker credential model.

For RunPod credential resolution, grant `ssm:GetParameter` only on the configured
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

If the `SecureString` uses a customer-managed KMS key, the controller role also needs
`kms:Decrypt` on that key ARN. Do not grant account-wide SSM, KMS, or administrator
access. The application relies on Boto3's normal credential chain; on EC2 the attached
instance profile supplies and refreshes temporary AWS credentials.

### RunPod

Store the persistent controller key as an SSM Parameter Store `SecureString` and put
only its name in `runpod.api_key_parameter`. The resolver calls `GetParameter` with
`WithDecryption=True` once when constructing a RunPod client. A non-empty
`RUNPOD_API_KEY` environment variable takes precedence for local or temporary use. The
key is rejected from TOML and never accepted as a CLI option, avoiding committed secrets
and process-list exposure. Resolved values are not persisted in configuration or local
state. HTTP authorization headers are never rendered. Provider and SSM failures expose
only safe operation/status context; user-facing errors pass through redaction.

Worker management uses REST API v2. Safe GET operations may retry; paid creation is sent once and
is never automatically repeated. A lost create response is reconciled only against the
complete generated infra identity. Start, stop, and destroy accept exact provider IDs;
destroy never resolves names or prefixes. `--yes` skips the human confirmation only and
does not disable cost, capacity, identity, or request validation.

### Local operational state

Created-worker metadata lives in `~/.local/state/wavcse-infra/workers.json`. The
directory is mode `0700`, the JSON file is mode `0600`, and writes use a temporary file
in the same directory followed by `fsync` and atomic replacement. The file may contain
provider IDs, generated names, requested/observed GPU configuration, catalog or observed
price, timestamps, lifecycle/readiness state, provider-reported SSH endpoint
coordinates, bootstrap version, non-secret health timestamps, disk capacity, and GPU
model/driver facts.

It must never contain the RunPod key, authorization headers, SSM values, AWS
credentials, private keys, or complete environment data. RunPod remains authoritative;
the local file is not permission to delete a different or similarly named Pod.

### Recorded jobs

Job records and bounded log copies live beneath `~/.local/state/wavcse-infra/jobs/`. A
record contains the normalized non-secret specification, the requested and independently
verified executed commit, worker/GPU provenance, declared input and output outcomes,
timestamps, exit code, and failure reason. It never contains the RunPod token, AWS
credentials, private keys, presigned URLs, or the values of
`runtime.environment_secrets`.

Job tracking credentials are referenced by name only. At submit time the controller
resolves them from its own process environment and sends the values on the SSH stdin
stream inside the descriptor JSON, so they never appear in a process argument list, a
durable record, a log line, or a worker file. Job environment entries may not use the
reserved families `AWS_`, `RUNPOD_`, `WAVCSE_`, `INFRA_`, or `SSH_`, nor any name
containing `PRIVATE_KEY`, so a specification cannot request a controller credential. A
literal environment entry may not look like a credential by name or carry bearer-shaped
content.

The local log copy is produced from the job's own stdout/stderr. It is bounded by
`jobs.log_tail_bytes` and is not scrubbed: a job that prints its own credentials has
printed them. Infrastructure-produced text and errors are redacted as everywhere else.

### SSH

Worker access uses the dedicated private key configured by `ssh.private_key`. Commands
refuse a key readable by group/other users; the private key never leaves the controller.
Only its public counterpart is registered in the RunPod account. `startSsh` causes
RunPod to inject account-registered public keys into compatible images. The CLI never
copies controller GitHub credentials, AWS credentials, the RunPod token, `~/.aws`, or
the SSH private key to a worker.

System OpenSSH is invoked with an argv, `shell=False`, an explicit identity, batch/key-
only authentication, bounded connect/command timeouts, and `-F /dev/null` so user SSH
configuration cannot silently redirect the connection. Basic proxied SSH and the
mapped public-IP endpoint are modeled separately. Automation forces `-T` and requires
the direct endpoint; the proxy is restricted to an explicitly interactive `-tt`
session. Neither is used for artifact transfer.

Host keys use a dedicated `~/.local/state/wavcse-infra/known_hosts` file with mode
`0600`, `StrictHostKeyChecking=accept-new`, and the global known-hosts file disabled for
these worker sessions only. This is deliberate trust on first use: it prevents silent
key changes after the first connection but cannot authenticate the very first endpoint,
so a first-connection network attacker remains a risk. A mismatch fails closed and
requires the operator to inspect the exact Pod endpoint before changing the entry. The
user's normal `~/.ssh/known_hosts` and global SSH configuration are not weakened.

Bootstrap and health scripts contain no credentials. Their small reviewed content is
sent on stdin to `bash -s` over direct SSH; fixed provider-derived values remain
separate positional arguments. The worker stores only a non-secret bootstrap-version
marker. Normal workers do not receive OMP, Codex, AGF, controller authentication
stores, or permanent cloud credentials.

### Controller agent tools

`controller/install-agents.sh` uses only official upstream sources. OMP's installer is
downloaded from the exact pinned `can1357/oh-my-pi` Git tag and its resulting release
binary is checked against a reviewed SHA-256 digest. Codex uses OpenAI's official
standalone installer with an explicit release; that installer verifies the selected
release digest. AGF is downloaded from the pinned `subinium/agf` GitHub release and
checked against its reviewed SHA-256 digest. Installer scripts are saved to temporary
files before execution rather than piped directly into a privileged shell.

These tools run as the controller user, not as root. The script may use `apt` only for
missing download primitives, and never weakens filesystem permissions. Tool
installation does not perform authentication. OMP and Codex credentials remain in
their user-owned upstream stores; AGF needs no account and reads existing local agent
session stores. No agent tool or controller authentication state is installed on a
normal GPU worker.

## Presigned URLs and worker transfer

Workers never receive AWS credentials, `~/.aws` state, a controller SSH private key, the
RunPod token, or GitHub write credentials. Object access is granted one transfer at a
time through a Signature Version 4 URL generated with the controller's instance-profile
role.

Each URL is scoped to:

- one bucket (always the configured `storage.bucket`);
- one exact object key, already resolved beneath `storage.prefix`;
- one operation (`GetObject` or `PutObject`), never a broader grant;
- a finite lifetime, default `storage.presign_expiry_seconds = 3600` and clamped to
  60–604800 seconds.

The URL is a bearer secret: anyone holding it can use it until it expires, and it can
stop working earlier when the EC2 role session rotates. It must not be persisted. The
project therefore keeps it out of local state, manifests, Git, documentation examples,
MLflow metadata, and ordinary logs, and `PresignedUrl.__repr__` renders
`url=<redacted>`. Only the command that was explicitly asked to produce a URL writes it
to standard output as its result.

Transport matters as much as generation. The controller sends the URL on the direct SSH
session's stdin, together with the reviewed transfer module, and never on a command
line, environment dump, or log line — so it does not appear in the controller's or the
worker's process argument list. Worker-side error text is passed through the same URL
redaction as controller-side errors.

IAM policy remains authoritative. A presigned URL cannot grant more than the role that
signed it, so the role should be limited to the required actions on the configured
bucket and prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::<bucket>",
      "Condition": {"StringLike": {"s3:prefix": ["wavcse/*"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::<bucket>/wavcse/*"
    }
  ]
}
```

`HeadObject` requires `s3:GetObject` on the object. Do not grant account-wide S3
permissions, `s3:DeleteObject`, or `s3:DeleteBucket` for this workflow.

## Integrity, materialization, and verification limits

Artifacts are identified by a SHA-256 digest computed from their bytes. Multi-
gigabyte files are never read into memory: the worker streams in bounded chunks, while
the controller reads only small manifests and S3 metadata. An S3 ETag is never treated
as a SHA-256 checksum
and is never compared against a recorded digest, because multipart uploads make the ETag
depend on part boundaries.

Downloads materialize through a temporary sibling file:

```text
<destination>.wavcse-partial-<pid>-<random>
  -> streamed write + incremental SHA-256
  -> expected size and digest checks when supplied
  -> fsync, then atomic link (or rename with --overwrite)
```

An incomplete or failing transfer never appears at the destination path; temporary files
are removed on failure, and an existing destination is never replaced without explicit
`--overwrite`. A supplied digest mismatch, a supplied size mismatch, an announced-size
mismatch, or a non-2xx response fails the transfer.

Uploads stream SHA-256 over the bytes handed to HTTP and report the resulting size and
digest. The controller then confirms the stored object's size with a HEAD request. A
default PUT is signed with `If-None-Match: *`, so a new object at the same key cannot be
silently replaced after the initial HEAD check. `--overwrite` removes that condition.
An HTTP PUT success alone is not treated as durable completion. One PUT cannot exceed
5 GB; larger objects require multipart upload, which Phase 5 does not implement.

`infra storage verify` proves existence, stored size, available metadata, and — when a
manifest is supplied — that the manifest describes exactly this object and size. It does
not prove content, because downloading a multi-gigabyte object back to the controller to
re-hash it is not part of this workflow. The command prints that limitation instead of
implying a stronger guarantee, and `StorageVerification.content_checksum_verified` is
always `false`.

The trust model is therefore: a digest is only as trustworthy as the producer that
recorded it. A worker-reported digest after upload is producer-claimed evidence, the
stored size is provider-verified evidence, and cryptographic confirmation requires
something that actually reads the bytes.

## Job execution and source integrity

A recorded job executes an anonymous HTTPS checkout of a full Git commit. The worker
receives no GitHub credential, deploy key, or token; the controller never copies a
credential to a worker for source access. The worker resolves the commit, checks it out
detached, requires a clean tree, and compares `HEAD` with the requested object ID; the
controller requires the worker's reported commit to match before the command starts. A
mismatch aborts the job on both sides, and the durable record stores the verified commit.
The runner checks HEAD and cleanliness again immediately before setup and before the
research command. A trusted research command can still change its own checkout or load
external code after launch; `executed_commit` attests to the verified checkout at the
command boundary, not to every instruction the command later runs.

The reviewed worker runner is installed at `jobs.runner_path` after SHA-256 verification
against the digest of the module in this repository, written through a same-directory
temporary file. Only that reviewed path is executed, and only its installed digest is
reused, so a running job keeps the code it started with. Job descriptors, including
secret values, are delivered on the SSH stdin stream and never as argv.

`infra job cancel` terminates the job's own process group after verifying its recorded
Linux process start time and group identity; a pidfd pins the PID during signalling,
and the supervisor command line is also checked.
Cancellation never touches provider lifecycle: it cannot stop, start, or destroy a
worker, and it refuses an unverifiable PID instead of signalling it. Phase 6 never
destroys a worker automatically, including after a failed or cancelled job.

## Logging and redaction

Never log:

- authorization headers or API tokens;
- AWS access keys, secret keys, or session tokens;
- private SSH keys;
- OMP or Codex authentication stores and tokens;
- full presigned URLs or query strings;
- SSM `SecureString` values;
- complete environment dumps.

RunPod errors expose the operation, status code, and a sanitized response summary. Debug
mode changes detail, not secret-handling policy.

## Threat assumptions

- A controller compromise can reach all systems authorized to its role and configured
  tokens; controller access and patching are operational security requirements.
- A worker compromise may expose data and short-lived values delivered to that worker.
  Scope and lifetime must therefore be minimized.
- Git commits and dependencies can execute code on workers. Exact commits improve
  reproducibility but do not make untrusted code safe.
- Presigned URLs should be constrained by IAM/bucket policy and protected like bearer
  tokens.

## Repository hygiene

`.env*` files are ignored except for the blank `.env.example`. CI needs no cloud
credentials. Tests use fake tokens and mocked HTTP. Before each phase commit, inspect the
staged diff and scan it for credentials and generated artifacts.

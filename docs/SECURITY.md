# Security

## Trust boundaries

The controller, GitHub source, private S3 bucket, and MLflow/DagsHub service are trusted
for their documented roles. A RunPod worker is a temporary execution environment and is
not trusted with durable credentials or the only copy of important data.

Private S3 remains the canonical store for every research artifact. A RunPod network
volume is rebuildable working cache attached to a Secure Cloud Pod, and a container disk
is ephemeral scratch. Neither may ever hold the only copy of a canonical research
artifact, so losing a volume must never lose one.

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

### Google Colab

The trusted single-user controller holds human-minted ADC. Every pinned CLI
0.7.4 call uses `--auth=adc`; automation never opens interactive auth. ADC,
runtime proxy tokens in `~/.config/colab-cli/sessions.json`, and CLI history
stay on the controller, never in source, config, job records or a worker.
Colab `auth`, Google Drive, long-lived AWS keys and Git write credentials
are forbidden on the runtime. S3 remains canonical; `/content` is scratch.

**Narrow accepted exception:** the pinned CLI logs all `colab exec` code
and output in plaintext local JSONL history. The controller is already
trusted with temporary job capabilities, so this local history risk is
accepted for research execution; it is not permission to log tokens or
presigned URLs in ordinary CLI output or job metadata. Pinned `colab upload`
records its local/remote **paths** in history, not file contents. The CLI
Jupyter Contents PUT carries the temporary envelope bytes; a fixed exec
launcher validates schema, session, job and expiry, deletes the remote file
after ingestion, and never embeds URL/secret values in exec source or `--env`.
Local envelopes are 0600 and removed after upload. A kernel output that
echoes a secret can still enter CLI history; research code must not print
secrets. The adapter restricts owned CLI directories to 0700 and token/log/
infra-owned history files to 0600. Keep a short manually reviewed retention
window for identifiable infra-owned history; never broadly delete Google
state or change unrelated users' files.

Colab `usage` reports the account's `paidComputeUnitsBalance` and a native
CU/hour rate, not a USD/hour offer. A zero paid balance is not zero compute
entitlement: it selects best-effort free-tier execution when
`colab.allow_free_tier` is set, and its reported CU/hour is observation-only,
not a billable cost or a paid-balance requirement. Only one infra-owned
allocation can be active, enabling before/after incremental CU attribution. The
post-allocation rate/minimum-balance rejection applies only in paid-CU mode and
releases the confirmed exact owned session; a small amount of CU may already be
charged. An ambiguous create blocks another allocation until reconciliation.
Destroy checks local ownership and provider identity, and requires explicit
confirmation. The CLI's SSH proxy is not used because it can auto-create
missing sessions. RunPod's USD/hour, SSH and stop/start contract is unchanged.
See [Colab](COLAB.md) and [ADR-030](DECISIONS.md).

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

`infra volume destroy` follows the same rule. It addresses one volume by exact provider
ID and never resolves a name, prints the target's id, name, data center, size, and tier
plus any tracked Pods that mount it (stating that they are not destroyed), states that
the rebuildable cache is permanently lost, and requires interactive confirmation unless
`--yes`. It never deletes an S3 object and never stops or destroys a Pod. Destroying a
Pod never destroys its volume: volume and Pod lifecycles are independent by construction
and covered by tests in both directions. Creating a network volume is a billable
persistent resource, so a durable `PENDING_CREATE` intent is written locally before the
create request is issued, the CLI prints the data center, size, tier, the published list
price when one applies, an estimated monthly cost, and the exact JSON request body, and
requires interactive confirmation unless `--yes`; the create POST is never retried, and
an ambiguous outcome is reconciled by exact infra identity. A create the provider
definitively refuses leaves no intent behind, and while any intent is unresolved a further
volume create is refused rather than issued, because a retry after silence is the one way
this command could create a duplicate billable resource.

### Network volume cache

The network volume is explicitly not a credential store and must never become one. Cache
operations never receive a presigned URL at all: bytes enter the cache only from a file
the canonical download already verified, so nothing about the cache can write an
authorization header, API token, presigned URL, temporary AWS credential, or private key
to the mounted volume. The only files the cache writes are artifact bytes, a metadata
document holding the non-secret artifact key, digest, size, and timestamp, and a marker
document holding the schema version, purpose, cache root, and creation time. An offline
test asserts that no bearer-shaped material appears anywhere on the cache volume. This
phase introduces no new credential, secret type, or long-lived token, and adds no static
AWS key to the controller or the worker.

Cache operations are performed by the same reviewed, stdlib-only worker program that
already handles artifact transfers, streamed over direct SSH stdin. They add no worker
installation, package, daemon, or listening service, and they run with the same worker
account privileges as the existing transfer operations. They never travel over the RunPod
proxy; the mapped public-IP direct SSH endpoint is still required.

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

Network-volume bookkeeping lives beside it in
`~/.local/state/wavcse-infra/volumes.json`, written atomically with a same-directory
rename under a local advisory lock, stored in a `0700` directory as a `0600` file exactly
like `workers.json`. It is non-secret operational state keyed by infra identity rather
than provider ID, because the identity is known before the paid create and the ID only
after a response, and it holds provider IDs, placement, size, and lifecycle bookkeeping
only.

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
role. Presigned URLs remain the only artifact access path, and this phase changes nothing
about their treatment: they are still time-limited, still object-and-action scoped, and
still redacted unless a command was explicitly asked to produce one. A network volume is
never an alternate route to canonical storage.

Each URL is scoped to:

- one bucket (always the configured `storage.bucket`);
- one exact object key, already resolved beneath `storage.prefix`;
- one operation (`GetObject` or `PutObject`), never a broader grant;
- a finite lifetime, default `storage.presign_expiry_seconds = 3600` and clamped to
  60–604800 seconds.

The controller establishes worker readiness *before* signing anything, so waiting for SSH
never consumes a URL's lifetime, and one bounded transfer attempt can never outlive its own
URL: the attempt's SSH bound is derived as `min(ssh.transfer_timeout_seconds,
granted_lifetime - 30)`. A lifetime signed with temporary credentials is additionally
capped by the credentials' own remaining validity (read from the refreshable credential
chain, with a 30-second margin), because such a URL stops working when the session token
expires whatever `ExpiresIn` asked for; when the remaining validity is too short to sign
anything usable the presign is refused as a transient condition rather than shortened
silently. A transfer that needs longer is retried with a new URL and resumes from the
ranges recorded beside the destination, so no single URL is ever extended to cover a whole
large artifact. The read-only verification probe sends no URL at all.

Artifact verification opens the destination once, with `O_NOFOLLOW`, and hashes the
descriptor it opened rather than re-opening the pathname, then re-checks that the
pathname still names that inode. A destination that is replaced, symlinked, or otherwise
redirected while it is being verified is reported as an unverifiable observation instead
of being accepted, so a verified digest can never describe a different file than the one
the caller asked about.

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

Downloads only ever materialize through a sibling staging path, and one destination has
exactly one transfer at a time:

```text
<destination>.wavcse-transfer.lock      (destination-wide lock, see below)

one-shot staging, removed on failure:
  small or size-unknown object:
    <destination>.wavcse-partial-<pid>-<random>
      -> streamed write + incremental SHA-256
      -> expected size and digest checks when supplied
      -> fsync, then link from the open staging inode
  large object with a known expected size but no expected SHA-256:
    the same one-shot name, filled by bounded parallel byte ranges

resumable staging, deliberately retained on a transient failure:
  large object with a known expected size and expected SHA-256:
    <destination>.wavcse-partial + <destination>.wavcse-partial.json
      -> bounded parallel inclusive byte ranges, each written at its own offset
      -> per-range HTTP 206 / Content-Range / Content-Length validation
      -> bounded per-range retries with backoff, fsync after each durable range
      -> completed range indices recorded atomically (no bearer material)
      -> whole-file SHA-256 streamed over the assembled artifact
      -> expected size and digest checks
      -> link from the open staging inode, then the staging name is released
```

Placement creates the destination with `linkat` from the open verified inode and then
proves the created entry is that inode, so it never depends on what a pathname names at
the moment of placement. `--overwrite` is therefore a two-step replace: the previous
entry is removed and the destination is then created from the verified inode, so the
destination name is briefly absent, and a racing writer that takes the name first is
never overwritten. On a worker where `/proc/self/fd` is unavailable the destination is
linked from the staging name; that link is immediately verified, and the entry is removed
again and the transfer fails if it is not the verified inode.

Neither staging form is ever the destination path, so a partially written or unverified
artifact cannot be mistaken for a complete one. Staging state is removed in these cases
and retained in no others:

| State | When it exists | What happens to it |
| --- | --- | --- |
| one-shot staging file | during one non-resumable transfer | removed on every failure, consumed on success |
| resumable staging file + range record | during and after a resumable transfer | retained on a transient failure so a later invocation can resume; removed on success, on a size or digest failure, or when the record is discarded as incompatible |
| completed artifact at the destination | only after verified placement | never replaced without explicit `--overwrite` |
| `... .wavcse-transfer.lock` | from the first transfer for that destination, whatever its transport | never unlinked; it is an empty lock, not an artifact or a credential |
| `... .wavcse-stage-*` (legacy) | only as crash residue from an earlier build's placement | released when it aliases our own staging inode |

Only the complete-artifact comparison of size and SHA-256 authorizes placement, so a
retained partial file is never itself treated as a valid artifact. A successful range
request, an HTTP 206 status, and an S3 ETag are each insufficient on their own.

Resumable state is only reused when the invocation can prove it describes the same bytes.
The range record names the destination, expected size, expected SHA-256, range granularity,
and completed range indices, and the record schema requires that digest; a version 1
record, or one missing the digest, is discarded rather than trusted. A download without an
expected SHA-256 is therefore never resumable: it still uses the parallel ranged transport,
but into a one-shot staging file with no persisted state, and it discards any leftover
resumable state for that destination first. The record never contains a presigned URL or
any part of its signature, so a resumed invocation must be given a new URL.

Transport failures that can be transient — connection reset, timeout, temporary 5xx,
truncated body, an incomplete read during the response body — are retried a bounded number
of times per range with backoff, and a failed attempt can never leave a range half-trusted:
a range is only recorded after its bytes are complete and fsynced. An expired or rejected
authorization, a malformed `Content-Range`, or an inconsistent announced size fails
immediately rather than looping. Every transport failure surface, including failures raised
while reading a response body, is converted to a transfer error whose message has URLs and
signatures stripped.

Staging files are opened with `O_NOFOLLOW` and validated through the returned descriptor:
regular-file mode, a single hard link, and a pathname that still names that same inode. When
a staging file has extra links, only aliases in our own `... .wavcse-stage-*` placement
namespace that resolve to that exact inode are released as crash residue; any other extra
link keeps the shared-file refusal, so a hard link planted to a victim file is still never
written through.
Placement hard-links the open inode (through `linkat` on the process descriptor path on a
Linux worker, with a re-verified pathname fallback where `/proc` is unavailable) relative to
an open destination-directory descriptor, so a staging pathname replaced after validation
cannot redirect what lands at the destination. Metadata records are opened the same way and
rejected if they are symlinks, non-regular files, or hard-linked. A stale staging name that
merely hard-links an already-placed artifact — the residue of a crash between placement and
cleanup — is removed while the lock is held instead of blocking the next transfer.

The lock file is a fixed sibling path opened with `O_NOFOLLOW`, validated the same way, and
locked with `flock(LOCK_EX | LOCK_NB)` for the whole critical section: inspecting the
destination, opening or creating staging state, downloading, verifying, placing, and
cleaning up. Every download takes it — sequential, size-unknown, non-resumable, and ranged
alike — so a transfer cannot bypass serialization by being small or by lacking a digest,
and an empty lock file is left beside the destination even for a one-connection transfer. Because it is never unlinked, its lifetime does not depend on the staging file
being renamed or removed, and a second transfer for the same destination is refused with an
actionable error rather than racing the first.

Uploads stream SHA-256 over the bytes handed to HTTP and report the resulting size and
digest. The controller then confirms the stored object's size with a HEAD request. A
default PUT is signed with `If-None-Match: *`, so a new object at the same key cannot be
silently replaced after the initial HEAD check. `--overwrite` removes that condition.
An HTTP PUT success alone is not treated as durable completion. One PUT cannot exceed
5 GB; larger objects require multipart upload, which Phase 5 does not implement.

A declared job output is held to a stronger rule than that HEAD check. After the upload
the controller reads the object back from canonical storage, streaming it through one
SHA-256, and accepts the output only when the object's bytes match the size and digest the
worker reported. The read is bound to the version a preceding metadata read reported when
the bucket provides one, so an object replaced between the two reads is still verified
against the bytes that were actually read. An object that merely exists at the declared
key with a plausible size, or with a matching modification time, is never accepted: its
existence is not its provenance. This costs one full read of each persisted output, which
is the price of a cryptographic claim about what canonical storage holds.

`infra storage verify` proves existence, stored size, available metadata, and — when a
manifest is supplied — that the manifest describes exactly this object and size. It does
not prove content, because downloading a multi-gigabyte object back to the controller to
re-hash it is not part of this workflow. The command prints that limitation instead of
implying a stronger guarantee, and `StorageVerification.content_checksum_verified` is
`false` for that metadata-level verification. The content-verifying path used for job
outputs returns the same model with `content_checksum_verified` set and the observed
`content_sha256` recorded.

The trust model is therefore: a digest is only as trustworthy as the producer that
recorded it. A worker-reported digest after upload is producer-claimed evidence, the
stored size is provider-verified evidence, and a persisted job output additionally carries
a controller-observed digest read back from canonical storage.

### Cache integrity

The cache is a shared, rebuildable store, so its threat model is poisoning and corruption
rather than confidentiality of new material. An entry is used only when the requested
identity, the recorded size, and the bytes on disk all agree, and the requested digest is
the identity — a filename is never trusted. A file whose name matches but whose content
differs cannot be returned, because identity is content-addressed and its bytes are
re-hashed both when the entry is selected and again as the copy is written. Placement then
links that verified file rather than moving a pathname, so a writer with the worker
account's own write access to the destination directory is the only actor that could change
the bytes afterwards - the same boundary every other placement in this tool has, and the
same one the canonical download has. A hit therefore carries exactly the integrity guarantee
of a fresh download:
the Phase 5 size and SHA-256 requirements are unchanged and are never weakened for cached
content, and corrupt bytes are never accepted because they came from provider-attached
storage.

An entry that contradicts its recorded identity is quarantined — moved into the cache's
`staging/` area, which is documented as safe to delete — reported as a warning, and
treated as a miss so the next canonical download rebuilds it. Every other cache failure
degrades to a warning and the canonical download proceeds: an absent entry, a quarantined
entry, an unusable cache root, or an interrupted lookup can only make a job slower, never
fail it. A definitive protocol violation the worker reports still raises, and cache
integrity problems are reported through a warning sink the CLI writes to stderr.

Cache paths are validated before they are used. The digest must be 64 hexadecimal
characters and a destination path must be absolute. No component between the cache root
and an entry may be a symbolic link: a symlinked cache root, symlinked prefix directory,
symlinked entry directory, or symlinked content file is refused rather than followed, and
the resolved entry path is additionally checked to stay inside the resolved cache root.

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
worker, and it refuses an unverifiable PID or process-group identity instead of signalling
it. A process group whose recorded identity cannot be proven is never signalled, because
killing an unrelated workload is unrecoverable while an unidentified survivor is not.
Phase 6 never destroys a worker automatically, including after a failed or cancelled job.

A job's terminal record also means nothing of that job is still running: the supervisor
terminates the process groups it recorded, with the same identity verification, and
refuses to write an outcome while any of them is still alive. Such a job stays nonterminal
and is reported for reconciliation rather than being declared finished.

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

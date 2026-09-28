# RunPod provider notes

## Selected API

Pod lifecycle and SSH endpoint discovery use RunPod REST API v2:

```text
https://api.runpod.io/v2
```

The implemented operations are:

```text
GET    /catalog/gpus
GET    /catalog/gpus/{id}
GET    /pods
GET    /pods/{id}
POST   /pods
POST   /pods/{id}/action   {"action":"start"}
POST   /pods/{id}/action   {"action":"stop"}
DELETE /pods/{id}
```

Authentication remains `Authorization: Bearer <token>`. The client resolves the token
once from a non-empty `RUNPOD_API_KEY`, otherwise from the SSM `SecureString` named by
`runpod.api_key_parameter`. It never writes the value to configuration or state.

RunPod now documents REST v1 as deprecated and scheduled for retirement on November
15, 2026. REST v2 is the current resource-management interface and adds the catalog
needed for pre-creation price and availability checks. Phase 3 therefore supersedes
the Phase 2 v1 decision in [ADR-012](DECISIONS.md#adr-012-migrate-worker-management-to-runpod-rest-api-v2).
Existing controller configuration must use `https://api.runpod.io/v2`; the client
rejects a v1 base URL with an actionable configuration error.

## GPU discovery and pricing

`infra worker gpu-types` requests `include=AVAILABILITY`, `product=POD`, the requested
GPU count, and one explicit cloud tier. The normalized result contains:

- exact GPU type ID used by Pod creation;
- display name and VRAM;
- Secure or Community cloud;
- current provider availability;
- maximum GPUs of that type on one machine;
- provider list price per GPU-hour and total GPU price for the requested count;
- per-datacenter availability where RunPod reports it.

RunPod documents catalog prices as the list price for one GPU. The CLI multiplies that
value by `--gpu-count`; it does not hardcode rates. `--max-price` compares the total
catalog price with the operator's limit before the create request. If price is absent,
the CLI cannot prove the guard and refuses creation whenever a maximum was supplied.
The catalog and create request are separate API operations, so RunPod does not provide
an atomic server-side price lock between them. The created Pod's provider-reported
`cost` is persisted when available.

Availability is advisory and can change between discovery and scheduling. The CLI
refuses `NONE` and unknown availability and never substitutes another GPU, cloud, GPU
count, or datacenter. RunPod can still reject a create if capacity disappears.

## Creation request

The v2 request is nested and names exactly one GPU type:

```json
{
  "name": "wavcse-training-<unique-suffix>",
  "cloud": "COMMUNITY",
  "gpu": {"id": "<exact-gpu-type-id>", "count": 1},
  "image": "<container-image>",
  "disk": 20
}
```

The CLI requires exactly one of `--image` or `--template`. Optional request fields cover
an explicit list of datacenter IDs, a host-local persistent volume, one existing
network volume, and RunPod's `startSsh` setup flag. Persistent and network volumes are
mutually exclusive. `--start-ssh` sends `startSsh: true` and exposes `22/tcp`; this is
required for a direct mapped endpoint and is recommended for Phase 4 bootstrap.

REST v2 currently has no interruptible/spot property in `CreatePodRequest` and its GPU
catalog does not expose a spot offer for Pod creation. `--interruptible` is retained as
an explicit interface choice but fails before any mutation. The CLI never silently
turns a spot request into an on-demand Pod. REST v1 did expose an `interruptible` field,
but using a deprecated create endpoint solely for that field would split lifecycle
semantics and is not implemented.

## Unique identity and ambiguous-create safety

Every CLI-created Pod receives an exact generated name:

```text
wavcse-<operator-prefix>-<12-hex-random-suffix>
```

REST v2 does not expose suitable Pod tags or an idempotency-key header, and RunPod does
not require names to be unique. The generated exact name is therefore the reconciliation
identity.

`POST /pods` is issued once. It is never covered by the GET retry loop. If a transport
failure, timeout, HTTP 5xx/429, or malformed success response makes the outcome
ambiguous, the client performs bounded `GET /pods` reconciliation:

- one exact-name match is adopted and its provider ID is persisted;
- multiple exact-name matches fail with an explicit duplicate warning;
- no match after the configured attempts fails safely and tells the operator to run
  `infra worker list`;
- the create POST is never automatically repeated because the API has no provider-
  enforced unique name or idempotency key.

This favors a recoverable manual inspection over accidentally creating a second paid
Pod.

## Lifecycle state normalization

RunPod v2 status values map as follows:

| RunPod status | Internal state |
| --- | --- |
| `PROVISIONING` | `PROVISIONING` |
| `STARTING` | `STARTING` |
| `RUNNING` | `RUNNING` |
| `EXITED` | `STOPPED` |
| `ERROR` | `ERROR` |
| `TERMINATED` | `DESTROYED` |
| missing/new value | `UNKNOWN` |

The native value is retained for diagnostics. Internal `STOPPING` and `TERMINATING`
states are available for orchestration even though the current Pod response enum does
not emit them.

Start and stop use `POST /pods/{id}/action`; destroy uses exact-ID
`DELETE /pods/{id}`. Mutation requests are not automatically retried. If a start, stop,
or destroy response is lost, the lifecycle layer reconciles with bounded GET polling.
Poll intervals back off to a configured maximum, safe GET failures are transient, and
timeouts report the last known provider state.

## SSH endpoint and public-key behavior

Current v2 Pod responses expose an `ssh` object with either or both of:

- `ssh.proxy`: `ssh.runpod.io:22` with a Pod-specific username. RunPod documents this
  basic connection as command capable but without SCP/SFTP support.
- `ssh.direct`: a public IP, provider-assigned external TCP port, and normally username
  `root`. This exists only when the machine supports a public IP, an SSH daemon is
  running, and container port `22/tcp` is exposed. The external port is not assumed to
  be 22.

`infra worker wait-ssh` repeatedly calls exact `GET /pods/{id}`, prefers `ssh.direct`,
falls back to `ssh.proxy`, and runs an authenticated remote `true`. It does not equate
RunPod `RUNNING` with SSH readiness. Missing IP/port metadata, startup connection
refusal, and transient provider GET failures remain within a bounded backoff; terminal
Pod states, authentication failure, host-key mismatch, and timeout are explicit errors.

On create, `startSsh` injects a `PUBLIC_KEY` value containing the account's registered
SSH public keys unless the request supplied one. It does nothing when the account has
no registered public key, and only compatible images start sshd from this convention.
RunPod official images support it. wavcse-infra does not manage account keys or send a
private key; register the public half of the configured dedicated worker key before
creating the Pod. The v2 reference describes `startSsh` as create-only: GET does not
return the flag and PATCH cannot enable it later.

RunPod's current documents use two names around image-level overrides: the v2 create
reference says the provisioner injects `PUBLIC_KEY`, while the general SSH guide tells
operators to set `SSH_PUBLIC_KEY` to override an account key for one Pod. This project
sets neither variable itself and relies only on `startSsh` plus account-registered keys,
avoiding an undocumented guess between the two names.

## Bootstrap, GPU health, and readiness

`infra worker bootstrap <id>` streams `worker/bootstrap.sh` to `bash -s` over SSH; it
does not require SCP/SFTP. The script is non-interactive and idempotent: on a supported
Ubuntu image it installs missing CA certificates, curl, Git, Python, uv, tar/gzip, and
basic process/filesystem utilities, creates `/workspace`, then atomically writes the
expected version to `~/.local/state/wavcse-worker/bootstrap-version`. It does not clone
wavCSE or install PyTorch, research dependencies, OMP, Codex, or AGF.

The subsequent health script reports the bootstrap marker, Git/Python/uv versions,
available bytes at the selected workspace path, and `nvidia-smi` facts: GPU count,
model, MiB, driver, and CUDA compatibility version. A valid NVIDIA GPU is required for
Phase 4 `READY`. AMD and other accelerators are reported as unsupported rather than
being tested with the wrong tool. Less than roughly 20 GiB free produces a warning
because the planned embeddings alone are approximately that size; it does not invent a
larger readiness minimum.

RunPod state and local readiness are separate. The local progression is `NOT_READY` →
`SSH_READY` → `BOOTSTRAPPED` → `GPU_HEALTHY` → `READY`; a required failed check records
`FAILED`. Stop/destroy returns a tracked worker to `NOT_READY`. Re-running bootstrap is
the supported recovery path after a partial failure or version mismatch.

## Stop, storage, and destroy costs

RunPod reports Pod `cost` as zero while status is `EXITED`, but that is the current
compute cost, not a guarantee of zero total cost. Current provider documentation says:

- container disk is erased on stop and is not charged while stopped;
- host-local volume disk is retained and continues to accrue storage charges, at a
  different stopped-Pod rate;
- network volumes continue to accrue their normal storage charge independently of Pod
  compute.

`stop` retains the Pod. `destroy` permanently terminates the Pod resource and requires
the exact provider ID plus confirmation unless `--yes` is supplied. Destroying a Pod
does not imply deletion of a separately managed network volume. S3 remains canonical;
Phase 4 does not transfer or verify artifacts.

## Local state

Created-worker metadata is written atomically to:

```text
~/.local/state/wavcse-infra/workers.json
```

The versioned JSON file is mode `0600`; its directory is mode `0700`. Writes use a
same-directory temporary file, `fsync`, and atomic replacement. It stores request and
observed resource details, price, timestamps, provider state, local readiness,
provider-reported SSH coordinates, bootstrap version, health timestamps, disk capacity,
and GPU facts. It never stores API tokens, authorization headers, SSM values, AWS
credentials, GitHub credentials, or private keys.

RunPod remains authoritative. List/show reads come from RunPod and only reconcile
records already tracked locally. Unrelated Pods in the same account are displayed but
are not claimed as wavcse-infra-owned. An already-absent exact-ID destroy updates local
state when possible and does not target a similar name.

## Read retries and errors

Only GET requests use automatic retries. Retryable conditions are transport/timeouts,
HTTP 429, and HTTP 5xx. Attempts are bounded, use exponential backoff, and cap each
delay. Authentication, permission, not-found, validation, conflict, redirects, and
other 4xx responses fail immediately.

REST v2 RFC 9457 error `detail` text is sanitized and bounded before display. Request
headers and complete response objects are never rendered. Authorization values and URL
query strings pass through central redaction.

## Current Phase 4 limitations

- REST v2 does not currently expose interruptible/spot Pod creation.
- The client-side maximum price check is not a provider-side atomic price reservation.
- Availability is a current catalog signal, not a capacity guarantee.
- Worker bootstrap currently supports Ubuntu images with `apt-get` and NVIDIA health
  through `nvidia-smi`; it will not mark AMD workers READY.
- Proxy SSH does not support SCP/SFTP. wavcse-infra uses stdin for its small scripts and
  deliberately leaves data transport to the later S3 phase.
- Trust-on-first-use cannot authenticate the first SSH host key. Later changed keys fail
  closed in the dedicated known-hosts file.
- No artifact transfer, exact-commit execution, research environment, or job management
  is implemented.
- No normal test or CI job calls the live API or performs a paid mutation.

## Official references

- [REST API v2 overview](https://docs.runpod.io/api-reference-v2/overview)
- [Migrate from API v1](https://docs.runpod.io/api-reference-v2/migrate-from-v1)
- [List GPU types](https://docs.runpod.io/api-reference-v2/catalog/list-gpu-types)
- [Create a Pod](https://docs.runpod.io/api-reference-v2/pods/create-a-pod)
- [List Pods](https://docs.runpod.io/api-reference-v2/pods/list-pods)
- [Get a Pod](https://docs.runpod.io/api-reference-v2/pods/get-a-pod)
- [Pod state transition](https://docs.runpod.io/api-reference-v2/pods/trigger-a-pod-state-transition)
- [Terminate a Pod](https://docs.runpod.io/api-reference-v2/pods/terminate-a-pod)
- [Pod pricing](https://docs.runpod.io/pods/pricing)
- [Connect to a Pod with SSH](https://docs.runpod.io/pods/configuration/use-ssh)

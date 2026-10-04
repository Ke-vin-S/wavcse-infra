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

Public-IP-filtered offer discovery and constrained creation use RunPod's current
GraphQL endpoint:

```text
https://api.runpod.io/graphql

gpuTypes.lowestPrice(input: {supportPublicIp: true, ...})
podFindAndDeployOnDemand(input: {supportPublicIp: true, ...})
```

This narrow mixed surface is required because REST v2 still cannot express the public-IP
placement constraint. RunPod's current official `runpodctl pod create --public-ip` uses
the same GraphQL placement mutation.

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

With `--require-direct-ssh`, discovery instead asks GraphQL `lowestPrice` for capacity
filtered by `supportPublicIp: true`, the requested cloud/count, and any datacenter IDs.
It displays only confirmed compatible offers, sorts them by compatible on-demand price,
and marks `PUBLIC IP` as `YES`. This price can be higher than the unfiltered GPU-type
catalog price and is the value used by `--max-price` during a constrained create.

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
mutually exclusive. `--start-ssh` sends `startSsh: true` and exposes `22/tcp`. Both are
necessary for direct SSH, but they are not sufficient on a Community Cloud host without
a public IP.

`--require-direct-ssh` requires `--start-ssh`, verifies a public-IP-filtered compatible
offer before confirmation, and creates through `podFindAndDeployOnDemand` with all three
requirements in one placement request: `supportPublicIp: true`, `startSsh: true`, and
`ports: "22/tcp"`. If no matching machine exists, the scheduler rejects the mutation
instead of placing a paid Pod on an unsuitable host. Unconstrained creates retain REST
v2 behavior for backward compatibility.

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

- `ssh.proxy`: `ssh.runpod.io:22` with a Pod-specific username. This is RunPod's basic
  terminal gateway rather than true SSH to the container. Live validation showed that
  it requires a PTY and answers non-interactive exec requests with an error on stdout
  and exit status zero.
- `ssh.direct`: a public IP, provider-assigned external TCP port, and normally username
  `root`. This exists only when the machine supports a public IP, an SSH daemon is
  running, and container port `22/tcp` is exposed. The external port is not assumed to
  be 22.

`infra worker show` renders both normalized endpoint kinds explicitly. A Pod may show no
public IP or direct SSH port while still exposing a usable `ssh.proxy`; configured
`22/tcp` alone does not prove that the host supports a public IP or direct mapping.

`infra worker wait-ssh` repeatedly calls exact `GET /pods/{id}`, requires `ssh.direct`,
and runs an authenticated remote marker command with PTY allocation disabled. Both exit
status zero and the marker are required, so a gateway-generated success status cannot
be mistaken for remote execution. `infra worker exec`, bootstrap, and health use the
same direct non-interactive mode. `infra worker ssh` is separate: it forces a PTY and
may fall back to `ssh.proxy` for a human terminal. Missing IP/port metadata, startup
connection refusal, and transient provider GET failures remain within a bounded
backoff; terminal Pod states, authentication failure, host-key mismatch, and timeout
are explicit errors.

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

`infra worker bootstrap <id>` streams the small reviewed `worker/bootstrap.sh` content
over direct SSH stdin and runs it with `bash -s`; it does not require SCP/SFTP or an
interactive PTY. The script is non-interactive and idempotent: on a
supported Ubuntu image it installs missing CA certificates, curl, Git, Python, uv,
tar/gzip, tmux, and basic process/filesystem utilities, creates `/workspace`, then
atomically writes the expected version to
`~/.local/state/wavcse-worker/bootstrap-version`. A successful exit is accepted only
with the expected completion marker. It does not clone wavCSE or install PyTorch,
research dependencies, OMP, Codex, or AGF.

After the completion marker, the controller also mirrors this repository's
application configuration onto the worker, installing `~/.tmux.conf` from
`apps/tmux/tmux.conf` and reporting `App config tmux: installed ~/.tmux.conf`. A
remote file that already matches is left untouched (`unchanged`), and a differing
existing file is preserved rather than overwritten. `infra worker apply-config <id>`
re-applies the configuration on demand; it prompts before replacing a differing file
and never prompts without a TTY, and `--yes` replaces it after backing the previous
file up to a `.wavcse-backup-<UTC timestamp>` sibling.

`/workspace` is a conventional execution directory, not evidence that storage is
mounted. RunPod's official image also defines `/workspace` as its workspace location,
and the bootstrap makes the directory available idempotently. When no persistent or
network volume was requested, it is part of the ephemeral container filesystem.

The subsequent health script emits a versioned, tab-delimited schema on stdout and keeps
remote stderr separate for diagnostics. The parser requires exactly one supported schema
declaration and validates every protocol row; a bounded, redacted stdout/stderr excerpt is
included when parsing fails. The protocol reports the bootstrap marker, Git/Python/uv
versions, execution-storage availability, and `nvidia-smi` facts: GPU count, model, MiB,
driver, and CUDA compatibility version. With no requested volume, disk availability is
measured on the ephemeral container filesystem at `/`; `/workspace` is not treated as a
required mount. With a requested persistent or network volume, availability is measured
at its configured path and `mountpoint` must confirm an actual mount there. A plain
directory cannot satisfy that requirement. A valid NVIDIA GPU is required for Phase 4
`READY`. AMD and other accelerators are reported as unsupported rather than being tested
with the wrong tool. Less than roughly 20 GiB free produces a warning because the planned
embeddings alone are approximately that size; it does not invent a larger readiness
minimum. Failure to inspect the selected filesystem is a required-check failure.

RunPod state and local readiness are separate. The local progression is `NOT_READY` →
`SSH_READY` → `BOOTSTRAPPED` → `GPU_HEALTHY` → `READY`; a required failed check records
`FAILED`. Stop/destroy returns a tracked worker to `NOT_READY`. Re-running bootstrap is
the supported recovery path after a partial failure or version mismatch.

## Recorded job execution over RunPod SSH

Phase 6 installs one reviewed, stdlib-only Python file at `jobs.runner_path` (default
`/root/.local/state/wavcse-worker/job_runner.py`, matching the `root` account used by
RunPod's direct SSH endpoint). Installation streams the module over direct SSH stdin,
writes it through a same-directory temporary file, and verifies its SHA-256 against the
digest computed from this repository; an identical installed digest is reported as
`unchanged` and nothing is rewritten. A non-root worker user needs `jobs.runner_path`
overridden, because the install path must be writable by the SSH account.

Each job phase is one bounded SSH command (`python3 <runner> prepare|start|inspect|logs|cancel`)
with a JSON descriptor on stdin. The descriptor for `start` includes declared secret
values, so no job data ever appears in a remote process argument list.

`start` launches a supervisor with `start_new_session=True` and no controlling terminal,
appending stdout/stderr to `<job>/logs/job.log`. Detaching is what makes a multi-hour run
survive controller or SSH interruption, and it is why Phase 6 needs neither tmux on the
worker nor a worker daemon. Each stage (setup, then the command) runs in its own process
group, so a timeout or `infra job cancel` can terminate the entire job tree without
signalling the supervisor or any other process. Before signalling, cancellation verifies
the recorded Linux process start time and process-group identity that the PID still
belongs to this job; a pidfd pins the PID while signalling, and the supervisor command
line is also checked. An unverifiable PID is refused rather than killed.

Status comes from the worker's own files: `state/pid.json`, `state/finished.json`,
`state/cancelled.json`, and the log size. RunPod remains authoritative for the Pod; the
local readiness that gate submission still comes from Phase 4 bootstrap/health, and Phase 6
never bootstraps, starts, or destroys a Pod.

Source checkout uses the anonymous HTTPS remote with `--filter=blob:none`, a targeted
`git fetch --depth 1 origin <commit>` (falling back to a full fetch), detached checkout,
`rev-parse HEAD` verification, and a clean-tree requirement. Git LFS smudge is disabled,
so LFS-tracked content is not fetched on the worker.

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
artifact materialization and verification go through the S3 presign paths described in
[Operations](OPERATIONS.md#artifact-storage-operations), not through the Pod filesystem.

## Artifact transfer over RunPod SSH

Artifact bytes never travel over SSH. RunPod's basic proxy is interactive-only, so
`infra storage download` and `infra storage upload` require the same mapped public-IP
direct endpoint as `worker exec`, bootstrap, and health. Over that channel the controller
streams the small reviewed transfer program and generated transfer assignments on
stdin; the URL is never an argument on either side.

Workers receive no AWS credential, `~/.aws` state, GitHub write credential, controller
SSH private key, or RunPod token. Their only S3 capability is the single object and
operation in the URL the controller just generated, until that URL expires. A worker
without Python 3 cannot transfer: run `infra worker bootstrap <id>` first, which
installs and health-checks it.

## Network volumes

A network volume is persistent, provider-attached storage that outlives a Pod. It is
rebuildable working storage, not canonical storage: S3 holds the only authoritative copy
of every artifact, and a lost volume costs a re-download rather than a lost result.

Phase 6.2 uses these REST v2 operations, all confirmed live against the current API:

```text
GET    /network-volumes                           -> {"networkVolumes":[...]}
GET    /network-volumes/{id}                      -> one NetworkVolume
POST   /network-volumes                           -> 201, body {"name","size","dataCenter"[,"type"]}
DELETE /network-volumes/{id}                       -> 204, no body
GET    /catalog/datacenters[?include=GPU_AVAILABILITY]
GET    /billing/network-volumes[?networkVolumeId=][&lastN=]
```

A network volume resource reports only `id`, `name`, `size`, `dataCenter`, and `type`.
`size` is GB with a provider floor of 10 and ceiling of 4096. `type` is `STANDARD` or
`HIGH_PERFORMANCE` and is immutable after creation; size can be increased but never
reduced. Names are not required to be unique, which is why every CLI-created volume
receives the generated `wavcse-vol-<prefix>-<12-hex>` identity that reconciliation
matches.

`GET /catalog/datacenters` carries `networkVolumeTypes` per data center, and that list is
empty for a data center that cannot host a volume at all. `infra volume datacenters`
prints exactly that catalog, and the same field is the pre-creation placement guard: a
data center that does not advertise the requested tier is rejected before any request is
issued.

### The data-center invariant

RunPod attaches network volumes only to Secure Cloud Pods, and a volume exists in exactly
one data center. RunPod's own documentation states the consequence: a Pod that mounts a
volume must be scheduled in that volume's data center, and the mount must be requested at
Pod creation because it cannot be attached or detached later.

`infra worker create --network-volume-id <id>` therefore resolves the volume first and
constrains the Pod request to `dataCenterIds: [<volume data center>]` before the offer is
looked up and before any paid request exists. An operator-supplied `--data-center` that
names anything else is rejected rather than quietly overridden, and `--cloud community`
with a network volume is rejected outright. If the requested GPU has no confirmed
availability in that data center, the command fails while it is still free to do so, with
a message naming the volume and its data center and stating that no Pod was created.

Placement is then verified from the provider's own answer, because a scheduler can only
accept or reject and the request is not proof of the result. A created Pod reported in a
different data center, or reported without the requested network mount, raises an
explicit error naming the created (billing) Pod and how to remove it. A create answer that
omits those fields is refreshed once from `GET /pods/{id}` before any conclusion is
drawn, so a sparse response is never mistaken for a wrong placement.

### Ambiguous create and the intent record

`POST /network-volumes` is issued once and never retried, exactly like a Pod create, and
for the same reason: the provider exposes no idempotency key and no unique-name
constraint, so a retry could create a second billable volume.

Before that request the controller durably records a `PENDING_CREATE` intent under the
volume's infra identity in `~/.local/state/wavcse-infra/volumes.json`. The intent is
written first because the identity is known before the paid call while the provider ID is
only known after a response arrives. If the response is lost, the client lists volumes and
matches the complete identity: one match is adopted, several are reported as ambiguous,
and none fails safely after bounded attempts. In that last case the intent survives, so
`infra volume list` can match the provider's own listing later and warns on stderr about
any intent the provider has not confirmed. An ambiguous delete is reconciled the same way
a Pod delete is: bounded `GET /network-volumes/{id}` polling until the volume is absent.

### Mount path and the rebuildable cache

`volumes.mount_path` (default `/workspace/cache`) is where a network volume is mounted and
is therefore also the worker's cache root. The default deliberately nests the mount inside
the ephemeral workspace instead of taking `/workspace` itself, so the job workspace and
job scratch stay on container disk while only the cache is persistent. An explicit
`--volume-mount-path` always wins, and a Pod with no network volume keeps the historical
`/workspace` default.

The cache itself is described in [Operations](OPERATIONS.md#network-volume-operations):
identity is an artifact's SHA-256, an entry is `artifacts/sha256/<first-two-hex>/<digest>/
{content,metadata.json}` under the mount point, publication is a single directory rename
after a verified staged copy, and every cache problem degrades to a canonical download.

### Storage pricing

RunPod exposes no network volume price field anywhere in REST v2: the resource reports
capacity and placement only, and the only money the API reports is billing that has
already been incurred, through `GET /billing/network-volumes`. RunPod's published list
price for standard network volume storage is **$0.07/GB/month, quoted for volumes up to
1 TB**, at the
time of writing, documented on its Pod pricing page. The provider publishes a different
rate for larger volumes of the same tier without stating the banding unambiguously, so a
request above 1 TB prints no estimate rather than one built on an unverified scope.
High-performance storage is
documented only as "a premium to standard storage" whose exact per-GB rate varies by data
center and appears only in RunPod's console, so this tool quotes no estimate for that
tier.

`infra volume create` therefore labels any figure it prints as a published list price
rather than a provider-reported charge, and `infra volume show` reports the account's
actual provider-billed amounts instead. The provider price is never used to decide
whether an operation is allowed.

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

A second document, `~/.local/state/wavcse-infra/volumes.json`, tracks created network
volumes with the same atomicity and permissions. It is keyed by infra identity rather
than provider ID, because a paid create has to be recorded before the provider answers.
Its life cycle is `PENDING_CREATE`, `AVAILABLE`, or `DESTROYED`. Worker records
additionally carry `network_volume_mount_path`, the provider-reported mount path of the
Pod's network volume, which is what enables that worker's cache.

## Read retries and errors

Only GET requests use automatic retries. Retryable conditions are transport/timeouts,
HTTP 429, and HTTP 5xx. Attempts are bounded, use exponential backoff, and cap each
delay. Authentication, permission, not-found, validation, conflict, redirects, and
other 4xx responses fail immediately.

REST v2 RFC 9457 error `detail` text is sanitized and bounded before display. Request
headers and complete response objects are never rendered. Authorization values and URL
query strings pass through central redaction.

## Current limitations

- REST v2 does not currently expose interruptible/spot Pod creation.
- The client-side maximum price check is not a provider-side atomic price reservation.
- Availability is a current catalog signal, not a capacity guarantee.
- Worker bootstrap currently supports Ubuntu images with `apt-get` and NVIDIA health
  through `nvidia-smi`; it will not mark AMD workers READY.
- RunPod's basic SSH proxy is interactive-only. It requires a PTY and can return its own
  error text with exit status zero for non-interactive requests. Automation therefore
  requires `ssh.direct`; proxy-only Pods cannot reach `READY`.
- REST v2's GPU catalog does not carry the host-level `supportPublicIp` property. Use
  `--require-direct-ssh` so discovery and creation use the GraphQL scheduler filter;
  unqualified REST catalog availability is not evidence of direct-SSH compatibility.
- Trust-on-first-use cannot authenticate the first SSH host key. Later changed keys fail
  closed in the dedicated known-hosts file.
- Exact-commit job execution requires the worker's `python3` (installed by
  `infra worker bootstrap`) and a writable `jobs.runner_path` for the SSH account.
- Phase 6 does not install research dependencies, provision workers automatically,
  schedule across workers, or resume a partially completed run.
- RunPod exposes no network volume price through its API, so a pre-creation cost is a
  published list-price estimate rather than a provider-reported charge; only incurred
  billing is provider-reported.
- A network volume constrains Pod placement to one data center. This is what makes it
  useful and also what limits it: capacity in that data center is the only capacity a
  Pod mounting it can use, and the volume cannot be moved.
- The rebuildable cache has no automatic eviction, size cap, or cross-data-center
  replication. Only a Pod with that volume mounted can read it.
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
- [Network volumes](https://docs.runpod.io/storage/network-volumes)
- [High-performance storage](https://docs.runpod.io/storage/high-performance-storage)
- [List network volumes](https://docs.runpod.io/api-reference-v2/network-volumes/list-network-volumes)
- [Create a network volume](https://docs.runpod.io/api-reference-v2/network-volumes/create-a-network-volume)
- [Delete a network volume](https://docs.runpod.io/api-reference-v2/network-volumes/delete-a-network-volume)
- [List data centers](https://docs.runpod.io/api-reference-v2/catalog/list-data-centers)
- [Network volume billing history](https://docs.runpod.io/api-reference-v2/billing/get-network-volume-billing-history)
- [Connect to a Pod with SSH](https://docs.runpod.io/pods/configuration/use-ssh)
- [RunPod GraphQL schema](https://graphql-spec.runpod.io/)
- [RunPod CLI Pod reference](https://docs.runpod.io/runpodctl/reference/runpodctl-pod)

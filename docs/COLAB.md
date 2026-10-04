# Google Colab execution

`google-colab-cli==0.7.4` is pinned on the trusted controller. Colab is a
first-class **ephemeral** execution provider: allocate, observe actual account
CU usage and physical GPU, enforce cost policy, bootstrap to READY, run one or
more compatible exact-commit jobs, persist outputs, release. There is no
resumable stop, network-volume cache, public-IP or SSH requirement.

## Execution modes

Colab has two billing/execution modes, both still `ProviderKind.COLAB` over
`ExecutionTransport.COLAB_EXEC` — a mode, never a second provider:

- `PAID_CU` — `paidComputeUnitsBalance > 0`; the existing CU-budget policy
  applies (minimum paid balance, maximum incremental CU/hour, maximum job CU,
  and paid balance covering the projected job CU).
- `FREE_TIER` — `paidComputeUnitsBalance == 0`; best-effort, interruptible
  execution whose capacity, accelerator, runtime and usage limits are not
  guaranteed. A free-tier job is never rejected for having no paid balance.

The CLI's `Current balance` is the account's `paidComputeUnitsBalance`. It is
empirically established that Colab allocates and runs a free-tier T4 with a
`0.00` paid balance and a nonzero provider-reported usage rate (observed
`1.07/hr`). Therefore:

```text
balance == 0  ->  no paid CU balance  ->  eligible to attempt free tier
              -/->  no Colab compute entitlement
```

In free tier the provider-reported CU/hour is recorded as **observed metering**,
never treated as a paid cost or as evidence that the account must hold enough
paid CU. No CU-to-USD conversion is invented. Actual provider allocation is the
only authority on whether a free accelerator is granted.

## Authentication and configuration

A human authenticates once on the controller (never in automated bootstrap):

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
```

The CLI uses `--auth=adc` explicitly. `infra doctor` checks CLI version, session
access, account balance and rate without allocating. No ADC file, CLI session
token, Google Drive, static AWS key or Git write credential is copied to a worker.

```toml
[colab]
enabled = true
default_gpu = "T4"
max_simultaneous_workers = 1
allow_free_tier = true
minimum_balance_cu = 5
max_incremental_rate_cu_per_hour = 3
max_job_cu = 10

[placement]
preferred_providers = ["colab", "runpod"]
```

`allow_free_tier` is the free-tier permission, independent of `minimum_balance_cu`
(which is paid-CU policy only). With `allow_free_tier = false`, a zero paid
balance is rejected before allocation. Values are operator-owned non-secret
limits, **not** provider prices. Defaults in the example are illustrative and
should be set to the operator's CU budget.

Only one infra-owned allocation is permitted. CLI 0.7.4 `usage` prints account
balance, aggregate rate (two decimal places) and active-assignment count; the
controller parses all three. On creation it records a unique `wavcse-` identity
before calling `new`, then compares `usage_after - usage_before`, requiring the
assignment count to increase by exactly one. In `PAID_CU` it additionally
requires a positive incremental CU/hour within policy and a post-allocation
balance at or above the configured minimum; in `FREE_TIER` it records the
observed rate without a rate or paid-balance gate. If a confirmed owned session
fails cost or bootstrap checks, it is released and marked absent only after
provider confirmation. Ambiguous creation is reconciled by exact identity and
never blindly repeated. Account observations are rounded; a changing unrelated
session makes attribution unsafe and causes rejection. **The post-allocation
guard may consume a small amount of CU before release.** No invented USD/hour
conversion is made. RunPod retains its USD/hour ceiling, SSH, storage and
lifecycle semantics.

## Free-tier limitations

These are explicitly accepted, not reasons to block execution: free-tier GPU
availability is not guaranteed; usage limits are dynamic and unpublished;
sessions may terminate unexpectedly; free sessions may have shorter runtime or
lower priority; accelerator availability may vary and a free T4 may not always
be granted; long jobs may be interrupted; and local Colab storage is ephemeral.
Classify the resulting failures rather than treating them as guard defects:

```text
T4/accelerator unavailable            -> provider capacity failure
free-tier usage limit reached         -> provider/quota failure
session terminated unexpectedly       -> infrastructure/session-loss failure
experiment command exits non-zero     -> experiment failure
```

Durable S3/job semantics remain the mitigation for ephemeral compute. Free-tier
execution is not claimed to be reliable.

## Worker and recorded job

```bash
infra provider list
infra doctor
infra worker create --provider colab --gpu T4
infra worker show <exact-owned-session-id>
infra worker health <exact-owned-session-id>
infra worker reconcile <exact-intent-id>
infra job submit <job-spec.json> --worker <exact-owned-session-id> --wait
infra job status <job-id>
infra worker destroy <exact-owned-session-id>
infra worker list --provider colab
```

Creation is confirmed interactively unless `--yes` is deliberate. RUNNING is
not READY: non-SSH bootstrap checks Python, Git, uv, physical NVIDIA GPU model,
PyTorch CUDA, scratch disk and GitHub reachability. `infra worker stop/start`
remain unsupported. Worker release requires locally tracked confirmed ownership,
provider-confirmed exact identity, and confirmation (or `--yes`). Destroy never
targets a prefix, an arbitrary user session, or a network volume.

### An abandoned allocation intent

Only one infra-owned allocation exists at a time, so a single unresolved create
intent blocks every later Colab allocation. That is deliberate: the intent is
written before the billable request precisely so a lost response cannot become a
second paid session, and a missing provider read is never treated as proof that
the allocation never happened.

The consequence is that a create whose session never appeared leaves an intent
that nothing retires on its own — `worker list` skips a pending record it cannot
see at the provider, and `worker destroy`/`bootstrap`/`show` refuse a record with
no confirmed ownership. The supported recovery is one explicit, bounded
operation on that exact identity:

```bash
infra worker reconcile <exact-intent-id>
```

It reads the provider and changes only local bookkeeping; it never creates,
starts, stops or destroys anything. It refuses unless **all** of these hold:

- the record is this controller's own Colab allocation, by exact infra identity;
- the intent is still unresolved (`create_pending`);
- it is at least one hour old — far longer than any create round trip, so it
  cannot still be an in-flight request;
- consecutive successful `sessions` listings omit the exact identity; and
- those listings agree with each other and with the account's own
  active-assignment count.

Anything else fails closed: an unreadable provider, an identity the provider
still lists, observations that disagree, a younger intent, a record that is not
this controller's allocation, or a confirmed session (which is released with
`infra worker destroy` instead). On success the intent becomes terminal and
provider-absent — the record is kept as history, and a later `worker create
--provider colab` is allowed again. Repeating the command is a no-op.

`infra job submit <spec>` selects an existing compatible READY worker in
configured provider order (Colab, then RunPod); `--provider runpod` restricts
selection. `--worker` pins one exact worker. The CLI never silently provisions
paid compute from a job spec: RunPod needs an explicit offer and human USD/hour
ceiling; create workers first. An existing Colab lease may be reused by ID only
while READY, idle, and — in `PAID_CU` — within the configured balance/rate/
job-timeout CU budget. `max_job_cu` bounds observed CU/hour multiplied by
declared maximum job runtime; it is not a guarantee against external account
usage changes. In `FREE_TIER` the projected paid-CU coverage is not applied: the
job is still gated on ownership, readiness, exactly one owned active assignment,
the requested/acceptable accelerator, and `allow_free_tier`. One Colab job at
a time avoids conflicting work. A research failure, undesirable metric or
uncertain outcome never triggers cross-provider rerun. `infra worker show`
reports the recorded billing mode and observed CU rate.

Both transports use the reviewed `worker/job_runner.py` and Phase 5 transfer
module: detached checkout verifies the full commit and clean tree, required
inputs are SHA-256 verified before launch, outputs go through presigned PUT and
independent controller-side S3 read-back plus streaming digest before success.
MLflow/DagsHub instrumentation stays in wavCSE. `/content` is ephemeral
scratch. The recorded job environment is a deterministic baseline plus declared
variables; because Colab mounts the NVIDIA driver libraries outside the default
loader path (`/usr/lib64-nvidia`), the Colab transport seeds `LD_LIBRARY_PATH`
with that directory so `nvidia-smi` and `torch.cuda` see the allocated
accelerator. A spec may override it. For long runs, an operator may call
`infra storage upload` on an
atomically published worker checkpoint while the detached job continues, or
research code must opt into its own approved in-run publication path. Merely
declaring a terminal job output cannot protect a checkpoint before the job
finishes. Infra does not schedule research-specific checkpoints.

## Local-history risk acceptance

The trusted, single-user controller deliberately accepts plaintext local CLI
history. Upstream [0.7.4 `upload`](https://github.com/googlecolab/google-colab-cli/blob/v0.7.4/src/colab_cli/commands/files.py)
uses Jupyter Contents PUT and records **paths** (`local`, `remote`), not bytes;
[execution.py](https://github.com/googlecolab/google-colab-cli/blob/v0.7.4/src/colab_cli/commands/execution.py)
records all `exec` source **and outputs**. Upstream `history.py` appends JSONL to
`~/.config/colab-cli/history/<session>.jsonl`; `state.py` stores runtime proxy
tokens in `~/.config/colab-cli/sessions.json`, and `common.py` writes
`~/.config/colab-cli/colab.log`. The controller restricts owned CLI directories
to 0700 and sessions/log/infra-history files to 0600. The remote envelope is an
ephemeral upload with restrictive local 0600 mode, schema/session/job/expiry
checks, one-time remote ingestion and deletion; the `exec` launcher contains no
presigned URL or secret. File-operation paths remain in history. Keep a short
operator-defined troubleshooting retention window; inspect and remove **only**
identifiable old infra-owned history manually after its capabilities have
expired. Do not broadly delete Google state. `ssh --proxy-mode` is not used:
it can silently allocate a missing session. Never pass capabilities in `--env`,
source code, normal logs, job records or Git.

If ADC is unavailable or requires interactive login, stop before allocation.
If a session disappears before/during execution, treat it as infrastructure
loss, not evidence of a failed scientific command. Terminal outputs count as
durable only after S3 read-back. Read [Security](SECURITY.md) and
[ADR-030](DECISIONS.md) for the narrowly accepted controller-history exception.

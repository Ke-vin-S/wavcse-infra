# Google Colab execution

`google-colab-cli==0.7.4` is pinned on the trusted controller. Colab is a
first-class **ephemeral** execution provider: allocate, observe actual account
CU usage and physical GPU, enforce cost policy, bootstrap to READY, run one or
more compatible exact-commit jobs, persist outputs, release. There is no
resumable stop, network-volume cache, public-IP or SSH requirement.

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
minimum_balance_cu = 5
max_incremental_rate_cu_per_hour = 3
max_job_cu = 10

[placement]
preferred_providers = ["colab", "runpod"]
```

Values are operator-owned non-secret limits, **not** provider prices. Defaults
in the example are illustrative and should be set to the operator's CU budget.
Only one infra-owned allocation is permitted. CLI 0.7.4 `usage` prints account
balance, aggregate rate (two decimal places) and active-assignment count; the
controller parses all three. On creation it records a unique `wavcse-` identity
before calling `new`, then compares `usage_after - usage_before`, requiring the
assignment count to increase by exactly one and the incremental CU/hour to be
positive and within policy. If a confirmed owned session fails cost or bootstrap
checks, it is released and marked absent only after provider confirmation.
Ambiguous creation is reconciled by exact identity and never blindly repeated.
Account observations are rounded; a changing unrelated session makes attribution
unsafe and causes rejection. **The post-allocation guard may consume a small
amount of CU before release.** No invented USD/hour conversion is made.
RunPod retains its USD/hour ceiling, SSH, storage and lifecycle semantics.

## Worker and recorded job

```bash
infra provider list
infra doctor
infra worker create --provider colab --gpu T4
infra worker show <exact-owned-session-id>
infra worker health <exact-owned-session-id>
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

`infra job submit <spec>` selects an existing compatible READY worker in
configured provider order (Colab, then RunPod); `--provider runpod` restricts
selection. `--worker` pins one exact worker. The CLI never silently provisions
paid compute from a job spec: RunPod needs an explicit offer and human USD/hour
ceiling; create workers first. An existing Colab lease may be reused by ID only
while READY, idle, and within the configured balance/rate/job-timeout CU budget.
`max_job_cu` bounds observed CU/hour multiplied by declared maximum job runtime;
it is not a guarantee against external account usage changes. One Colab job at
a time avoids conflicting work. A research failure, undesirable metric or
uncertain outcome never triggers cross-provider rerun.

Both transports use the reviewed `worker/job_runner.py` and Phase 5 transfer
module: detached checkout verifies the full commit and clean tree, required
inputs are SHA-256 verified before launch, outputs go through presigned PUT and
independent controller-side S3 read-back plus streaming digest before success.
MLflow/DagsHub instrumentation stays in wavCSE. `/content` is ephemeral
scratch. For long runs, an operator may call `infra storage upload` on an
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

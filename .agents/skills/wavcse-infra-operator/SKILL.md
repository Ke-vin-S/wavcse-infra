---
name: wavcse-infra-operator
description: Operate the wavCSE control plane with the infra CLI: reconcile provider state, run worker and volume lifecycle, and submit exact-commit jobs safely.
---

# wavCSE Infrastructure Operator

`wavcse-infra` is the control plane for reproducible wavCSE runs: a persistent but stoppable EC2
controller, disposable RunPod GPU workers, GitHub for code distribution, and S3 for canonical large
artifacts. Research code, experiments, tests, and MLflow reporting stay in the separate `wavCSE`
repository.

Act through the `infra` CLI. Every operation below already carries cost guards, identity safeguards,
bounded retries, and provenance. Reimplementing it with raw `ssh`, `aws`, or provider HTTP calls
discards those properties and is a defect.

## Autonomous callers

An automated caller driving this CLI from the wavCSE checkout relies on the contract in
`docs/OPERATIONS.md` ("Autonomous callers"): it owns what it creates, supplies its own
price ceiling, declares verified inputs and outputs, reconciles an ambiguous outcome
before retrying, and is the reaper — nothing here expires or cleans up on its own. When
a failure needs infrastructure judgement rather than a CLI call, expect to be asked
about one scope at a time; the research side never changes directory to ask.

## Controller and worker responsibility

- Controller: the authoritative writable `wavCSE` clone, Git commits and pushes, cloud credentials,
  the `infra` CLI, job submission and reconciliation. OMP runs here.
- Workers: disposable execution only. A worker clones `wavCSE`, checks out an explicit
  already-pushed commit, materializes inputs, runs one command, and can be destroyed.
- The flow is one-way: edit on the controller, test, commit, push, then let the worker fetch the
  exact pushed commit. A worker must never hold the only copy of a change, and it never holds a push
  credential because its detached checkout comes from an anonymous `https://` remote.
- Configure a Git commit identity only where commits are deliberately intended, namely the
  controller. Never build a commit-and-push path on a worker, and never use a worker tree to resolve
  uncommitted changes.
- Keep research logic in `wavCSE` and infrastructure logic in `wavcse-infra`.

## Reconcile before you mutate

Provider state is authoritative; local state beneath the controller state directory is non-secret
convenience. Never act on remembered worker IDs, IPs, ports, prices, branch names, job outcomes, or
current availability.

```
infra config validate      # parses configuration; no network calls
infra doctor               # controller, credential source, and connectivity checks
infra worker list          # reconciles tracked workers against the provider
infra worker show <worker-id>
infra volume list          # also warns about unresolved create intents
```

`infra doctor` reports the loaded configuration path, AWS region, S3 bucket, and worker SSH key
before checking tools, credential resolution, endpoints, instance-profile identity, and S3 access.
It is read-only; required failures exit 1 and invalid TOML exits 2. Global `--config`,
`--runpod-api-url`, `--runpod-timeout`, and `--verbose` options must appear before the command name.

## Worker lifecycle

Creation is paid. Discover an exact offer with `infra worker gpu-types` first; GPU choice and price
ceiling policy are the `gpu-research-operator` skill's subject.

```
infra worker create --gpu '<exact-gpu-type-id>' --cloud '<cloud-tier>' \
  --image '<reviewed-image>' --container-disk <gb> --volume <gb> \
  --start-ssh --require-direct-ssh --max-price '<usd-per-hour>'
```

Exactly one of `--image` or `--template` is required. `--max-price` compares the printed total GPU
price for `--gpu-count` before the billable request; it is never bypassed by `--yes`, and an absent
or unusable price refuses the create. Read the whole printed plan and confirm it. The CLI refuses
unconfirmed (`NONE`/`UNKNOWN`) availability, rejects `--interruptible` rather than silently
falling back to on-demand capacity, and never substitutes another GPU, cloud, count, or data
center. Automation needs direct SSH: `--start-ssh` plus `--require-direct-ssh`, with the public
half of the configured worker key registered in the RunPod account.

Then walk the ladder explicitly:

```
infra worker wait-ssh <worker-id>
infra worker bootstrap <worker-id>   # idempotent; requires READY health
infra worker health <worker-id>      # read-only inspection
```

Provider status and local readiness differ. Provider states are `PROVISIONING`, `STARTING`,
`RUNNING`, `STOPPING`, `STOPPED`, `TERMINATING`, `DESTROYED`, `ERROR`, and `UNKNOWN`. Local
readiness climbs `NOT_READY` -> `SSH_READY` -> `BOOTSTRAPPED` -> `GPU_HEALTHY` -> `READY`, or
records `FAILED`. Provider `RUNNING` does not mean `READY`: bootstrap proves non-interactive
execution and requires the version marker, Git, Python, uv, usable execution storage, any requested
volume mount, and a healthy NVIDIA GPU. Rerunning bootstrap is the recovery path after a partial
failure.

Steady state and teardown:

```
infra worker stop <worker-id>        # retains the Pod
infra worker start <worker-id>
infra worker destroy <worker-id>     # exact provider ID; confirmation unless --yes
```

`stop` stops compute, but retained host-local volume storage and network-volume storage can keep
billing, and the container disk is erased. `destroy` never accepts a name or prefix and never
deletes a network volume. Humans use `infra worker ssh <worker-id>` (PTY, may fall back to the
RunPod proxy); automation uses `infra worker exec <worker-id> -- <command> [args...]`, which
requires the mapped direct endpoint and disables PTY allocation.

## Exact-commit jobs

One versioned job specification drives one attempt on one explicit `READY` worker. The worker must
prove the requested source, commit, and inputs before the command starts.

Spec fields: `source.repository` (anonymous `https://` only) and `source.commit` (full 40- or
64-character ID; branches, tags, short prefixes, and `HEAD` are rejected); `command.argv` (an
argument vector, never a shell command string, with an optional relative `working_directory` inside
the verified checkout); optional `setup.argv`; `runtime.timeout_seconds`, non-secret
`runtime.environment`, `runtime.environment_secrets`; `inputs[]` (`artifact`, `destination`, and a
`manifest` or `sha256`/`size_bytes`); `outputs[]` (`path`, `artifact`, `required`, `overwrite`);
`tracking.metadata`. Unknown fields, duplicates, absolute or `..` paths, reserved variable names,
and bearer values are rejected before any worker is contacted.

```
infra job submit <job-spec.json> --worker <exact-worker-id> [--wait --wait-timeout <s>]
infra job status <job-id> [--json]
infra job logs <job-id> [--tail-bytes <n>] [--local]
infra job cancel <job-id>
```

Job states are `PENDING`, `PREPARING`, `RUNNING`, `SUCCEEDED`, `FAILED`, and `CANCELLED`.
`SUCCEEDED` requires exit code 0 and every required declared output persisted and size-verified at
the controller. A scientific failure stays `FAILED` with its exit code, stage, timeout flag, logs,
and provenance; optional outputs are still persisted for debugging.

`PREPARING` is a real, resumable phase, not a waypoint. When the record carries
`reconciliation_required`, its preparation phase is one of `installing_runner`, `preparing_source`,
`materializing_inputs`, or `starting_command`. Reconcile it with `infra job status <job-id>`: a
download that finished after the controller stopped watching is verified and reused, a running one
is reported and never duplicated, a dead one resumes from the ranges recorded on the worker, and a
command that may already have been launched is never launched again. `FAILED` always means the
system has evidence; uncertainty keeps the job reconcilable instead. A stopped or restarting worker
is unreachable rather than failed: start it and run `infra job status` again.

Terminal states are frozen and one job is one attempt. Never resubmit or rerun a failed job ID; a
retry is a new job ID, because repeating a submission duplicates the transfer or the run.
`infra job cancel <job-id>` is the deliberate way to end a job: it terminates only that job's own
verified process group, never the worker, and it refuses an unverifiable process identity instead
of signalling it.

## Where data may live

- S3, under the configured bucket and prefix, is canonical.
- A RunPod network volume is a rebuildable cache, never the only copy of anything. It exists in
  exactly one data center, attaches only to Secure Cloud Pods, and must be attached at Pod creation;
  it cannot be attached later.
- Container disk and the job workspace are scratch. A job directory holds `source/`, `inputs/`,
  `outputs/`, `logs/`, and `state/`; `/workspace` existing is not evidence that storage is mounted.
- The job framework owns input materialization and output persistence. Declare `inputs[]` and
  `outputs[]` instead of hand-rolling transfers inside the command.

Artifact identity, manifests, transfer, cache inspection and verification internals, and the
`infra storage` and `infra volume cache` commands: use the `wavcse-artifact-pipeline` skill.

## Secrets

- The RunPod token comes from a non-empty `RUNPOD_API_KEY`, or on the controller from the SSM
  `SecureString` named by `runpod.api_key_parameter` read through the instance profile. It is never
  taken from TOML or from a CLI option.
- Job secret values are named in `runtime.environment_secrets` and resolved from the submitting
  shell; the values are never persisted in the specification or the record.
- Workers never get long-lived AWS access keys, the RunPod key, the controller's worker SSH private
  key, or a GitHub push credential; they receive only time-limited, single-purpose presigned URLs.
- Never print, log, or paste a presigned URL, an Authorization header, a private key, or a
  credential value, and never dump the environment. Output is redacted, but do not rely on that;
  treat a URL as a secret until it expires.

## Destructive operations

- Require an explicit resource identifier plus interactive confirmation, unless `--yes` is
  deliberately supplied for non-interactive automation.
- `--yes` bypasses only the confirmation. It never bypasses validation, the maximum-price guard,
  availability checks, or the exact-ID requirement.
- Names and prefixes are not identities: `worker destroy` and `volume destroy` take an exact
  provider ID, and an already-absent ID is reported as absent rather than resolved to some other
  resource.
- `volume destroy` never touches a Pod, and `worker destroy` never touches a volume.
- `infra volume forget <infra-identity>` removes local bookkeeping only and changes nothing at the
  provider.
- Cleanup may only destroy resources this tool created and tracks. Never delete a resource because
  its name looks similar.

## Failures, retries, and cost

- Safe reads (provider GETs, SSH readiness polls) retry within a configured bound. Mutations
  (create, start, stop, destroy, cancel) are never automatically retried.
- Transient failures are transport timeouts, throttling, HTTP 5xx/429, connection refusal, and an
  endpoint that is simply not ready yet. Hard failures are authentication failure, host-key
  mismatch, a terminal provider state, a validation error, and a rejected exact identifier. Fix the
  named cause instead of retrying a hard failure.
- A retry must never duplicate a paid resource. Creates are issued once and reconciled by the
  generated `wavcse-...` identity; after an ambiguous outcome run `infra worker list` or
  `infra volume list` and inspect the printed identity before creating anything again. While a
  volume create intent is unresolved, further volume creates are refused.
- Make cost visible: state the provider-reported total hourly price and the storage configuration
  before creating anything, keep the printed plan, and never silently fall back to a more expensive
  resource or provider.
- Never change the model, checkpoint, pooling, layer set, dataset membership, splits, preprocessing,
  label mapping, or precision to make a run faster or cheaper. If the cheaper resource cannot
  satisfy the requirement, report that instead.

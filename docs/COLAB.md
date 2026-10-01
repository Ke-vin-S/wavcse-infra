# Google Colab provider boundary

Colab CLI 0.7.4 is the validated controller-side command. `controller/bootstrap.sh`
installs it without performing OAuth or allocating a runtime. Colab is disabled by
default (`[colab] enabled = false`). When enabled, `infra doctor` checks the
pinned CLI version and read-only session access; `infra worker list --provider colab`
reads and normalizes session state. `infra worker show <tracked-session-name>`
requires an exact locally tracked identity. A Colab session is ephemeral; RunPod
retains its existing SSH, stop/start, persistent-volume, and price semantics.

## One-time authentication

An operator, not an automated job, runs on the controller:

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
```

`gcloud` is an operator-installed prerequisite, not bundled with
`google-colab-cli`. If `gcloud --version` is unavailable, install the
[official Google Cloud CLI](https://cloud.google.com/sdk/docs/install)
on the controller before minting ADC. Bootstrap never starts this login.

The validated CLI defaults to **oauth2**, so every invocation from this project
passes `--auth=adc` explicitly. To inspect authentication without allocating
compute, run `colab --auth=adc sessions`. The CLI's own local session metadata
includes runtime proxy tokens; never copy it to a worker or commit it. `colab
auth` injects Google credentials into the runtime and is not used. Google Drive
is not canonical storage: S3 remains the authority for artifact bytes.

Non-secret configuration fields in `config/infra.example.toml` are `enabled`,
`cli`, `command_timeout_seconds`, and `lifecycle_timeout_seconds`; the
matching `WAVCSE_INFRA_COLAB_*` environment settings follow CLI >
environment > user TOML > defaults. No Google access or refresh credential
belongs in TOML, environment overrides, job records, or worker state.

## Current safety gates

Colab **allocation and recorded execution are not enabled** in this change.
The CLI's `usage` command provides account-level compute units but no
provider-observed price for a selected accelerator *before* allocation. The
existing cost invariant refuses an absent price: `infra worker create --provider
colab --gpu T4` reports usage, then rejects without calling `colab new`, even
with `--max-price` and `--yes`. No price is guessed and no paid session is
silently created.

Colab CLI 0.7.4's `exec` implementation writes the complete executed source
**and outputs** into `$HOME/.config/colab-cli/history/<session>.jsonl`; its
`--env` option is written to that history too. Neither a presigned S3 URL nor a
job secret may pass through this path. Its `ssh --proxy-mode -s NAME` command
automatically creates a missing named runtime, including during a disappearance
race; it cannot safely replace the secret-carrying direct SSH transport.
Consequently `infra job submit`, `infra storage download/upload`, and arbitrary
`infra worker exec` reject tracked Colab identities instead of recording a
weaker experiment or leaking a bearer URL. A Colab worker is **not READY** for
recorded jobs. No Colab input/output may be called durable until the controller
independently reads the canonical S3 object back and hashes it.

`infra worker stop` means resumable compute and rejects Colab; upstream `colab
stop` is terminal release. `infra worker destroy` recognizes only a tracked,
confirmed infra-owned exact Colab session identity, prints the target and
requires confirmation unless `--yes`, invokes terminal release once, then
marks it absent only after provider session listing confirms that outcome. It
never releases a session merely because its name resembles `wavcse-*`. Colab
network volumes, stop/resume, and direct SSH are not emulated.

A future change needs **both** (1) an approved, enforceable compute-unit cost
policy or provider-observed price and (2) a verified execution channel that
does not persist presigned URLs or job secrets or auto-create sessions on
reconnect. Only then can the existing exact-commit job runner and S3
publication checks be reused; do not create a second, weaker job system. A
university-managed static SSH worker does not need provisioning or destroy;
provider identity and execution transport are separate so this boundary can
be added without changing the job specification.

## Provider failure versus experiment failure

CLI missing, ADC unavailable/expired, malformed session response, missing
session, quota exhausted, accelerator unavailable, and ambiguous allocation
are provider errors. A training command's nonzero exit is a job result, not
provider capacity evidence and not a reason to select another provider. All
ordinary tests mock subprocesses; they allocate no GPU or compute units.

Official source reviewed: [google-colab-cli v0.7.4](https://github.com/googlecolab/google-colab-cli/tree/v0.7.4),
notably `src/colab_cli/commands/execution.py` (history),
`src/colab_cli/commands/ssh.py` (auto-allocation), and
`src/colab_cli/commands/session.py` (session output and allocation).

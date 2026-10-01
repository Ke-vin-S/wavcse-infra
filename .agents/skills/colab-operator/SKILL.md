---
name: colab-operator
description: Operate the Google Colab execution provider through wavcse-infra: ADC, CU usage, ephemeral allocation, bootstrap readiness, uploaded-envelope execution, and ownership-guarded release.
---

# Colab operator

Operate through `infra`, not raw provider calls. Read `docs/COLAB.md` for the
exact CLI 0.7.4 account, history and ephemeral-session contract. Never allocate
compute in diagnostics or CI. `infra provider list`, `infra doctor` and
`infra worker list --provider colab` are read-only provider observations.

- ADC is a one-time human controller action. If absent or expired, stop and report:

  ```bash
  gcloud auth application-default login \
    --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
  ```

- Every invocation uses `--auth=adc`; upstream defaults to interactive OAuth.
  Never call `colab auth` or copy ADC/session credentials to the runtime.
- Check account balance and aggregate CU/hour before requesting a T4 or another
  explicitly required GPU. The CLI's `Current balance` is the account's
  `paidComputeUnitsBalance`: `> 0` selects `PAID_CU`, `0` selects best-effort
  `FREE_TIER` (permitted by `colab.allow_free_tier`), and zero does **not** mean
  no compute entitlement. `infra worker create --provider colab --gpu T4`
  prints the mode and requires confirmation unless `--yes`. Creation claims one
  unique infra identity before issuing a billable request and measures account
  usage after allocation. In `PAID_CU` it enforces the configured minimum
  balance, incremental CU/hour ceiling and projected job CU; the guard is
  post-allocation and may consume a small amount of CU before a rejected owned
  lease is released. In `FREE_TIER` the observed CU/hour is recorded, not gated,
  and the job still requires ownership, readiness, exactly one owned active
  assignment and the requested accelerator. One active infra-owned lease at a time.
- Provider RUNNING is not READY: bootstrap checks Python, Git, uv, physical GPU,
  PyTorch CUDA, disk and network; compare the observed model with the requested
  accelerator. Never install OMP/Codex on a normal worker.
- The same reviewed exact-commit runner and presigned S3 transfer module execute
  through an uploaded short-lived job envelope and a non-secret fixed launcher.
  CLI 0.7.4 `upload` records file paths, not file contents, in history; `exec`
  records source and outputs. Temporary local envelope is 0600; remote envelope
  is deleted after ingestion. Never put URLs, secrets, or `--env` values in
  `colab exec` source. The trusted controller accepts restricted plaintext CLI
  history; workers still receive no long-lived credentials.
- `/content` is ephemeral scratch. S3 is canonical; required inputs must pass
  digest verification and declared outputs require independent canonical read-back.
  Long jobs must opt into periodic checkpoint publication in wavCSE itself.
- `infra worker stop` and `start` are unsupported: upstream stop is terminal
  release. `infra worker destroy <exact-id>` releases only a locally confirmed
  infra-owned session after confirmation. Never adopt or delete unrelated names.
  Reuse an existing READY owned ID only while healthy, idle and within CU policy;
  release it after a bounded compatible batch.
- Auth, quota, accelerator and session loss are provider failures. A research
  command's nonzero exit is a job failure, not a provider fallback opportunity.
  An interrupted phase remains reconcilable; inspect its job ID instead of
  resubmitting.

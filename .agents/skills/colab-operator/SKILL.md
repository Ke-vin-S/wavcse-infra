---
name: colab-operator
description: Diagnose and operate the Google Colab provider through wavcse-infra while preserving ADC, price, ownership, and exact-commit safeguards.
---

# Colab operator

Use `infra` interfaces first. Do not invoke the Colab CLI directly in normal
research orchestration; `colab --auth=adc sessions` and `usage` are read-only
diagnostics. Consult `docs/COLAB.md` for supported commands and the current
price/secret-transport gates. Do not allocate a runtime in a probe or a test.

- Authentication is a human, one-time controller action. Never trigger an
  interactive login during autonomous work. If ADC is missing, expired, or
  lacks scopes, classify **AUTH_REQUIRED**, stop, and report exactly:

  ```bash
  gcloud auth application-default login \
    --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
  ```

- Always use `--auth=adc`; the pinned 0.7.4 CLI defaults to interactive OAuth.
  Do not run `colab auth -s` or inject controller Google credentials into a VM.
- Inspect account usage/quota before proposing allocation. The CLI does **not**
  report a pre-allocation per-GPU USD/hour price. `infra worker create --provider
  colab` currently refuses without attempting allocation, even with `--yes`.
  Do not bypass that price guard with a raw `colab new` invocation. No provider
  fallback may be inferred from account quota.
- Prefer a compatible **infra-owned**, provider-confirmed idle session when
  policy permits; never adopt another user's assignment from its name alone.
  Once allocation becomes supported, verify actual GPU model/CUDA on the VM
  before marking it READY; provider RUNNING alone is insufficient.
- Colab local disk and kernel state are ephemeral scratch. S3 remains canonical;
  MLflow/DagsHub reporting stays with wavCSE. Checkpoints that matter must be
  published during long runs, not only at process exit; checkpoint policy and
  research contents belong in wavCSE.
- Colab 0.7.4 records **all** `exec` code and output in local history. Never
  deliver job secrets or S3 presigned URLs by `exec` or `--env`. Its SSH proxy
  auto-creates missing sessions; never use it as a secret transport in normal
  orchestration. Recorded Colab jobs cannot currently satisfy the existing
  exact-commit and canonical-output contract and must be refused, not weakened.
- `infra worker stop` is resumable and therefore unsupported for Colab. Only
  `infra worker destroy` releases a proven owned exact session, with explicit
  confirmation (or deliberate `--yes`). Never destroy an untracked session.
  Release owned sessions after work unless explicitly retained, once canonical
  results have been independently verified.
- Distinguish CLI/auth/capacity/quota/session loss (**provider failure**) from
  a verified experiment command exit (**job failure**). Never silently retry a
  failed experiment on another provider.

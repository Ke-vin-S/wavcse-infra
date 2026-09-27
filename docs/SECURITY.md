# Security

## Trust boundaries

The controller, GitHub source, private S3 bucket, and MLflow/DagsHub service are trusted
for their documented roles. A RunPod worker is a temporary execution environment and is
not trusted with durable credentials or the only copy of important data.

## Credentials

### AWS

The controller uses an attached EC2 instance profile. Boto3 discovers and refreshes the
temporary role credentials through its standard credential chain; the project does not
accept or install static AWS access keys. `infra doctor` requires Boto3's resolved
credential method to be `iam-role` before making STS or S3 calls, so an accidentally
exported static key is reported rather than used for the diagnostic.

The role should grant only required actions for the configured bucket and `wavcse/`
prefix. Phase 1 diagnostics use STS `GetCallerIdentity` and, when a bucket is configured,
a bounded S3 prefix listing. Later storage phases will require narrowly scoped
`GetObject`, `PutObject`, and `HeadObject` permissions.

### RunPod

Set `RUNPOD_API_KEY` in the controller environment or an external secret facility. The
key is rejected from the TOML configuration and never accepted as a CLI option, avoiding
committed secrets and process-list exposure. HTTP authorization headers and provider
errors pass through redaction before user display.

### SSH

Future worker access will use a dedicated key. Private keys remain on the controller.
Host-key handling must be explicit; globally disabling strict host-key checking is not
allowed. RunPod basic proxied SSH and full public-IP SSH are separate endpoint types.

## Presigned URLs

Future S3 transfers will use Signature Version 4 URLs scoped to one object and method.
URLs are bearer credentials until expiry, can generally be reused during that period,
and can expire earlier when the EC2 role session rotates. They must not appear in logs or
state files. Upload flows must avoid accidental replacement and verify checksums and
durability before worker deletion.

## Logging and redaction

Never log:

- authorization headers or API tokens;
- AWS access keys, secret keys, or session tokens;
- private SSH keys;
- full presigned URLs or query strings;
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

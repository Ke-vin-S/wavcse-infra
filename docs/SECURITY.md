# Security

## Trust boundaries

The controller, GitHub source, private S3 bucket, and MLflow/DagsHub service are trusted
for their documented roles. A RunPod worker is a temporary execution environment and is
not trusted with durable credentials or the only copy of important data.

## Credentials

### Configuration files

`config/infra.example.toml` and `~/.config/wavcse-infra/config.toml` contain only
non-secret configuration. Bootstrap copies the example once with user-only permissions
and refuses to replace an existing file. `.env.example` lists supported variables but
contains no values and is not loaded automatically. Real tokens remain in the process
environment or an explicitly managed external secret facility and are never committed.

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
committed secrets and process-list exposure. HTTP authorization headers are never
rendered. Provider failures expose only the operation and safe status context;
user-facing errors pass through redaction before display.

### SSH

Future worker access will use a dedicated key. Private keys remain on the controller.
Host-key handling must be explicit; globally disabling strict host-key checking is not
allowed. RunPod basic proxied SSH and full public-IP SSH are separate endpoint types.

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
- OMP or Codex authentication stores and tokens;
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

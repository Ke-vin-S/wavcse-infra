# Operations

## Configuration

Create the user configuration:

```bash
mkdir -p ~/.config/wavcse-infra
cp config/infra.example.toml ~/.config/wavcse-infra/config.toml
```

Supported environment variables:

| Variable | Purpose |
| --- | --- |
| `RUNPOD_API_KEY` | RunPod bearer token; environment only |
| `WAVCSE_INFRA_AWS_REGION` | Region for STS and S3 diagnostics |
| `WAVCSE_INFRA_CONFIG` | Alternate user TOML path |
| `WAVCSE_INFRA_RUNPOD_API_URL` | RunPod REST base URL |
| `WAVCSE_INFRA_RUNPOD_TIMEOUT_SECONDS` | Per-request timeout |
| `WAVCSE_INFRA_RUNPOD_READ_ATTEMPTS` | Total safe read attempts |
| `WAVCSE_INFRA_RUNPOD_RETRY_BACKOFF_SECONDS` | Initial retry delay |
| `WAVCSE_INFRA_S3_BUCKET` | Private canonical artifact bucket |
| `WAVCSE_INFRA_S3_PREFIX` | Bucket prefix, default `wavcse` |
| `WAVCSE_INFRA_WAVCSE_PATH` | Controller wavCSE checkout |
| `WAVCSE_INFRA_SSH_PRIVATE_KEY` | Dedicated worker key path |
| `WAVCSE_INFRA_EXPECT_OMP` | Whether doctor requires `omp` |
| `WAVCSE_INFRA_MLFLOW_URL` | Optional MLflow health endpoint |

Empty values are treated as unset. CLI options override environment values. Validate the
result without network calls:

```bash
infra config validate
```

## Initial controller setup

Prerequisites outside this repository:

1. Launch a supported Ubuntu EC2 instance.
2. Attach an instance profile with least-privilege access to the private artifact
   bucket/prefix. Do not create local static AWS credentials.
3. Configure controller SSH access and host security through normal AWS operations.
4. Apply `controller/cloud-init.yaml` as user data, or clone this repository and run
   `controller/bootstrap.sh` manually.
5. Complete user-specific GitHub, RunPod, DagsHub/MLflow, and OMP authentication.
6. Clone the separate wavCSE repository under `~/projects/wavCSE`.
7. Configure `~/.config/wavcse-infra/config.toml` and run `infra doctor`.

Bootstrap installs controller prerequisites and the locked Python project. It is
idempotent and safe to rerun. It does not install OMP, inject secrets, or provision
cloud resources.

## Routine read-only operations

```bash
infra doctor
```

RunPod worker reads become available in Phase 2:

```bash
infra worker list
infra worker show <worker-id>
```

`infra doctor` checks local tools, Python version, OMP policy, the wavCSE path, optional
SSH key, RunPod credential presence, network endpoints, EC2 instance-profile identity,
and the configured S3 prefix. Required failures produce exit 1; invalid configuration
produces exit 2. Optional unconfigured checks are reported as warnings or skips.

Phase 2 `worker list` and `worker show` call only documented GET endpoints. They do not
change provider state.

## Controller reconstruction

1. Recreate an Ubuntu EC2 instance and attach the existing scoped instance profile.
2. Apply the thin cloud-init or clone `wavcse-infra` and run bootstrap.
3. Restore user-managed authentication from its authoritative secret systems.
4. Clone `wavCSE` and check out the required development branch.
5. Restore non-secret user configuration.
6. Run `infra doctor`, then reconcile RunPod state with `infra worker list`.

The recovery process does not copy state from a worker. Code comes from GitHub, large
artifacts from S3, and experiment metadata from MLflow/DagsHub.

## Failure handling

- RunPod 401/403: verify `RUNPOD_API_KEY` in the current process; do not print it.
- RunPod 404 on `worker show`: verify the immutable provider worker ID and account.
- RunPod 429/5xx or transport failure: safe reads retry within the configured bound.
- AWS identity failure: verify an instance profile is attached and IMDS access is not
  blocked. Do not work around it by creating permanent access keys.
- S3 failure: verify region, bucket, prefix, and role policy separately.
- Tool check failure: rerun bootstrap, then `make check`.

No automated cleanup is present in Phases 0–2, and no paid resource is created.

## Bootstrap implementation notes

- Supported target: Ubuntu with the normal `apt` repositories and either a non-root
  invoking user, `SUDO_USER`, or the standard `ubuntu` account.
- `WAVCSE_INFRA_CONTROLLER_USER` explicitly selects the target account when needed.
- `WAVCSE_INFRA_UV_VERSION` can override the documented pinned uv version for a
  controlled upgrade.
- uv is installed into the target user's `~/.local/bin` without modifying shell files.
- Python 3.12 and the exact `uv.lock` environment are synchronized on every run.
- OMP, credentials, and user-specific external authentication remain explicit
  post-bootstrap steps.

The cloud-init file assumes the default Ubuntu account and the public canonical
repository URL. Customize those two non-secret values in an EC2 launch template when
necessary. It intentionally does not update an existing checkout, preventing first-boot
automation from overwriting controller work.

## Official operational references

- [AWS: IAM roles for Amazon EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html)
- [Boto3 credential provider chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html)
- [cloud-init boot stages](https://cloudinit.readthedocs.io/en/latest/explanation/boot.html)
- [cloud-init module reference](https://cloudinit.readthedocs.io/en/latest/reference/modules.html)
- [uv installation](https://docs.astral.sh/uv/getting-started/installation/)
- [uv installer configuration](https://docs.astral.sh/uv/reference/installer/)

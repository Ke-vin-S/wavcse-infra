# RunPod provider notes

## Selected API

Phases 0–2 use the stable REST API v1 base URL:

```text
https://rest.runpod.io/v1
```

Implemented Phase 2 operations:

```text
GET /pods
GET /pods/{podId}
```

Authentication is an `Authorization: Bearer <token>` header. At client construction,
the credential resolver prefers a non-empty `RUNPOD_API_KEY`; otherwise it decrypts the
SSM `SecureString` named by `runpod.api_key_parameter`. SSM is read only once for that
client lifetime. The value must never be logged or persisted.

RunPod also publishes REST API v2 under `https://api.runpod.io/v2`. As of
2026-09-27 the provider labels v2 public beta and warns that its endpoints and behavior
may change before general availability. [ADR-008](DECISIONS.md#adr-008-use-stable-runpod-rest-api-v1-while-rest-api-v2-is-beta)
records the decision to keep the production read path on v1 until a deliberate review.

## Normalization

The provider returns `desiredStatus` with documented values `RUNNING`, `EXITED`, and
`TERMINATED`. The application maps these to `RUNNING`, `STOPPED`, and `DESTROYED`
while retaining the native value. Unknown or missing statuses normalize to `UNKNOWN`.

Where available, the internal worker view includes provider ID, name, GPU display name
and count, effective/base hourly cost, public IP, mapped SSH port, datacenter, image,
interruptibility, and last-started timestamp. Missing fields remain absent rather than
being inferred. Both reads request the documented `includeMachine=true` expansion so
machine and datacenter fields are available when RunPod has assigned them.

The v1 sample schema represents `costPerHr` as a string and `adjustedCostPerHr` as a
number. Normalization accepts either numeric representation and uses decimal values to
avoid binary floating-point cost artifacts.

## Read retries

Only safe GET operations retry. Retryable conditions are transport/timeouts, HTTP 429,
and HTTP 5xx. Attempts are bounded, use exponential backoff, and cap each delay at 30
seconds. Authentication, not found, redirects, validation, and other 4xx failures are
not retried.

Resource creation will require a separate conservative design because retrying after an
ambiguous create response can duplicate paid Pods.

## SSH behavior

RunPod documents two key-authenticated paths:

- Basic SSH is proxied through RunPod and does not support SCP or SFTP.
- Full SSH requires a machine with public-IP support, TCP port 22 exposed, a running
  SSH daemon, and the provider-mapped external port.

The Phase 2 API reports `publicIp` and `portMappings["22"]` when present; that is not
equivalent to a successful SSH health check. SSH readiness belongs to a later phase.
Artifact transfer must not depend on SCP.

## Current limitations

- No create/start/stop/destroy calls.
- No live API call in CI or unit tests.
- No SSH endpoint validation or worker health checks.
- No local reconciliation state.
- No price/capacity selection.

## Official references

- [REST API v1 overview](https://docs.runpod.io/api-reference/overview)
- [List Pods](https://docs.runpod.io/api-reference/pods/GET/pods)
- [Find a Pod by ID](https://docs.runpod.io/api-reference/pods/GET/pods/podId)
- [Connect to a Pod with SSH](https://docs.runpod.io/pods/configuration/use-ssh)
- [REST API v2 announcement](https://www.runpod.io/blog/runpods-rest-api-v2-is-here-one-api-for-your-entire-gpu-stack)

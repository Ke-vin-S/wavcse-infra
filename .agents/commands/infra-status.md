---
description: Reconcile controller, provider, and local state read-only and report disagreements.
---

# Status and reconciliation

Produce one read-only reconciliation report: what the controller can reach, what the
provider reports, what local records claim, and where those three disagree. Change
nothing while investigating.

Skills: wavcse-infra-operator
Boundaries: read-only, no-commit, no-paid-compute

## Sequence

1. Controller health — `infra doctor`. Checks the active config file, AWS region, S3
   bucket setting, SSH private key, Git, Python, uv, tmux, OMP, Codex, AGF, the wavCSE
   checkout, RunPod credential resolution, RunPod connectivity, AWS identity, S3 access,
   and MLflow connectivity; failed required checks exit non-zero. Record failures and
   name the failing check — repair is a separate, authorized task.
   `infra config validate` parses configuration without contacting any service.
2. Provider-visible workers — `infra worker list --read-only [--json]`, then
   `infra worker show <exact-worker-id> --read-only [--json]` for the IDs that matter. Report provider
   state, GPU, price/hour, and locally recorded readiness; provider `RUNNING` is not
   local `READY`.
3. Provider-visible network volumes — `infra volume list --read-only [--json]`,
   `infra volume show <volume-id> --read-only [--json]` (which includes provider-reported incurred
   billing), and `infra volume datacenters [--json]`.
4. Tracked local state — the records under `~/.local/state/wavcse-infra/` (`workers.json`,
   `volumes.json`) are credential-free and supplemental. The `--read-only` provider
   views leave these records untouched.
5. Canonical storage reachability — `infra storage list --prefix <relative-key-prefix>
   --limit <n> [--json]` proves the configured bucket and prefix are readable;
   `infra storage verify <artifact>` confirms one object's existence, size, metadata, and
   manifest consistency without downloading it.
6. Rebuildable cache state (optional) — `infra volume cache stats --worker <worker-id>
   [--wait-timeout <s>] [--command-timeout <s>] [--json]` reports the network-volume
   cache. The cache is never canonical.
7. Disagreements — state each one explicitly, with the command that produced it.

## Disagreements to look for

- Local record with no provider counterpart: reported absent, or `worker show --read-only`
  returned 404. Absence of a local record never means absence of
  a provider resource, and a recorded timestamp is not current truth.
- Provider resource with no local record: a Pod or volume not created by this tool.
- Volume `lifecycle_state` of `PENDING_CREATE` with no matching provider volume — see
  the stderr warning from `infra volume list`. The provider may have created it: inspect
  the provider console before creating anything with that identity.
- Locally recorded readiness, endpoint, disk, or GPU facts that a fresh
  `infra worker health <exact-worker-id>` no longer supports.
- A Pod reported outside its network volume's data center, or without the requested mount.
- Cost that outlives compute: a stopped Pod retains host-local volume storage, and a
  network volume is billed independently of any Pod.

## Report shape

Give the controller, workers, volumes, canonical storage, and disagreements as separate
sections; cite exact identifiers and the command behind each finding; mark the provider
as authoritative and local records as supplemental. State what could not be checked
rather than inferring it.

## Prohibited here

No `infra worker create`, `start`, `stop`, or `destroy`; no `infra volume create`,
`destroy`, or `forget`; no `infra storage download` or `upload`; no `infra job submit` or
`cancel`. Do not print presigned URLs — they are bearer secrets and this report does not
need them.

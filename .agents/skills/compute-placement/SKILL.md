---
name: compute-placement
description: Choose an existing READY execution provider for exact-commit jobs using native cost, compatibility, and failure boundaries.
---

# Compute placement

Use `infra provider list`, `infra doctor`, `infra worker list`, and the user TOML
`[placement] preferred_providers` rather than remembered prices or sessions.
An explicit `--worker` pins the worker; `--provider runpod` or `--provider colab`
restricts selection. Unspecified jobs consider READY infra-owned Colab first,
then READY RunPod. This is a placement preference, not a research assumption.
Future static university compute may precede Colab, but it is not implemented.

Placement does not implicitly provision paid capacity. If no compatible READY worker
exists, create one explicitly under its own guard and then submit. Do not claim
RunPod automatic provisioning: its offer, storage and human USD/hour ceiling
cannot be deduced from a job spec. Never substitute a different accelerator
without checking VRAM and study requirements.

Colab is inexpensive for many ephemeral workloads but bills in native compute
units. Inspect balance, current account CU/hour, maximum incremental rate, and
estimated maximum job CU from observed rate and declared timeout. One active
infra-owned Colab lease at a time makes before/after attribution possible.
A freshly allocated session is charged while bootstrapping and may be terminated
after a small CU charge by the post-allocation rate guard. Reuse only an exact
infra-owned READY session with compatible accelerator, healthy provider state,
no active local job, and acceptable cost; the same ID may run a bounded batch,
then must be released. `/content` is scratch, not storage. Periodic checkpoint
publication for long jobs belongs to research code, not this scheduler.

RunPod retains USD/hour offer and ceiling, explicit cloud tier and capacity,
SSH transport, optional network-volume cache, and resumable Pod lifecycle.
Choose it when Colab auth, quota, accelerator, capacity, or cost policy makes
Colab unavailable, when persistent execution/storage or stable long runtime
matters, or when its measured cost-to-verified-artifact is better. Compare
expected runtime, transfer size, disk, bootstrap amortization and checkpointability;
never fabricate a CU-to-USD conversion.

Fallback before experiment execution may respond to provider auth, quota,
capacity, accelerator, cost rejection, or transport failure. A recorded job
whose command started is not retried elsewhere because of a nonzero exit,
poor metric, undesired scientific result, or uncertain outcome. Reconcile
`PREPARING` and `RUNNING` by job ID, not by resubmitting. An explicit provider
request never falls through to another provider. Never silently duplicate a
paid allocation or a research attempt.

---
name: gpu-research-operator
description: Choose, benchmark, and retire paid GPUs by cost to a verified artifact, not utilization: GPU selection, price ceilings, throughput measurement, bottleneck diagnosis, stopping compute.
---

# GPU Research Operator

Use this skill when choosing which GPU to rent, deciding whether a paid worker is worth keeping,
explaining why a GPU workload is slow, or deciding when to stop paying for compute.

This skill decides economics and capacity. Worker lifecycle, job submission, and CLI flag detail
belong to the `wavcse-infra-operator` skill; artifact identity, transfer, and cache semantics belong
to the `wavcse-artifact-pipeline` skill.

## Objective: cost to a verified artifact

The objective is minimum practical cost-to-verified-artifact:

    time to a correct, verified artifact times the cost of the resources it consumed

It is not maximum GPU utilization. A GPU idling between batches while a correct artifact lands
sooner and cheaper is a success, not a defect. Treat "the GPU is only at 30%" as an observation to
explain, never as a goal by itself.

## Where the policy lives

Read the current configuration instead of remembering it. `config/infra.example.toml` lists every
supported non-secret setting, and the operator's user copy of that file is authoritative at runtime.
`infra <command> --help` is the authority for flags; `docs/RUNPOD.md` and `docs/OPERATIONS.md`
document provider behavior and operating procedures.

Never hardcode a price, ceiling, availability, or capacity into a plan. Discover each at decision
time.

## Choosing capacity by total expected cost

RunPod GPU discovery reports exact type, VRAM, cloud tier, availability,
maximum GPUs per machine and provider list price. Colab instead exposes
account CU balance and aggregate CU/hour; a positive paid balance is `PAID_CU`
and enforces the one-owned-session before/after CU guard, while a zero paid
balance is best-effort `FREE_TIER` (when `colab.allow_free_tier`), whose
observed CU/hour is metering evidence rather than billable cost. Never invent a
USD/hour conversion. Compare candidates
on provider-native cost, expected time and verified artifact outcome:

- RunPod's discovered hourly price, or Colab's observed incremental CU/hour;
- expected wall clock for this workload, projected from a measurement (below), not from a
  specification sheet;
- data-transfer and storage cost that continues after compute stops;
- the value of finishing sooner, which is real but must be justified, not assumed.

Do not pick the cheapest hourly GPU merely because it is cheapest: a slow device on a long job can
cost more in total. Do not pick the most expensive one blindly either: a fast device that a serial,
CPU-bound, or storage-bound pipeline cannot feed simply bills faster. Never assume wall clock scales
with the price ratio or with advertised throughput numbers. A public-IP-compatible offer can be
priced above the unfiltered catalog price, and the price guard compares the filtered value.

## A provider-native ceiling belongs to a human

For RunPod, `--max-price` is the maximum accepted total GPU USD/hour for the
whole request and is checked before billable creation. Always supply it for
an autonomous RunPod create; never widen an authorized ceiling silently.
For Colab, configure minimum balance, maximum incremental CU/hour and
maximum job CU; these are paid-CU policy and apply only while the balance is
positive. The paid rate is observable only after a single owned allocation,
so a rejected allocation may consume a small amount of CU before immediate
release. `colab.allow_free_tier` controls best-effort zero-balance execution.
Never substitute a guessed Colab USD/hour value.

Raise authorized limits only with a human decision; never widen a ceiling,
bypass the guard or create by another route. Billable create and irreversible
destroy safeguards are in `wavcse-infra-operator` and `colab-operator`.

RunPod container scratch is erased on stop, while host-local and network-volume
storage may keep billing. Destroying a Pod never destroys its volume. Colab
scratch is ephemeral and no provider volume is assumed; release its lease after
durable outputs are verified. Cache cleanup belongs to `wavcse-artifact-pipeline`.

## Measure before scaling

Benchmark the real workload before committing many GPU-hours:

- measure per-sample (or per-unit) throughput on representative samples of the actual pipeline, then
  project total time as remaining units divided by the measured rate;
- use the exact scientific configuration and the real data shape;
- do not extrapolate from an unrelated workload, a synthetic tensor benchmark, or a different
  dataset;
- a few minutes of steady state is normally enough; if the job would finish in minutes anyway, run
  it instead of tuning it;
- one sample is not a guarantee. Repeat before drawing a conclusion.

Measured throughput belongs to a specific machine, provider path, and dataset. It is evidence for
the run in front of you, not a constant to reuse. For the transfer path, `docs/OPERATIONS.md`
records a controlled download benchmark that compares the default concurrency with a single
connection on an existing `READY` worker and requires identical size and digest from both runs;
never create a worker to run a benchmark.

## Diagnose the bottleneck

Collect cheap evidence before changing anything:

- GPU utilization and VRAM during the run, against the `nvidia-smi` facts the readiness health check
  reports for the worker;
- load average against the worker's core count;
- per-process CPU and thread counts from a worker-side inspection (see the `wavcse-infra-operator`
  skill for worker access);
- wall clock per unit of work, from the job log;
- disk free space on the worker filesystem and on the cache volume.

Classify the limitation before choosing a fix:

- GPU-bound: utilization stays high and progress tracks GPU work.
- CPU decode: GPU repeatedly idles while CPU is saturated; progress is limited by data preparation.
- Storage latency: long stalls aligned with reads, low CPU and low GPU together, especially when
  many small files are involved.
- Python/IO orchestration: one process busy with serialization, uploads, or per-sample file
  operations while the accelerator waits.

Choose the smallest fix for the classification. Do not tune by guessing from a utilization
percentage alone.

## Two failure modes in this project

Both were observed on this project's workloads; no in-repo measurement record of either was located
when this skill was written `[UNVERIFIED]`.

### Thread oversubscription

Each work process can start a large default CPU thread pool. Running several such processes
multiplies that pool, so cores thrash on context switches and throughput can fall as concurrency
rises. Symptoms: load average far above the core count, high system time, and flat or falling
per-unit throughput as concurrency is increased.

Fix by making the arithmetic explicit: bound each process's thread pool to the cores it actually
needs so total threads stay at or below available cores, and choose the lowest concurrency near
saturation. Raise concurrency in measured steps.

### Small-file latency on a network volume

Network storage can show good sequential bandwidth and still behave badly on many tiny files,
because each file costs its own round trip. Symptoms overlap with the storage-latency
classification above: long stalls aligned with reads while both CPU and GPU are idle.

Fix by batching where the semantics already allow it: transfer archive or chunked bundles instead of
file-by-file, keep the content-addressed cache populated so canonical bytes are materialized once,
and keep transfer concurrency bounded because concurrency multiplies sockets per worker. Never
invent a new packing format and never change what the research code reads; a batching change that
alters inputs is a methodology change, not an optimization. Cache-hit rules are the
`wavcse-artifact-pipeline` skill.

## Safe optimization rules

Apply these in order; earlier rules dominate later ones:

1. Remove paid-time waste that does not touch the science: prepare inputs, verify artifacts, resolve
   the exact commit, and write the job specification before compute is billed. Keep one GPU-heavy
   workload on a paid worker at a time.
2. Overlap independent work: verification, packaging, and transferring a finished artifact can run
   while the GPU works on the next unit, with bounded memory and disk.
3. Feed the accelerator: raise data-loading parallelism only where the loader targets CPU, then
   re-check the load average for oversubscription.
4. Reuse verified warm state: a verified cache hit or a retained worker is cheaper than
   re-downloading and re-provisioning, provided identity is verified rather than assumed.

Never change scientific semantics to raise utilization or lower cost: model, checkpoint, pooling,
layer set, dataset membership, splits, preprocessing, label mapping, or precision. A numerically
consequential change belongs to the research design and needs explicit human approval, not an
infrastructure decision.

## Stop optimizing when

Stop tuning and deliver when any of these holds:

- the measured improvement is small relative to the wall clock it cost to find, and the job is
  short;
- the accelerator is already fed and the remaining limitation is scientific;
- the remaining fix requires changing research semantics;
- further tuning costs more than it saves;
- added complexity raises the risk of an unverified artifact.

Prefer shipping a verified artifact over a faster unverified one. An optimization that leaves
correctness ambiguous has negative value.

## Monitor a long run, then stop paying

Monitor periodically rather than continuously; a quiet log is not a failure. Track state, progress
against the projection, and starvation signals. Long jobs survive SSH or controller interruption by
design, so inspect a job the controller cannot currently see instead of restarting it.

When the GPU-critical work is finished:

1. confirm every required output is persisted and verified in canonical storage and that no sole
   copy remains on the worker or its cache (see the `wavcse-artifact-pipeline` skill);
2. record the GPU used, the discovered price, the measured throughput, the identified bottleneck,
   the optimizations retained, the wall time, and the approximate cost;
3. stop the paid Pod as soon as no useful work remains, and destroy it when it will not be reused.
   Address a network volume separately: it keeps accruing storage charges, and destroying a Pod
   never destroys it.

Leaving a Pod running "in case" is an ongoing charge with no artifact to show for it.

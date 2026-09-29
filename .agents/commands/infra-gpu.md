---
description: Quote current GPU capacity and price, then provision only on explicit authorization.
---

# GPU capacity, cost, and authorized compute

Advisory first, mutation second: nothing here creates a resource until a human has
authorized one exact plan.

Skills: gpu-research-operator, wavcse-infra-operator
Boundaries: may-provision-compute

## Sequence

1. Discover offers for the required data center and cloud. Read-only and free:

   ```bash
   infra worker gpu-types --cloud COMMUNITY --gpu-count 1 --require-direct-ssh
   infra worker gpu-types --cloud SECURE --gpu-count 1 --data-center '<exact-dc-id>'
   ```

   `--data-center` is repeatable and `--json` renders machine-readable output. Report per
   offer: the exact GPU type ID that `--gpu` needs, display name, VRAM, current provider
   availability, the maximum GPUs of that type on one machine, the provider list price
   per GPU-hour, and the total GPU price for the requested count. With
   `--require-direct-ssh`, discovery asks the scheduler for public-IP-capable capacity
   only, so the reported price can exceed the unfiltered catalog price; that filtered
   value is what `--max-price` is compared against.

2. Compute the expected total and show the arithmetic, never a bare number:

   ```text
   expected total = provider list price per GPU-hour x gpu-count x planned hours
                  + storage retained after the Pod stops
                  + any network volume's independent storage charge
   ```

   Prices are provider list prices observed at run time, not durable facts — re-read them
   before acting. When a price is absent the guard cannot be proven, so refuse rather than
   estimate.

3. Respect availability. RunPod refuses `NONE` and unknown availability, and this
   workflow never substitutes another GPU type, cloud, GPU count, or data center.
   Availability is advisory and can change between discovery and scheduling.

4. Authorization gate. Present the exact plan — GPU type ID, count, cloud, data center,
   image or template, storage, availability, and expected total — and act only on explicit
   human authorization for that plan. Never create above the operator's configured
   ceiling, and never provision a second worker by default: it needs its own
   authorization.

5. Create once with the ceiling enforced:

   ```bash
   infra worker create --gpu '<exact-gpu-type-id>' --gpu-count 1 --cloud COMMUNITY \
     --image '<reviewed-container-image>' --container-disk 20 --volume 0 \
     --start-ssh --require-direct-ssh --max-price '<maximum-total-usd-per-hour>'
   ```

   `--yes` bypasses only the interactive confirmation; it never bypasses validation,
   `--max-price`, or availability. `--interruptible` is rejected because the provider
   interface cannot back it. A Pod mounting a network volume (`--network-volume-id`) is
   constrained to that volume's data center.

6. Reach readiness: `infra worker show <exact-worker-id>`, then
   `infra worker wait-ssh <exact-worker-id>`, `infra worker bootstrap <exact-worker-id>`,
   and `infra worker health <exact-worker-id>`. Provider `RUNNING` alone is not `READY`.

7. Retention and teardown: `infra worker stop <exact-worker-id>` retains the Pod;
   `infra worker destroy <exact-worker-id>` terminates it and leaves separately managed
   network volumes untouched. Both accept `--wait-timeout <seconds>`.

8. Ambiguous create: if the response is lost, do not repeat the paid request. Inspect
   `infra worker list` for the generated identity before issuing another create.

## Never

Never change the model, checkpoint, pooling, layer set, dataset membership, splits,
preprocessing, label mapping, or precision to make a job faster or cheaper. If the
scientific workload does not fit the authorized ceiling, report that instead.

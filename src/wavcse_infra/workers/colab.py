"""One owned ephemeral Colab lease, native CU guard, and non-SSH readiness."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from wavcse_infra.config import ColabConfig, JobsConfig, SshConfig
from wavcse_infra.errors import (
    AmbiguousCreateError,
    ColabAcceleratorUnavailableError,
    ColabQuotaError,
    CostGuardError,
    ProviderError,
    ProviderOperationAmbiguousError,
    ProviderResponseError,
    StateError,
    WorkerBootstrapError,
)
from wavcse_infra.models import ColabBillingMode, ProviderKind, Worker
from wavcse_infra.providers.colab import ColabClient, ColabUsage
from wavcse_infra.state import WorkerRecord, WorkerStateStore

# Stable, non-secret code; only its results are written to CLI execution history.
_HEALTH = """import json, os, shutil, subprocess, sys, urllib.request
try:
    import torch
    gpu = subprocess.check_output(
        ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
        text=True, timeout=20
    ).strip().splitlines()
    assert len(gpu) == 1 and torch.cuda.is_available(), 'CUDA unavailable'
    if not shutil.which('uv'):
        subprocess.run([sys.executable, '-m', 'pip', 'install', 'uv'], check=True,
                       timeout=120, capture_output=True)
    assert shutil.which('git') and shutil.which('uv'), 'git or uv unavailable'
    assert sys.version_info >= (3, 10), 'Python is too old'
    assert shutil.disk_usage('/content').free >= 1024 * 1024 * 1024, 'insufficient scratch disk'
    urllib.request.urlopen('https://github.com', timeout=15).close()
    marker = os.path.expanduser('~/.local/state/wavcse-worker/bootstrap-version')
    os.makedirs(os.path.dirname(marker), mode=0o700, exist_ok=True)
    with open(marker, 'w') as handle: handle.write('1\\n')
    print('wavcse_colab_health\\t' + json.dumps({
        'gpu': gpu[0], 'disk': shutil.disk_usage('/content').free
    }))
except Exception:
    print('wavcse_colab_health_failed')
"""
_GPU = re.compile(r"(?:NVIDIA )?(T4|L4|G4|H100|A100)(?: GPU)?(?:\b|$)", re.I)


def normalize_gpu(model: str) -> str:
    """Compare a physical nvidia-smi model with the assigned accelerator."""
    match = _GPU.search(model)
    if match is None:
        raise ColabAcceleratorUnavailableError("Colab reported an unsupported physical GPU model")
    return match[1].upper()


# An unresolved allocation intent older than this, whose exact identity consecutive
# successful provider listings show absent, can no longer be an in-flight create: the
# bound is far longer than any provider create round trip, so absence is proof rather than
# a possibly incomplete read. It is the same one-hour bound the orchestration side applies
# to its own durable create intents, so both halves of the system answer that question the
# same way instead of inventing a second timeout.
INTENT_ABANDON_AFTER_HOURS = Decimal("1")

# Consecutive authoritative listings that must agree before an intent is retired. This is
# the repeated-observation rule ambiguous creation already applies to its own identity.
INTENT_ABSENCE_OBSERVATIONS = 2


def _hours_since(started: datetime, now: datetime) -> Decimal:
    """Fractional hours between two timestamps; never negative."""

    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    seconds = (now - started).total_seconds()
    return (Decimal(max(seconds, 0)) / Decimal(3600)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class ColabIntentAssessment:
    """Read-only evidence about one unresolved Colab allocation intent."""

    identity: str
    age_hours: Decimal
    observations: int
    observed_assignments: int
    retirable: bool
    detail: str


class ColabLifecycle:
    def __init__(self, client: ColabClient, state: WorkerStateStore, config: ColabConfig) -> None:
        self.client, self.state, self.config = client, state, config

    def create(self, gpu: str) -> tuple[Worker, ColabUsage, ColabUsage]:
        """Allocate once, record identity first, measure incremental CU, bootstrap or release."""
        self.client.validate_gpu(gpu)
        self.client.version()
        # Never clear an unresolved intent from a missing single provider read.
        existing = [
            r
            for r in self.state.list_records()
            if r.provider.value == "colab" and not r.provider_absent
        ]
        if existing:
            raise CostGuardError(
                f"Colab has an owned lease or pending allocation: {existing[0].infra_identity}"
            )
        before = self.client.usage_snapshot()
        mode = before.billing_mode
        # A zero paid CU balance is not zero entitlement: it selects best-effort free
        # tier, which is permitted only when explicitly enabled. A paid balance still
        # enforces the configured minimum.
        if mode is ColabBillingMode.FREE_TIER and not self.config.allow_free_tier:
            raise ColabQuotaError(
                f"Colab paid CU balance is {before.paid_balance_cu} CU and free-tier "
                "execution is disabled by colab.allow_free_tier"
            )
        if mode is ColabBillingMode.PAID_CU and (
            before.paid_balance_cu < self.config.minimum_balance_cu
        ):
            raise ColabQuotaError(
                f"Colab paid CU balance {before.paid_balance_cu} CU is below "
                f"minimum {self.config.minimum_balance_cu} CU"
            )
        self.client.list_workers()  # Reconcile provider before paid mutation.
        name = f"wavcse-{uuid4().hex[:16]}"
        self.state.record_colab_intent(name, gpu)
        try:
            worker = self.client.create_worker(name, gpu)
        except (ColabQuotaError, ColabAcceleratorUnavailableError) as exc:
            # A definite provider refusal can retire its intent only when
            # repeated authoritative session listings also show no allocation.
            try:
                observations = [
                    [item for item in self.client.list_workers() if item.id == name]
                    for _ in range(2)
                ]
            except ProviderError as observation_error:
                raise AmbiguousCreateError(
                    f"Colab {name} refused creation but session state is unavailable; "
                    "reconcile this exact intent before another allocation"
                ) from observation_error
            if all(not observation for observation in observations):
                self.state.mark_destroyed(name)
                raise
            if all(len(observation) == 1 for observation in observations):
                self.state.record_colab_created(observations[-1][0])
                self._release_confirmed(name)
                raise
            raise AmbiguousCreateError(
                f"Colab {name} may still exist after a refused create; "
                "reconcile or release its exact owned identity"
            ) from exc
        self.state.record_colab_created(worker)
        try:
            after = self.client.usage_snapshot()
            if after.assignments != before.assignments + 1:
                raise CostGuardError(
                    f"Colab {name}: active assignment count changed from {before.assignments} "
                    f"to {after.assignments}; incremental CU cannot be attributed safely"
                )
            rate = after.rate_cu_per_hour - before.rate_cu_per_hour
            if mode is ColabBillingMode.PAID_CU:
                if rate <= 0 or rate > self.config.max_incremental_rate_cu_per_hour:
                    raise CostGuardError(
                        f"Colab {name}: observed incremental {rate} CU/hour outside (0, "
                        f"{self.config.max_incremental_rate_cu_per_hour}] CU/hour; "
                        "COST_POLICY_REJECTION"
                    )
                if after.paid_balance_cu < self.config.minimum_balance_cu:
                    raise CostGuardError(
                        f"Colab {name}: balance below configured minimum after allocation"
                    )
            # Free tier bills no paid CU, so its reported rate is recorded as observed
            # metering evidence, never as a paid-balance or rate-ceiling gate.
            self.bootstrap(name, rate=max(rate, Decimal(0)), baseline=before, billing_mode=mode)
            return worker, before, after
        except Exception:
            # Cleanup only the confirmed exact identity; an ambiguous release
            # stays tracked and never authorizes deleting another session.
            self._release_confirmed(name)
            raise

    def _release_confirmed(self, name: str) -> None:
        record = self.state.get(name)
        if record is None or record.create_pending or record.provider_absent:
            raise ProviderOperationAmbiguousError(
                f"Colab {name} has no confirmed owned identity for terminal release"
            )
        with suppress(ProviderOperationAmbiguousError):
            self.client.destroy_worker(record)
        try:
            present = any(item.id == name for item in self.client.list_workers())
        except ProviderError as exc:
            raise ProviderOperationAmbiguousError(
                f"Colab {name} terminal release could not be confirmed; "
                "reconcile the exact owned session before retrying"
            ) from exc
        if present:
            raise ProviderOperationAmbiguousError(
                f"Colab {name} still exists after terminal release; "
                f"inspect and release with infra worker destroy {name}"
            )
        self.state.mark_destroyed(name)

    def bootstrap(
        self,
        name: str,
        *,
        rate: Decimal | None = None,
        baseline: ColabUsage | None = None,
        billing_mode: ColabBillingMode | None = None,
    ) -> str:
        try:
            return self._bootstrap(name, rate=rate, baseline=baseline, billing_mode=billing_mode)
        except Exception:
            self.state.mark_colab_failed(name)
            raise

    def _bootstrap(
        self,
        name: str,
        *,
        rate: Decimal | None = None,
        baseline: ColabUsage | None = None,
        billing_mode: ColabBillingMode | None = None,
    ) -> str:
        worker = self.client.get_worker(name)
        record = self.state.get(name)
        if record is None or record.create_pending or record.provider_absent:
            raise WorkerBootstrapError(f"Colab {name} has no confirmed owned lease")
        result = self.client.exec_code(name, _HEALTH, timeout=self.config.command_timeout_seconds)
        matches = [
            line.split("\t", 1)[1]
            for line in result.splitlines()
            if line.startswith("wavcse_colab_health\t")
        ]
        if len(matches) != 1:
            raise WorkerBootstrapError(
                f"Colab {name} failed Python, git, uv, CUDA, disk or network readiness"
            )
        try:
            health = json.loads(matches[0])
            model = str(health["gpu"])
            disk = int(health["disk"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderResponseError(f"Colab {name} returned invalid health evidence") from exc
        observed = normalize_gpu(model)
        if observed != worker.gpu_type or observed != record.requested_gpu_type:
            raise ColabAcceleratorUnavailableError(
                f"Colab {name} physical GPU {observed} differs from requested "
                f"{record.requested_gpu_type}"
            )
        # Install the exact same SHA-checked runner used by RunPod before READY
        # is externally usable. A failed install invalidates readiness.
        from wavcse_infra.jobs.colab_transport import ColabExecutor, ColabJobExecutor, ColabWaiter

        client = self.client
        waiter = ColabWaiter(client, self.state, require_ready=False)
        ColabJobExecutor(
            client,
            waiter,
            ColabExecutor(client),
            SshConfig(),
            JobsConfig(
                runner_path="/content/.wavcse/job_runner.py",
                worker_root="/content/.wavcse/jobs",
            ),
        ).install_runner(name)
        self.state.record_colab_ready(
            name,
            gpu_model=model,
            rate=rate or record.observed_rate_cu_per_hour or Decimal(0),
            disk_bytes=disk,
            baseline_rate=baseline.rate_cu_per_hour if baseline else None,
            baseline_assignments=baseline.assignments if baseline else None,
            billing_mode=billing_mode,
        )
        return model

    def assess_intent(self, name: str, *, now: datetime | None = None) -> ColabIntentAssessment:
        """Gather the evidence for retiring one unresolved allocation intent. Read-only.

        Every condition is required and none is inferred: an exact infra-owned identity, a
        still-unresolved intent, an age past the abandonment bound, and consecutive
        successful provider listings that all omit the identity and agree with the
        account's own active-assignment count. Anything else raises, because the failure
        this guards against is retiring the intent for a session that does exist and then
        allocating a second one.
        """

        record = self.state.get(name)
        if record is None:
            raise StateError(f"No tracked worker record has infra identity {name!r}")
        if record.provider is not ProviderKind.COLAB:
            raise StateError(
                f"Worker {name} is a {record.provider.value} record, not a Colab session"
            )
        if record.infra_identity != name or record.provider_worker_id != name:
            raise StateError(
                f"Worker {name} does not carry the exact infra identity of this allocation"
            )
        moment = now or self.state.now()
        age = _hours_since(record.creation_timestamp, moment)
        if not record.create_pending:
            if record.provider_absent:
                return ColabIntentAssessment(
                    identity=name,
                    age_hours=age,
                    observations=0,
                    observed_assignments=0,
                    retirable=False,
                    detail="the intent is already terminal and provider-absent",
                )
            raise StateError(
                f"Colab session {name} is a confirmed allocation, not an unresolved intent; "
                "reconcile it with `infra worker list` and release it with `infra worker destroy`"
            )
        if age < INTENT_ABANDON_AFTER_HOURS:
            raise StateError(
                f"Colab allocation intent {name} is only {age} h old; an intent younger than "
                f"{INTENT_ABANDON_AFTER_HOURS} h may still be an in-flight create and is never "
                "abandoned on inference"
            )
        listed: list[int] = []
        for _ in range(INTENT_ABSENCE_OBSERVATIONS):
            listing = self.client.list_workers()
            if any(worker.id == name for worker in listing):
                raise StateError(
                    f"Colab session {name} is present in the provider listing; it is not an "
                    "abandoned intent and is never retired by inference"
                )
            listed.append(len(listing))
        usage = self.client.usage_snapshot()
        if len(set(listed)) != 1 or usage.assignments != listed[0]:
            raise StateError(
                f"Colab observations for {name} disagree: sessions listed {listed} against "
                f"{usage.assignments} active assignment(s); refusing to retire an intent whose "
                "absence is not consistently proven"
            )
        return ColabIntentAssessment(
            identity=name,
            age_hours=age,
            observations=len(listed),
            observed_assignments=usage.assignments,
            retirable=True,
            detail="consecutive successful listings omit the exact identity",
        )

    def retire_intent(self, assessment: ColabIntentAssessment) -> WorkerRecord | None:
        """Apply the local transition an assessment authorizes. Never contacts a provider.

        The store re-checks the structural precondition, so an assessment that has gone
        stale - the session appeared, or another controller process already retired the
        intent - cannot retire anything but a still-unresolved intent.
        """

        if not assessment.retirable:
            return None
        retired = self.state.mark_colab_intent_absent(assessment.identity)
        if retired is None:
            raise StateError(
                f"Colab allocation intent {assessment.identity} is no longer an unresolved "
                "intent; reconcile it again before acting"
            )
        return retired

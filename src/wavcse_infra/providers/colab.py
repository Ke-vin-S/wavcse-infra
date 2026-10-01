"""Pinned Google Colab CLI adapter; no private Google API calls or interactive login."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from wavcse_infra.config import ColabConfig
from wavcse_infra.errors import (
    AmbiguousCreateError,
    ColabAcceleratorUnavailableError,
    ColabAuthenticationRequiredError,
    ColabCliMissingError,
    ColabQuotaError,
    ProviderError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ProviderResponseError,
    ProviderUnavailableError,
    ProviderValidationError,
    UnsupportedProviderOperationError,
)
from wavcse_infra.models import (
    ColabBillingMode,
    ExecutionTransport,
    ProviderKind,
    Worker,
    WorkerState,
)
from wavcse_infra.redaction import redact
from wavcse_infra.state import WorkerRecord

PINNED_COLAB_VERSION = "0.7.4"
ADC_LOGIN_COMMAND = (
    "gcloud auth application-default login \\\n"
    "  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,"
    "https://www.googleapis.com/auth/userinfo.email,"
    "https://www.googleapis.com/auth/colaboratory"
)
_GPU_TYPES = frozenset({"T4", "L4", "G4", "H100", "A100"})
_SESSION_NAME = re.compile(r"^wavcse-[0-9a-f]{12,32}$")
_SESSION_LINE = re.compile(
    r"^\[(?P<name>[^\]]+)\] (?P<endpoint>[^ |]+) \| Hardware: "
    r"(?P<gpu>[^|]+) \| Shape: (?P<shape>[^|]+) \| Variant: "
    r"(?P<variant>[^|]+?)(?: \| Status: (?P<status>IDLE|BUSY(?: \([^|\n]*\))?))?$"
)

_BALANCE = re.compile(r"^Current balance: ([0-9]+(?:\.[0-9]+)?) compute units$")
_RATE = re.compile(r"^Usage rate: ([0-9]+(?:\.[0-9]+)?)/hr$")
_ASSIGNMENTS = re.compile(r"^Active assignments: ([0-9]+)$")


@dataclass(frozen=True)
class ColabUsage:
    """One account-wide CU observation from the pinned CLI.

    `paid_balance_cu` is the CLI's `Current balance`, which is the account's
    `paidComputeUnitsBalance`: it is the paid CU balance only, not total compute
    entitlement. `rate_cu_per_hour` is the provider-reported aggregate usage rate and
    is observation-only when the paid balance is zero.
    """

    paid_balance_cu: Decimal
    rate_cu_per_hour: Decimal
    assignments: int

    @property
    def billing_mode(self) -> ColabBillingMode:
        """Select the execution mode from the paid CU balance, never from entitlement."""

        if self.paid_balance_cu > 0:
            return ColabBillingMode.PAID_CU
        return ColabBillingMode.FREE_TIER


def parse_usage(text: str) -> ColabUsage:
    """Parse the pinned CLI's rounded, account-wide CU observation; refuse missing fields."""

    lines = text.strip().splitlines()
    if len(lines) != 3:
        raise ProviderResponseError("Colab usage returned an unrecognized CU report")
    balance, rate, count = (
        _BALANCE.fullmatch(lines[0]),
        _RATE.fullmatch(lines[1]),
        _ASSIGNMENTS.fullmatch(lines[2]),
    )
    if balance is None or rate is None or count is None:
        raise ProviderResponseError("Colab usage returned an unrecognized CU report")
    try:
        return ColabUsage(Decimal(balance[1]), Decimal(rate[1]), int(count[1]))
    except InvalidOperation as exc:
        raise ProviderResponseError("Colab usage returned invalid CU values") from exc


@dataclass(frozen=True)
class ColabCommandResult:
    """One captured result, never printed without redaction."""

    operation: str
    session: str | None
    exit_code: int
    stdout: str
    stderr: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


class ColabClient:
    """Invoke CLI 0.7.4 with ADC; resource mutation is never blindly retried."""

    def __init__(self, config: ColabConfig, *, runner: Runner = subprocess.run) -> None:
        self.config = config
        self._runner = runner
        # Upstream may write token-bearing state and execution history with the
        # process umask; pre-create private directories before its first invocation.
        self._secure_local_state()

    def _call(
        self,
        operation: str,
        *args: str,
        session: str | None = None,
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> ColabCommandResult:
        self._secure_local_state()
        if not shutil.which(self.config.cli):
            raise ColabCliMissingError(
                f"Colab {operation} for {session or 'account'}: "
                f"{self.config.cli} is not installed; "
                f"run uv tool install google-colab-cli=={PINNED_COLAB_VERSION}"
            )
        argv = [self.config.cli, "--auth=adc", operation, *args]
        try:
            completed = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.config.command_timeout_seconds,
                **({"input": input_text} if input_text is not None else {}),
                check=False,
            )
        except FileNotFoundError as exc:
            raise ColabCliMissingError(
                f"Colab {operation} for {session or 'account'}: CLI not installed"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            target = f"Colab {operation} for {session or 'account'} timed out"
            if operation in {"new", "stop"}:
                raise ProviderOperationAmbiguousError(
                    f"{target}; its outcome is unknown. Inspect infra worker list "
                    "before taking another action"
                ) from exc
            raise ProviderUnavailableError(f"{target}; no provider state was inferred") from exc
        except OSError as exc:
            raise ProviderUnavailableError(
                f"Colab {operation} for {session or 'account'} could not invoke CLI "
                f"(OS error {exc.errno})"
            ) from exc
        finally:
            # Upstream appends history even on failed exec; restrict any file it
            # created before returning a diagnostic to the caller.
            self._secure_local_state()
        result = ColabCommandResult(
            operation, session, completed.returncode, completed.stdout, completed.stderr
        )
        if result.exit_code:
            self._raise_failure(result)
        return result

    @staticmethod
    def _raise_failure(result: ColabCommandResult) -> None:
        # Upstream may echo arbitrary HTTP bodies or code. Classify privately,
        # never expose untrusted bytes in infrastructure diagnostics.
        lower = (result.stderr or result.stdout).lower()
        prefix = (
            f"Colab {result.operation} for {result.session or 'account'} exited {result.exit_code}"
        )
        auth_terms = (
            "credential",
            "application default",
            "authentication",
            "401",
            "403",
            "scope",
            "unauthorized",
        )
        if any(term in lower for term in auth_terms):
            raise ColabAuthenticationRequiredError(
                f"{prefix}: ADC authentication required. Run:\n{ADC_LOGIN_COMMAND}"
            )
        if "quota" in lower or "compute unit" in lower or "too many active sessions" in lower:
            raise ColabQuotaError(f"{prefix}: account quota/compute units exhausted")
        if "accelerator" in lower or "capacity" in lower or "entitlement" in lower:
            raise ColabAcceleratorUnavailableError(
                f"{prefix}: requested accelerator cannot be allocated"
            )
        if "not found" in lower or "no active session" in lower or "no session" in lower:
            raise ProviderNotFoundError(f"{prefix}: session disappeared or was not found")
        raise ProviderUnavailableError(
            f"{prefix}: provider refused the operation; inspect account/session access "
            "with infra doctor"
        )

    def version(self) -> str:
        result = self._call("version")
        match = re.fullmatch(r"Version: ([0-9]+\.[0-9]+\.[0-9]+)", result.stdout.strip())
        if not match:
            raise ProviderResponseError("Colab version returned an unrecognized response")
        version = match.group(1)
        if version != PINNED_COLAB_VERSION:
            raise ProviderValidationError(
                f"Colab CLI version {version} is unvalidated; install "
                f"google-colab-cli=={PINNED_COLAB_VERSION}"
            )
        return version

    def list_workers(self) -> list[Worker]:
        self.version()
        result = self._call("sessions")
        if result.stdout.strip() == "[colab] No active sessions found on server.":
            return []
        rows = result.stdout.splitlines()
        if not rows:
            raise ProviderResponseError("Colab sessions returned no recognizable listing")
        workers = [self._parse_worker(row, operation="sessions") for row in rows]
        if len({worker.id for worker in workers}) != len(workers):
            raise ProviderResponseError(
                "Colab sessions returned duplicate names; no ownership was inferred"
            )
        return workers

    def get_worker(self, name: str) -> Worker:
        self._validate_name(name)
        self.version()
        # The backend assignment listing is authoritative; status alone is local metadata.
        matching = [worker for worker in self.list_workers() if worker.id == name]
        if not matching:
            raise ProviderNotFoundError(f"Colab session {name} is absent from provider sessions")
        if len(matching) != 1:
            raise ProviderResponseError(f"Colab sessions returned duplicate identity {name}")
        try:
            result = self._call("status", "-s", name, session=name)
        except ProviderNotFoundError as exc:
            raise ProviderUnavailableError(
                f"Colab session {name} was listed by the backend but its local "
                "status disappeared; reconcile sessions again before concluding absence"
            ) from exc
        row = result.stdout.splitlines()
        if not row:
            raise ProviderResponseError(f"Colab status for {name} returned no worker")
        observed = self._parse_worker(row[0], operation="status")
        if observed.id != name:
            raise ProviderResponseError(f"Colab status returned {observed.id} instead of {name}")
        return observed

    @staticmethod
    def _parse_worker(row: str, *, operation: str) -> Worker:
        match = _SESSION_LINE.fullmatch(row.strip())
        if not match:
            raise ProviderResponseError(f"Colab {operation} response could not be parsed safely")
        name = match.group("name")
        if name == "?":
            # The CLI has no local mapping for this backend assignment. Never claim it.
            raise ProviderResponseError(
                "Colab sessions contains an unidentifiable backend assignment; "
                "inspect it in the Colab console without adopting or deleting it"
            )
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) is None:
            raise ProviderResponseError(f"Colab {operation} returned an invalid session identity")
        gpu = match.group("gpu").strip()
        if gpu not in _GPU_TYPES | {"CPU", "V5E1", "V6E1"}:
            raise ProviderResponseError(f"Colab {operation} returned an unknown accelerator")
        status = match.group("status")
        return Worker(
            provider=ProviderKind.COLAB,
            execution_transport=ExecutionTransport.COLAB_EXEC,
            id=name,
            name=name,
            state=WorkerState.RUNNING,
            # BUSY may include an upstream exec filename (possibly sensitive).
            native_status="BUSY" if status and status.startswith("BUSY") else status or "RUNNING",
            gpu_type=None if gpu == "CPU" else gpu,
            gpu_count=0 if gpu == "CPU" else 1,
        )

    def usage(self) -> str:
        self.version()
        result = self._call("usage")
        if not result.stdout.strip():
            raise ProviderResponseError("Colab usage returned an empty result")
        return redact(result.stdout.strip())

    def usage_snapshot(self) -> ColabUsage:
        """Obtain numeric account observations without inferring a USD price."""

        return parse_usage(self.usage())

    def upload_file(self, name: str, local_path: Path, remote_path: str) -> None:
        self._validate_name(name)
        self._call("upload", "-s", name, str(local_path), remote_path, session=name)

    def download_file(self, name: str, remote_path: str, local_path: Path) -> None:
        self._validate_name(name)
        self._call("download", "-s", name, remote_path, str(local_path), session=name)

    def remove_file(self, name: str, remote_path: str) -> None:
        self._validate_name(name)
        self._call("rm", "-s", name, remote_path, session=name)

    def exec_code(self, name: str, code: str, *, timeout: float | None = None) -> str:
        """Execute stable non-secret source via stdin; CLI history records code and outputs."""

        self._validate_name(name)
        result = self._call(
            "exec",
            "-s",
            name,
            "--timeout",
            str(timeout or self.config.command_timeout_seconds),
            session=name,
            input_text=code,
            timeout=(timeout or self.config.command_timeout_seconds) + 30,
        )
        return result.stdout

    @staticmethod
    def _secure_local_state() -> None:
        """Restrict existing CLI token/history state without deleting other users' files."""

        root = Path.home() / ".config" / "colab-cli"
        try:
            for directory in (root, root / "history"):
                if directory.is_symlink():
                    raise ProviderValidationError("Colab CLI state directory must not be a symlink")
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if directory.stat().st_uid != os.getuid():
                    raise ProviderValidationError("Colab CLI state directory has another owner")
                directory.chmod(0o700)
            for path in (
                root / "sessions.json",
                root / "colab.log",
                *(root / "history").glob("wavcse-*.jsonl"),
            ):
                if path.is_symlink():
                    raise ProviderValidationError("Colab CLI state contains an unsafe symlink")
                if path.exists():
                    if not path.is_file() or path.stat().st_uid != os.getuid():
                        raise ProviderValidationError("Colab CLI state contains an unsafe file")
                    path.chmod(0o600)
        except OSError as exc:
            raise ProviderValidationError(
                f"Colab CLI local state permissions could not be restricted (OS error {exc.errno})"
            ) from exc

    @staticmethod
    def validate_gpu(gpu: str) -> None:
        if gpu not in _GPU_TYPES:
            raise ProviderValidationError(
                f"Colab accelerator {gpu!r} is unsupported; choose " + ", ".join(sorted(_GPU_TYPES))
            )

    def create_worker(self, name: str, gpu: str) -> Worker:
        self._validate_name(name)
        self.validate_gpu(gpu)
        self.version()
        try:
            self._call(
                "new",
                "-s",
                name,
                "--gpu",
                gpu,
                session=name,
                timeout=self.config.lifecycle_timeout_seconds,
            )
        except (ProviderOperationAmbiguousError, ProviderUnavailableError) as exc:
            # A paid create can succeed before the CLI loses its reply; never retry it.
            try:
                matches = [worker for worker in self.list_workers() if worker.id == name]
            except ProviderError:
                matches = []
            if len(matches) == 1:
                return matches[0]
            raise AmbiguousCreateError(
                f"Colab allocation {name} has an unknown outcome; run infra worker list "
                "and inspect this exact identity before any new allocation"
            ) from exc
        deadline = time.monotonic() + self.config.lifecycle_timeout_seconds
        while True:
            try:
                return self.get_worker(name)
            except (ProviderNotFoundError, ProviderUnavailableError) as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AmbiguousCreateError(
                        f"Colab allocation {name} was acknowledged but provider RUNNING "
                        "state could not be verified; reconcile this exact intent "
                        "before another allocation"
                    ) from exc
                time.sleep(min(2.0, remaining))

    def destroy_worker(self, record: WorkerRecord) -> None:
        """Release only a confirmed locally owned exact session."""

        if (
            record.provider is not ProviderKind.COLAB
            or record.provider_worker_id != record.infra_identity
            or record.create_pending
            or record.provider_absent
        ):
            raise UnsupportedProviderOperationError(
                "Colab terminal release requires a confirmed infra-owned session record"
            )
        name = record.provider_worker_id
        self._validate_name(name)
        self.version()
        self._call("stop", "-s", name, session=name, timeout=self.config.lifecycle_timeout_seconds)

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _SESSION_NAME.fullmatch(name):
            raise UnsupportedProviderOperationError(
                f"Colab session {redact(name)[:100]} is not an exact wavcse-infra session identity"
            )

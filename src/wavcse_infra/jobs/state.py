"""Durable non-secret local job state beneath the controller state directory.

One JSON document per job keeps a job's history independent of every other job, so a
corrupt or partially written record cannot hide an unrelated recorded run. Writes reuse
the same atomic same-directory replacement as worker state and never contain credentials,
presigned URLs, or secret environment values.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from wavcse_infra.errors import JobStateError
from wavcse_infra.jobs.models import (
    JobRecord,
    JobState,
    ensure_transition,
    validate_job_id,
)
from wavcse_infra.state import write_json_atomically

DEFAULT_JOB_STATE_DIRECTORY = Path("~/.local/state/wavcse-infra/jobs")


class JobStateStore:
    """Read and atomically replace one durable document per recorded job."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.directory = (directory or DEFAULT_JOB_STATE_DIRECTORY).expanduser()
        self._now = now or (lambda: datetime.now(UTC))

    def path_for(self, job_id: str) -> Path:
        """Return the durable record path for one canonical job ID."""

        return self.directory / f"{validate_job_id(job_id)}.json"

    def log_path(self, job_id: str) -> Path:
        """Return the local bounded log-copy path for one canonical job ID."""

        return self.directory / f"{validate_job_id(job_id)}.log"

    def list_records(self) -> list[JobRecord]:
        """Return every readable job record, ordered by creation time and ID."""

        if not self.directory.exists():
            return []
        records: list[JobRecord] = []
        for path in sorted(self.directory.glob("job-*.json")):
            records.append(self._load_path(path))
        records.sort(key=lambda record: (record.created_at, record.job_id))
        return records

    def get(self, job_id: str) -> JobRecord | None:
        """Return one job record, or None when that job was never recorded here."""

        path = self.path_for(job_id)
        if not path.exists():
            return None
        return self._load_path(path)

    def require(self, job_id: str) -> JobRecord:
        """Return one job record or fail with the canonical missing-ID message."""

        record = self.get(job_id)
        if record is None:
            raise JobStateError(
                f"No local record exists for job {validate_job_id(job_id)}; "
                "run `infra job submit` on this controller first"
            )
        return record

    def create(self, record: JobRecord) -> JobRecord:
        """Persist a brand-new job record without replacing an existing one."""

        path = self.path_for(record.job_id)
        if path.exists():
            raise JobStateError(
                f"A local record already exists for job {record.job_id}; job IDs are never reused"
            )
        self._write(record)
        return record

    def save(self, record: JobRecord) -> JobRecord:
        """Persist an updated record, refreshing its modification timestamp."""

        updated = record.model_copy(update={"updated_at": self._now()})
        self._write(updated)
        return updated

    def transition(
        self,
        record: JobRecord,
        target: JobState,
        *,
        reason: str | None = None,
        **updates: Any,
    ) -> JobRecord:
        """Apply one legal state transition together with recorded evidence fields."""

        ensure_transition(record.state, target)
        values: dict[str, Any] = dict(updates)
        if target is not record.state:
            values["state"] = target
            values["state_reason"] = reason
        elif reason is not None:
            values["state_reason"] = reason
        return self.save(record.model_copy(update=values))

    def write_log(self, job_id: str, text: str) -> Path:
        """Store a bounded local copy of the job's own output for post-run inspection."""

        path = self.log_path(job_id)
        _write_text_atomically(path, text)
        return path

    def read_log(self, job_id: str) -> str | None:
        """Return the stored local log copy, or None when nothing was captured."""

        path = self.log_path(job_id)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise JobStateError(f"Could not read local job log {path}: {exc}") from exc

    def _load_path(self, path: Path) -> JobRecord:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise JobStateError(f"Could not read job state {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise JobStateError(f"Job state {path} is not valid JSON: {exc}") from exc
        try:
            return JobRecord.model_validate(payload)
        except ValidationError as exc:
            raise JobStateError(f"Job state {path} is invalid: {exc}") from exc

    def _write(self, record: JobRecord) -> None:
        write_json_atomically(self.path_for(record.job_id), record.model_dump(mode="json"))


def _write_text_atomically(path: Path, text: str) -> None:
    directory = path.parent
    temporary_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory,
            prefix=f".{path.stem}-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise JobStateError(f"Could not atomically write {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

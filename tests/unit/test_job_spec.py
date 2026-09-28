"""Version 1 job specification validation, state machine, and serialization."""

from __future__ import annotations

import json

import pytest
from job_fakes import job_spec_document

from wavcse_infra.errors import JobSpecError
from wavcse_infra.jobs.models import (
    LEGAL_JOB_TRANSITIONS,
    TERMINAL_JOB_STATES,
    JobSpec,
    JobState,
    can_transition,
    ensure_transition,
    job_spec_json,
    load_job_spec,
    validate_job_id,
)

COMMIT = "a" * 40


def _spec(**overrides: object) -> JobSpec:
    return load_job_spec(json.dumps(job_spec_document(**overrides)))


def test_valid_spec_round_trips_through_normalized_json() -> None:
    spec = _spec(
        setup={"argv": ["uv", "sync", "--locked"]},
        runtime={
            "timeout_seconds": 3600,
            "environment": {"PYTHONUNBUFFERED": "1"},
            "environment_secrets": ["MLFLOW_TRACKING_PASSWORD"],
        },
        inputs=[
            {
                "artifact": "embeddings/v1/voxceleb.tar",
                "destination": "voxceleb.tar",
                "manifest": "embeddings/v1/voxceleb.manifest.json",
            }
        ],
        outputs=[
            {"path": "outputs/metrics.json", "artifact": "jobs/run-1/metrics.json"},
        ],
        tracking={"metadata": {"study": "DG-0004", "seed": "42"}},
    )

    restored = load_job_spec(job_spec_json(spec))

    assert restored == spec
    assert restored.source.commit == COMMIT
    assert restored.command.argv[4] == "--seed"
    assert restored.secret_environment_names == ("MLFLOW_TRACKING_PASSWORD",)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"schema_version": 2},
        {"schema_version": "1"},
    ],
)
def test_missing_or_unsupported_schema_version_is_rejected(document: dict) -> None:
    payload = job_spec_document()
    payload.update(document)
    payload["schema_version"] = document.get("schema_version")

    with pytest.raises(JobSpecError):
        load_job_spec(json.dumps(payload))


def test_load_reports_non_json_and_non_object_documents() -> None:
    with pytest.raises(JobSpecError, match="not valid JSON"):
        load_job_spec("{")
    with pytest.raises(JobSpecError, match="must be a JSON object"):
        load_job_spec("[1, 2]")


@pytest.mark.parametrize(
    "commit",
    ["main", "HEAD", "v1.2.3", "abc123", "A" * 40 + "b", "z" * 40, ""],
)
def test_only_a_full_commit_object_id_is_accepted(commit: str) -> None:
    with pytest.raises(JobSpecError):
        _spec(source={"repository": "https://github.com/Synergy-io/wavCSE.git", "commit": commit})


def test_uppercase_commit_is_normalized_to_lowercase() -> None:
    spec = _spec(
        source={"repository": "https://github.com/Synergy-io/wavCSE.git", "commit": "A" * 40}
    )

    assert spec.source.commit == "a" * 40


@pytest.mark.parametrize(
    "repository",
    [
        "git@github.com:Synergy-io/wavCSE.git",
        "ssh://git@github.com/Synergy-io/wavCSE.git",
        "https://token@github.com/Synergy-io/wavCSE.git",
        "https://github.com/Synergy-io/wavCSE.git?ref=main",
        "https://github.com/Synergy-io/wavCSE.git#main",
        "http://github.com/Synergy-io/wavCSE.git",
        "https:///wavCSE.git",
        "https://github.com/wav CSE.git",
    ],
)
def test_repository_must_be_anonymous_https(repository: str) -> None:
    with pytest.raises(JobSpecError):
        _spec(source={"repository": repository, "commit": COMMIT})


def test_shell_command_string_is_not_a_valid_command() -> None:
    with pytest.raises(JobSpecError):
        _spec(command="uv run python train.py --seed 42")


def test_argv_must_be_a_non_empty_argument_vector() -> None:
    for argv in ([], [""], ["python", "bad\nargument"], [123]):
        with pytest.raises(JobSpecError):
            _spec(command={"argv": argv})


def test_argv_refuses_presigned_urls_in_command_and_setup() -> None:
    bearer = "https://s3.example/object?X-Amz-Signature=secret"
    for field in ("command", "setup"):
        with pytest.raises(JobSpecError, match="presigned URLs"):
            _spec(**{field: {"argv": ["python3", bearer]}})


def test_working_directory_may_not_escape_or_be_absolute() -> None:
    for working_directory in ("/workspace", "../outside", "a/../../b", " ", "a//b"):
        with pytest.raises(JobSpecError):
            _spec(command={"argv": ["python", "x.py"], "working_directory": working_directory})

    spec = _spec(command={"argv": ["python", "x.py"], "working_directory": "improvements/base"})
    assert spec.command.working_directory == "improvements/base"


@pytest.mark.parametrize(
    "destination",
    ["/etc/passwd", "../escape.tar", "a/../../b", "", "dir/", "white space.tar"],
)
def test_input_destination_cannot_escape_the_job_workspace(destination: str) -> None:
    with pytest.raises(JobSpecError):
        _spec(
            inputs=[{"artifact": "embeddings/v1/a.tar", "destination": destination}],
        )


@pytest.mark.parametrize(
    "path", ["/abs/output.json", "source/checkout.py", "state/pid.json", "../x"]
)
def test_output_path_cannot_escape_or_target_reserved_directories(path: str) -> None:
    with pytest.raises(JobSpecError):
        _spec(outputs=[{"path": path, "artifact": "jobs/run-1/output.bin"}])


def test_inputs_and_outputs_must_not_repeat_declarations() -> None:
    with pytest.raises(JobSpecError):
        _spec(
            inputs=[
                {"artifact": "embeddings/v1/a.tar", "destination": "a.tar"},
                {"artifact": "embeddings/v1/a.tar", "destination": "b.tar"},
            ]
        )
    with pytest.raises(JobSpecError):
        _spec(
            outputs=[
                {"path": "outputs/a.json", "artifact": "jobs/run-1/a.json"},
                {"path": "outputs/a.json", "artifact": "jobs/run-1/b.json"},
            ]
        )


def test_input_cannot_declare_a_manifest_and_an_explicit_digest() -> None:
    with pytest.raises(JobSpecError):
        _spec(
            inputs=[
                {
                    "artifact": "embeddings/v1/a.tar",
                    "destination": "a.tar",
                    "manifest": "embeddings/v1/a.manifest.json",
                    "sha256": "b" * 64,
                }
            ]
        )


def test_input_artifact_keys_use_the_storage_namespace_rule() -> None:
    for artifact in ("/embeddings/v1/a.tar", "embeddings//a.tar", "embeddings/../a.tar", "a.tar?"):
        with pytest.raises(JobSpecError):
            _spec(inputs=[{"artifact": artifact, "destination": "a.tar"}])


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HF_TOKEN", "value"),
        ("AWS_PROFILE", "value"),
        ("RUNPOD_API_KEY", "value"),
        ("WAVCSE_JOB_DIRECTORY", "/workspace/x"),
        ("INFRA_GIT_COMMIT", "deadbeef"),
        ("SSH_AUTH_SOCK", "/tmp/sock"),
        ("MY_PRIVATE_KEY_PATH", "/x"),
    ],
)
def test_literal_environment_rejects_reserved_and_credential_shaped_names(
    name: str, value: str
) -> None:
    with pytest.raises(JobSpecError):
        _spec(runtime={"environment": {name: value}})


def test_literal_environment_rejects_bearer_values_and_empty_entries() -> None:
    with pytest.raises(JobSpecError):
        _spec(
            runtime={
                "environment": {"DATA_URL": "https://bucket.s3.amazonaws.com/x?X-Amz-Signature=abc"}
            }
        )
    with pytest.raises(JobSpecError):
        _spec(runtime={"environment": {"EMPTY": ""}})


def test_secret_environment_names_are_validated_and_reserved_names_refused() -> None:
    spec = _spec(runtime={"environment_secrets": ["MLFLOW_TRACKING_USERNAME"]})
    assert spec.runtime.environment_secrets == ("MLFLOW_TRACKING_USERNAME",)

    for name in ("mlflow_password", "1BAD", "AWS_SECRET_ACCESS_KEY", "RUNPOD_API_KEY", "WAVCSE_X"):
        with pytest.raises(JobSpecError):
            _spec(runtime={"environment_secrets": [name]})


def test_secret_environment_names_must_be_unique() -> None:
    with pytest.raises(JobSpecError):
        _spec(runtime={"environment_secrets": ["MLFLOW_TRACKING_USERNAME"] * 2})


def test_tracking_metadata_rejects_bearer_material_and_unknown_fields() -> None:
    with pytest.raises(JobSpecError):
        _spec(tracking={"metadata": {"note": "see https://host/x?X-Amz-Signature=abc"}})
    with pytest.raises(JobSpecError):
        _spec(unknown_field="value")
    with pytest.raises(JobSpecError):
        _spec(runtime={"unknown_field": "value"})


def test_timeout_bounds_are_enforced() -> None:
    with pytest.raises(JobSpecError):
        _spec(runtime={"timeout_seconds": 0})
    with pytest.raises(JobSpecError):
        _spec(runtime={"timeout_seconds": 604801})


def test_job_id_validation_rejects_traversal_shaped_values() -> None:
    assert validate_job_id("job-0123456789abcdef") == "job-0123456789abcdef"
    for job_id in ("job-0123456789ABCDEF", "job-123", "../../etc/passwd", "job-0123456789abcdef/"):
        with pytest.raises(JobSpecError):
            validate_job_id(job_id)


def test_state_machine_permits_only_declared_transitions() -> None:
    assert can_transition(JobState.PENDING, JobState.PREPARING)
    assert can_transition(JobState.PREPARING, JobState.RUNNING)
    assert can_transition(JobState.RUNNING, JobState.SUCCEEDED)
    assert can_transition(JobState.RUNNING, JobState.FAILED)
    assert can_transition(JobState.RUNNING, JobState.CANCELLED)
    assert can_transition(JobState.FAILED, JobState.FAILED)

    for terminal in TERMINAL_JOB_STATES:
        assert LEGAL_JOB_TRANSITIONS[terminal] == frozenset()
        for target in JobState:
            if target is terminal:
                continue
            assert not can_transition(terminal, target)


def test_impossible_transitions_raise() -> None:
    ensure_transition(JobState.PENDING, JobState.PREPARING)

    with pytest.raises(JobSpecError, match="PENDING -> SUCCEEDED"):
        ensure_transition(JobState.PENDING, JobState.SUCCEEDED)
    with pytest.raises(JobSpecError, match="SUCCEEDED -> RUNNING"):
        ensure_transition(JobState.SUCCEEDED, JobState.RUNNING)

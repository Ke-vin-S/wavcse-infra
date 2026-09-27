from wavcse_infra.redaction import redact


def test_redacts_authorization_headers_and_secret_assignments() -> None:
    message = (
        "Authorization: Bearer token-value, "
        "RUNPOD_API_KEY=another-value AWS_SECRET_ACCESS_KEY: final-value"
    )

    redacted = redact(message)

    assert "token-value" not in redacted
    assert "another-value" not in redacted
    assert "final-value" not in redacted
    assert redacted.count("<redacted>") == 3


def test_redacts_query_string_from_urls() -> None:
    value = "upload failed for https://bucket.s3.example/key?X-Amz-Signature=secret"

    redacted = redact(value)

    assert redacted == "upload failed for https://bucket.s3.example/key?<redacted>"

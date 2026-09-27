"""Central redaction for credentials and bearer-style URLs."""

from __future__ import annotations

import re

_AUTHORIZATION_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(runpod_api_key|aws_access_key_id|aws_secret_access_key|aws_session_token)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_URL_QUERY_PATTERN = re.compile(r"(https?://[^\s?#]+)\?[^\s]+", re.IGNORECASE)


def redact(text: object) -> str:
    """Return user-facing text with known secret-bearing forms removed."""

    value = str(text)
    value = _AUTHORIZATION_PATTERN.sub(r"\1<redacted>", value)
    value = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2<redacted>", value)
    return _URL_QUERY_PATTERN.sub(r"\1?<redacted>", value)

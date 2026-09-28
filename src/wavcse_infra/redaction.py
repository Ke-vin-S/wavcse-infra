"""Central redaction for credentials and bearer-style URLs."""

from __future__ import annotations

import re

_AUTHORIZATION_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(runpod_api_key|aws_access_key_id|aws_secret_access_key|aws_session_token)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_URL_QUERY_PATTERN = re.compile(r"(https?://[^\s?#]+)\?[^\s]+", re.IGNORECASE)
_SIGNED_PARAMETER_PATTERN = re.compile(
    r"(?i)\b(x-amz-(?:signature|credential|security-token)|awsaccesskeyid)"
    r"(\s*[:=]\s*)[^\s&;,]+"
)
# Bearer material that must never be persisted in local state or manifests, even when it
# appears inside otherwise harmless free text.
_BEARER_MATERIAL_PATTERN = re.compile(
    r"(?i)(?:x-amz-(?:signature|credential|security-token|algorithm)|"
    r"aws(?:accesskeyid|_access_key_id|_secret_access_key|_session_token)|"
    r"runpod_api_key|authorization\s*[:=]|https?://\S+\?)"
)


def contains_bearer_material(text: str) -> bool:
    """Return whether text carries a presigned URL, credential, or authorization value."""

    return _BEARER_MATERIAL_PATTERN.search(text) is not None


def redact(text: object) -> str:
    """Return user-facing text with known secret-bearing forms removed."""

    value = str(text)
    value = _AUTHORIZATION_PATTERN.sub(r"\1<redacted>", value)
    value = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2<redacted>", value)
    value = _URL_QUERY_PATTERN.sub(r"\1?<redacted>", value)
    return _SIGNED_PARAMETER_PATTERN.sub(r"\1\2<redacted>", value)

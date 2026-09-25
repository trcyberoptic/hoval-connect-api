"""Keep useful remote errors without logging credentials from their bodies."""

from __future__ import annotations

import json
import re
from typing import Any

_REDACTED = "**REDACTED**"
_SECRET_NAMES = (
    "password",
    "passwd",
    "token",
    "id_token",
    "access_token",
    "refresh_token",
    "plant_access_token",
    "x_plant_access_token",
    "plant_id",
    "plant_external_id",
    "authorization",
    "client_secret",
    "api_key",
    "email",
    "username",
)
_SECRET_KEYS = {name.replace("_", "") for name in _SECRET_NAMES}
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:"
    + "|".join(name.replace("_", "[_-]?") for name in _SECRET_NAMES)
    + r"\b)[\"']?\s*[:=]\s*)"
    + r"(?:\"(?:\\.|[^\"\\])*(?:\"|\\?\Z)|'(?:\\.|[^'\\])*(?:'|\\?\Z)|[^\s,;&}\]\"']+)"
)
_AUTHORIZATION = re.compile(r"(?i)\b(Bearer|Basic)\s+[^\s\"'<>;,]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PLANT_ID = re.compile(r"(?<!\d)\d{15}(?!\d)")


def _redact_text(value: str) -> str:
    value = _AUTHORIZATION.sub(lambda match: match[1] + " " + _REDACTED, value)
    value = _ASSIGNMENT.sub(lambda match: match[1] + _REDACTED, value)
    value = _JWT.sub(_REDACTED, value)
    value = _EMAIL.sub(_REDACTED, value)
    return _PLANT_ID.sub(_REDACTED, value)


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            _redact_text(key): (
                _REDACTED
                if re.sub(r"[_-]", "", key).lower() in _SECRET_KEYS
                else _redact_json(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return _redact_text(value) if isinstance(value, str) else value


def redact_remote_error_body(body: str) -> str:
    """Redact JSON and free-text credentials, then limit the logged excerpt."""
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):
        redacted = _redact_text(body)
    else:
        try:
            redacted = json.dumps(_redact_json(value), ensure_ascii=False)
        except RecursionError:
            return "<nested error response omitted>"
    return redacted[:200] or "<empty>"

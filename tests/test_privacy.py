"""Remote error bodies must keep explanations without leaking credentials."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "custom_components/hoval_connect/privacy.py"
_SPEC = importlib.util.spec_from_file_location("hoval_privacy_test", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
redact_remote_error_body = _MODULE.redact_remote_error_body


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "password",
        "id_token",
        "accessToken",
        "plantAccessToken",
        "X-Plant-Access-Token",
        "client_secret",
    ],
)
def test_json_credentials_are_redacted_even_when_nested(key):
    body = json.dumps({"detail": "Denied", "nested": [{key: "opaque-example-secret"}]})
    result = redact_remote_error_body(body)
    assert "opaque-example-secret" not in result
    assert "Denied" in result
    assert "REDACTED" in result


@pytest.mark.parametrize(
    "body",
    [
        "token=opaque-example-secret",
        'error: "password": "opaque-example-secret"',
        "error: 'access_token': 'opaque-example-secret'",
        "Bearer opaque-example-secret",
        "Basic opaque-example-secret",
        "Authorization: Bearer opaque-example-secret",
        "Authorization: Basic opaque-example-secret",
        'error: "password": "prefix opaque-example-secret',
        "error: 'password': 'prefix opaque-example-secret",
        'error: "password": "prefix opaque-example-secret\\',
        "email=user@example.invalid",
        "Failed for user@example.invalid",
        "/plants/123456789012345/circuits",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl",
    ],
)
def test_free_text_and_malformed_json_redacted(body):
    result = redact_remote_error_body(body)
    assert "opaque-example-secret" not in result
    assert "user@example.invalid" not in result
    assert "123456789012345" not in result
    assert "eyJhbGci" not in result
    assert "REDACTED" in result


@pytest.mark.parametrize("key", ["plant_id", "plantExternalId", "plantId"])
def test_numeric_plant_identifiers_redacted(key):
    result = redact_remote_error_body(json.dumps({key: 123456789012345}))
    assert "123456789012345" not in result
    assert "REDACTED" in result


def test_sanitizes_before_truncating_and_preserves_short_errors():
    assert redact_remote_error_body('{"detail":"plant not assigned"}') == (
        '{"detail": "plant not assigned"}'
    )
    result = redact_remote_error_body(json.dumps({"token": "s" * 500, "detail": "Denied"}))
    assert "sssss" not in result
    assert "Denied" in result
    assert len(redact_remote_error_body("x" * 501)) == 200
    assert redact_remote_error_body("") == "<empty>"

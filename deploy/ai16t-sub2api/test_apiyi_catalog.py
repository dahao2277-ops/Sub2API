from __future__ import annotations

from apiyi_catalog import _sanitize


def test_catalog_sanitizer_removes_secret_fields_and_material() -> None:
    secret = "sk-example-private-value"
    payload = {
        "data": [{"id": "stable-model", "note": secret}],
        "authorization": "Bearer " + secret,
        "nested": {"token": "not-safe", "label": "ok"},
    }
    result = _sanitize(payload, secret)
    assert "authorization" not in result
    assert "token" not in result["nested"]
    assert result["data"][0]["note"] == "[REDACTED]"

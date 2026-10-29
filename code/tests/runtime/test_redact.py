"""Tests for the durable error secret-redaction boundary."""

from __future__ import annotations

import pytest

from ceo_arena.runtime._redact import redact_sensitive_text


@pytest.mark.parametrize(
    ("value", "secret"),
    (
        ("OPENAI_API_KEY=plainsecret123", "plainsecret123"),
        ("CEO_ARENA_NEWAPI_API_KEY: plainsecret123", "plainsecret123"),
        ("{'Authorization': 'headersecret123'}", "headersecret123"),
        ('{"X-Api-Key": "headersecret123"}', "headersecret123"),
        ("access_token=tokensecret123", "tokensecret123"),
        ("Bearer bearer-secret", "bearer-secret"),
        ("provider returned sk-fakesecret123", "sk-fakesecret123"),
    ),
)
def test_redact_sensitive_text_covers_labeled_and_prefixed_secrets(
    value: str,
    secret: str,
) -> None:
    redacted = redact_sensitive_text(value)

    assert redacted is not None
    assert secret not in redacted
    assert "[REDACTED]" in redacted

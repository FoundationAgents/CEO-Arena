"""Small secret-redaction boundary for durable runtime errors."""

from __future__ import annotations

import re

_SECRET_LABEL = r"(?:[A-Z0-9]+[_-])*(?:API[_-]?KEY|TOKEN|AUTHORIZATION|SECRET|PASSWORD)"
_SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[^\s,;]+"), "Bearer [REDACTED]"),
    (
        re.compile(
            rf"(?i)(?<![A-Z0-9_-])(?P<label>['\"]?{_SECRET_LABEL}['\"]?)"
            r"(?P<separator>\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]&]+)"
        ),
        r"\g<label>\g<separator>[REDACTED]",
    ),
)


def redact_sensitive_text(value: str | None) -> str | None:
    """Remove common credential forms before an error reaches disk."""

    if value is None:
        return None
    redacted = value
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted

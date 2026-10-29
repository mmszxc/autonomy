"""Credential masking and HTML escaping."""

from __future__ import annotations

import html
import re
from typing import Any

_PATTERNS = (
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}"), "Bearer ***"),
    (
        re.compile(r"(?i)(\bapi[_-]?key\s*[:=]\s*)[\"']?[^\s,;\"']{4,}[\"']?"),
        r"\1[已打码]",
    ),
    (
        re.compile(r"(?i)(\b(?:access[_-]?token|secret)\s*[:=]\s*)[\"']?[^\s,;\"']{4,}[\"']?"),
        r"\1[已打码]",
    ),
)


def redact(value: Any) -> str:
    text = "" if value is None else str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def safe_text(value: Any) -> str:
    return html.escape(redact(value), quote=True)

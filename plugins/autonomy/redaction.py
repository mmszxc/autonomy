"""Shared text redaction before persistence or diagnostic display."""
import re

_RE_ENV_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]{2,}[ \t]*=[^\n]*$")
_RE_KV_SECRET = re.compile(r'''(?ix)(["']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|authorization)\b["']?\s*[:=]\s*)(["']?)[^\s,}\]]+\2''')
_RE_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/=]+")
_RE_URL_AUTH = re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@")
_RE_PRIVATE_KEY = re.compile(r"(?s)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----")
_RE_PREFIX_TOKEN = re.compile(r"\b(?:sk|ghp|gho|xox[bpa]|AKIA)[-_A-Za-z0-9]{12,}\b")
_RE_LONG_RANDOM = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")


def redact(text):
    """Drop suspected secrets, tokens and .env assignment lines. Never raises."""
    s = str(text or "")
    s = _RE_ENV_LINE.sub("[REDACTED_ENV_LINE]", s)
    s = _RE_PRIVATE_KEY.sub("[REDACTED_PRIVATE_KEY]", s)
    s = _RE_URL_AUTH.sub(r"\1[REDACTED]@", s)
    s = _RE_BEARER.sub("Bearer [REDACTED]", s)
    s = _RE_KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]" + m.group(2), s)
    s = _RE_PREFIX_TOKEN.sub("[REDACTED]", s)
    s = _RE_LONG_RANDOM.sub("[REDACTED]", s)
    return s

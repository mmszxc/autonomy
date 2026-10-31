"""Read-only, allowlisted model route status."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..config import _raw_config

_FALLBACK = "Provider fallback"
_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T][0-9:.+-]+)")


def _scalar(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value.strip()[:128] if ch.isprintable())
    if not value or value.startswith(("$", "/", "\\")) or "://" in value:
        return None
    return value


def _route(value: Any, provider: Any = None) -> dict[str, str | None] | None:
    if isinstance(value, dict):
        provider, value = value.get("provider", provider), value.get("model")
    model = _scalar(value)
    provider = _scalar(provider)
    return {"provider": provider, "model": model} if provider or model else None


def _reason(line: str) -> tuple[str, str]:
    text = line.lower()
    for needles, code, label in (
        (("timeout", "timed out"), "timeout", "请求超时"),
        (("429", "rate limit"), "rate_limited", "触发限流"),
        (("401", "403", "unauthorized", "auth"), "auth", "认证失败"),
        (("context length", "context window"), "context_too_long", "上下文超限"),
        (("connection", "refused", "dns"), "connection", "连接失败"),
        (("404", "model not found"), "model_unavailable", "模型不可用"),
        (("502", "503", "504", "unavailable"), "upstream", "上游不可用"),
    ):
        if any(item in text for item in needles):
            return code, label
    return "provider_error", "Provider 请求失败"


def _last_fallback(home: Path) -> dict[str, str | None] | None:
    log_dir = home / "logs"
    try:
        files = sorted((p for p in log_dir.glob("*.log*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)[:7]
    except OSError:
        return None
    latest: tuple[str, str] | None = None
    for path in files:
        try:
            with path.open("rb") as handle:
                size = path.stat().st_size
                handle.seek(max(0, size - 262144))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if _FALLBACK not in line:
                continue
            match = _TIME.match(line)
            stamp = match.group(1) if match else ""
            if latest is None or stamp > latest[0]:
                latest = stamp, line
            break
    if latest is None:
        return None
    code, reason = _reason(latest[1])
    return {"time": latest[0] or None, "reason_code": code, "reason": reason}


def load(home: Path, profile: str) -> dict[str, Any]:
    cfg = _raw_config(home)
    configured = bool(cfg)
    primary = _route(cfg.get("model"), cfg.get("provider")) if configured else None
    fallbacks = []
    raw = cfg.get("fallback_providers", []) if configured else []
    if isinstance(raw, list):
        for item in raw:
            route = _route(item)
            if route:
                fallbacks.append(route)
    event = _last_fallback(home)
    return {
        "profile": profile,
        "config_status": "loaded" if configured else "unavailable",
        "primary": primary,
        "fallback_providers": fallbacks,
        "last_fallback": event,
        "summary": "最近发生过模型降级" if event else "一直使用主路由",
    }

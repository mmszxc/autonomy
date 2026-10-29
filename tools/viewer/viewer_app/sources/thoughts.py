"""Read and parse autonomy thought log rotations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..redact import safe_text

_HEADER = re.compile(
    r"^=== \[(?P<ts>[^]]+)\] run_id=(?P<run_id>\S+) status=(?P<status>\S+)"
    r"(?: mode=(?P<mode>\S+))?(?: model=(?P<model>\S+))?"
    r" \((?P<tokens>[^)]*)\) ===$"
)
MAX_BYTES = 4 * 1024 * 1024
MAX_BODY = 200_000


def _tail(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - MAX_BYTES))
        text = handle.read().decode("utf-8", errors="replace")
    if size > MAX_BYTES:
        marker = text.find("=== [")
        return text[marker:] if marker >= 0 else ""
    return text


def _finish(entry: dict[str, Any]) -> dict[str, Any]:
    body = "\n".join(entry.pop("_body", [])).strip()
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n……[查看器已截断]"
    elapsed = re.search(r"elapsed\s*[=:]\s*([0-9.]+)", body, re.I)
    prefetch = re.search(
        r"(?:本轮预取记忆|预取记忆|prefetch(?:ed)?(?:_count)?)[^\n]{0,48}?"
        r"(?:count\s*[=: ]\s*|[=: ]\s*)([0-9]+)",
        body,
        re.I,
    )
    tools = [
        line.strip()
        for line in body.splitlines()
        if re.search(r"tool|工具|seed_(?:search|add|update)|写入|文件", line, re.I)
    ][:100]
    summary = ""
    for marker in ("结论", "下一步", "未完成", "【结束原因】"):
        position = body.find(marker)
        if position >= 0:
            summary = body[position : position + 500].strip()
            break
    if not summary:
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        summary = (lines[-1] if lines else "")[:500]
    entry.update(
        elapsed=float(elapsed.group(1)) if elapsed else None,
        prefetch_count=int(prefetch.group(1)) if prefetch else None,
        summary=safe_text(summary),
        body=safe_text(body),
        tools=[safe_text(line) for line in tools],
    )
    for key in ("ts", "run_id", "status", "mode", "model"):
        entry[key] = safe_text(entry.get(key) or "")
    return entry


def parse(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        match = _HEADER.match(line.strip())
        if match:
            if current:
                entries.append(_finish(current))
            current = match.groupdict()
            current["tokens"] = {
                key: int(value)
                for key, value in re.findall(
                    r"(prompt|completion|total)=([0-9]+)", match.group("tokens") or ""
                )
            }
            current["_body"] = []
        elif current:
            current["_body"].append(line)
    if current:
        entries.append(_finish(current))
    return entries


def load(log_path: Path, count: int = 20) -> list[dict[str, Any]]:
    count = max(1, min(int(count), 100))
    paths = [Path(f"{log_path}.{number}") for number in (3, 2, 1)] + [log_path]
    entries: list[dict[str, Any]] = []
    for path in paths:
        text = _tail(path)
        if text:
            entries.extend(parse(text))
    return list(reversed(entries[-count:]))

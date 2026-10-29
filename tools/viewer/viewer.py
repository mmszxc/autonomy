#!/usr/bin/env python3
"""Entry point for the dependency-free Hermes read-only viewer."""

from __future__ import annotations

from viewer_app.config import parse_config
from viewer_app.server import serve


def main() -> int:
    config = parse_config()
    serve(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

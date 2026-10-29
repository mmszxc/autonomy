"""Command-line and environment configuration."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class ViewerConfig:
    profile: str
    profile_home: Path
    host: str = "0.0.0.0"
    port: int = 8765
    token: str = ""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes 小智自主思考与种子只读查看器")
    parser.add_argument("--profile", default=os.getenv("HERMES_VIEWER_PROFILE", "xiaozhi"))
    parser.add_argument("--profile-home", default=os.getenv("HERMES_VIEWER_HOME"))
    parser.add_argument("--host", default=os.getenv("HERMES_VIEWER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_VIEWER_PORT", "8765")))
    parser.add_argument("--token", default=os.getenv("HERMES_VIEWER_TOKEN", ""))
    return parser


def parse_config(argv: Sequence[str] | None = None) -> ViewerConfig:
    args = build_parser().parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.profile):
        raise SystemExit("invalid profile name")
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    home = (
        Path(args.profile_home).expanduser()
        if args.profile_home
        else Path.home() / ".hermes" / "profiles" / args.profile
    ).resolve()
    if not home.is_dir():
        raise SystemExit(f"profile directory does not exist: {home}")
    return ViewerConfig(args.profile, home, args.host, args.port, args.token)

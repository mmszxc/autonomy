"""Command-line and environment configuration."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


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


def _raw_config(home: Path) -> dict[str, Any]:
    path = home / "config.yaml"
    try:
        import yaml  # type: ignore
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return value if isinstance(value, dict) else {}
    except (ImportError, OSError, ValueError):
        return {}


def resolve_paths(home: Path) -> dict[str, Path]:
    """Mirror autonomy/seed_memory path precedence without writing anything."""
    cfg = _raw_config(home)
    auto = cfg.get("autonomy") if isinstance(cfg.get("autonomy"), dict) else {}
    paths = auto.get("paths") if isinstance(auto.get("paths"), dict) else {}
    memory = cfg.get("memory") if isinstance(cfg.get("memory"), dict) else {}
    seed = memory.get("seed_memory") if isinstance(memory.get("seed_memory"), dict) else {}
    data_dir = Path(auto.get("data_dir") or paths.get("data_dir") or home / "autonomy_data").expanduser()
    explicit_db = paths.get("db_path") or auto.get("db_path")
    db = Path(explicit_db).expanduser() if explicit_db else (data_dir / "autonomy.db" if (data_dir / "autonomy.db").is_file() else home / "autonomy" / "autonomy.db")
    thought = Path(auto.get("thought_log") or paths.get("thought_log") or data_dir / "thoughts.log").expanduser()
    seed_db_value = seed.get("db_path")
    seed_db = Path(seed_db_value).expanduser() if seed_db_value else home / "seed_data" / "seeds" / "seeds.db"
    if not seed_db.is_file() and (home / "seed_data" / "seeds.db").is_file():
        seed_db = home / "seed_data" / "seeds.db"
    queue_value = seed.get("queue_dir")
    queue = Path(queue_value).expanduser() if queue_value else home / "seed_data" / "seed_queue"
    if not queue.is_dir() and (home / "seed_queue").is_dir():
        queue = home / "seed_queue"
    return {"data_dir": data_dir, "autonomy_db": db, "thought_log": thought, "seed_db": seed_db, "queue": queue}

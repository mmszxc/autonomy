"""SQLite connections that cannot write to profile databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote


def connect(path: Path) -> sqlite3.Connection:
    absolute = path.expanduser().resolve()
    uri = "file:" + quote(absolute.as_posix(), safe="/:~") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection

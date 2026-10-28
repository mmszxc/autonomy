"""把本机种子库 hermes_home/seeds/seeds.db 导出为 JSON 归档 + 清单。

严格只读源库：用 sqlite3 的 "file:...?mode=ro" URI 打开，不写、不移动、不删源库，
也不碰源库的 -wal / -shm（只读取它们的元信息）。不连接任何服务器。
不打印 embedding 的数值内容（只打印字节数）。

用法：
    python tools/export_seeds_archive.py
    python tools/export_seeds_archive.py --db hermes_home/seeds/seeds.db --out data/seeds_archive

产出：
    data/seeds_archive/seed-<id>.json   每行一个 JSON 文件，字段原样保留，BLOB 用 base64
    data/seeds_archive/manifest.json    源库 hash/大小、各表行数、每个导出文件的 hash
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = "hermes_home/seeds/seeds.db"
DEFAULT_OUT = "data/seeds_archive"
PREFERRED_MAIN_TABLE = "seeds"
PREFERRED_ID_COLUMN = "id"


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = None
    return connection


def list_user_tables(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = []
    for name, sql in rows:
        columns = [
            {
                "name": col[1],
                "type": col[2],
                "notnull": bool(col[3]),
                "default": col[4],
                "pk_index": col[5],
            }
            for col in connection.execute(f'PRAGMA table_info("{name}")').fetchall()
        ]
        count = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        tables.append({"name": name, "sql": sql, "columns": columns, "row_count": count})
    return tables


def pick_main_table(tables: list[dict]) -> dict:
    named = next((t for t in tables if t["name"] == PREFERRED_MAIN_TABLE), None)
    if named is not None:
        return named
    return max(tables, key=lambda t: (t["row_count"], len(t["columns"])))


def pick_id_column(table: dict) -> str:
    names = [c["name"] for c in table["columns"]]
    if PREFERRED_ID_COLUMN in names:
        return PREFERRED_ID_COLUMN
    for column in names:
        if column.lower() in {"seed_id", "uid", "key"}:
            return column
    return names[0]


def encode_row(row: tuple, columns: list[dict]) -> dict:
    """把一行转成 dict；BLOB 转 base64 并加 <列名>_encoding='base64'。"""
    record: dict = {}
    for column, value in zip(columns, row):
        name = column["name"]
        if isinstance(value, (bytes, bytearray)):
            record[name] = base64.b64encode(bytes(value)).decode("ascii")
            record[f"{name}_encoding"] = "base64"
            record[f"{name}_bytes"] = len(value)
        else:
            record[name] = value
    return record


def safe_filename(stem: str) -> str:
    cleaned = str(stem).replace("/", "_").replace("\\", "_").replace(":", "_")
    return cleaned.strip() or "unnamed"


def main() -> int:
    parser = argparse.ArgumentParser(description="只读导出 seeds.db 为 JSON 归档")
    parser.add_argument("--db", default=DEFAULT_DB, help="源库路径（只读打开）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="归档输出目录")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir

    if not db_path.is_file():
        print(f"源库不存在：{db_path}")
        return 2

    source_sha = sha256_of_file(db_path)
    source_size = db_path.stat().st_size

    connection = connect_read_only(db_path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = list_user_tables(connection)
        if not tables:
            print("源库里没有用户表")
            return 2

        main_table = pick_main_table(tables)
        id_column = pick_id_column(main_table)
        columns = main_table["columns"]

        out_dir.mkdir(parents=True, exist_ok=True)
        exported: list[dict] = []
        seen_ids: set[str] = set()

        cursor = connection.execute(f'SELECT * FROM "{main_table["name"]}"')
        for row in cursor.fetchall():
            record = encode_row(row, columns)
            raw_id = record.get(id_column)
            if raw_id is None:
                print(f"跳过 {main_table['name']} 中 {id_column} 为 NULL 的行")
                continue
            seed_id = str(raw_id)
            if seed_id in seen_ids:
                print(f"重复 id，跳过：{seed_id}")
                continue
            seen_ids.add(seed_id)

            file_name = f"seed-{safe_filename(seed_id)}.json"
            file_path = out_dir / file_name
            payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
            file_path.write_text(payload, encoding="utf-8")
            exported.append(
                {
                    "name": file_name,
                    "seed_id": seed_id,
                    "sha256": sha256_of_file(file_path),
                    "size_bytes": file_path.stat().st_size,
                }
            )

        sidecars = {}
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.exists():
                sidecars[sidecar.name] = sidecar.stat().st_size

        manifest = {
            "generated_at": utc_now_iso(),
            "tool": "tools/export_seeds_archive.py",
            "source_db": {
                "path": db_path.as_posix(),
                "sha256": source_sha,
                "size_bytes": source_size,
                "journal_mode": journal_mode,
                "sidecar_sizes": sidecars,
                "opened_read_only": True,
            },
            "tables": {
                table["name"]: {
                    "row_count": table["row_count"],
                    "columns": table["columns"],
                    "create_sql": table["sql"],
                }
                for table in tables
            },
            "main_table": main_table["name"],
            "id_column": id_column,
            "exported_file_count": len(exported),
            "blob_handling": "BLOB 列导出为 base64 字符串，并加 <列名>_encoding=base64 与 <列名>_bytes",
            "files": sorted(exported, key=lambda item: item["name"]),
        }

        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        connection.close()

    print(f"源库       : {db_path.as_posix()}")
    print(f"源库 SHA256: {source_sha[:16]}...  ({source_size} bytes, journal_mode={journal_mode})")
    for table in tables:
        print(f"表 {table['name']:<10} 行数={table['row_count']} 列数={len(table['columns'])}")
    print(f"主表       : {main_table['name']} (id 列 = {id_column})")
    print(f"导出文件数 : {len(exported)} -> {out_dir.as_posix()}/")
    print(f"清单       : {manifest_path.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

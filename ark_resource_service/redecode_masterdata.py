"""Re-decode selected raw MasterData tables with the bundled schemas.

This is intentionally an explicit maintenance command.  It does not run at
service startup and always preserves the previous canonical JSON in a backup
directory before replacing it.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile

from .memory_unpacker import MemoryUnpacker


EXPECTED_ROOT_KEYS = {
    "activity_table": "BasicInfo",
    "item_table": "Items",
    "roguelike_topic_table": "Details",
}


def latest_raw(root: Path, table: str) -> Path:
    candidates = list((root / "gamedata" / "excel").glob(f"{table}*.bytes"))
    if not candidates:
        raise FileNotFoundError(f"raw table not found: {table}")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as output:
        json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
        temporary = Path(output.name)
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tables", nargs="+", choices=sorted(EXPECTED_ROOT_KEYS))
    parser.add_argument("--resource-root", type=Path, default=Path("/output"))
    parser.add_argument("--unpacker-root", type=Path, default=Path("/opt/ark-unpacker"))
    parser.add_argument("--backup-root", type=Path)
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = args.backup_root or args.resource_root / ".masterdata-backups" / f"{stamp}-schema-refresh"
    decoder = MemoryUnpacker(args.unpacker_root, args.resource_root, Path("/tmp"), frozenset({"text"}))

    decoded_tables = []
    for table in args.tables:
        source = latest_raw(args.resource_root, table)
        schema, decoded = decoder._decode_masterdata(source.read_bytes(), source.stem)  # noqa: SLF001
        if schema != table:
            raise RuntimeError(f"unexpected schema for {source.name}: {schema}")
        required_key = EXPECTED_ROOT_KEYS[table]
        if not isinstance(decoded, dict) or required_key not in decoded:
            raise RuntimeError(f"decoded {table} is missing root key {required_key}")
        decoded_tables.append((table, source, decoded))

    for table, source, decoded in decoded_tables:
        target = args.resource_root / "masterdata" / f"{table}.json"
        backup_root.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            shutil.copy2(target, backup_root / target.name)
        atomic_json_write(target, decoded)
        print(f"{table}: {source.name} -> {target} (backup: {backup_root})")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Audit LevelId references and stage verified, immutable historical JSON files.

Use publish_backfill.py after reviewing the report. The source commit and
Git blob/SHA-256 hashes are recorded, and live resources are never modified.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path, PurePosixPath
import time
from urllib.request import Request, urlopen

REPOSITORY = "Kengxxiao/ArknightsGameData"
PREFIX = "zh_CN/gamedata/levels/"


def fetch(url: str, limit: int = 16 * 1024 * 1024) -> bytes:
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "ArkResource-HistoricalBackfill/1.0"}), timeout=30) as response:
                payload = response.read(limit + 1)
            if len(payload) > limit:
                raise ValueError("source exceeds size limit")
            return payload
        except Exception:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise AssertionError("unreachable")


def valid_level_id(value: str) -> bool:
    parts = PurePosixPath(value).parts
    return (len(parts) >= 2 and parts[0] in {"activities", "obt", "rogue", "sandbox"}
            and ".." not in parts and not value.startswith("/"))


def collect_references(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    refs: dict[str, list[str]] = {}
    ignored: set[str] = set()

    def walk(value, source):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() == "levelid" and isinstance(item, str) and item:
                    level = item.lower()
                    if valid_level_id(level):
                        refs.setdefault(level, []).append(source)
                    else:
                        ignored.add(item)
                else:
                    walk(item, source)
        elif isinstance(value, list):
            for item in value:
                walk(item, source)

    for path in sorted((root / "MasterData").glob("*.json")):
        walk(json.loads(path.read_text(encoding="utf-8")), path.name)
    return refs, sorted(ignored)


def validate_level(payload: bytes) -> dict:
    def invalid_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")
    level = json.loads(payload.decode("utf-8"), parse_constant=invalid_constant)
    grid = level["mapData"]["map"]
    tiles = level["mapData"]["tiles"]
    if not grid or not grid[0] or any(len(row) != len(grid[0]) for row in grid):
        raise ValueError("invalid map dimensions")
    if any(type(index) is not int or not 0 <= index < len(tiles) for row in grid for index in row):
        raise ValueError("invalid map tile index")
    if not isinstance(level["routes"], list) or not isinstance(level["waves"], list):
        raise ValueError("missing routes or waves")
    return level


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--revision", required=True, help="Immutable source commit SHA")
    parser.add_argument("--source-tree", type=Path)
    parser.add_argument("--source-prefix", default=PREFIX,
                        choices=[f"{region}/gamedata/levels/" for region in
                                 ("zh_CN", "zh_TW", "en_US", "ja_JP", "ko_KR")],
                        help="Use another region only after checking the exact stage identity and data")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    if len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision):
        parser.error("revision must be a full commit SHA")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    refs, ignored = collect_references(args.root)
    tree = json.loads(args.source_tree.read_text()) if args.source_tree else json.loads(fetch(
        f"https://api.github.com/repos/{REPOSITORY}/git/trees/{args.revision}?recursive=1"))
    if tree.get("truncated"):
        raise RuntimeError("source tree is truncated")
    sources = {entry["path"][len(args.source_prefix):-5].lower(): entry for entry in tree["tree"]
               if entry["path"].startswith(args.source_prefix) and entry["path"].endswith(".json")}
    levels = args.root / "gamedata" / "levels"
    missing = [level for level in refs if not any((levels / level).with_suffix(suffix).is_file()
                                                for suffix in (".bytes", ".json"))]
    matched = {level: sources[level] for level in missing if level in sources}
    report = {"repository": REPOSITORY, "revision": args.revision, "sourcePrefix": args.source_prefix,
              "referencedLevels": len(refs), "missingBefore": len(missing),
              "sourceMatches": len(matched), "unavailable": [x for x in missing if x not in sources],
              "ignoredMetadataIdentifiers": ignored, "files": {}, "failures": {}}

    def download(level, source):
        target = (args.work_dir / "staged" / level).with_suffix(".json")
        payload = target.read_bytes() if target.exists() else fetch(
            f"https://raw.githubusercontent.com/{REPOSITORY}/{args.revision}/{source['path']}")
        blob = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
        if blob != source["sha"]:
            raise ValueError("Git blob checksum mismatch")
        decoded = validate_level(payload)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return {"sourcePath": source["path"], "blobSha": blob,
                "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                "rows": len(decoded["mapData"]["map"]),
                "columns": len(decoded["mapData"]["map"][0]),
                "waves": len(decoded["waves"]), "tables": sorted(set(refs[level]))}

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        futures = {pool.submit(download, level, source): level for level, source in matched.items()}
        for count, future in enumerate(as_completed(futures), 1):
            level = futures[future]
            try:
                report["files"][level] = future.result()
            except Exception as error:
                report["failures"][level] = type(error).__name__
            if count % 50 == 0 or count == len(futures):
                print(f"checked {count}/{len(futures)}, failures={len(report['failures'])}", flush=True)

    report["verified"] = len(report["files"])
    (args.work_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("referencedLevels", "missingBefore", "sourceMatches", "verified")}
                     | {"unavailable": len(report["unavailable"]), "failures": len(report["failures"]),
                        "published": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()

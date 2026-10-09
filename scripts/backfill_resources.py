#!/usr/bin/env python3
"""Recover removed bundles from explicit old resource versions into an archive.

Uses the configured CDN without printing it. Does not modify the current
manifest, unpack records, schemas or resource output. Publication is a separate
missing-file-only operation after inspection of the staged exports.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ark_resource_service.app import service, dat_name, atomic_json, safe_member
from ark_resource_service.memory_unpacker import MemoryUnpacker


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="append", required=True, help="Newest first")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", version) for version in args.version):
        parser.error("invalid resource version")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    current = json.loads((service.root / "State/hot_update_list.json").read_text())
    names = {a["name"] for a in current["abInfos"]}
    service.client.discover()
    candidates = {}
    manifests = args.work_dir / "manifests"
    manifests.mkdir(exist_ok=True)
    bases = {}
    for version in args.version:
        base, manifest = service.client.manifest(version)
        bases[version] = base
        atomic_json(manifests / f"{version}.json", manifest)
        for asset in manifest["abInfos"]:
            safe_member(asset["name"])
            if asset["name"] not in names:
                candidates.setdefault(asset["name"], (version, asset))
    report_path = args.work_dir / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {
        "currentVersion": current["versionId"], "versions": args.version,
        "candidates": len(candidates), "downloadBytes": sum(a.get("totalSize", 0) for _, a in candidates.values()),
        "completed": {}, "failures": {},
    }
    unpackers = {version: MemoryUnpacker(service.unpacker_root, args.work_dir / "staged" / version,
                                        args.work_dir / "logs" / version, service.export_types - {"masterdata"},
                                        service.typetree_types, service.schema_sync.modules_dir)
                 for version in args.version}

    def recover(version, asset):
        name = asset["name"]
        reservation = service.asset_reservation(asset)
        service.memory_budget.acquire(reservation)
        try:
            archive = service.client.download_bytes(f"{bases[version]}/{dat_name(name)}",
                max(int(asset.get("totalSize", 0)) + 1024 * 1024, 2 * 1024 * 1024))
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                data = source.read(name)
            expected_md5 = str(asset.get("md5", ""))
            md5_verifiable = len(expected_md5) == 32
            if (len(data) != asset["abSize"]
                    or (md5_verifiable and hashlib.md5(data).hexdigest() != expected_md5)):
                raise ValueError("bundle checksum mismatch")
            unpack_error = None
            try:
                exported = unpackers[version].unpack(data, name, "historical-backfill")
            except Exception as error:
                exported = 0
                unpack_error = type(error).__name__
            raw_archived = exported == 0
            if raw_archived:
                raw_path = args.work_dir / "raw" / version / name
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_bytes(data)
            return {"version": version, "md5": expected_md5, "md5Verified": md5_verifiable,
                    "bundleSha256": hashlib.sha256(data).hexdigest(), "exported": exported,
                    "rawArchived": raw_archived, "unpackError": unpack_error,
                    "outputDir": unpackers[version].relative_destination(name)}
        finally:
            service.memory_budget.release(reservation)

    todo = {name: item for name, item in candidates.items()
            if name not in report["completed"] or (report["completed"][name]["exported"] == 0
                                                   and not report["completed"][name].get("rawArchived"))
            or report["completed"][name].get("unpackError") == "MasterDataDecodeError"}
    print(json.dumps({"candidates": len(candidates), "pending": len(todo),
                      "downloadBytes": report["downloadBytes"]}), flush=True)
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 2))) as pool:
        futures = {pool.submit(recover, version, asset): name for name, (version, asset) in todo.items()}
        for count, future in enumerate(as_completed(futures), 1):
            name = futures[future]
            try:
                report["completed"][name] = future.result()
                report["failures"].pop(name, None)
            except Exception as error:
                report["failures"][name] = type(error).__name__
            atomic_json(report_path, report)
            if count % 20 == 0 or count == len(futures):
                print(json.dumps({"processed": count, "total": len(futures),
                                  "completed": len(report["completed"]),
                                  "failed": len(report["failures"])}), flush=True)


if __name__ == "__main__":
    main()

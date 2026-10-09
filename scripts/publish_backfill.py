#!/usr/bin/env python3
"""Publish archived files only when absent, retaining hashes for safe rollback."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def publish(sources: list[Path], output: Path, report_path: Path, apply: bool) -> dict:
    if report_path.exists():
        previous = json.loads(report_path.read_text())
        if previous.get("applied") and previous.get("published"):
            raise FileExistsError("choose a new report path to preserve the previous publication/rollback record")
    report = {"sources": [str(p) for p in sources], "output": str(output),
              "published": {}, "identicalExisting": 0, "differentExisting": [], "excluded": []}
    for source in sources:
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source)
            if any(part.startswith(".") for part in relative.parts) or relative.parts[0].lower() == "masterdata":
                report["excluded"].append(relative.as_posix())
                continue
            target = output / relative
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                    report["identicalExisting"] += 1
                else:
                    report["differentExisting"].append(relative.as_posix())
                continue
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Write completely before linking it into place. link() fails
                # rather than overwriting a file created by a concurrent updater.
                fd, temporary = tempfile.mkstemp(prefix=".backfill-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(path.read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        report["differentExisting"].append(relative.as_posix())
                        continue
                finally:
                    os.unlink(temporary)
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValueError(f"publication checksum mismatch: {relative}")
            report["published"][relative.as_posix()] = {"sha256": digest, "bytes": path.stat().st_size,
                                                        "source": str(source)}
    report["applied"] = apply
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if any(not path.is_dir() for path in args.source):
        parser.error("all source directories must exist")
    result = publish(args.source, args.output, args.report, args.publish)
    print(json.dumps({"published" if args.publish else "planned": len(result["published"]),
                      "identicalExisting": result["identicalExisting"],
                      "differentExisting": len(result["differentExisting"]),
                      "excluded": len(result["excluded"])}))


if __name__ == "__main__":
    main()

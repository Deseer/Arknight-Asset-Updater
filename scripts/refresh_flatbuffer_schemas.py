#!/usr/bin/env python3
"""Compile pinned current CN FlatBuffers schemas into Ark-Unpacker's modules."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
from urllib.request import urlopen


REVISION = "7855e1ab8b7b122d44930c7d3a754a8e2fc39fdd"
SCHEMAS = {
    "activity_table": "115994a5d14b80d3506d2328424e1d398a5ff2f51fbb92918804622032616b67",
    "item_table": "19c61659d708e63929946da41d6d09dd849af157b32277f281ba910cac13eb91",
    "building_data": "75f9ae64a5a66fc2426a01a1438a64b0111666ff64ddffbaf427690ed3758a6f",
    "roguelike_topic_table": "88bdb93fa8156701aac08bfec5c005e0435f847d9bca13a265dae772e466c68a",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flatc", default="flatc")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, help="Use checksum-verified local schema sources")
    args = parser.parse_args()

    args.destination.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    for name, expected_sha256 in SCHEMAS.items():
        url = (
            "https://raw.githubusercontent.com/ArknightsAssets/ArknightsFlatbuffers/"
            f"{REVISION}/cn/{name}.fbs"
        )
        if args.source_dir:
            schema = (args.source_dir / f"{name}.fbs").read_bytes()
        else:
            with urlopen(url, timeout=60) as response:
                schema = response.read()
        actual_sha256 = hashlib.sha256(schema).hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(f"schema checksum mismatch for {name}: {actual_sha256}")

        source = args.work_dir / f"{name}.fbs"
        source.write_bytes(schema)
        subprocess.run(
            [args.flatc, "--python", "--gen-onefile", "-o", str(args.work_dir), str(source)],
            check=True,
        )
        generated = args.work_dir / f"{name}_generated.py"
        root_match = re.search(rb"^root_type\s+(.+);\s*$", schema, re.MULTILINE)
        if not root_match:
            raise RuntimeError(f"schema has no root_type: {name}")
        root_type = root_match.group(1).decode("ascii")
        output = generated.read_bytes().rstrip() + f"\n\nROOT_TYPE = {root_type}\n".encode()
        (args.destination / f"{name}.py").write_bytes(output)


if __name__ == "__main__":
    main()

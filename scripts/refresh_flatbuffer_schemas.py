#!/usr/bin/env python3
"""Compile pinned current CN FlatBuffers schemas into Ark-Unpacker's modules."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess
from urllib.request import urlopen


REVISION = "58293a5c389bf877c00a45b593470d406ffdba73"
SCHEMAS = {
    "activity_table": "13cb9f08a457db453392a9be3290d957fc2e32d0f52bf32968c34b7e532696c3",
    "item_table": "98f0d0a944502dbf4343032ab4dd76b61619d10a39b1e1fcaa1dd379c73e992d",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flatc", default="flatc")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()

    args.destination.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    for name, expected_sha256 in SCHEMAS.items():
        url = (
            "https://raw.githubusercontent.com/ArknightsAssets/ArknightsFlatbuffers/"
            f"{REVISION}/cn/{name}.fbs"
        )
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

"""Fetch and compile immutable CN schema snapshots, preserving the last usable set."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

import requests


LOGGER = logging.getLogger("uvicorn.error")
REPOSITORY = "ArknightsAssets/ArknightsFlatbuffers"


def load_modules(directory: Path) -> list:
    modules = []
    for path in sorted(directory.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"_ark_schema_{directory.parent.name}.{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "ROOT_TYPE"):
            raise ValueError(f"missing ROOT_TYPE: {path.name}")
        modules.append(module)
    if not modules:
        raise ValueError("empty schema snapshot")
    return modules


class SchemaSync:
    def __init__(self, root: Path, interval: float = 1800, proxy: str = ""):
        self.root = root
        self.interval = max(60, interval)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "ArkResourceService-schema-sync/1.0"
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.snapshot = None
        self.error = None
        self.next_check = 0.0
        try:
            pointer = json.loads((root / "current.json").read_text())
            revision = pointer["revision"]
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("invalid cached revision")
            snapshot = json.loads((root / revision / "manifest.json").read_text())
            if snapshot["revision"] != revision:
                raise ValueError("cached revision mismatch")
            for name, sha in snapshot["modules"].items():
                if not re.fullmatch(r"[a-zA-Z0-9_]+", name):
                    raise ValueError("invalid cached module name")
                if hashlib.sha256((root / revision / "modules" / f"{name}.py").read_bytes()).hexdigest() != sha:
                    raise ValueError(f"cached module checksum mismatch: {name}")
            load_modules(root / revision / "modules")
            self.snapshot = snapshot
            # Persist polling time across the updater's deliberate recycles.
            self.next_check = float(pointer.get("checkedAt", 0)) + self.interval
        except FileNotFoundError:
            pass
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("schema_cache_unusable: %s", self.error)

    @property
    def hashes(self) -> dict[str, str]:
        return self.snapshot["sources"] if self.snapshot else {}

    @property
    def modules_dir(self) -> Path | None:
        return self.root / self.snapshot["revision"] / "modules" if self.snapshot else None

    def _save_pointer(self, revision: str, checked_at: float) -> None:
        temporary = self.root / "current.tmp"
        temporary.write_text(json.dumps({"revision": revision, "checkedAt": checked_at}))
        os.replace(temporary, self.root / "current.json")

    def refresh(self) -> bool:
        if time.time() < self.next_check:
            return False
        checked_at = time.time()
        self.next_check = checked_at + self.interval
        staging = None
        try:
            response = self.session.get(f"https://api.github.com/repos/{REPOSITORY}/commits/main", timeout=(10, 25))
            response.raise_for_status()
            revision = response.json()["sha"]
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("invalid upstream revision")
            self.root.mkdir(parents=True, exist_ok=True)
            if self.snapshot and self.snapshot["revision"] == revision:
                self._save_pointer(revision, checked_at)
                self.error = None
                return False
            with self.session.get(f"https://codeload.github.com/{REPOSITORY}/tar.gz/{revision}",
                                  stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                archive = bytearray()
                for chunk in response.iter_content(1024 * 1024):
                    archive.extend(chunk)
                    if len(archive) > 32 * 1024 * 1024:
                        raise ValueError("schema archive exceeds size limit")
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
            sources = staging / "sources"
            modules = staging / "modules"
            sources.mkdir()
            modules.mkdir()
            hashes = {}
            prefix = f"ArknightsFlatbuffers-{revision}/cn/"
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar:
                    if not member.name.startswith(prefix):
                        continue
                    name = member.name[len(prefix):]
                    if not re.fullmatch(r"[a-zA-Z0-9_]+\.fbs", name):
                        continue
                    if not member.isfile() or member.size > 4 * 1024 * 1024 or len(hashes) >= 512:
                        raise ValueError("invalid CN schema archive member")
                    data = tar.extractfile(member).read()
                    (sources / name).write_bytes(data)
                    hashes[name[:-4]] = hashlib.sha256(data).hexdigest()
            if not {"character_table", "roguelike_topic_table", "activity_table", "building_data"} <= hashes.keys():
                raise ValueError("incomplete CN schema snapshot")
            subprocess.run(["flatc", "--python", "--gen-onefile", "-o", str(modules),
                            *map(str, sorted(sources.glob("*.fbs")))],
                           check=True, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (modules / "__init__.py").unlink(missing_ok=True)
            for source in sorted(sources.glob("*.fbs")):
                root = re.search(rb"^root_type\s+([a-zA-Z0-9_]+);\s*$", source.read_bytes(), re.MULTILINE)
                if not root:
                    raise ValueError(f"schema has no root_type: {source.name}")
                generated = modules / f"{source.stem}_generated.py"
                output = generated.read_bytes().rstrip() + b"\n\nROOT_TYPE = " + root[1] + b"\n"
                generated.unlink()
                (modules / f"{source.stem}.py").write_bytes(output)
            load_modules(modules)
            snapshot = {"revision": revision, "sources": hashes, "modules": {
                path.stem: hashlib.sha256(path.read_bytes()).hexdigest() for path in modules.glob("*.py")}}
            (staging / "manifest.json").write_text(json.dumps(snapshot))
            destination = self.root / revision
            if destination.exists():
                shutil.rmtree(destination)
            os.replace(staging, destination)
            staging = None
            self._save_pointer(revision, checked_at)
            self.snapshot = snapshot
            self.error = None
            LOGGER.info("schema_sync_activated revision=%s tables=%s", revision, len(hashes))
            return True
        except Exception as exc:
            # Keep URLs/proxy details out of logs and status responses.
            self.error = type(exc).__name__
            self.next_check = checked_at + min(self.interval, 300)
            LOGGER.warning("schema_sync_failed type=%s; retaining previous schemas", self.error)
            return False
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)

    def status(self) -> dict:
        return {"revision": self.snapshot["revision"] if self.snapshot else "bundled",
                "tables": len(self.hashes), "checkSeconds": self.interval,
                "nextCheckAt": self.next_check, "lastError": self.error}

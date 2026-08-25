from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import uuid
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any


class MemoryUnpacker:
    """Adapt Ark-Unpacker's UnityPy/LZ4AK pipeline to in-memory bundle bytes."""

    def __init__(self, unpacker_root: Path, output_root: Path, log_root: Path):
        self.unpacker_root = unpacker_root
        self.output_root = output_root
        self.log_root = log_root
        self._write_lock = threading.Lock()
        root = str(unpacker_root)
        if root not in sys.path:
            sys.path.insert(0, root)

        # Importing ResolveAB registers the Arknights LZ4AK decompressor in UnityPy.
        import UnityPy
        from src.ResolveAB import Resource, TreeReader
        from src.ResolveSpine import SpineAsset
        from src.utils.SaverUtils import SafeSaver

        self.UnityPy = UnityPy
        self.Resource = Resource
        self.TreeReader = TreeReader
        self.SpineAsset = SpineAsset
        self.SafeSaver = SafeSaver

    def _destination(self, asset_name: str) -> Path:
        relative = PurePosixPath(asset_name)
        return self.output_root.joinpath(*relative.with_suffix("").parts)

    def relative_destination(self, asset_name: str) -> str:
        return self._destination(asset_name).relative_to(self.output_root).as_posix()

    def _staging_directory(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / f".ark-staging-{destination.name}-{uuid.uuid4().hex}"
        staging.mkdir(parents=True, exist_ok=False)
        return staging

    def _commit(self, staging: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.previous")
        with self._write_lock:
            if destination.exists():
                os.replace(destination, previous)
            os.replace(staging, destination)
        if previous.exists():
            shutil.rmtree(previous)

    def _write_item(self, destination: Path, item: Any) -> bool:
        destination.mkdir(parents=True, exist_ok=True)
        name = self.SafeSaver._sanitize_dest(str(destination / f"{item.name}{item.ext}"))
        target = Path(name)
        with self._write_lock:
            if target.is_file() and target.read_bytes() == item.data:
                return False
            if target.exists():
                target = Path(self.SafeSaver._purify_name(str(target)))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(item.data)
            os.replace(temporary, target)
        return True

    def unpack_ab(self, data: bytes, asset_name: str) -> int:
        destination = self._destination(asset_name)
        staging = self._staging_directory(destination)
        resource = self.Resource(self.UnityPy.load(BytesIO(data)))
        exported = 0

        # Preserve the AB export behavior and also emit Spine assets directly.
        try:
            for spine in self.SpineAsset.from_resource(resource):
                try:
                    spine.process_path()
                    for item in spine.iter_export_items():
                        exported += int(self._write_item(staging, item))
                except Exception:
                    continue
        except Exception:
            pass

        for roi_type in ("Image", "Text", "Audio", "Mesh"):
            for obj in resource.get_objects_by_roi_type(roi_type):
                try:
                    for item in self.SafeSaver.iter_object_export_items(obj):
                        exported += int(self._write_item(staging, item))
                except Exception:
                    continue

        typetrees: dict[str, Any] = {}
        for obj in resource.env.objects:
            if not hasattr(obj, "read_typetree"):
                continue
            try:
                tree = obj.read_typetree()
                if tree:
                    typetrees[str(obj.path_id)] = tree
            except Exception:
                continue
        if typetrees:
            name = resource.name or PurePosixPath(asset_name).name
            item = self.SafeSaver.ExportItem(
                f"TT_{name}",
                ".json",
                self.SafeSaver.serialize_json_data({name: typetrees}, "utf-8", 4),
            )
            exported += int(self._write_item(staging, item))
        self._commit(staging, destination)
        return exported

    def unpack_usm(self, data: bytes, asset_name: str, job_id: str) -> int:
        # ffmpeg and Ark-Unpacker's USM parser require a path. /dev/shm is tmpfs,
        # so the compressed source never touches the external drive.
        memory_root = Path("/dev/shm/ark-resource-service") / job_id
        memory_root.mkdir(parents=True, exist_ok=True)
        source = memory_root / PurePosixPath(asset_name).name
        source.write_bytes(data)
        destination = self._destination(asset_name)
        staging = self._staging_directory(destination)
        log = self.log_root / f"{job_id}-usm.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "python",
            str(self.unpacker_root / "Main.py"),
            "--input",
            str(source),
            "--output",
            str(staging),
            "--logging-level",
            "3",
            "--mode",
            "cu",
        ]
        try:
            with log.open("a", encoding="utf-8") as output:
                result = subprocess.run(command, cwd=self.unpacker_root, stdout=output, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f"USM unpacker exited {result.returncode}")
            exported = sum(1 for p in staging.rglob("*") if p.is_file())
            self._commit(staging, destination)
            return exported
        finally:
            source.unlink(missing_ok=True)
            try:
                memory_root.rmdir()
            except OSError:
                pass

    def unpack(self, data: bytes, asset_name: str, job_id: str) -> int:
        if asset_name.endswith((".ab", ".bin")):
            return self.unpack_ab(data, asset_name)
        if asset_name.endswith(".usm"):
            return self.unpack_usm(data, asset_name, job_id)
        return 0


def digest(data: bytes) -> tuple[int, str]:
    return len(data), hashlib.md5(data).hexdigest()

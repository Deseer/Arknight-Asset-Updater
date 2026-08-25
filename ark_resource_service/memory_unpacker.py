from __future__ import annotations

import hashlib
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any


EXPORT_TYPE_ALIASES = {
    "image": "image",
    "images": "image",
    "sprite": "image",
    "texture2d": "image",
    "text": "text",
    "textasset": "text",
    "audio": "audio",
    "audioclip": "audio",
    "mesh": "mesh",
    "spine": "spine",
    "video": "video",
    "usm": "video",
    "masterdata": "masterdata",
    "typetree": "typetree",
}


def install_unitypy_gzip_fallback() -> None:
    """Treat false-positive GZIP headers inside Unity bundles as resource data."""
    from UnityPy.helpers import ImportHelper

    current = ImportHelper.check_file_type
    if getattr(current, "_ark_gzip_fallback", False):
        return

    def tolerant_check_file_type(input_):
        try:
            return current(input_)
        except gzip.BadGzipFile:
            if isinstance(input_, ImportHelper.EndianBinaryReader):
                reader = input_
            else:
                reader = ImportHelper.EndianBinaryReader(input_)
            reader.Position = 0
            return ImportHelper.FileType.ResourceFile, reader

    tolerant_check_file_type._ark_gzip_fallback = True
    ImportHelper.check_file_type = tolerant_check_file_type


def parse_export_types(value: str) -> frozenset[str]:
    result = set()
    for item in value.split(","):
        normalized = item.strip().lower()
        if not normalized:
            continue
        if normalized not in EXPORT_TYPE_ALIASES:
            raise ValueError(f"unsupported export type: {item.strip()}")
        result.add(EXPORT_TYPE_ALIASES[normalized])
    return frozenset(result)


class MemoryUnpacker:
    """Adapt Ark-Unpacker's UnityPy/LZ4AK pipeline to in-memory bundle bytes."""

    def __init__(
        self,
        unpacker_root: Path,
        output_root: Path,
        log_root: Path,
        export_types: frozenset[str],
        typetree_types: frozenset[str] = frozenset(),
    ):
        self.unpacker_root = unpacker_root
        self.output_root = output_root
        self.log_root = log_root
        self.export_types = export_types
        self.typetree_types = {value.lower() for value in typetree_types}
        self._write_lock = threading.Lock()
        root = str(unpacker_root)
        if root not in sys.path:
            sys.path.insert(0, root)

        # Importing ResolveAB registers the Arknights LZ4AK decompressor in UnityPy.
        import UnityPy
        from src.ResolveAB import Resource, TreeReader
        from src.ResolveSpine import SpineAsset
        from src.DecodeTextAsset import ArkAESLibrary, ArkFBOLibrary, FBOHandler
        from src.utils.SaverUtils import SafeSaver

        # UnityPy 1.25.3 probes any nested payload beginning with 1f 8b as GZIP.
        # Some valid Arknights resource nodes collide with those two bytes but use a
        # non-GZIP third byte; fall back to ResourceFile instead of aborting the bundle.
        install_unitypy_gzip_fallback()

        self.UnityPy = UnityPy
        self.Resource = Resource
        self.TreeReader = TreeReader
        self.SpineAsset = SpineAsset
        self.ArkAESLibrary = ArkAESLibrary
        self.ArkFBOLibrary = ArkFBOLibrary
        self.FBOHandler = FBOHandler
        self.SafeSaver = SafeSaver

    @staticmethod
    def _clean_component(component: str) -> str:
        # [uc], [pack] and [ucp] are upstream build markers, not user-facing categories.
        cleaned = re.sub(r"^(?:\[(?:ucp?|pack)\])+", "", component, flags=re.IGNORECASE)
        return cleaned or "unnamed"

    @classmethod
    def semantic_container_path(cls, container: str) -> PurePosixPath:
        path = PurePosixPath(container.replace("\\", "/"))
        parts = list(path.parts)
        if parts and parts[0].lower() in {"dyn", "assets"}:
            parts.pop(0)
        return PurePosixPath(*(cls._clean_component(part) for part in parts))

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

    def _write_semantic_item(self, relative: PurePosixPath, item: Any, path_id: int) -> bool:
        relative = self.semantic_container_path(relative.as_posix())
        if item.ext:
            relative = relative.with_suffix(item.ext)
        elif not relative.suffix:
            relative = relative.with_name(item.name)
        target = self.output_root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            if target.is_file():
                if target.read_bytes() == item.data:
                    return False
                target = target.with_name(f"{target.stem}__{path_id}{target.suffix}")
                if target.is_file() and target.read_bytes() == item.data:
                    return False
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(item.data)
            os.replace(temporary, target)
        return True

    @staticmethod
    def _container_map(resource: Any) -> dict[int, list[PurePosixPath]]:
        result: dict[int, list[PurePosixPath]] = {}
        for container, pointer in resource.env.container.items():
            path_id = getattr(pointer, "path_id", None)
            if path_id is None:
                path_id = getattr(getattr(pointer, "m_PathID", None), "value", None)
            if path_id is None:
                continue
            result.setdefault(int(path_id), []).append(PurePosixPath(container))
        return result

    def _decode_masterdata(self, raw: bytes, name: str) -> tuple[str, Any] | None:
        lowered = name.lower()
        candidates = []
        for module in self.ArkFBOLibrary.CN:
            schema = module.__name__.split(".")[-1]
            if schema in lowered:
                candidates.append((schema, getattr(module, "ROOT_TYPE", None)))
        for schema, root_type in sorted(candidates, key=lambda value: len(value[0]), reverse=True):
            if root_type is None or len(raw) <= 128:
                continue
            try:
                return schema, self.FBOHandler(bytearray(raw)[128:], root_type).to_json_dict()
            except Exception:
                continue
        try:
            decrypted = self.ArkAESLibrary.aes_cbc_decrypt_bytes(raw, self.ArkAESLibrary.MASK_V2)
            try:
                return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name), json.loads(decrypted)
            except (UnicodeError, json.JSONDecodeError):
                import bson

                return re.sub(r"[^a-zA-Z0-9_.-]+", "_", name), bson.loads(decrypted)
        except Exception:
            return None

    def unpack_ab(self, data: bytes, asset_name: str) -> int:
        resource = self.Resource(self.UnityPy.load(BytesIO(data)))
        exported = 0
        containers = self._container_map(resource)

        # Preserve the AB export behavior and also emit Spine assets directly.
        try:
            if "spine" not in self.export_types:
                raise StopIteration
            for spine in self.SpineAsset.from_resource(resource):
                try:
                    spine.process_path()
                    for item in spine.iter_export_items():
                        exported += int(
                            self._write_semantic_item(PurePosixPath(f"{item.name}{item.ext}"), item, 0)
                        )
                except Exception:
                    continue
        except (Exception, StopIteration):
            pass

        enabled_roi = {
            "Image": "image",
            "Text": "text",
            "Audio": "audio",
            "Mesh": "mesh",
        }
        image_sprite_paths = {
            path.as_posix()
            for obj in resource.get_objects_by_type(self.UnityPy.classes.Sprite)
            for path in containers.get(obj.object_reader.path_id, [])
        }
        image_sprite_names = {
            getattr(obj, "m_Name", "")
            for obj in resource.get_objects_by_type(self.UnityPy.classes.Sprite)
        }
        for roi_type, export_type in enabled_roi.items():
            if export_type not in self.export_types and not (roi_type == "Text" and "masterdata" in self.export_types):
                continue
            for obj in resource.get_objects_by_roi_type(roi_type):
                reader = obj.object_reader
                object_paths = containers.get(reader.path_id, [])
                if roi_type == "Image" and type(obj).__name__ == "Texture2D":
                    filtered_paths = [path for path in object_paths if path.as_posix() not in image_sprite_paths]
                    if object_paths and not filtered_paths:
                        continue
                    if not object_paths and getattr(obj, "m_Name", "") in image_sprite_names:
                        continue
                    object_paths = filtered_paths
                try:
                    if roi_type == "Text" and "masterdata" in self.export_types:
                        raw = obj.m_Script.encode("utf-8", "surrogateescape")
                        decoded = self._decode_masterdata(raw, getattr(obj, "m_Name", "textasset"))
                        if decoded:
                            schema, value = decoded
                            item = self.SafeSaver.ExportItem(
                                schema,
                                ".json",
                                json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8", errors="surrogateescape"),
                            )
                            exported += int(self._write_semantic_item(PurePosixPath("masterdata") / schema, item, reader.path_id))
                            continue
                    if export_type not in self.export_types:
                        continue
                    for item in self.SafeSaver.iter_object_export_items(obj):
                        if object_paths:
                            for path in object_paths:
                                exported += int(self._write_semantic_item(path, item, reader.path_id))
                        else:
                            fallback_root = self.semantic_container_path(
                                PurePosixPath(asset_name).with_suffix("").as_posix()
                            )
                            fallback = fallback_root / f"{item.name}{item.ext}"
                            exported += int(self._write_semantic_item(fallback, item, reader.path_id))
                except Exception:
                    continue

        if "typetree" in self.export_types:
            for obj in resource.env.objects:
                object_type = getattr(getattr(obj, "type", None), "name", "")
                if object_type.lower() not in self.typetree_types or not hasattr(obj, "read_typetree"):
                    continue
                try:
                    tree = obj.read_typetree()
                    if not tree:
                        continue
                    name = obj.peek_name() or f"{object_type}_{obj.path_id}"
                    item = self.SafeSaver.ExportItem(
                        f"{name}__{obj.path_id}",
                        ".json",
                        self.SafeSaver.serialize_json_data(tree, "utf-8", 2),
                    )
                    object_paths = containers.get(obj.path_id, [])
                    if object_paths:
                        relative = PurePosixPath("metadata") / self.semantic_container_path(
                            object_paths[0].with_suffix("").as_posix()
                        ) / object_type / item.name
                    else:
                        bundle = self.semantic_container_path(PurePosixPath(asset_name).with_suffix("").as_posix())
                        relative = PurePosixPath("metadata") / bundle / object_type / item.name
                    exported += int(self._write_semantic_item(relative, item, obj.path_id))
                except Exception:
                    continue
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
        if asset_name.endswith(".usm") and "video" in self.export_types:
            return self.unpack_usm(data, asset_name, job_id)
        return 0


def digest(data: bytes) -> tuple[int, str]:
    return len(data), hashlib.md5(data).hexdigest()

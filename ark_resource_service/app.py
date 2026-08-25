from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import shutil
import threading
import time
import uuid
import zipfile
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

CONFIG_URL = "https://ak-conf.hypergryph.com/config/prod/official/network_config"
PLATFORM = "Android"
USER_AGENT = "ArkResourceService/1.0"
EXPORT_LAYOUT_VERSION = "semantic-container-v1"
LOGGER = logging.getLogger("uvicorn.error")


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def dat_name(asset_name: str) -> str:
    stem = asset_name.replace("/", "_").replace("#", "__").rsplit(".", 1)[0]
    return f"{stem}.dat"


def safe_member(name: str) -> PurePosixPath:
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts:
        raise RuntimeError(f"unsafe archive member: {name}")
    return member


@dataclass
class Job:
    id: str
    status: str = "accepted"
    phase: str = "accepted"
    reason: str = "manual"
    dry_run: bool = False
    version: str | None = None
    detail: str = ""
    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    cancel_requested: bool = False


@dataclass
class PreparedAsset:
    asset: dict[str, Any]
    bundle: bytes
    reservation: int
    started: float
    download_ms: int


class UpdateRequest(BaseModel):
    dry_run: bool = False


class MemoryBudget:
    def __init__(self, limit_bytes: int):
        self.limit = limit_bytes
        self.used = 0
        self.peak = 0
        self.condition = threading.Condition()

    def acquire(self, requested: int) -> None:
        requested = max(1, requested)
        with self.condition:
            while self.used and self.used + requested > self.limit:
                self.condition.wait()
            self.used += requested
            self.peak = max(self.peak, self.used)

    def release(self, requested: int) -> None:
        with self.condition:
            self.used = max(0, self.used - max(1, requested))
            self.condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self.condition:
            return {"limitBytes": self.limit, "usedBytes": self.used, "peakBytes": self.peak}


class OfficialClient:
    def __init__(self, root: Path, download_pool_size: int = 2):
        self.root = root
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.download_pool_size = max(1, download_pool_size)
        self.download_sessions = threading.local()
        self.version_url = ""
        self.cdn_root = ""
        self.etag = ""
        self.last_modified = ""
        self.version: dict[str, str] = {}
        self.last_checked_at = ""
        self.last_network_refresh = 0.0
        self.lock = threading.Lock()

    def download_session(self) -> requests.Session:
        session = getattr(self.download_sessions, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.download_pool_size,
                pool_maxsize=self.download_pool_size,
                pool_block=True,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self.download_sessions.session = session
        return session

    def download_bytes(self, url: str, max_bytes: int) -> bytes:
        output = io.BytesIO()
        with self.download_session().get(url, stream=True, timeout=(30, 180)) as response:
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared and declared > max_bytes:
                raise RuntimeError(f"response exceeds configured memory allowance: {declared} > {max_bytes}")
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)
                    if output.tell() > max_bytes:
                        raise RuntimeError(
                            f"stream exceeds configured memory allowance: {output.tell()} > {max_bytes}"
                        )
        return output.getvalue()

    def discover(self, force: bool = False) -> None:
        with self.lock:
            if not force and self.version_url and time.time() - self.last_network_refresh < 3600:
                return
            response = self.session.get(CONFIG_URL, timeout=20)
            response.raise_for_status()
            content = response.json()["content"]
            config = json.loads(content) if isinstance(content, str) else content
            network = config["configs"][config["funcVer"]]["network"]
            self.version_url = network["hv"].replace("{0}", PLATFORM)
            self.cdn_root = network["hu"].rstrip("/")
            self.last_network_refresh = time.time()

    def check_version(self, conditional: bool = True) -> tuple[bool, dict[str, str]]:
        self.discover()
        headers: dict[str, str] = {}
        if conditional and self.etag:
            headers["If-None-Match"] = self.etag
        if conditional and self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        response = self.session.get(self.version_url, headers=headers, timeout=15)
        self.last_checked_at = utc_now()
        if response.status_code == 304:
            return False, self.version
        response.raise_for_status()
        new_version = {str(k): str(v) for k, v in response.json().items()}
        changed = new_version.get("resVersion") != self.version.get("resVersion")
        self.version = new_version
        self.etag = response.headers.get("ETag", "")
        self.last_modified = response.headers.get("Last-Modified", "")
        return changed, self.version

    def manifest(self, resource_version: str) -> tuple[str, dict[str, Any]]:
        base = f"{self.cdn_root}/{PLATFORM}/assets/{resource_version}"
        response = self.session.get(f"{base}/hot_update_list.json", timeout=90)
        response.raise_for_status()
        manifest = response.json()
        if manifest.get("versionId") != resource_version:
            raise RuntimeError("resource version and manifest version differ")
        return base, manifest


class Service:
    def __init__(self) -> None:
        self.root = Path(os.getenv("ARK_STATE_ROOT", "/state"))
        self.output_root = Path(os.getenv("ARK_OUTPUT_ROOT", "/output"))
        self.legacy_root = Path(os.getenv("ARK_LEGACY_ROOT", "/nonexistent-legacy-root"))
        config = load_json(Path(os.getenv("ARK_CONFIG", "/app/config/service.json")), {})
        self.poll_seconds = max(2.0, float(os.getenv("ARK_POLL_SECONDS", config.get("pollSeconds", 5))))
        self.download_workers = max(
            1, int(os.getenv("ARK_DOWNLOAD_WORKERS", config.get("downloadWorkers", 2)))
        )
        self.export_workers = max(
            1, int(os.getenv("ARK_EXPORT_WORKERS", config.get("exportWorkers", 2)))
        )
        self.prefetch_bundles = max(
            0, int(os.getenv("ARK_PREFETCH_BUNDLES", config.get("prefetchBundles", 2)))
        )
        self.decode_memory_factor = max(
            1.0, float(os.getenv("ARK_DECODE_MEMORY_FACTOR", config.get("decodeMemoryFactor", 6.0)))
        )
        self.bootstrap_packs = os.getenv("ARK_BOOTSTRAP_PACKS", str(config.get("bootstrapPacks", True))).lower() == "true"
        from .memory_unpacker import parse_export_types

        self.export_types = parse_export_types(
            os.getenv("ARK_EXPORT_TYPES", "image,spine,text,audio,mesh,video,masterdata")
        )
        self.typetree_types = frozenset(
            item.strip()
            for item in os.getenv(
                "ARK_TYPETREE_TYPES",
                "MonoBehaviour,Material,AnimatorController,AnimationClip,AssetBundle,GameObject,Transform,RectTransform,ParticleSystem,ParticleSystemRenderer,MeshRenderer,SkinnedMeshRenderer,SpriteRenderer,MonoScript",
            ).split(",")
            if item.strip()
        )
        self.memory_budget_mb = max(512, int(os.getenv("ARK_MEMORY_BUDGET_MB", config.get("memoryBudgetMB", 768))))
        self.container_memory_limit = os.getenv("ARK_CONTAINER_MEMORY_LIMIT", "unlimited")
        self.container_shm_size = os.getenv("ARK_CONTAINER_SHM_SIZE", "768m")
        self.memory_budget = MemoryBudget(self.memory_budget_mb * 1024 * 1024)
        self.unpacker_root = Path(os.getenv("ARK_UNPACKER_ROOT", "/opt/ark-unpacker"))
        self.memory_unpacker = None
        self.client = OfficialClient(self.root, self.download_workers)
        self.jobs: dict[str, Job] = {}
        self.active_job: str | None = None
        self.lock = threading.Lock()
        self.unpacker_lock = threading.Lock()
        self.records_lock = threading.Lock()
        self.records: dict[str, dict[str, Any]] = load_json(self.root / "State" / "unpacked_records.json", {})
        self.records_journal = self.root / "State" / "unpacked_records.jsonl"
        try:
            for line in self.records_journal.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                self.records[entry["name"]] = entry["record"]
        except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError):
            pass
        for asset_name, record in self.records.items():
            record.setdefault("outputDir", PurePosixPath(asset_name).with_suffix("").as_posix())
        self.stop = threading.Event()
        self.watcher: threading.Thread | None = None
        self.sync_failures = 0
        self.next_sync_attempt = 0.0
        for name in ("State", "Logs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        for stale in self.output_root.rglob(".ark-staging-*"):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)

    def save_job(self, job: Job) -> None:
        job.updated_at = utc_now()
        atomic_json(self.root / "State" / "jobs" / f"{job.id}.json", asdict(job))

    def submit(self, reason: str, dry_run: bool = False) -> Job:
        with self.lock:
            if self.active_job:
                active = self.jobs.get(self.active_job)
                if active and active.status in {"accepted", "running"}:
                    return active
            job = Job(id=str(uuid.uuid4()), reason=reason, dry_run=dry_run)
            self.jobs[job.id] = job
            self.active_job = job.id
            self.save_job(job)
            threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
            return job

    def update(self, job: Job, phase: str, detail: str = "") -> None:
        job.status = "running"
        job.phase = phase
        job.detail = detail
        self.save_job(job)

    def run_job(self, job: Job) -> None:
        try:
            self.update(job, "fetching_version")
            _, version_info = self.client.check_version(conditional=False)
            version = version_info["resVersion"]
            job.version = version
            self.update(job, "fetching_manifest")
            base_url, manifest = self.client.manifest(version)
            assets = manifest.get("abInfos", [])
            allowed = {asset["name"]: asset for asset in assets}
            old_manifest = load_json(self.root / "State" / "hot_update_list.json", {})
            self.drain_legacy_bundles(job, allowed)
            missing = [a for a in assets if not self.is_current(a)]
            job.total = len(missing)
            self.update(job, "planning", f"{len(missing)}/{len(assets)} resources need synchronization")
            if job.dry_run:
                job.status, job.phase = "completed", "completed"
                self.save_job(job)
                return
            if self.bootstrap_packs and not old_manifest and missing:
                self.process_packs(job, base_url, manifest, allowed)
            missing = [a for a in assets if not self.is_current(a)]
            job.total = len(missing)
            self.update(job, "downloading_unpacking", f"streaming and unpacking {len(missing)} resources")
            if missing:
                self.process_assets(job, base_url, missing)
            atomic_json(self.root / "State" / "hot_update_list.json", manifest)
            self.compact_records()
            job.status = "completed"
            if job.skipped:
                job.phase = "completed_with_skips"
                job.detail = f"synchronized and unpacked; skipped {job.skipped} resource(s)"
            else:
                job.phase, job.detail = "completed", "synchronized and unpacked"
            self.sync_failures = 0
            self.next_sync_attempt = 0.0
            self.save_job(job)
        except InterruptedError as error:
            job.status, job.phase, job.detail = "cancelled", "cancelled", str(error)
            self.save_job(job)
        except Exception as error:
            LOGGER.exception("asset_sync_failed job=%s phase=%s detail=%s", job.id, job.phase, job.detail)
            self.sync_failures += 1
            self.next_sync_attempt = time.time() + min(300.0, 15.0 * (2 ** min(self.sync_failures - 1, 4)))
            job.failed += 1
            job.status, job.phase, job.detail = "failed", "failed", f"{type(error).__name__}: {error}"
            self.save_job(job)
        finally:
            with self.lock:
                if self.active_job == job.id:
                    self.active_job = None

    @property
    def records_path(self) -> Path:
        return self.root / "State" / "unpacked_records.json"

    def is_current(self, asset: dict[str, Any]) -> bool:
        with self.records_lock:
            record = self.records.get(asset["name"])
        return bool(
            record
            and record.get("hash") == asset.get("hash")
            and record.get("md5") == asset.get("md5")
            and record.get("exportProfile") == self.export_profile
        )

    @property
    def export_profile(self) -> str:
        types = ",".join(sorted(self.export_types))
        trees = ",".join(sorted(value.lower() for value in self.typetree_types))
        return f"{EXPORT_LAYOUT_VERSION}:{types}:{trees}"

    def mark_current(self, asset: dict[str, Any], exported: int) -> None:
        with self.records_lock:
            record = {
                "hash": asset.get("hash", ""),
                "md5": asset.get("md5", ""),
                "size": int(asset.get("abSize", 0) or 0),
                "exported": exported,
                "exportProfile": self.export_profile,
                "outputDir": self.get_unpacker().relative_destination(asset["name"]),
                "unpackedAt": utc_now(),
            }
            self.records[asset["name"]] = record
            self.records_journal.parent.mkdir(parents=True, exist_ok=True)
            with self.records_journal.open("a", encoding="utf-8") as journal:
                journal.write(json.dumps({"name": asset["name"], "record": record}, ensure_ascii=False) + "\n")

    def compact_records(self) -> None:
        with self.records_lock:
            atomic_json(self.records_path, self.records)
            self.records_journal.unlink(missing_ok=True)

    def mark_skipped(self, job: Job, asset: dict[str, Any], error: BaseException) -> None:
        entry = {
            "jobId": job.id,
            "name": asset["name"],
            "hash": asset.get("hash", ""),
            "md5": asset.get("md5", ""),
            "error": f"{type(error).__name__}: {error}"[:2000],
            "skippedAt": utc_now(),
        }
        path = self.root / "State" / "skipped_assets.jsonl"
        with self.records_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get_unpacker(self):
        if self.memory_unpacker is None:
            with self.unpacker_lock:
                if self.memory_unpacker is None:
                    from .memory_unpacker import MemoryUnpacker

                    self.memory_unpacker = MemoryUnpacker(
                        self.unpacker_root,
                        self.output_root,
                        self.root / "Logs",
                        self.export_types,
                        self.typetree_types,
                    )
        return self.memory_unpacker

    def validate_and_unpack(self, job: Job, data: bytes, asset: dict[str, Any]) -> int:
        size = int(asset.get("abSize", 0) or 0)
        md5 = str(asset.get("md5", ""))
        if size and len(data) != size:
            raise RuntimeError(f"size mismatch: {asset['name']}")
        if len(md5) == 32 and hashlib.md5(data).hexdigest() != md5:
            raise RuntimeError(f"md5 mismatch: {asset['name']}")
        exported = self.get_unpacker().unpack(data, asset["name"], job.id)
        self.mark_current(asset, exported)
        return exported

    def process_archive_bytes(self, job: Job, archive: bytes, allowed: dict[str, dict[str, Any]]) -> int:
        processed = 0
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            for info in source.infolist():
                if info.is_dir() or info.filename not in allowed or self.is_current(allowed[info.filename]):
                    continue
                member = safe_member(info.filename)
                asset = allowed[member.as_posix()]
                self.validate_and_unpack(job, source.read(info), asset)
                processed += 1
        return processed

    def drain_legacy_bundles(self, job: Job, allowed: dict[str, dict[str, Any]]) -> None:
        bundle_root = self.legacy_root
        if not bundle_root.is_dir():
            return
        legacy = [p for p in bundle_root.rglob("*") if p.is_file() and p.relative_to(bundle_root).as_posix() in allowed]
        if not legacy:
            return
        for index, path in enumerate(legacy, 1):
            if job.cancel_requested:
                raise InterruptedError("cancel requested")
            name = path.relative_to(bundle_root).as_posix()
            asset = allowed[name]
            self.update(job, "migrating_legacy", f"memory unpack {index}/{len(legacy)}: {name}")
            if not self.is_current(asset):
                reserve = max(path.stat().st_size * 2, 1024 * 1024)
                self.memory_budget.acquire(reserve)
                try:
                    self.validate_and_unpack(job, path.read_bytes(), asset)
                finally:
                    self.memory_budget.release(reserve)
            path.unlink()

    def process_packs(self, job: Job, base: str, manifest: dict[str, Any], allowed: dict[str, Any]) -> None:
        state_path = self.root / "State" / "packs.json"
        state = load_json(state_path, {})
        version = manifest["versionId"]
        completed = set(state.get("completed", [])) if state.get("version") == version else set()
        packs = manifest.get("packInfos", [])
        for index, pack in enumerate(packs, 1):
            if job.cancel_requested:
                raise InterruptedError("cancel requested")
            name = pack["name"]
            if name in completed:
                continue
            self.update(job, "memory_download_unpack", f"pack {index}/{len(packs)}: {name}")
            reserve = max(int(pack.get("totalSize", 0) or 0) * 2, 1024 * 1024)
            self.memory_budget.acquire(reserve)
            try:
                max_bytes = max(int(pack.get("totalSize", 0) or 0) + 1024 * 1024, 2 * 1024 * 1024)
                archive = self.client.download_bytes(f"{base}/{name}.dat", max_bytes)
                self.process_archive_bytes(job, archive, allowed)
            finally:
                self.memory_budget.release(reserve)
            completed.add(name)
            self.compact_records()
            atomic_json(state_path, {"version": version, "completed": sorted(completed)})

    def asset_reservation(self, asset: dict[str, Any]) -> int:
        archive_size = int(asset.get("totalSize", 0) or 0)
        bundle_size = int(asset.get("abSize", 0) or 0)
        return max(int(archive_size + bundle_size * self.decode_memory_factor), 1024 * 1024)

    def prepare_one(self, job: Job, base: str, asset: dict[str, Any]) -> PreparedAsset:
        name = asset["name"]
        started = time.monotonic()
        worker = threading.current_thread().name
        LOGGER.info(
            "asset_download_start job=%s worker=%s name=%s downloadBytes=%s bundleBytes=%s",
            job.id,
            worker,
            name,
            asset.get("totalSize", 0),
            asset.get("abSize", 0),
        )
        reserve = self.asset_reservation(asset)
        self.memory_budget.acquire(reserve)
        try:
            max_bytes = max(int(asset.get("totalSize", 0) or 0) + 1024 * 1024, 2 * 1024 * 1024)
            archive = self.client.download_bytes(f"{base}/{dat_name(name)}", max_bytes)
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                if name not in source.namelist():
                    raise RuntimeError(f"archive did not contain {name}")
                bundle = source.read(name)
            size = int(asset.get("abSize", 0) or 0)
            md5 = str(asset.get("md5", ""))
            if size and len(bundle) != size:
                raise RuntimeError(f"size mismatch: {name}")
            if len(md5) == 32 and hashlib.md5(bundle).hexdigest() != md5:
                raise RuntimeError(f"md5 mismatch: {name}")
        except Exception:
            self.memory_budget.release(reserve)
            raise
        download_ms = round((time.monotonic() - started) * 1000)
        LOGGER.info(
            "asset_download_done job=%s worker=%s name=%s elapsedMs=%s reservationBytes=%s",
            job.id,
            worker,
            name,
            download_ms,
            reserve,
        )
        return PreparedAsset(asset, bundle, reserve, started, download_ms)

    def export_one(self, job: Job, prepared: PreparedAsset) -> str:
        name = prepared.asset["name"]
        worker = threading.current_thread().name
        export_started = time.monotonic()
        LOGGER.info("asset_export_start job=%s worker=%s name=%s", job.id, worker, name)
        try:
            exported = self.get_unpacker().unpack(prepared.bundle, name, job.id)
            self.mark_current(prepared.asset, exported)
        finally:
            self.memory_budget.release(prepared.reservation)
        LOGGER.info(
            "asset_done job=%s worker=%s name=%s exported=%s downloadMs=%s exportMs=%s elapsedMs=%s",
            job.id,
            worker,
            name,
            exported,
            prepared.download_ms,
            round((time.monotonic() - export_started) * 1000),
            round((time.monotonic() - prepared.started) * 1000),
        )
        return name

    def process_assets(self, job: Job, base: str, assets: list[dict[str, Any]]) -> None:
        remaining = iter(assets)
        downloads: set[Future[PreparedAsset]] = set()
        exports: dict[Future[str], dict[str, Any]] = {}
        ready: deque[PreparedAsset] = deque()
        exhausted = False
        first_error: BaseException | None = None

        with ThreadPoolExecutor(
            max_workers=self.download_workers, thread_name_prefix="ark-download"
        ) as download_pool, ThreadPoolExecutor(
            max_workers=self.export_workers, thread_name_prefix="ark-export"
        ) as export_pool:
            while downloads or exports or ready or not exhausted:
                cancelled = job.cancel_requested
                stop_scheduling = cancelled or first_error is not None

                if stop_scheduling:
                    while ready:
                        self.memory_budget.release(ready.popleft().reservation)
                    for future in downloads:
                        future.cancel()
                else:
                    while ready and len(exports) < self.export_workers:
                        prepared = ready.popleft()
                        future = export_pool.submit(self.export_one, job, prepared)
                        exports[future] = prepared.asset

                    may_prefetch = len(ready) < self.prefetch_bundles
                    may_feed_export = len(exports) < self.export_workers
                    while len(downloads) < self.download_workers and (may_prefetch or may_feed_export):
                        try:
                            asset = next(remaining)
                        except StopIteration:
                            exhausted = True
                            break
                        downloads.add(download_pool.submit(self.prepare_one, job, base, asset))
                        may_feed_export = len(exports) + len(downloads) < self.export_workers

                active = downloads | set(exports)
                if not active:
                    if stop_scheduling or exhausted:
                        break
                    continue

                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    if future in downloads:
                        downloads.remove(future)
                        if future.cancelled():
                            continue
                        try:
                            prepared = future.result()
                            if stop_scheduling or job.cancel_requested:
                                self.memory_budget.release(prepared.reservation)
                            else:
                                ready.append(prepared)
                        except BaseException as error:
                            if first_error is None:
                                first_error = error
                    else:
                        asset = exports.pop(future)
                        try:
                            future.result()
                            job.completed += 1
                            processed = job.completed + job.skipped
                            if processed % 20 == 0:
                                budget = self.memory_budget.snapshot()
                                self.update(
                                    job,
                                    "downloading_unpacking",
                                    f"{processed}/{job.total}; skipped {job.skipped}; memory {budget['usedBytes'] // 1048576}/{budget['limitBytes'] // 1048576} MiB",
                                )
                        except BaseException as error:
                            job.failed += 1
                            job.skipped += 1
                            self.mark_skipped(job, asset, error)
                            LOGGER.error(
                                "asset_skipped job=%s name=%s error=%s: %s",
                                job.id,
                                asset["name"],
                                type(error).__name__,
                                error,
                            )

            # A running download cannot be cancelled. Its result owns a memory reservation,
            # so collect and release it before propagating cancellation or the first error.
            for future in downloads:
                try:
                    prepared = future.result()
                except BaseException:
                    continue
                self.memory_budget.release(prepared.reservation)

        if job.cancel_requested:
            raise InterruptedError("cancel requested")
        if first_error is not None:
            raise first_error

    def watch(self) -> None:
        failures = 0
        while not self.stop.is_set():
            try:
                changed, version = self.client.check_version(conditional=True)
                failures = 0
                state = load_json(self.root / "State" / "hot_update_list.json", {})
                incomplete = state.get("versionId") != version.get("resVersion")
                if changed or (incomplete and time.time() >= self.next_sync_attempt):
                    self.submit("version_changed" if changed else "startup_incomplete")
                delay = self.poll_seconds + random.uniform(0, min(1.0, self.poll_seconds * 0.1))
            except Exception:
                failures += 1
                delay = min(300.0, self.poll_seconds * (2 ** min(failures, 6)))
                try:
                    self.client.discover(force=True)
                except Exception:
                    pass
            self.stop.wait(delay)

    def start(self) -> None:
        self.watcher = threading.Thread(target=self.watch, name="version-watcher", daemon=True)
        self.watcher.start()

    def shutdown(self) -> None:
        self.stop.set()
        if self.watcher:
            self.watcher.join(timeout=10)


service = Service()


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    yield
    service.shutdown()


app = FastAPI(title="Arknights Resource Service", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, Any]:
    active = service.jobs.get(service.active_job) if service.active_job else None
    return {"status": "ok", "activeJob": asdict(active) if active else None, "lastVersionCheck": service.client.last_checked_at}


@app.get("/v1/version")
def version() -> dict[str, Any]:
    return {"official": service.client.version, "etag": service.client.etag, "lastModified": service.client.last_modified, "lastCheckedAt": service.client.last_checked_at, "pollSeconds": service.poll_seconds, "versionUrl": service.client.version_url}


@app.get("/v1/config")
def runtime_config() -> dict[str, Any]:
    return {
        "storagePolicy": "memory-to-unpacked-only",
        "memoryBudgetMB": service.memory_budget_mb,
        "memoryBudget": service.memory_budget.snapshot(),
        "downloadWorkers": service.download_workers,
        "exportWorkers": service.export_workers,
        "prefetchBundles": service.prefetch_bundles,
        "decodeMemoryFactor": service.decode_memory_factor,
        "containerMemoryLimit": service.container_memory_limit,
        "sharedMemorySize": service.container_shm_size,
        "pollSeconds": service.poll_seconds,
        "exportTypes": sorted(service.export_types),
        "typeTreeTypes": sorted(service.typetree_types),
        "exportLayout": EXPORT_LAYOUT_VERSION,
    }


@app.post("/v1/assets/update", status_code=202)
def update(request: UpdateRequest) -> dict[str, Any]:
    return asdict(service.submit("manual", request.dry_run))


@app.get("/v1/jobs")
def jobs() -> list[dict[str, Any]]:
    return [asdict(job) for job in sorted(service.jobs.values(), key=lambda item: item.created_at, reverse=True)]


@app.get("/v1/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    if job_id not in service.jobs:
        saved = load_json(service.root / "State" / "jobs" / f"{job_id}.json", None)
        if saved:
            return saved
        raise HTTPException(404, "job not found")
    return asdict(service.jobs[job_id])


@app.post("/v1/jobs/{job_id}/cancel", status_code=202)
def cancel(job_id: str) -> dict[str, Any]:
    target = service.jobs.get(job_id)
    if not target:
        raise HTTPException(404, "job not found")
    target.cancel_requested = True
    service.save_job(target)
    return asdict(target)

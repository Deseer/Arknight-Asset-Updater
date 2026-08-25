from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    cancel_requested: bool = False


class UpdateRequest(BaseModel):
    dry_run: bool = False


class OfficialClient:
    def __init__(self, root: Path):
        self.root = root
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.version_url = ""
        self.cdn_root = ""
        self.etag = ""
        self.last_modified = ""
        self.version: dict[str, str] = {}
        self.last_checked_at = ""
        self.last_network_refresh = 0.0
        self.lock = threading.Lock()

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
        self.root = Path(os.getenv("ARK_DATA_ROOT", "/data"))
        self.poll_seconds = max(2.0, float(os.getenv("ARK_POLL_SECONDS", "5")))
        self.workers = max(1, int(os.getenv("ARK_DOWNLOAD_WORKERS", "4")))
        self.auto_unpack = os.getenv("ARK_AUTO_UNPACK", "true").lower() == "true"
        self.bootstrap_packs = os.getenv("ARK_BOOTSTRAP_PACKS", "true").lower() == "true"
        self.unpacker_root = Path(os.getenv("ARK_UNPACKER_ROOT", "/opt/ark-unpacker"))
        self.client = OfficialClient(self.root)
        self.jobs: dict[str, Job] = {}
        self.active_job: str | None = None
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.watcher: threading.Thread | None = None
        self.sync_failures = 0
        self.next_sync_attempt = 0.0
        for name in ("Bundles", "Downloads", "Queue", "State", "Unpacked", "Logs"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

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
            old_assets = {asset["name"]: asset for asset in old_manifest.get("abInfos", [])}
            missing = [a for a in assets if not self.is_current(a, old_assets.get(a["name"]))]
            job.total = len(missing)
            self.update(job, "planning", f"{len(missing)}/{len(assets)} resources need synchronization")
            if job.dry_run:
                job.status, job.phase = "completed", "completed"
                self.save_job(job)
                return
            queue = self.root / "Queue" / job.id
            if not old_manifest:
                # A legacy/bootstrap deployment may already have verified bundle files but
                # no completed manifest yet. Queue them once so they are not skipped by the
                # unpack phase when pack download resumes.
                for asset in assets:
                    if self.is_current(asset, None):
                        self.link_queue(queue, asset["name"])
            if self.bootstrap_packs and not old_manifest and missing:
                self.process_packs(job, base_url, manifest, allowed, queue)
            missing = [a for a in assets if not self.is_current(a, old_assets.get(a["name"]))]
            job.total = len(missing)
            self.update(job, "downloading", f"downloading {len(missing)} individual resources")
            if missing:
                with ThreadPoolExecutor(max_workers=self.workers) as pool:
                    futures = {pool.submit(self.download_one, base_url, a, allowed): a for a in missing}
                    for future in as_completed(futures):
                        if job.cancel_requested:
                            for pending in futures:
                                pending.cancel()
                            raise InterruptedError("cancel requested")
                        name = future.result()
                        self.link_queue(queue, name)
                        job.completed += 1
                        if job.completed % 20 == 0:
                            self.update(job, "downloading", f"{job.completed}/{job.total}")
            atomic_json(self.root / "State" / "hot_update_list.json", manifest)
            if self.auto_unpack and queue.exists() and any(p.is_file() for p in queue.rglob("*")):
                self.unpack(job, queue)
            job.status, job.phase, job.detail = "completed", "completed", "synchronized and unpacked"
            self.sync_failures = 0
            self.next_sync_attempt = 0.0
            self.save_job(job)
        except InterruptedError as error:
            job.status, job.phase, job.detail = "cancelled", "cancelled", str(error)
            self.save_job(job)
        except Exception as error:
            self.sync_failures += 1
            self.next_sync_attempt = time.time() + min(300.0, 15.0 * (2 ** min(self.sync_failures - 1, 4)))
            job.failed += 1
            job.status, job.phase, job.detail = "failed", "failed", f"{type(error).__name__}: {error}"
            self.save_job(job)
        finally:
            with self.lock:
                if self.active_job == job.id:
                    self.active_job = None

    def is_current(self, asset: dict[str, Any], old: dict[str, Any] | None) -> bool:
        path = self.root / "Bundles" / PurePosixPath(asset["name"])
        if not path.is_file():
            return False
        size = int(asset.get("abSize", 0) or 0)
        if size and path.stat().st_size != size:
            return False
        return old is None or (old.get("hash") == asset.get("hash") and old.get("md5") == asset.get("md5"))

    @staticmethod
    def download(url: str, destination: Path, retried: bool = False) -> None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing = destination.stat().st_size if destination.exists() else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        with session.get(url, headers=headers, stream=True, timeout=(30, 180)) as response:
            if response.status_code == 416 and destination.is_file():
                # A previous process may have finished the transfer but died before
                # extraction/rename. Official resources are ZIP .dat files, so a valid
                # central directory is sufficient to continue without downloading again.
                if zipfile.is_zipfile(destination):
                    return
                if retried:
                    response.raise_for_status()
                destination.unlink(missing_ok=True)
                return Service.download(url, destination, retried=True)
            mode = "ab" if existing and response.status_code == 206 else "wb"
            response.raise_for_status()
            with destination.open(mode) as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        output.write(chunk)

    def extract(self, archive: Path, allowed: dict[str, dict[str, Any]]) -> list[str]:
        result: list[str] = []
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                if info.is_dir() or info.filename not in allowed:
                    continue
                member = safe_member(info.filename)
                asset = allowed[member.as_posix()]
                target = self.root / "Bundles" / member
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".partial")
                digest = hashlib.md5()
                with source.open(info) as reader, temporary.open("wb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        digest.update(chunk)
                        writer.write(chunk)
                size = int(asset.get("abSize", 0) or 0)
                md5 = str(asset.get("md5", ""))
                if size and temporary.stat().st_size != size:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(f"size mismatch: {member}")
                if len(md5) == 32 and digest.hexdigest() != md5:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(f"md5 mismatch: {member}")
                os.replace(temporary, target)
                result.append(member.as_posix())
        return result

    def process_packs(self, job: Job, base: str, manifest: dict[str, Any], allowed: dict[str, Any], queue: Path) -> None:
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
            self.update(job, "bootstrap_packs", f"pack {index}/{len(packs)}: {name}")
            archive = self.root / "Downloads" / f"{name}.dat.part"
            self.download(f"{base}/{name}.dat", archive)
            for extracted in self.extract(archive, allowed):
                self.link_queue(queue, extracted)
            archive.unlink(missing_ok=True)
            completed.add(name)
            atomic_json(state_path, {"version": version, "completed": sorted(completed)})

    def download_one(self, base: str, asset: dict[str, Any], allowed: dict[str, Any]) -> str:
        name = asset["name"]
        archive = self.root / "Downloads" / f"{dat_name(name)}.part"
        self.download(f"{base}/{dat_name(name)}", archive)
        extracted = self.extract(archive, allowed)
        archive.unlink(missing_ok=True)
        if name not in extracted:
            raise RuntimeError(f"archive did not contain {name}")
        return name

    def link_queue(self, queue: Path, name: str) -> None:
        if not name.endswith((".ab", ".bin", ".usm")):
            return
        source = self.root / "Bundles" / PurePosixPath(name)
        target = queue / PurePosixPath(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    def unpack(self, job: Job, queue: Path) -> None:
        self.update(job, "unpacking", "running pinned Ark-Unpacker backend")
        log = self.root / "Logs" / f"{job.id}.log"
        base = ["python", str(self.unpacker_root / "Main.py"), "--input", str(queue), "--output", str(self.root / "Unpacked"), "--logging-level", "3"]
        commands: list[list[str]] = []
        if any(queue.rglob("*.ab")) or any(queue.rglob("*.bin")):
            commands.append(base + ["--mode", "ab", "--image", "--text", "--audio", "--spine", "--mesh", "--typetree", "--group"])
        if any(queue.rglob("*.usm")):
            commands.append(base + ["--mode", "cu"])
        with log.open("a", encoding="utf-8") as output:
            for command in commands:
                result = subprocess.run(command, cwd=self.unpacker_root, stdout=output, stderr=subprocess.STDOUT)
                if result.returncode:
                    raise RuntimeError(f"Ark-Unpacker exited {result.returncode}; see {log}")
        shutil.rmtree(queue)

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

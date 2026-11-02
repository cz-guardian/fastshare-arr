"""Download queue + worker threads (the 'NZBGet' part)."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import extract
from .fastshare import FastshareClient, FastshareError, RangeNotSatisfiable, RetryableError

log = logging.getLogger("manager")
HISTORY_LIMIT = 500


@dataclass
class FileItem:
    url: str
    filename: str = ""
    size: int = 0
    done: int = 0
    complete: bool = False


@dataclass
class Job:
    nzo_id: str
    name: str
    category: str
    priority: int = 0
    password: str = ""
    files: list = field(default_factory=list)  # list[FileItem]
    status: str = "Queued"  # Queued Downloading Paused Extracting Completed Failed
    fail_message: str = ""
    storage: str = ""
    created: float = 0.0
    completed: float = 0.0
    download_time: int = 0

    @property
    def total(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def done(self) -> int:
        return sum(f.size if f.complete and f.size else f.done for f in self.files)


class _Interrupt(Exception):
    def __init__(self, reason):
        self.reason = reason  # 'cancel' | 'pause' | 'global_pause'


def sanitize(name: str, default: str = "download") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .")
    return name[:200] or default


class _Meter:
    def __init__(self):
        self.t, self.n, self.speed = time.time(), 0, 0.0

    def add(self, n):
        self.n += n
        now = time.time()
        if now - self.t >= 1:
            inst = self.n / (now - self.t)
            self.speed = inst if not self.speed else 0.6 * self.speed + 0.4 * inst
            self.t, self.n = now, 0


class Manager:
    def __init__(self, cfg, fs: FastshareClient):
        self.cfg, self.fs = cfg, fs
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.queue: list[Job] = []
        self.history: list[Job] = []
        self.paused = False
        self.categories = list(cfg.categories)
        self._events: dict[str, threading.Event] = {}  # cancel flags
        self._active: dict[str, _Meter] = {}
        self._dirty, self._last_save = False, 0.0
        self._state = cfg.config_dir / "state.json"
        self._stop = False
        cfg.incomplete_dir.mkdir(parents=True, exist_ok=True)
        cfg.complete_dir.mkdir(parents=True, exist_ok=True)
        self._load()

    # ---------------------------------------------------------- persistence --
    def _load(self):
        if not self._state.exists():
            return
        try:
            raw = json.loads(self._state.read_text())

            def mk(d):
                files = [FileItem(**f) for f in d.pop("files", [])]
                return Job(files=files, **d)

            self.queue = [mk(d) for d in raw.get("queue", [])]
            self.history = [mk(d) for d in raw.get("history", [])]
            self.categories = sorted(set(self.categories) | set(raw.get("categories", [])))
            for j in self.queue:
                if j.status in ("Downloading", "Extracting"):
                    j.status = "Queued"
            log.info("Restored %d queued / %d history jobs", len(self.queue), len(self.history))
        except Exception:
            log.exception("Could not read state file; starting empty")

    def _save(self, force=False):
        with self.lock:
            now = time.time()
            if not force and (not self._dirty or now - self._last_save < 2):
                return
            data = {
                "queue": [asdict(j) for j in self.queue],
                "history": [asdict(j) for j in self.history[:HISTORY_LIMIT]],
                "categories": self.categories,
            }
            tmp = self._state.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self._state)
            self._dirty, self._last_save = False, now

    # ----------------------------------------------------------- public API --
    def add(self, items, name, category="*", priority=0, password="") -> str:
        category = category or "*"
        if category.lower() in ("default", ""):
            category = "*"
        job = Job(
            nzo_id="SABnzbd_nzo_" + secrets.token_hex(4),
            name=sanitize(name),
            category=category,
            priority=priority,
            password=password,
            files=[FileItem(url=i["url"], size=i.get("size", 0), filename=sanitize(i.get("name", ""), "") if i.get("name") else "") for i in items],
            created=time.time(),
            status="Paused" if priority == -2 else "Queued",
        )
        with self.cv:
            if category != "*" and category not in self.categories:
                self.categories.append(category)
            self.queue.append(job)
            self._dirty = True
            self._save(force=True)
            self.cv.notify_all()
        log.info("Queued %s (%d file(s)) cat=%s", job.name, len(job.files), job.category)
        return job.nzo_id

    def remove_queue(self, ids, del_files=True) -> list:
        removed = []
        with self.cv:
            for jid in ids:
                job = next((j for j in self.queue if j.nzo_id == jid), None)
                if not job:
                    continue
                ev = self._events.get(jid)
                if ev:
                    ev.set()
                self.queue.remove(job)
                shutil.rmtree(self._tmp(job), ignore_errors=True)
                removed.append(jid)
            self._dirty = True
            self._save(force=True)
        return removed

    def remove_history(self, ids, del_files=False) -> list:
        removed = []
        with self.lock:
            for jid in ids:
                job = next((j for j in self.history if j.nzo_id == jid), None)
                if not job:
                    continue
                self.history.remove(job)
                if del_files and job.storage:
                    shutil.rmtree(job.storage, ignore_errors=True)
                removed.append(jid)
            self._dirty = True
            self._save(force=True)
        return removed

    def set_paused(self, paused: bool, ids=None):
        with self.cv:
            if ids is None:
                self.paused = paused
            else:
                for j in self.queue:
                    if j.nzo_id in ids and j.status in ("Queued", "Paused", "Downloading"):
                        j.status = "Paused" if paused else "Queued"
            self._dirty = True
            self._save(force=True)
            self.cv.notify_all()

    def snapshot(self):
        with self.lock:
            speeds = {k: m.speed for k, m in self._active.items()}
            return (
                [(j, speeds.get(j.nzo_id, 0.0)) for j in list(self.queue)],
                list(self.history),
            )

    # --------------------------------------------------------------- workers --
    def start(self):
        for i in range(self.cfg.max_active_jobs):
            threading.Thread(target=self._worker, name=f"worker-{i}", daemon=True).start()
        threading.Thread(target=self._saver, name="saver", daemon=True).start()

    def stop(self):
        self._stop = True
        with self.cv:
            for ev in self._events.values():
                ev.set()
            self.cv.notify_all()
        self._save(force=True)

    def _saver(self):
        while not self._stop:
            time.sleep(2)
            try:
                self._save()
            except Exception:
                log.exception("state save failed")

    def _next_job(self):
        with self.cv:
            while not self._stop:
                if not self.paused:
                    ready = [j for j in self.queue if j.status == "Queued" and j.nzo_id not in self._active]
                    if ready:
                        job = sorted(ready, key=lambda j: (-j.priority, j.created))[0]
                        job.status = "Downloading"
                        self._active[job.nzo_id] = _Meter()
                        self._events[job.nzo_id] = threading.Event()
                        return job
                self.cv.wait(timeout=5)
        return None

    def _worker(self):
        while True:
            job = self._next_job()
            if job is None:
                return
            t0 = time.time()
            try:
                self._run_job(job)
                self._finish(job, ok=True, elapsed=time.time() - t0)
            except _Interrupt as i:
                with self.cv:
                    if i.reason == "cancel":
                        pass  # already removed from the queue
                    elif job in self.queue:
                        job.status = "Paused" if i.reason == "pause" else "Queued"
            except FastshareError as e:
                log.error("Job %s failed: %s", job.name, e)
                job.fail_message = str(e)
                self._finish(job, ok=False, elapsed=time.time() - t0)
            except extract.ExtractError as e:
                log.error("Job %s unpack failed: %s", job.name, e)
                job.fail_message = f"Unpacking failed: {e}"
                self._finish(job, ok=False, elapsed=time.time() - t0, keep=True)
            except Exception as e:  # noqa: BLE001
                log.exception("Job %s crashed", job.name)
                job.fail_message = f"Internal error: {e}"
                self._finish(job, ok=False, elapsed=time.time() - t0)
            finally:
                with self.cv:
                    self._active.pop(job.nzo_id, None)
                    self._events.pop(job.nzo_id, None)
                    self._dirty = True
                    self._save(force=True)

    def _finish(self, job: Job, ok: bool, elapsed: float, keep: bool = False):
        with self.cv:
            if job not in self.queue:  # removed while finishing
                return
            job.status = "Completed" if ok else "Failed"
            job.completed = time.time()
            job.download_time = int(elapsed)
            self.queue.remove(job)
            self.history.insert(0, job)
            del self.history[HISTORY_LIMIT:]
        if not ok and not keep:
            shutil.rmtree(self._tmp(job), ignore_errors=True)

    # ----------------------------------------------------------- job runner --
    def _tmp(self, job: Job) -> Path:
        return self.cfg.incomplete_dir / job.nzo_id

    def _check(self, job: Job):
        ev = self._events.get(job.nzo_id)
        if ev and ev.is_set():
            raise _Interrupt("cancel")
        if job.status == "Paused":
            raise _Interrupt("pause")
        if self.paused:
            raise _Interrupt("global_pause")

    def _run_job(self, job: Job):
        tmp = self._tmp(job)
        tmp.mkdir(parents=True, exist_ok=True)
        for idx, f in enumerate(job.files):
            if not f.complete:
                self._download_file(job, idx, f, tmp / f"{idx}.part")
        self._postprocess(job, tmp)

    def _download_file(self, job, idx, f: FileItem, part: Path):
        attempt, meter = 0, self._active[job.nzo_id]
        while True:
            self._check(job)
            try:
                start = part.stat().st_size if part.exists() else 0
                if start == 0:
                    self.fs.ensure_credit(f.size)
                try:
                    resp, fname = self.fs.open_download(f.url, start)
                except RangeNotSatisfiable:
                    if f.size and start >= f.size:  # already fully downloaded
                        f.complete = True
                        return
                    part.unlink(missing_ok=True)
                    continue
                with resp:
                    clen = int(resp.headers.get("Content-Length") or 0)
                    if start and resp.status == 206:
                        mode, total = "ab", start + clen if clen else 0
                        m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
                        if m:
                            total = int(m.group(1))
                    else:
                        mode, start, total = "wb", 0, clen
                    f.filename = f.filename or sanitize(fname, f"file{idx}")
                    f.size = total or f.size
                    f.done = start
                    with open(part, mode) as out:
                        while True:
                            self._check(job)
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            f.done += len(chunk)
                            meter.add(len(chunk))
                            lim = self.cfg.speed_limit_kbps * 1024
                            if lim:
                                time.sleep(len(chunk) / lim)
                if total and f.done < total:
                    raise RetryableError(f"connection closed at {f.done}/{total} bytes")
                f.size = f.size or f.done
                f.complete = True
                self.fs.spend(f.size)
                with self.lock:
                    self._dirty = True
                return
            except RetryableError as e:
                attempt += 1
                if attempt > self.cfg.retries:
                    raise FastshareError(f"Giving up on {f.url}: {e}") from e
                wait = min(60, 2 ** attempt)
                log.warning("Retry %d/%d for %s in %ds (%s)", attempt, self.cfg.retries, f.url, wait, e)
                ev = self._events[job.nzo_id]
                if ev.wait(wait):
                    raise _Interrupt("cancel")

    def _postprocess(self, job: Job, tmp: Path):
        cat_dir = self.cfg.complete_dir if job.category in ("*", "") else self.cfg.complete_dir / sanitize(job.category)
        dest = cat_dir / sanitize(job.name)
        n = 1
        while dest.exists() and not job.storage:
            n += 1
            dest = cat_dir / f"{sanitize(job.name)}.{n}"
        dest.mkdir(parents=True, exist_ok=True)
        for idx, f in enumerate(job.files):
            part = tmp / f"{idx}.part"
            if part.exists():
                target, k = dest / (f.filename or f"file{idx}"), 1
                while target.exists():
                    k += 1
                    target = dest / f"{target.stem}.{k}{target.suffix}"
                shutil.move(str(part), target)
        job.storage = str(dest)
        shutil.rmtree(tmp, ignore_errors=True)
        if self.cfg.extract:
            job.status = "Extracting"
            extract.extract_all(dest, job.password, self.cfg.delete_archives)

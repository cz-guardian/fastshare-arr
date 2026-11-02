"""SABnzbd-compatible HTTP API so Sonarr/Radarr/Lidarr/Prowlarr treat us as a SABnzbd client."""
from __future__ import annotations

import json
import logging
import shutil
import urllib.parse
import urllib.request
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .indexer import Indexer
from .ingest import IngestError, is_fastshare_url, parse_payload, split_password

log = logging.getLogger("api")
SAB_VERSION = "4.3.3"  # what we claim to be; arrs gate features on this
MAX_BODY = 25 * 1024 * 1024
PRIORITY = {"-100": 0, "-2": -2, "-1": -1, "0": 0, "1": 1, "2": 2}
PRIO_NAME = {-2: "Paused", -1: "Low", 0: "Normal", 1: "High", 2: "Force"}


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _hms(sec: float) -> str:
    sec = int(max(sec, 0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def parse_multipart(content_type: str, body: bytes):
    raw = b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    fields, files = {}, {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


class Api:
    def __init__(self, cfg, manager):
        self.cfg, self.m = cfg, manager

    # ------------------------------------------------------------ dispatch --
    def handle(self, params: dict, files: dict) -> dict:
        mode = params.get("mode", "")
        if mode == "version":
            return {"version": SAB_VERSION}
        if params.get("apikey") != self.cfg.api_key:
            return {"status": False, "error": "API Key Incorrect"}
        fn = getattr(self, f"mode_{mode}", None)
        if fn is None:
            return {"status": False, "error": f"not implemented: {mode}"}
        try:
            return fn(params, files)
        except IngestError as e:
            return {"status": False, "error": str(e)}

    # --------------------------------------------------------------- config --
    def _cats(self):
        names = ["*"] + [c for c in self.m.categories if c != "*"]
        return [
            {"name": n, "order": i, "pp": "", "script": "Default", "dir": "" if n == "*" else n, "newzbin": "", "priority": -100}
            for i, n in enumerate(names)
        ]

    def mode_get_config(self, p, f):
        return {
            "config": {
                "misc": {
                    "complete_dir": str(self.cfg.complete_dir),
                    "download_dir": str(self.cfg.incomplete_dir),
                    "enable_tv_sorting": False,
                    "enable_movie_sorting": False,
                    "enable_date_sorting": False,
                    "pre_check": False,
                    "history_retention": "",
                    "history_retention_option": "all",
                    "history_retention_number": 0,
                },
                "categories": self._cats(),
                "servers": [],
            }
        }

    def mode_get_cats(self, p, f):
        return {"categories": [c["name"] for c in self._cats()]}

    def mode_fullstatus(self, p, f):
        return {"status": {"completedir": str(self.cfg.complete_dir), "version": SAB_VERSION}}

    def mode_server_stats(self, p, f):
        return {"total": 0, "month": 0, "week": 0, "day": 0, "servers": {}}

    def mode_pause(self, p, f):
        self.m.set_paused(True)
        return {"status": True}

    def mode_resume(self, p, f):
        self.m.set_paused(False)
        return {"status": True}

    # ---------------------------------------------------------------- queue --
    def mode_queue(self, p, f):
        name, value = p.get("name", ""), p.get("value", "")
        ids = [v for v in value.split(",") if v]
        if name == "delete":
            if value == "all":
                ids = [j.nzo_id for j, _ in self.m.snapshot()[0]]
            return {"status": True, "nzo_ids": self.m.remove_queue(ids)}
        if name in ("pause", "resume"):
            self.m.set_paused(name == "pause", ids)
            return {"status": True, "nzo_ids": ids}

        jobs, _ = self.m.snapshot()
        cat = p.get("cat") or p.get("category")
        if cat:
            wanted = set(cat.split(","))
            jobs = [(j, s) for j, s in jobs if j.category in wanted]
        if p.get("nzo_ids"):
            wanted = set(p["nzo_ids"].split(","))
            jobs = [(j, s) for j, s in jobs if j.nzo_id in wanted]

        total_all = sum(j.total for j, _ in jobs)
        left_all = sum(max(j.total - j.done, 0) for j, _ in jobs)
        speed = sum(s for _, s in jobs)
        slots = []
        for i, (j, s) in enumerate(jobs):
            left = max(j.total - j.done, 0)
            pct = int(j.done * 100 / j.total) if j.total else 0
            status = {"Downloading": "Downloading", "Extracting": "Extracting", "Paused": "Paused"}.get(j.status, "Queued")
            slots.append({
                "status": status, "index": i, "password": j.password, "avg_age": "0d", "script": "None",
                "direct_unpack": None, "mb": f"{j.total / 1048576:.2f}", "mbleft": f"{left / 1048576:.2f}",
                "mbmissing": "0.0", "size": _size(j.total), "sizeleft": _size(left), "filename": j.name,
                "labels": [], "priority": PRIO_NAME.get(j.priority, "Normal"), "cat": j.category,
                "timeleft": _hms(left / s) if s else "0:00:00", "percentage": str(pct),
                "nzo_id": j.nzo_id, "unpackopts": "3",
            })
        start = int(p.get("start", 0) or 0)
        limit = int(p.get("limit", 0) or 0)
        view = slots[start:start + limit] if limit else slots[start:]
        busy = any(j.status == "Downloading" for j, _ in jobs)
        free = shutil.disk_usage(self.cfg.complete_dir).free / 1073741824
        return {"queue": {
            "version": SAB_VERSION, "paused": self.m.paused, "pause_int": "0", "paused_all": self.m.paused,
            "diskspace1": f"{free:.2f}", "diskspace2": f"{free:.2f}", "diskspacetotal1": "0", "diskspacetotal2": "0",
            "diskspace1_norm": f"{free:.1f} G", "diskspace2_norm": f"{free:.1f} G",
            "speed": f"{speed / 1024:.0f} K", "kbpersec": f"{speed / 1024:.2f}", "speedlimit": "", "speedlimit_abs": "",
            "size": _size(total_all), "sizeleft": _size(left_all), "mb": f"{total_all / 1048576:.2f}",
            "mbleft": f"{left_all / 1048576:.2f}", "timeleft": _hms(left_all / speed) if speed else "0:00:00",
            "eta": "unknown", "noofslots_total": len(slots), "noofslots": len(view),
            "status": "Paused" if self.m.paused else ("Downloading" if busy else "Idle"),
            "have_warnings": "0", "finish": 0, "slots": view,
        }}

    # -------------------------------------------------------------- history --
    def mode_history(self, p, f):
        name, value = p.get("name", ""), p.get("value", "")
        if name == "delete":
            ids = [v for v in value.split(",") if v]
            if value == "all":
                ids = [j.nzo_id for j in self.m.snapshot()[1]]
            return {"status": True, "nzo_ids": self.m.remove_history(ids, p.get("del_files") == "1")}
        _, hist = self.m.snapshot()
        cat = p.get("cat") or p.get("category")
        if cat:
            wanted = set(cat.split(","))
            hist = [j for j in hist if j.category in wanted]
        if p.get("nzo_ids"):
            wanted = set(p["nzo_ids"].split(","))
            hist = [j for j in hist if j.nzo_id in wanted]
        start = int(p.get("start", 0) or 0)
        limit = int(p.get("limit", 0) or 0)
        view = hist[start:start + limit] if limit else hist[start:]
        slots = [{
            "name": j.name, "nzb_name": j.name + ".nzb", "category": j.category, "pp": "D", "script": "",
            "report": "", "url": "", "status": j.status, "nzo_id": j.nzo_id, "storage": j.storage,
            "path": j.storage, "script_line": "", "download_time": j.download_time, "postproc_time": 0,
            "stage_log": [], "downloaded": j.done, "completeness": None, "fail_message": j.fail_message,
            "url_info": "", "bytes": j.total or j.done, "meta": None, "series": None, "md5sum": "",
            "password": j.password, "duplicate_key": "", "archive": False, "size": _size(j.total or j.done),
            "action_line": "", "completed": int(j.completed), "retry": 0,
        } for j in view]
        return {"history": {
            "noofslots": len(hist), "ppslots": 0, "day_size": "0 B", "week_size": "0 B", "month_size": "0 B",
            "total_size": "0 B", "last_history_update": 0, "slots": slots, "version": SAB_VERSION,
        }}

    # ------------------------------------------------------------------ add --
    def _enqueue(self, payload, p):
        name = p.get("nzbname") or payload.name or ""
        name, pw = split_password(name)
        if not name:
            from urllib.parse import unquote, urlsplit
            first = payload.items[0]
            name = first.get("name") or unquote(urlsplit(first["url"]).path.rsplit("/", 1)[-1]) or "fastshare-download"
            name = name.rsplit(".", 1)[0] if "." in name else name
        prio = PRIORITY.get(str(p.get("priority", "0")), 0)
        cat = p.get("cat") or p.get("category") or "*"
        jid = self.m.add(payload.items, name, cat, prio, pw or payload.password)
        return {"status": True, "nzo_ids": [jid]}

    def mode_addfile(self, p, f):
        item = f.get("name") or f.get("nzbfile") or next(iter(f.values()), None)
        if item is None:
            return {"status": False, "error": "no file uploaded"}
        return self._enqueue(parse_payload(item[1]), p)

    def mode_addurl(self, p, f):
        url = p.get("name", "")
        if is_fastshare_url(url):
            from .ingest import Payload
            return self._enqueue(Payload(items=[{"url": url.strip(), "size": 0, "name": ""}]), p)
        if not url.startswith(("http://", "https://")):
            return {"status": False, "error": "invalid url"}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"fastshare-arr/{__version__}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(5 * 1024 * 1024)
        except Exception as e:  # noqa: BLE001
            return {"status": False, "error": f"could not fetch release: {e}"}
        return self._enqueue(parse_payload(data), p)


def make_server(cfg, manager, fs=None) -> ThreadingHTTPServer:
    api = Api(cfg, manager)
    indexer = Indexer(cfg, fs) if fs is not None else None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            log.debug("%s " + fmt, self.address_string(), *a)

        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _raw(self, body, ctype, code):
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve(self, method):
            u = urllib.parse.urlsplit(self.path)
            path = u.path.rstrip("/")
            params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
            files = {}
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if 0 < length <= MAX_BODY else b""
            if length > MAX_BODY:
                return self._send({"status": False, "error": "payload too large"}, 413)
            if method == "POST" and body:
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("multipart/"):
                    fields, files = parse_multipart(ctype, body)
                    params.update(fields)
                elif ctype.startswith("application/x-www-form-urlencoded"):
                    params.update({k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()})
            if path in ("", "/healthz"):
                return self._send({"service": "fastshare-arr", "version": __version__})
            if indexer is not None and path.startswith("/newznab"):
                base = f"http://{self.headers.get('Host') or '127.0.0.1'}"
                if path.endswith("/api"):
                    return self._raw(*indexer.handle(params, base))
                if "/nzb/" in path:
                    token = path.rsplit("/", 1)[-1].removesuffix(".nzb")
                    return self._raw(*indexer.nzb(token, params))
            if not path.endswith("/api"):
                return self._send({"status": False, "error": "not found"}, 404)
            self._send(api.handle(params, files))

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

    return ThreadingHTTPServer((cfg.host, cfg.port), Handler)

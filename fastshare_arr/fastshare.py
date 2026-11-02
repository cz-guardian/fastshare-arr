"""Client for Fastshare's Kodi JSON API

    GET {base}/api/api_kodi.php?process=login&login=U&password=P
        -> {"user": {"hash": "...", "data": {"value": <credit MB>}, "unlimited": "True"|...}}
    GET {base}/api/api_kodi.php?process=search&pagination=200&term=Q&adult=false
        -> {"search": {"file": [{"filename", "data": {"value": bytes}, "download_url", ...}]}}
    GET <download_url>  with header  Cookie: FASTSHARE=<hash>   -> the file (Range supported)
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from pathlib import Path

log = logging.getLogger("fastshare")
CREDIT_REFRESH = 300  # seconds


class FastshareError(Exception):
    """Permanent failure (bad login, no credit, file gone). Do not retry."""


class RetryableError(Exception):
    """Transient failure (network, 5xx). Retry with backoff."""


class RangeNotSatisfiable(Exception):
    pass


def filename_from_headers(headers, url: str) -> str:
    cd = headers.get("Content-Disposition")
    if cd:
        m = Message()
        m["content-disposition"] = cd
        name = m.get_filename()
        if name:
            return urllib.parse.unquote(name) if "%" in name else name
    return urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) or "download.bin"


class FastshareClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.RLock()
        self.hash = ""
        self.credit_mb = 0
        self.unlimited = False
        self._login_at = 0.0

    # ------------------------------------------------------------ plumbing --
    def _open(self, url, headers=None, timeout=60):
        req = urllib.request.Request(url, headers={"User-Agent": self.cfg.user_agent, **(headers or {})})
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 416:
                raise RangeNotSatisfiable() from e
            if e.code in (401, 403):
                raise FastshareError(f"HTTP {e.code} from Fastshare (session/credit problem)") from e
            if e.code in (404, 410):
                raise FastshareError(f"HTTP {e.code}: file not found or deleted") from e
            raise RetryableError(f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # str(e) never contains the request URL, so credentials are not leaked into logs
            raise RetryableError(f"network error: {getattr(e, 'reason', e)}") from e

    def api(self, **params) -> dict:
        url = f"{self.cfg.fs_api_base}/api/api_kodi.php?" + urllib.parse.urlencode(params)
        with self._open(url) as r:
            raw = r.read(5_000_000)
        try:
            return json.loads(raw)
        except ValueError as e:
            self._dump("api_not_json", raw.decode("utf-8", "replace"))
            raise FastshareError("Fastshare API did not return JSON (wrong FASTSHARE_API_BASE?)") from e

    def _dump(self, label, body):
        try:
            d = Path(self.cfg.config_dir) / "debug"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{int(time.time())}-{label}.txt").write_text(body[:100_000], encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------- account --
    def login(self):
        with self._lock:
            if not (self.cfg.fs_user and self.cfg.fs_pass):
                raise FastshareError("FASTSHARE_USER / FASTSHARE_PASS not set")
            r = self.api(process="login", login=self.cfg.fs_user, password=self.cfg.fs_pass)
            user = r.get("user") or {}
            if not user.get("hash"):
                raise FastshareError("Fastshare login failed (wrong username/password?)")
            self.hash = user["hash"]
            try:
                self.credit_mb = int((user.get("data") or {}).get("value") or 0)
            except ValueError:
                self.credit_mb = 0
            self.unlimited = str(user.get("unlimited")) == "True"
            self._login_at = time.time()
            log.info("Logged in as %s (%s)", self.cfg.fs_user,
                     "unlimited" if self.unlimited else f"{self.credit_mb} MB credit")

    def ensure_login(self, max_age=CREDIT_REFRESH):
        with self._lock:
            if not self.hash or time.time() - self._login_at > max_age:
                self.login()

    def ensure_credit(self, size_bytes: int):
        """Same rule as the Kodi add-on: credit (MB) must exceed the file size."""
        self.ensure_login()
        if self.unlimited or not size_bytes:
            return
        need_mb = size_bytes / 1048576
        if self.credit_mb <= need_mb:
            raise FastshareError(f"Not enough Fastshare credit ({self.credit_mb} MB left, file needs {need_mb:.0f} MB)")

    def spend(self, size_bytes: int):
        with self._lock:
            self.credit_mb = max(0, self.credit_mb - int(size_bytes / 1048576))

    # -------------------------------------------------------------- search --
    def search(self, term: str, limit: int = 200) -> list:
        self.ensure_login(3600)
        r = self.api(process="search", pagination=limit, term=term, adult="true" if self.cfg.fs_adult else "false")
        return (r.get("search") or {}).get("file") or []

    def top(self) -> list:
        r = self.api(process="top", kodi=1)
        return (r.get("search") or {}).get("file") or []

    # ------------------------------------------------------------ download --
    def open_download(self, url: str, start: int = 0):
        """Return (response, filename). Re-logs in once if Fastshare answers with a web page."""
        for attempt in (1, 2):
            self.ensure_login()
            headers = {"Cookie": f"FASTSHARE={self.hash}"}
            if start > 0:
                headers["Range"] = f"bytes={start}-"
            resp = self._open(url, headers)
            if resp.headers.get_content_type() not in ("text/html", "application/xhtml+xml"):
                return resp, filename_from_headers(resp.headers, resp.geturl())
            body = resp.read(200_000).decode("utf-8", "replace")
            resp.close()
            if attempt == 1:
                log.info("Got a web page instead of the file; refreshing login")
                self.login()
                continue
            self._dump("html_instead_of_file", body)
            raise FastshareError("Fastshare returned a web page instead of the file (expired link or no credit)")
        raise FastshareError("unreachable")
